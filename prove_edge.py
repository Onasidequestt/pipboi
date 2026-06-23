#!/usr/bin/env python3
"""
prove_edge.py — S94b · READ-ONLY edge prover for the deep_pool deploy gate.

THE PROBLEM IT SOLVES
  The EV-sizing genes (the real SOL lever) are gated behind PROVING the deep_pool edge
  +EV LIVE: KEPT-cohort (euphoria/aggressive/sniper) deep_pool closes, n≥15, clean net≥0,
  ghost≤10%. But deep_pool barely fires live — it's barred in dead/normal regimes and the
  market sits in `normal` ~75-85% of the time — so the gate has been stuck at n=5 for ages.
  At the live fire-rate the gate would take WEEKS/never to accumulate. We can't prove the
  edge by waiting.

WHAT THIS DOES (no live state touched; THE ONE OPERATOR RULE holds)
  Replays the durable forward_obs log through the SAME deep_pool admission predicate the
  live observer uses, and applies a REALIZABLE-EXIT model (paper_lab/lab.realized_return —
  stop-loss + trail-ride on the recorded fwd/fwd_min/fwd_max path, with a friction haircut
  and a ghost proxy). This accumulates the deep_pool edge's realized-EQUIVALENT EV at the
  full CANDIDATE rate (hundreds of obs) instead of the ~0 live-fire rate — giving the
  statistical power the live gate lacks, broken out BY REGIME so we can test whether the
  `normal` skip is still justified under the CURRENT scorer + a realistic exit.

  CRUCIAL — it CALIBRATES the shadow model against the ACTUAL live realized deep_pool
  closes (trades.db). If shadow-EV ≈ live-EV on the n we DO have, the shadow estimate on the
  larger candidate set is trustworthy. This is the bridge the raw forward_obs never had
  (forward_obs logs RAW forward returns — not exit-modeled, not ghost-aware — which is why
  the live gate never trusted it; this re-derives realizable, ghost-haircut returns).

USAGE
  python3 prove_edge.py [--friction 1.5] [--exit ride|tpsl|hold] [--min-score 0]
"""
import os, sys, json, math, glob, sqlite3, argparse, statistics
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "paper_lab"))
import lab  # realized_return, regime_of, _se  (read-only, stdlib)

FWD_OBS  = os.path.join(ROOT, "shared_memory", "forward_obs.jsonl")
KEPT     = {"euphoria", "aggressive", "sniper"}     # the +EV-regime cohort the gate counts
SKIPPED  = {"normal", "dead"}                       # _DEEP_POOL_SKIP_REGIMES
# live deep_pool predicate constants (observer.py)
MIN_LIQ      = 50_000.0
M5_MIN       = 1.0
BS_MIN       = 1.0
LIQ_MC_MIN   = 0.10
MAX_DRAIN    = -0.01     # lqv ≥ -0.01 (not draining at entry)
GHOST_FWDMIN = -90.0     # fwd_min ≤ -90% ⇒ treat as a ghost/unsellable (full-loss proxy);
                         # forward_obs returns are in PERCENT (fwd_min winsor-floored at -90).

# realizable exit policies (lab.realized_return dicts). The live non-canary exit ≈ "ride":
# smart-trail activates ~+10% and rides, SL ~-10%. "tpsl" = a fixed +7%/-10% bracket.
EXITS = {
    "ride": {"type": "trail", "act": 10.0, "retention": 0.75, "sl": 10.0},
    "tpsl": {"type": "tpsl",  "tp": 7.0,  "sl": 10.0},
    "hold": {"type": "hold"},
}


def _liq_mc(r):
    """liq/mc, falling back to liq/fdv when mc is missing (forward_obs mc is often 0)."""
    liq = r.get("liq") or 0.0
    mc  = r.get("mc") or 0.0
    if mc > 0:
        return liq / mc
    fdv = r.get("fdv") or 0.0
    return (liq / fdv) if fdv > 0 else 0.0


def deep_pool_admit(r):
    """Faithful replica of observer.py's deep_pool admission (regime skip NOT applied here —
    we measure ALL regimes to test the skip). Returns (ok, strong_rule)."""
    liq = r.get("liq") or 0.0
    if liq < MIN_LIQ:
        return False, ""
    lqv = r.get("lqv")
    lqv = 0.0 if lqv is None else lqv
    if lqv < MAX_DRAIN:                  # drain-at-entry exitability guard
        return False, ""
    m5 = r.get("m5")
    bs = r.get("bs")
    if m5 is None or bs is None:
        return False, ""
    if m5 < M5_MIN or bs < BS_MIN:
        return False, ""
    lmc = _liq_mc(r)
    if lmc < LIQ_MC_MIN:                 # deep relative to size
        return False, ""
    # REQUIRE_STRONG: admit only strict/filling sub-cohorts (drop the weak flat tail)
    filling = lqv > 0.01
    strict  = (m5 >= 2.0 and lmc >= 0.10 and bs >= 1.0)
    if strict and filling: rule = "strict_filling"
    elif filling:          rule = "filling"
    elif strict:           rule = "strict"
    else:                  rule = ""
    if not rule:
        return False, ""
    return True, rule


