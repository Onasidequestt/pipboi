#!/usr/bin/env python3
"""
runner_watch.py — pre-registered, fail-closed verdict tracker for the RUNNEREDGE trial.

WHY THIS EXISTS  (S119)
  The RUNNEREDGE momentum-runner lane (bots/bot1/momentum_override.json) is a VALIDATED
  +EV edge on paper — realizable ev_lo +14% (harsh) to +32% (trail), win 0.62-0.74, 220
  distinct mints, walk-forward OOS +16%, P(>0)=1.000 (read-only on 44k matured forward_obs).
  Its ONE unproven risk is LIVE execution into sub-$10k pools (route-death between price
  reads). The trial runs Bot1-only at ×0.5 size with rug_screen + drain-guard + trail
  wrapped around it; ~20-40 realized closes settle whether the live edge holds.

  This watcher judges that trial HONESTLY — the same discipline as kill_criterion.py:
  a PRE-REGISTERED verdict (n / ev_lo bars / ghost / concentration), hashed so the
  goalposts cannot be moved after seeing the data, applied mechanically every run.

  It is READ-ONLY. It opens bots/bot1/trades.db with ?mode=ro, writes only its own
  config (runner_watch.json, once) and log (runner_watch_log.jsonl). It NEVER touches
  bots/, canaries, genes, or any policy. The verdict NEVER acts — on SCALE-CANDIDATE it
  PRINTS the suggested operator action; on KILL it PRINTS the revert. It performs neither.

COHORT
  Closes in bots/bot1/trades.db where momentum_override=1 AND rowid > trial_start_rowid
  (rowid, never ts — the bot3-clock-skew lesson). Realized return-% = pnl_sol/size_sol*100
  (both fields exist post-S80/S105). Win/loss is break-even-NEUTRAL by % (the S89 lesson:
  |ret%| < 1 is neutral, not a loss). Ghost via prestige_tracker._is_ghost (imported, not
  re-defined). Concentration mirrors the S105-audit diversity rule (≥8 distinct mints, no
  mint >40% of the cohort) so a single lucky token cannot flash a false SCALE-CANDIDATE.

VERDICT
  SCALE-CANDIDATE : n≥20 ∧ ev_lo>0 ∧ ghost-rate≤10% ∧ ≥8 distinct mints ∧ no mint >40%
  KILL            : n≥20 ∧ ev_lo<−5%   (live route-death worse than modeled → $30k gate was right)
  PENDING         : otherwise, with the binding constraint named.

USAGE
  python3 runner_watch.py            # scaffold config (first run) / print verdict
  python3 runner_watch.py --json     # machine-readable verdict
  exit code: 0 on PENDING / SCALE-CANDIDATE, nonzero on KILL.
"""
import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "runner_watch.json"
LOG_PATH = ROOT / "runner_watch_log.jsonl"
BOT = 1                                   # Bot1-only trial (bot2/3 = clean control)
DB_URI = f"file:{ROOT}/bots/bot{BOT}/trades.db?mode=ro"

# Ghost classification — IMPORTED from the gate's single source of truth, never re-defined.
import prestige_tracker as pt             # _is_ghost (pure dict fn; no DB touched on import)

# Fields that DEFINE the criterion (hashed for goalpost-move detection).
_CRITERION_FIELDS = (
    "metric", "trial_start_rowid", "n_threshold", "ev_lo_pass", "ev_lo_kill",
    "ghost_max", "min_distinct_mints", "max_mint_share", "validated_floor_pct",
)

