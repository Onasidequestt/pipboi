#!/usr/bin/env python3
"""
lane_watch.py — S121 V2: pre-registered, fail-closed verdict per LANE (generalizes runner_watch.py).

ONE watcher for every lane in the registry (lanes.py). Each lane gets a hashed, write-once verdict
config (lane_watch/<name>.json) seeded from the lane spec's `watcher` block, so goalposts can't move
after seeing data. The cohort is judged on REALIZED proceeds return-% (pnl_sol/size_sol, break-even-
neutral by %) over the lane's bot trades.db, rowid > arm_rowid. Ghost via prestige_tracker._is_ghost;
ev_lo via honest_objective.ev_lo (the ONE shared bound); concentration via the S105-audit diversity
rule (≥8 distinct mints, no mint >40%).

VERDICT (per lane, from its watcher bars):
  SCALE-CANDIDATE : n≥n_threshold ∧ ev_lo>ev_lo_pass ∧ ghost≤ghost_max ∧ ≥min_mints ∧ top_share≤max_share
  KILL            : n≥n_threshold ∧ ev_lo<ev_lo_kill
  PENDING         : otherwise (names the binding constraint).

READ-ONLY. Opens trades.db ?mode=ro; writes only lane_watch/<name>.json (once) + lane_watch_log.jsonl.
The verdict NEVER acts — it PRINTS the suggested operator action. This SUPERSEDES runner_watch.py for
the runner lane (it reproduces the same verdict); the old runner_watch.py + loop can keep running until
the operator switches the loop to lane_watch.

USAGE
  python3 lane_watch.py --lane runner      # one lane
  python3 lane_watch.py --all              # every registered lane with a cohort source
  python3 lane_watch.py --all --json
  exit code: nonzero if ANY lane verdict is KILL.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import lanes as _lanes
import prestige_tracker as pt           # _is_ghost (single ghost def)
import honest_objective as _ho          # ev_lo — the ONE shared bound

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "lane_watch"
LOG_PATH = ROOT / "lane_watch_log.jsonl"

_CRITERION_FIELDS = (
    "metric", "arm_rowid", "n_threshold", "ev_lo_pass", "ev_lo_kill",
    "ghost_max", "min_distinct_mints", "max_mint_share", "validated_floor_pct",
)

_ACTIONS = {
    "SCALE-CANDIDATE": "Edge holds live. SUGGESTED (operator runs it; this tool never does): enable the "
                       "lane canary on bot2/bot3 + consider lifting size_mult. Sizing arms ONLY through "
                       "arm_genes v2 / the promotion ladder — never by hand. Do NOT write ev_sizing.json.",
    "KILL": "Live worse than modeled. REVERT (operator runs it): rm the lane's canary on bot1 (hot ≤30s).",
    "PENDING": "Not enough realized closes yet. Keep accruing; re-check. Fire-rate is the usual binding "
               "constraint on free-tier tape — wire dex_discovery to un-starve it.",
}


def _now():
    return datetime.now(timezone.utc)


def _db_uri(bot: int) -> str:
    return f"file:{ROOT}/bots/bot{bot}/trades.db?mode=ro"


def _current_max_rowid(bot: int) -> int:
    try:
        c = sqlite3.connect(_db_uri(bot), uri=True)
        row = c.execute("SELECT MAX(rowid) FROM trades").fetchone()
        c.close()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _config_path(name: str) -> Path:
    return CONFIG_DIR / f"{name}.json"


def _config_hash(cfg: dict) -> str:
    payload = {k: cfg.get(k) for k in _CRITERION_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _scaffold(spec: dict) -> dict:
    """Write-once pre-registration of the lane's verdict config (seeded from the lane spec)."""
    name = spec["name"]
    bot = int(spec.get("bot", 1))
    w = spec.get("watcher", {}) or {}
    cfg = {
        "lane": name,
        "bot": bot,
        "metric": w.get("metric", f"{name} lane realized return-% (pnl_sol/size_sol), break-even-neutral"),
        "arm_rowid": _current_max_rowid(bot),           # cohort = lane closes after THIS rowid
        "n_threshold": int(w.get("n_threshold", 20)),
        "ev_lo_pass": float(w.get("ev_lo_pass", 0.0)),
        "ev_lo_kill": float(w.get("ev_lo_kill", -5.0)),
        "ghost_max": float(w.get("ghost_max", 0.10)),
        "min_distinct_mints": int(w.get("min_distinct_mints", 8)),
        "max_mint_share": float(w.get("max_mint_share", 0.40)),
        "validated_floor_pct": float(w.get("validated_floor_pct", 0.0)),
    }
    CONFIG_DIR.mkdir(exist_ok=True)
    _config_path(name).write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