def load_rows():
    rows = []
    if not os.path.exists(FWD_OBS):
        return rows
    for line in open(FWD_OBS):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("fwd") is None or r.get("fwd_min") is None or r.get("fwd_max") is None:
            continue
        rows.append(r)
    rows.sort(key=lambda x: x.get("ts", 0.0))
    return rows


def realized(r, exit_name, friction):
    """Realizable return % with a ghost haircut. fwd/min/max are already in % units."""
    row = {"fwd": r["fwd"], "fwd_min": r["fwd_min"], "fwd_max": r["fwd_max"]}
    ret = lab.realized_return(row, EXITS[exit_name], friction_pct=friction)
    # ghost proxy: a pool that cratered to ~-100% intramove was likely unsellable → the
    # trail/SL can't actually fill; book the conservative near-total loss.
    if r["fwd_min"] is not None and r["fwd_min"] <= GHOST_FWDMIN:
        ret = min(ret, r["fwd_min"] - friction)
    return ret


def stats(rets):
    n = len(rets)
    if n == 0:
        return None
    mean = statistics.mean(rets)
    return {
        "n": n,
        "mean": mean,
        "ev_lo": mean - 1.64 * lab._se(rets),
        "median": statistics.median(rets),
        "win": sum(1 for x in rets if x > 0) / n,
        "ghost": sum(1 for x in rets if x <= GHOST_FWDMIN) / n,
    }