_SCAFFOLD = {
    "metric": "Bot1 momentum_override (RUNNEREDGE) realized return-% (pnl_sol/size_sol), break-even-neutral",
    "trial_start_rowid": None,            # set on first scaffold = current max rowid of bot1
    "n_threshold": 20,                    # ~20-40 realized closes settle the live route-death risk
    "ev_lo_pass": 0.0,                    # SCALE: ev_lo > 0% (+EV after the modeled friction)
    "ev_lo_kill": -5.0,                   # KILL: ev_lo < −5% (live worse than modeled → $30k gate was right)
    "ghost_max": 0.10,                    # ≤10% ghost-rate (the unsellable / route-death tail)
    "min_distinct_mints": 8,             # S105-audit diversity rule — ≥8 distinct mints
    "max_mint_share": 0.40,              # no single mint > 40% of the cohort
    "validated_floor_pct": 14.0,         # the harsh validated ev_lo floor; gap = realized mean − this
    "actions": {
        "SCALE-CANDIDATE": "Edge holds live. SUGGESTED (operator runs it, this tool never does): "
                           "enable bots/bot2/momentum_override.json + bots/bot3/momentum_override.json, "
                           "and consider lifting the ×0.5 size. Do NOT arm ev_sizing.",
        "KILL": "Live route-death worse than modeled — the $30k gate was right. "
                "REVERT (operator runs it): rm bots/bot1/momentum_override.json (hot, code goes inert ≤30s).",
        "PENDING": "Not enough realized closes yet. Keep accruing; re-check. RUNNEREDGE fires are rare "
                   "on free-tier tape (~42 qualifying candidates/day) — fire-rate is the binding constraint.",
    },
}


def _now():
    return datetime.now(timezone.utc)