def _load_config(spec: dict) -> dict:
    p = _config_path(spec["name"])
    if not p.exists():
        return _scaffold(spec)
    cfg = json.loads(p.read_text())
    if cfg.get("arm_rowid") is None:
        cfg["arm_rowid"] = _current_max_rowid(int(cfg.get("bot", spec.get("bot", 1))))
        p.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


def _cohort_rows(spec: dict, cfg: dict):
    """READ-ONLY: the lane's closes after arm_rowid, rowid-ordered (cohort_match via lanes.py)."""
    bot = int(cfg.get("bot", spec.get("bot", 1)))
    arm = int(cfg["arm_rowid"])
    rows = []
    try:
        c = sqlite3.connect(_db_uri(bot), uri=True)
        for (rid, d) in c.execute("SELECT rowid, data FROM trades WHERE event='close' ORDER BY rowid"):
            if rid <= arm:
                continue
            try:
                r = json.loads(d)
            except Exception:
                continue
            if not _lanes.cohort_match(r, spec):
                continue
            r["_rowid"] = rid
            rows.append(r)
        c.close()
    except Exception as e:
        print(f"  (read error: {e})", file=sys.stderr)
    return rows


def _concentration(rows):
    mints = [r.get("mint") for r in rows if r.get("mint")]
    n = len(mints)
    cnt = Counter(mints)
    top_mint, top_n = cnt.most_common(1)[0] if cnt else (None, 0)
    return {"distinct": len(cnt), "top_mint": top_mint, "top_n": top_n,
            "top_share": (top_n / n) if n else 0.0}


def evaluate(spec: dict, cfg: dict) -> dict:
    rows = _cohort_rows(spec, cfg)
    rets, no_size, ghosts = [], 0, 0
    wins = losses = neutral = 0
    for r in rows:
        if pt._is_ghost(r):
            ghosts += 1
        sz = r.get("size_sol") or 0.0
        if sz <= 0:
            no_size += 1
            continue
        ret = (r.get("pnl_sol", 0.0) or 0.0) / sz * 100.0
        rets.append(ret)
        if ret > 1.0:
            wins += 1
        elif ret < -1.0:
            losses += 1
        else:
            neutral += 1

    n_sized = len(rets)
    ev_lo, mean, _ = _ho.ev_lo(rets)
    if n_sized == 0:
        ev_lo, mean = None, None
    conc = _concentration(rows)
    ghost_rate = (ghosts / len(rows)) if rows else 0.0
    decisive = wins + losses
    win_rate = (wins / decisive) if decisive else None
    floor = float(cfg["validated_floor_pct"])
    gap = (mean - floor) if mean is not None else None

    n_thr = int(cfg["n_threshold"])
    bar_pass = float(cfg["ev_lo_pass"]); bar_kill = float(cfg["ev_lo_kill"])
    ghost_max = float(cfg["ghost_max"])
    min_mints = int(cfg["min_distinct_mints"]); max_share = float(cfg["max_mint_share"])

    have_n = n_sized >= n_thr
    ev_pass = (ev_lo is not None) and (ev_lo > bar_pass)
    ev_kill = (ev_lo is not None) and (ev_lo < bar_kill)
    ghost_ok = ghost_rate <= ghost_max
    diverse = conc["distinct"] >= min_mints and conc["top_share"] <= max_share

    if have_n and ev_pass and ghost_ok and diverse:
        verdict, binding = "SCALE-CANDIDATE", None
    elif have_n and ev_kill:
        verdict, binding = "KILL", f"ev_lo {ev_lo:.2f}% < kill bar {bar_kill:.1f}%"
    else:
        verdict = "PENDING"
        if not have_n:
            binding = f"n: {n_sized}/{n_thr} sized realized closes (fire-rate limited)"
        elif not ghost_ok:
            binding = f"ghost-rate {ghost_rate:.0%} > {ghost_max:.0%}"
        elif not diverse:
            binding = (f"concentration: {conc['distinct']}/{min_mints} distinct mints, "
                       f"top {conc['top_share']:.0%} (cap {max_share:.0%})")
        else:
            binding = f"ev_lo {ev_lo:.2f}% in [{bar_kill:.1f}, {bar_pass:.1f}] — not conclusive"

    return {
        "lane": spec["name"], "verdict": verdict, "binding": binding,
        "n_cohort": len(rows), "n_sized": n_sized, "no_size": no_size,
        "mean_ret_pct": round(mean, 3) if mean is not None else None,
        "ev_lo_pct": round(ev_lo, 3) if ev_lo is not None else None,
        "gap_vs_floor_pct": round(gap, 3) if gap is not None else None,
        "validated_floor_pct": floor,
        "win_rate": round(win_rate, 3) if win_rate is not None else None,
        "wins": wins, "losses": losses, "neutral": neutral,
        "ghosts": ghosts, "ghost_rate": round(ghost_rate, 4),
        "distinct_mints": conc["distinct"], "top_mint": conc["top_mint"],
        "top_share": round(conc["top_share"], 3),
        "arm_rowid": int(cfg["arm_rowid"]),
        "n_threshold": n_thr, "ev_lo_pass": bar_pass, "ev_lo_kill": bar_kill,
        "ts": _now().isoformat(),
    }