def live_calibration(min_score):
    """Realized EV (pnl_sol/size_sol %) of the ACTUAL live deep_pool closes, by KEPT/SKIPPED,
    to anchor the shadow model. Reads each bot's trades.db (read-only)."""
    kept, skipped = [], []
    for b in (1, 2, 3):
        dbp = os.path.join(ROOT, "bots", f"bot{b}", "trades.db")
        if not os.path.exists(dbp):
            continue
        try:
            con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
            for (d,) in con.execute("SELECT data FROM trades WHERE event='close'"):
                r = json.loads(d)
                if r.get("play") not in ("deep_pool", "brain_rule"):
                    continue
                ps, sz = r.get("pnl_sol"), r.get("size_sol")
                if ps is None or not sz:
                    continue
                if str(r.get("exit_reason", "")).startswith("ghost") or r.get("ghost"):
                    pct = -100.0  # ghost = full loss, consistent with the shadow proxy
                else:
                    pct = ps / sz * 100.0
                (kept if r.get("regime") in KEPT else skipped).append(pct)
            con.close()
        except Exception:
            continue
    return kept, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--friction", type=float, default=1.5, help="round-trip cost %% haircut (forward_obs fwd is RAW)")
    ap.add_argument("--exit", default="ride", choices=list(EXITS), help="realizable exit model")
    ap.add_argument("--min-score", type=float, default=0.0, help="extra score floor (live strict_gate=65)")
    args = ap.parse_args()

    rows = load_rows()
    if not rows:
        print("no forward_obs with fwd_min/max — nothing to prove."); return

    span_days = max((rows[-1]["ts"] - rows[0]["ts"]) / 86400.0, 1e-9)
    by_reg = defaultdict(list)
    by_rule = defaultdict(list)
    n_admit = 0
    for r in rows:
        ok, rule = deep_pool_admit(r)
        if not ok:
            continue
        if args.min_score and (r.get("score") or 0) < args.min_score:
            continue
        n_admit += 1
        ret = realized(r, args.exit, args.friction)
        by_reg[lab.regime_of(r.get("agg"))].append(ret)
        by_rule[rule].append(ret)

    print("═" * 78)
    print(f"  PROVE-THE-EDGE · deep_pool realizable-EV by regime  (read-only, forward_obs)")
    print(f"  exit={args.exit}  friction={args.friction}%  min_score={args.min_score or '—'}"
          f"  ·  {len(rows)} obs over {span_days:.1f}d")
    print("═" * 78)
    print(f"  deep_pool-admissible candidates: {n_admit}  ({n_admit/span_days:.0f}/day)"
          f"  — vs live deep_pool fires ≈ 0/day")
    print()
    print(f"  {'regime':<11}{'n':>5}{'mean%':>8}{'ev_lo%':>8}{'med%':>7}{'WR':>6}{'ghost':>7}  cohort  verdict")
    print(f"  {'-'*70}")
    order = ["euphoria", "aggressive", "sniper", "normal", "dead", "unknown"]
    kept_rets, skip_rets = [], []
    for reg in order:
        s = stats(by_reg.get(reg, []))
        if not s:
            continue
        coh = "KEPT" if reg in KEPT else ("skip" if reg in SKIPPED else "—")
        if reg in KEPT: kept_rets += by_reg[reg]
        if reg in SKIPPED: skip_rets += by_reg[reg]
        verdict = "+EV ✓" if s["ev_lo"] > 0 else ("≈0" if s["mean"] > 0 else "−EV")
        print(f"  {reg:<11}{s['n']:>5}{s['mean']:>8.2f}{s['ev_lo']:>8.2f}{s['median']:>7.2f}"
              f"{s['win']*100:>5.0f}%{s['ghost']*100:>6.0f}%  {coh:<6}  {verdict}")

    print(f"  {'-'*70}")
    ks, ss = stats(kept_rets), stats(skip_rets)
    if ks:
        print(f"  {'KEPT (e/a/s)':<11}{ks['n']:>5}{ks['mean']:>8.2f}{ks['ev_lo']:>8.2f}"
              f"{ks['median']:>7.2f}{ks['win']*100:>5.0f}%{ks['ghost']*100:>6.0f}%  "
              f"  {'★ PROVES +EV' if ks['ev_lo']>0 else 'not yet ev_lo>0'}")
    if ss:
        print(f"  {'SKIPPED (n/d)':<11}{ss['n']:>5}{ss['mean']:>8.2f}{ss['ev_lo']:>8.2f}"
              f"{ss['median']:>7.2f}{ss['win']*100:>5.0f}%{ss['ghost']*100:>6.0f}%  "
              f"  {'← skip JUSTIFIED' if ss['ev_lo']<=0 else '← skip may be STALE (now +EV)'}")

    # ---- sub-rule breakdown (which sub-cohort carries the edge) ----
    print()
    print("  by strong-rule sub-cohort (all regimes):")
    for rule in ("strict_filling", "filling", "strict"):
        s = stats(by_rule.get(rule, []))
        if s:
            print(f"    {rule:<16} n={s['n']:>4}  mean {s['mean']:+.2f}%  ev_lo {s['ev_lo']:+.2f}%  WR {s['win']*100:.0f}%")

    # ---- live calibration (the trust bridge) ----
    lk, lsk = live_calibration(args.min_score)
    print()
    print("  ── LIVE CALIBRATION (actual trades.db deep_pool/brain_rule closes) ──")
    for label, live, shadow in (("KEPT", lk, ks), ("SKIPPED", lsk, ss)):
        if live:
            lm = statistics.mean(live)
            sm = f"{shadow['mean']:+.2f}%" if shadow else "—"
            agree = ""
            if shadow:
                agree = " ✓ shadow≈live" if abs(lm - shadow["mean"]) < 1.5 else " ⚠ shadow≠live (model gap)"
            print(f"    {label:<8} live n={len(live):>2} mean {lm:+.2f}%   shadow {sm}{agree}")
        else:
            print(f"    {label:<8} live n= 0  (no live closes yet — shadow is the only read)")

    # ---- ETA / verdict ----
    print()
    cand_per_day_kept = sum(len(by_reg.get(r, [])) for r in KEPT) / span_days
    print("  ── VERDICT ──")
    if ks and ks["ev_lo"] > 0:
        print(f"    ★ The deep_pool edge clears ev_lo>0 in the KEPT regimes on {ks['n']} realizable")
        print(f"      candidates (vs the live gate's n=5). The edge is REAL under a {args.exit} exit.")
    elif ks and ks["mean"] > 0:
        print(f"    ◑ KEPT mean is +{ks['mean']:.2f}% but ev_lo={ks['ev_lo']:+.2f}% (n={ks['n']}): directionally")
        print(f"      +EV, not yet statistically clear. More candidates / a tighter exit would settle it.")
    else:
        print(f"    ✗ KEPT cohort is not +EV under this exit/friction — the edge does NOT prove out here.")
    if ss and ks and ss["mean"] < ks["mean"]:
        print(f"    · normal/dead skip looks {'JUSTIFIED' if ss['ev_lo']<=0 else 'questionable'}: "
              f"SKIPPED mean {ss['mean']:+.2f}% vs KEPT {ks['mean']:+.2f}%.")
    print(f"    · candidate rate in KEPT regimes ≈ {cand_per_day_kept:.0f}/day — the evidence to settle")
    print(f"      this exists NOW in forward_obs; the live gate is starved only because deep_pool")
    print(f"      can't fire often enough, NOT because the data is missing.")
    print("═" * 78)


if __name__ == "__main__":
    main()
