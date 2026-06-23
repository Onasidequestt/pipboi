#!/usr/bin/env python3
"""
kill_criterion.py — pre-registered, write-once verdict tracker for the deep_pool edge.

WHY THIS EXISTS
  An edge you keep "giving more time" is an edge you never kill. This tool forces a
  PRE-COMMITMENT: the operator writes the pass bar, the sample size, and the deadline
  ONCE (in kill_criterion.json), and from then on every run emits a single mechanical
  verdict — PASS / FAIL / PENDING / EXPIRED_FAIL — against THAT criterion. Moving the
  goalposts later is detected (the config is hashed) and printed as a loud warning, so
  you cannot quietly relax the bar after seeing the data.

  It NEVER recomputes the gate itself — it imports prestige_tracker._fleet_deep_pool_stats
  (the single source of truth: n, clean net ◎, ghost-rate) and adds only the ev_lo of the
  SAME cohort's realized return-% (pnl_sol/size_sol). The verdict's PASS/FAIL is the
  operator's pre-registered bar applied to those numbers.

  Default READ-ONLY. --enforce executes ONLY the operator's pre-committed DISARM action on
  FAIL/EXPIRED_FAIL (rm bots/bot*/ev_sizing.json) — the safe direction. It can NEVER arm.
  (THE ONE OPERATOR RULE: genes are never armed programmatically.)

USAGE
  python3 kill_criterion.py                 # scaffold the config (first run) / print verdict
  python3 kill_criterion.py --json          # machine-readable verdict
  python3 kill_criterion.py --enforce       # on FAIL/EXPIRED_FAIL only: run the disarm cmd
"""
import argparse
import glob
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "kill_criterion.json"
LOG_PATH = ROOT / "kill_criterion_log.jsonl"

# Single source of truth for the gate — IMPORTED, never recomputed here.
import prestige_tracker as pt   # _fleet_deep_pool_stats, _closes, _is_ghost, _DP_PLAYS, MIN_CLOSES, GHOST_MAX, BOTS

# The disarm action is the only side effect --enforce may ever take.
_DISARM_CMD = "rm -f bots/bot*/ev_sizing.json"

# Fields that DEFINE the criterion (hashed for goalpost-move detection).
_CRITERION_FIELDS = ("metric", "n_threshold", "ev_lo_pass", "deadline", "actions")

_SCAFFOLD = {
    "metric": "deep_pool+brain_rule clean realized return-% ev_lo (gate cohort, ghost-excluded)",
    "n_threshold": 30,
    "ev_lo_pass": None,        # OPERATOR MUST SET — e.g. 0.0 (ev_lo >= 0%, +EV after friction)
    "deadline": None,          # OPERATOR MUST SET — ISO date, e.g. "2026-07-15"
    "actions": {
        "PASS": "Edge proved. The deploy gate (prestige_tracker/arm_genes) may arm EV-sizing "
                "per the normal sequence. Do NOT arm by hand. This tool never arms.",
        "FAIL": "Edge disproven at n>=threshold. Disarm: rm bots/bot*/ev_sizing.json  "
                "(--enforce runs exactly this). Then revert the admission widener.",
        "PENDING": "Not enough closes yet and deadline not reached. Keep accruing; re-check.",
        "EXPIRED_FAIL": "Deadline passed without conclusive proof. Disarm: rm bots/bot*/ev_sizing.json "
                        "(--enforce runs exactly this). The slice failed to fire enough to prove out.",
    },
}


def _now():
    return datetime.now(timezone.utc)