def _current_max_rowid():
    try:
        c = sqlite3.connect(DB_URI, uri=True)
        row = c.execute("SELECT MAX(rowid) FROM trades").fetchone()
        c.close()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _config_hash(cfg):
    payload = {k: cfg.get(k) for k in _CRITERION_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _scaffold_config():
    cfg = dict(_SCAFFOLD)
    cfg["trial_start_rowid"] = _current_max_rowid()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    print("=" * 72)
    print("  RUNNER WATCH — scaffolded a fresh config (write-once pre-registration)")
    print("=" * 72)
    print(f"  Wrote {CONFIG_PATH.name} with trial_start_rowid={cfg['trial_start_rowid']}.")
    print("  The cohort is every momentum_override close with rowid > that value.")
    print("  The criterion is hashed; editing it later prints a GOALPOST-MOVED warning.")
    print("=" * 72)
    return cfg


def _load_config():
    if not CONFIG_PATH.exists():
        return _scaffold_config()
    cfg = json.loads(CONFIG_PATH.read_text())
    if cfg.get("trial_start_rowid") is None:
        cfg["trial_start_rowid"] = _current_max_rowid()
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


def _ev_lo(rets):
    """ev_lo = mean − 1.64·SE  (SE = population stdev / sqrt(n)) — matches kill_criterion/lab/prove_edge."""
    n = len(rets)
    if n == 0:
        return None, None, 0
    mean = sum(rets) / n
    if n < 2:
        return mean, mean, n
    var = sum((x - mean) ** 2 for x in rets) / n
    se = math.sqrt(var) / math.sqrt(n)
    return mean - 1.64 * se, mean, n


def _cohort_rows(trial_start_rowid):
    """READ-ONLY: the momentum_override closes after the trial start, rowid-ordered."""
    rows = []
    try:
        c = sqlite3.connect(DB_URI, uri=True)
        for (rid, d) in c.execute(
            "SELECT rowid, data FROM trades WHERE event='close' ORDER BY rowid"
        ):
            if rid <= trial_start_rowid:
                continue
            try:
                r = json.loads(d)
            except Exception:
                continue
            if not r.get("momentum_override"):
                continue
            r["_rowid"] = rid
            rows.append(r)
        c.close()
    except Exception as e:
        print(f"  (read error: {e})", file=sys.stderr)
    return rows


def _concentration(rows):
    """Cohort concentration (mirrors ride_ab._concentration on a single arm): distinct mints,
    the dominant mint + its count-share. A single-token-driven verdict is visible + blockable."""
    mints = [r.get("mint") for r in rows if r.get("mint")]
    n = len(mints)
    cnt = Counter(mints)
    distinct = len(cnt)
    top_mint, top_n = cnt.most_common(1)[0] if cnt else (None, 0)
    top_share = (top_n / n) if n else 0.0
    return {"distinct": distinct, "top_mint": top_mint, "top_n": top_n, "top_share": top_share}


def evaluate(cfg):
    trial_start = int(cfg["trial_start_rowid"])
    rows = _cohort_rows(trial_start)

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
    ev_lo, mean, _ = _ev_lo(rets)
    conc = _concentration(rows)
    ghost_rate = (ghosts / len(rows)) if rows else 0.0
    decisive = wins + losses
    win_rate = (wins / decisive) if decisive else None
    floor = float(cfg["validated_floor_pct"])
    gap = (mean - floor) if mean is not None else None

    n_thr = int(cfg["n_threshold"])
    bar_pass = float(cfg["ev_lo_pass"])
    bar_kill = float(cfg["ev_lo_kill"])
    ghost_max = float(cfg["ghost_max"])
    min_mints = int(cfg["min_distinct_mints"])
    max_share = float(cfg["max_mint_share"])

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
            binding = f"n: {n_sized}/{n_thr} sized realized closes (fire-rate limited — see note)"
        elif not ghost_ok:
            binding = f"ghost-rate {ghost_rate:.0%} > {ghost_max:.0%}"
        elif not diverse:
            binding = (f"concentration: {conc['distinct']}/{min_mints} distinct mints, "
                       f"top {conc['top_share']:.0%} (cap {max_share:.0%})")
        else:
            binding = f"ev_lo {ev_lo:.2f}% in [{bar_kill:.1f}, {bar_pass:.1f}] — not conclusive"

    return {
        "verdict": verdict,
        "binding": binding,
        "n_cohort": len(rows),
        "n_sized": n_sized,
        "no_size": no_size,
        "mean_ret_pct": round(mean, 3) if mean is not None else None,
        "ev_lo_pct": round(ev_lo, 3) if ev_lo is not None else None,
        "gap_vs_floor_pct": round(gap, 3) if gap is not None else None,
        "validated_floor_pct": floor,
        "win_rate": round(win_rate, 3) if win_rate is not None else None,
        "wins": wins, "losses": losses, "neutral": neutral,
        "ghosts": ghosts, "ghost_rate": round(ghost_rate, 4),
        "distinct_mints": conc["distinct"], "top_mint": conc["top_mint"],
        "top_share": round(conc["top_share"], 3),
        "trial_start_rowid": trial_start,
        "n_threshold": n_thr, "ev_lo_pass": bar_pass, "ev_lo_kill": bar_kill,
        "ts": _now().isoformat(),
    }


def _goalpost_check(cfg):
    """Warn (loudly) if the criterion changed since the last logged evaluation."""
    cur = _config_hash(cfg)
    last = None
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    last = json.loads(line)
                except Exception:
                    pass
    if last and last.get("config_hash") and last["config_hash"] != cur:
        print("!" * 72)
        print("  ⚠  GOALPOST-MOVED — the pre-registered criterion changed since the last run.")
        print(f"     was {last['config_hash'][:12]} … now {cur[:12]}")
        print("     Moving the bar after seeing data invalidates the pre-registration.")
        print("!" * 72)
    return cur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="machine-readable verdict")
    args = ap.parse_args()

    cfg = _load_config()
    cfg_hash = _goalpost_check(cfg)
    res = evaluate(cfg)
    res["config_hash"] = cfg_hash

    # Append to the log (the only write besides the one-time config).
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(res) + "\n")
    except Exception:
        pass

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        v = res["verdict"]
        mark = {"SCALE-CANDIDATE": "🟢", "KILL": "🔴", "PENDING": "⏳"}.get(v, "·")
        print("=" * 72)
        print(f"  RUNNER WATCH — {mark} {v}")
        print("=" * 72)
        print(f"  cohort (momentum_override, rowid>{res['trial_start_rowid']}): "
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
        print(f"  → {cfg['actions'].get(v, '')}")
        print("=" * 72)

    sys.exit(1 if res["verdict"] == "KILL" else 0)


if __name__ == "__main__":
    main()
