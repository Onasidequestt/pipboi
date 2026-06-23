#!/usr/bin/env python3
"""
regime_realized.py  ·  S86  ·  READ-ONLY

The self-validation tool S85 owed: is the kept-regime (euphoria/aggressive/sniper)
deep_pool edge REAL *live*, or only on paper (forward_obs)?

For every CLEAN (ghost-excluded) deep_pool/brain_rule close across the fleet, bucket
realized pnl_sol by REGIME × PLAY and report n / clean-net / WR / mean / ev_lo
(mean − 1.64·SE), alongside the PAPER forward-return mean for the same regime. A
per-regime confidence verdict (TRUST n≥8 · THIN 3-7 · PAPER-ONLY <3) tells you how
much to trust the realized number.

★ The gate-relevant line: the KEPT-regime cohort's clean-n and clean-net, and how
many more good closes until it reaches the gate's n≥15 @ net≥0 *on the cohort we
actually trade* (vs the legacy all-regime gate that still carries dead normal history).

Ghost definition MIRRORS prestige_tracker._is_ghost (the GATE's definition) so this
tool's clean-net is directly comparable to the deploy gate. NOT regime_ev's
exit-reason version — that one answers a different (per-play edge) question.

Usage:
  python3 regime_realized.py            # the table + kept-regime gate readout
  python3 regime_realized.py --paper    # add the paper forward_obs comparison column
  python3 regime_realized.py --all       # include pre-clean-era rows (default: clean era only)
"""
import json, sqlite3, math, argparse, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOTS = (1, 2, 3)
DP_PLAYS = {"deep_pool", "brain_rule"}          # mirrors prestige_tracker._DP_PLAYS
KEPT = ("euphoria", "aggressive", "sniper")     # S85 admits these
SKIPPED = ("normal", "dead")
CLEAN_ERA = "2026-06-05"                          # matches regime_ev / edge_report
MIN_CLOSES = 15                                   # gate n
FWD_OBS = ROOT / "shared_memory" / "forward_obs.jsonl"
REGIME_ORDER = ["euphoria", "aggressive", "sniper", "normal", "dead", "?"]


def _is_ghost(d: dict) -> bool:
    """GATE definition (prestige_tracker._is_ghost): flagged ghost, OR a ~zero-pnl
    close that nonetheless lost > 0.02 SOL (the unsellable-remainder signature)."""
    if d.get("ghost"):
        return True
    return abs(d.get("pnl", 0.0) or 0.0) < 1e-9 and (d.get("pnl_sol", 0.0) or 0.0) < -0.02


def _regime_of_agg(agg: float) -> str:
    if agg >= 600_000: return "euphoria"
    if agg >= 280_000: return "aggressive"
    if agg >= 110_000: return "normal"
    if agg >= 48_000:  return "sniper"
    return "dead"


def _collect_realized(all_time: bool):
    """{regime: {play: [pnl_sol,...]}} clean only, + ghost counts per regime."""
    by = {}      # regime -> play -> list of clean pnl_sol
    ghosts = {}  # regime -> count
    for b in BOTS:
        db = ROOT / f"bots/bot{b}/trades.db"
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        q = ("SELECT ts, data FROM trades WHERE event='close' "
             "AND json_extract(data,'$.play') IN ('deep_pool','brain_rule')")
        for r in con.execute(q):
            if not all_time and r["ts"] < CLEAN_ERA:
                continue
            try:
                d = json.loads(r["data"])
            except Exception:
                continue
            reg = d.get("regime") or "?"
            if _is_ghost(d):
                ghosts[reg] = ghosts.get(reg, 0) + 1
                continue
            play = d.get("play", "?")
            by.setdefault(reg, {}).setdefault(play, []).append(d.get("pnl_sol", 0.0) or 0.0)
        con.close()
    return by, ghosts


def _stats(xs):
    n = len(xs)
    if n == 0:
        return dict(n=0, net=0.0, mean=0.0, wr=0.0, ev_lo=0.0, se=0.0)
    net = sum(xs); mean = net / n
    wr = sum(1 for x in xs if x > 0) / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in xs) / (n - 1)
        se = math.sqrt(var) / math.sqrt(n)
    else:
        se = 0.0
    return dict(n=n, net=net, mean=mean, wr=wr, ev_lo=mean - 1.64 * se, se=se)