def _goalpost_check(name: str, cfg: dict) -> str:
    cur = _config_hash(cfg)
    last = None
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("lane") == name:
                last = j
    if last and last.get("config_hash") and last["config_hash"] != cur:
        print("!" * 72)
        print(f"  ⚠  GOALPOST-MOVED [{name}] — the pre-registered criterion changed since the last run.")
        print(f"     was {last['config_hash'][:12]} … now {cur[:12]}")
        print("!" * 72)
    return cur


def run_lane(spec: dict) -> dict:
    cfg = _load_config(spec)
    res = evaluate(spec, cfg)
    res["config_hash"] = _goalpost_check(spec["name"], cfg)
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(res) + "\n")
    except Exception:
        pass
    return res


def _print(res: dict):
    v = res["verdict"]
    mark = {"SCALE-CANDIDATE": "🟢", "KILL": "🔴", "PENDING": "⏳"}.get(v, "·")
    print("=" * 72)
    print(f"  LANE WATCH [{res['lane']}] — {mark} {v}")
    print("=" * 72)
    print(f"  cohort (lane={res['lane']}, rowid>{res['arm_rowid']}): "
          f"{res['n_cohort']} closes ({res['n_sized']} sized, {res['no_size']} no-size)")
    if res["n_sized"]:
        print(f"  realized return-%:  mean {res['mean_ret_pct']}%   ev_lo {res['ev_lo_pct']}%   "
              f"(pass>{res['ev_lo_pass']}, kill<{res['ev_lo_kill']})")
        print(f"  gap vs +{res['validated_floor_pct']}% validated floor: {res['gap_vs_floor_pct']}pp")
        print(f"  win-rate (BE-neutral): {res['win_rate']}  "
              f"(W{res['wins']}/L{res['losses']}/N{res['neutral']})")
        print(f"  ghosts: {res['ghosts']} ({res['ghost_rate']:.0%})   "
              f"distinct mints: {res['distinct_mints']}   top mint {res['top_share']:.0%}")
    if res["binding"]:
        print(f"  binding constraint: {res['binding']}")
    print(f"  → {_ACTIONS.get(v, '')}")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", help="judge one lane by name")
    ap.add_argument("--all", action="store_true", help="judge every registered lane")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()

    reg = _lanes.registry()
    if not reg:
        print("  no lanes registered (lanes/*.json) — nothing to watch.")
        return 0

    if args.lane:
        specs = [reg[args.lane]] if args.lane in reg else []
        if not specs:
            print(f"  unknown lane '{args.lane}'. Registered: {', '.join(reg)}", file=sys.stderr)
            return 2
    else:
        specs = list(reg.values())

    results = [run_lane(s) for s in specs]
    if args.json:
        print(json.dumps({"lanes": results}, indent=2))
    else:
        for res in results:
            _print(res)
    return 1 if any(r["verdict"] == "KILL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