def _parse_deadline(s):
    """Accept 'YYYY-MM-DD' or a full ISO timestamp; return an aware datetime (UTC)."""
    if not s:
        return None
    try:
        if len(str(s)) == 10:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _config_hash(cfg):
    """Stable hash over only the criterion-defining fields (sorted, canonical)."""
    payload = {k: cfg.get(k) for k in _CRITERION_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _scaffold_config():
    CONFIG_PATH.write_text(json.dumps(_SCAFFOLD, indent=2) + "\n")
    print("=" * 70)
    print("  KILL CRITERION — scaffolded a fresh config (write-once pre-registration)")
    print("=" * 70)
    print(f"  Wrote {CONFIG_PATH.name}. EDIT it, then re-run.")
    print("  You MUST set:")
    print("    • ev_lo_pass  — the pass bar, e.g. 0.0   (ev_lo of return-% >= this)")
    print("    • deadline    — ISO date,    e.g. \"2026-07-15\"")
    print("  Everything else has a sane default. The config is hashed on first")
    print("  evaluation; changing it later prints a GOALPOST-MOVED warning.")
    print("=" * 70)


def _load_config():
    cfg = json.loads(CONFIG_PATH.read_text())
    missing = [k for k in ("ev_lo_pass", "deadline") if cfg.get(k) in (None, "")]
    if missing:
        print("=" * 70)
        print("  KILL CRITERION — REFUSING TO EVALUATE")
        print("=" * 70)
        print(f"  {CONFIG_PATH.name} exists but these operator fields are unset: {', '.join(missing)}")
        print("  Set them (the pre-commitment must come BEFORE the data), then re-run.")
        print("=" * 70)
        sys.exit(2)
    if _parse_deadline(cfg.get("deadline")) is None:
        print(f"  ✗ deadline '{cfg.get('deadline')}' is not a valid ISO date. Fix it and re-run.")
        sys.exit(2)
    return cfg


def _last_log_entry():
    if not LOG_PATH.exists():
        return None
    last = None
    for line in LOG_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except Exception:
            pass
    return last


def _ev_lo(rets):
    """ev_lo = mean − 1.64·SE  (SE = population stdev / sqrt(n)) — matches lab._se / prove_edge."""
    n = len(rets)
    if n == 0:
        return None, 0.0, 0
    mean = sum(rets) / n
    if n < 2:
        return mean, mean, n
    var = sum((x - mean) ** 2 for x in rets) / n        # population variance (pstdev), like lab._se
    se = math.sqrt(var) / math.sqrt(n)
    return mean - 1.64 * se, mean, n


def _cohort_returns():
    """Realized return-% (pnl_sol/size_sol*100) for the EXACT gate cohort, using
    prestige_tracker's own primitives so the cohort definition cannot drift from the gate:
    play ∈ _DP_PLAYS, clean (not _is_ghost), and skip-regime rows excluded UNLESS normal_slice
    (the S98/S99 deliberately-traded slice). Mirrors _fleet_deep_pool_stats' filter exactly."""
    rets = []
    no_size = 0
    for b in pt.BOTS:
        for r in pt._closes(b):
            play = r.get("play") or r.get("tier") or r.get("insane_tier")
            if play not in pt._DP_PLAYS:
                continue
            if r.get("regime") in pt._GATE_SKIP_REGIMES and not r.get("normal_slice"):
                continue
            if pt._is_ghost(r):
                continue
            sz = r.get("size_sol") or 0.0
            if sz > 0:
                rets.append((r.get("pnl_sol", 0.0) or 0.0) / sz * 100.0)
            else:
                no_size += 1
    return rets, no_size


def evaluate(cfg):
    """Compute the cohort stats (gate numbers IMPORTED, ev_lo added) and the single verdict."""
    n, net, ghosts, gr = pt._fleet_deep_pool_stats()      # SINGLE SOURCE OF TRUTH
    rets, no_size = _cohort_returns()
    ev_lo, mean, n_ret = _ev_lo(rets)

    n_thr = int(cfg["n_threshold"])
    bar = float(cfg["ev_lo_pass"])
    deadline = _parse_deadline(cfg["deadline"])
    past_deadline = _now() > deadline

    have_n = n >= n_thr
    ghost_ok = gr <= pt.GHOST_MAX
    ev_ok = (ev_lo is not None) and (ev_lo >= bar)
    is_pass = have_n and ev_ok and ghost_ok

    if is_pass:
        verdict = "PASS"
    elif have_n:
        verdict = "FAIL"                 # conclusive: enough data, bars not met
    elif past_deadline:
        verdict = "EXPIRED_FAIL"         # ran out of time without conclusive data
    else:
        verdict = "PENDING"

    return {
        "verdict": verdict,
        "n": n, "n_threshold": n_thr,
        "net_sol": round(net, 6),
        "ev_lo_pct": (round(ev_lo, 4) if ev_lo is not None else None),
        "ev_lo_pass": bar,
        "mean_pct": (round(mean, 4) if mean is not None else None),
        "n_returns": n_ret,
        "closes_missing_size_sol": no_size,
        "ghost_rate": round(gr, 4), "ghost_max": pt.GHOST_MAX,
        "ghosts": ghosts,
        "deadline": cfg["deadline"], "past_deadline": past_deadline,
        "metric": cfg["metric"],
    }


def _goalpost_check(cfg, cur_hash):
    """If the most recent log entry recorded a different config hash, warn loudly with a diff."""
    prev = _last_log_entry()
    if not prev or prev.get("config_hash") in (None, cur_hash):
        return
    print("\n" + "!" * 70)
    print("  ⚠  GOALPOST-MOVED WARNING — the kill criterion changed since the last run")
    print("!" * 70)
    print(f"  last evaluated: {prev.get('ts')}  (hash {str(prev.get('config_hash'))[:12]}…)")
    print(f"  now:            {_now().isoformat()}  (hash {cur_hash[:12]}…)")
    old = prev.get("config", {})
    for k in _CRITERION_FIELDS:
        ov, nv = old.get(k), cfg.get(k)
        if json.dumps(ov, sort_keys=True) != json.dumps(nv, sort_keys=True):
            print(f"    • {k}:  {ov!r}  →  {nv!r}")
    print("  A pre-registered criterion is supposed to be FIXED before the data. If you")
    print("  loosened the bar after seeing a FAIL, that is moving the goalposts — on record now.")
    print("!" * 70)


def _enforce_disarm(verdict):
    """Run ONLY the disarm action, ONLY on FAIL/EXPIRED_FAIL. Never arms anything."""
    if verdict not in ("FAIL", "EXPIRED_FAIL"):
        print(f"  --enforce: verdict is {verdict}; no action taken (enforcement fires only on FAIL/EXPIRED_FAIL).")
        return False
    targets = sorted(glob.glob(str(ROOT / "bots" / "bot*" / "ev_sizing.json")))
    print(f"  --enforce: {verdict} → executing the pre-committed DISARM (safe direction only):")
    print(f"             {_DISARM_CMD}")
    if not targets:
        print("             (no ev_sizing.json files present — nothing to disarm; sizing already off.)")
        return True
    removed = []
    for t in targets:
        try:
            os.remove(t)
            removed.append(os.path.relpath(t, ROOT))
        except Exception as e:
            print(f"             ! failed to remove {t}: {e}")
    print(f"             removed: {', '.join(removed) if removed else 'none'}")
    return True


def _append_log(result, cfg, cur_hash, enforced):
    entry = {
        "ts": _now().isoformat(),
        "verdict": result["verdict"],
        "n": result["n"], "ev_lo_pct": result["ev_lo_pct"],
        "net_sol": result["net_sol"], "ghost_rate": result["ghost_rate"],
        "enforced": enforced,
        "config_hash": cur_hash,
        "config": {k: cfg.get(k) for k in _CRITERION_FIELDS},
    }
    with open(LOG_PATH, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _print_human(result, cfg):
    v = result["verdict"]
    icon = {"PASS": "✅", "FAIL": "🔴", "PENDING": "⏳", "EXPIRED_FAIL": "⛔"}[v]
    print("=" * 70)
    print("  KILL CRITERION — pre-registered verdict")
    print("=" * 70)
    print(f"  metric:   {result['metric']}")
    ev = result["ev_lo_pct"]
    ev_s = f"{ev:+.3f}%" if ev is not None else "n/a (no sized closes)"
    print(f"  cohort:   n={result['n']} (need ≥{result['n_threshold']})   "
          f"clean net ◎{result['net_sol']:+.4f}   ghost {result['ghost_rate']:.0%} (≤{result['ghost_max']:.0%})")
    print(f"  ev_lo:    {ev_s}   (pass bar: ≥{result['ev_lo_pass']:+.3f}%   "
          f"from n={result['n_returns']} sized closes)")
    if result["closes_missing_size_sol"]:
        print(f"            ⚠ {result['closes_missing_size_sol']} cohort close(s) lack size_sol → excluded from ev_lo (pre-S80 legacy).")
    print(f"  deadline: {result['deadline']}  ({'PASSED' if result['past_deadline'] else 'not yet'})")
    print("  " + "─" * 66)
    print(f"  {icon}  VERDICT: {v}")
    print(f"  pre-committed action for {v}:")
    for ln in str(cfg["actions"].get(v, "(none written)")).splitlines() or ["(none written)"]:
        print(f"      {ln}")
    if v in ("FAIL", "EXPIRED_FAIL"):
        print(f"  → run with --enforce to execute the disarm:  {_DISARM_CMD}")
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(description="Pre-registered kill-criterion verdict tracker (read-only by default).")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--enforce", action="store_true",
                    help="on FAIL/EXPIRED_FAIL ONLY, run the disarm (rm bots/bot*/ev_sizing.json); never arms")
    args = ap.parse_args()

    if not CONFIG_PATH.exists():
        _scaffold_config()
        sys.exit(2)

    cfg = _load_config()
    cur_hash = _config_hash(cfg)
    result = evaluate(cfg)

    enforced = False
    if not args.json:
        _goalpost_check(cfg, cur_hash)
        _print_human(result, cfg)
    if args.enforce:
        enforced = _enforce_disarm(result["verdict"])

    # durable history (this tool's only write besides --enforce's disarm)
    _append_log(result, cfg, cur_hash, enforced)

    if args.json:
        out = dict(result)
        out["config_hash"] = cur_hash
        out["enforced"] = enforced
        print(json.dumps(out, indent=2))

    # exit code: 0 only on PASS or PENDING; nonzero on FAIL/EXPIRED_FAIL (cron-friendly)
    sys.exit(0 if result["verdict"] in ("PASS", "PENDING") else 1)


if __name__ == "__main__":
    main()