def _paper_by_regime():
    """Mean forward return per regime over forward_obs (broad deep_pool-ish: liq present)."""
    acc = {}
    if not FWD_OBS.exists():
        return acc
    with open(FWD_OBS) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            reg = _regime_of_agg(d.get("agg", 0.0) or 0.0)
            fwd = d.get("fwd")
            if fwd is None:
                continue
            acc.setdefault(reg, []).append(fwd)
    return {k: (sum(v) / len(v), len(v)) for k, v in acc.items()}


def _verdict(n):
    if n >= 8:  return "TRUST"
    if n >= 3:  return "THIN"
    if n >= 1:  return "PAPER-ONLY"
    return "NO-DATA"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include pre-clean-era rows")
    ap.add_argument("--paper", action="store_true", help="add forward_obs paper column")
    a = ap.parse_args()

    by, ghosts = _collect_realized(a.all)
    paper = _paper_by_regime() if a.paper else {}

    span = "ALL-TIME" if a.all else f"clean era (since {CLEAN_ERA})"
    print("=" * 74)
    print(f"  REGIME × PLAY — REALIZED deep_pool/brain_rule edge ({span}, ghosts excluded)")
    print("=" * 74)
    hdr = f"  {'regime':<11}{'play':<14}{'n':>3} {'cleanNet◎':>10} {'mean◎':>9} {'WR':>5} {'ev_lo◎':>9}  verdict"
    if a.paper:
        hdr += "   paper.fwd"
    print(hdr)
    for reg in REGIME_ORDER:
        plays = by.get(reg, {})
        if not plays and reg not in ghosts:
            continue
        tag = "KEEP" if reg in KEPT else ("SKIP" if reg in SKIPPED else "")
        for play in sorted(plays):
            s = _stats(plays[play])
            line = (f"  {reg:<11}{play:<14}{s['n']:>3} {s['net']:>+10.4f} {s['mean']:>+9.4f} "
                    f"{s['wr']:>4.0%} {s['ev_lo']:>+9.4f}  {_verdict(s['n']):<10}")
            if a.paper and reg in paper:
                pm, pn = paper[reg]
                line += f"  {pm:+6.2f}% (n={pn})"
            print(line)
        if reg in ghosts:
            print(f"  {reg:<11}{'(ghosts)':<14}{ghosts[reg]:>3}   excluded from net (bounded by ghost-RATE gate)")
        if tag:
            print(f"             └─ {tag}")
    print("-" * 74)

    # ── THE GATE-RELEVANT READOUT ──
    kept_xs = [p for reg in KEPT for p in sum(by.get(reg, {}).values(), [])]
    skip_xs = [p for reg in SKIPPED for p in sum(by.get(reg, {}).values(), [])]
    ks, ss = _stats(kept_xs), _stats(skip_xs)
    all_xs = kept_xs + skip_xs
    legacy = _stats(all_xs)
    total_ghosts = sum(ghosts.values())
    n_all = len(all_xs) + total_ghosts
    gr = total_ghosts / n_all if n_all else 0.0

    print("  ★ GATE DECOMPOSITION (clean = ghost-excluded net):")
    print(f"     LEGACY gate (all regimes):  n={n_all:>2} (clean {legacy['n']}) "
          f"clean-net ◎{legacy['net']:+.4f}  ghost {gr:.0%}")
    print(f"     SKIPPED cohort (normal/dead): n={ss['n']:>2}  clean-net ◎{ss['net']:+.4f}   ← the drag we no longer trade")
    print(f"     KEPT cohort (euph/aggr/snip): n={ks['n']:>2}  clean-net ◎{ks['net']:+.4f}   ev_lo/trade ◎{ks['ev_lo']:+.4f}")
    print()
    # how many more kept-regime closes to reach n>=15 @ net>=0 on the kept cohort
    need_n = max(0, MIN_CLOSES - ks['n'])
    if ks['net'] >= 0 and ks['n'] >= MIN_CLOSES:
        print("     → KEPT-cohort gate: ✅ would PASS (n≥15 AND net≥0).")
    elif ks['net'] >= 0:
        print(f"     → KEPT-cohort net is ALREADY ≥0 ◎{ks['net']:+.4f}; needs {need_n} more kept closes to reach n≥15.")
    else:
        per = ks['mean'] if ks['mean'] else 0.0
        print(f"     → KEPT-cohort net ◎{ks['net']:+.4f} (<0); needs +EV closes to climb AND {need_n} more for n≥15.")
    print("     (Legacy gate also blocked by the −normal drag it still carries — see Block-2 shadow.)")
    print("=" * 74)


if __name__ == "__main__":
    main()
