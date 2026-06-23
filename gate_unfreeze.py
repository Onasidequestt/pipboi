#!/usr/bin/env python3
"""
gate_unfreeze.py — READ-ONLY research. Can we unfreeze the EV-sizing deploy gate by
raising deep_pool's FIRE-RATE without re-adding the -EV tail S85 removed?

THE BOTTLENECK (S94b, quantified): deep_pool — the only gate-advancing play — is barred in
{dead,normal} and the market is `normal` ~75-85% of the time, so it fires ~0x/day and the
gate is frozen at n=5 (needs n>=15). The deep_pool edge IS proven +EV in the KEPT regimes.

THE QUESTION: S85 skipped `normal` because the WHOLE normal deep_pool cohort was ~-EV
(strict_filling by_regime normal -0.99). But that's the blended cohort. Is there a TIGHT,
recency-stable +EV sub-slice INSIDE normal that we could admit to multiply fire-rate and
carry the gate to n>=15 — without re-adding the -EV bulk?

Reuses prove_edge.py's faithful live predicate + realizable-exit model (ghost-aware).
NOTHING is applied; this only reads forward_obs + prints. THE ONE OPERATOR RULE holds.

USAGE: python3 gate_unfreeze.py [--friction 1.5] [--exit ride|tpsl|hold]
"""
import argparse, statistics
from collections import defaultdict
import prove_edge as pe   # deep_pool_admit, realized, stats, load_rows, KEPT, SKIPPED


def _split_stats(rets, n_half):
    """stats + recency: mean of first half vs second half (chronological)."""
    s = pe.stats(rets)
    if s is None:
        return None
    h1 = rets[:n_half]; h2 = rets[n_half:]
    s["h1"] = statistics.mean(h1) if h1 else None
    s["h2"] = statistics.mean(h2) if h2 else None
    s["stable"] = bool(h1 and h2 and statistics.mean(h1) > 0 and statistics.mean(h2) > 0)
    return s


def fmt(tag, s, fires_per_day=None):
    if s is None or s["n"] == 0:
        return f"  {tag:34} n=0"
    fr = f" fires/d {fires_per_day:5.1f}" if fires_per_day is not None else ""
    h1 = f"{s['h1']:+5.2f}" if s.get("h1") is not None else "  . "
    h2 = f"{s['h2']:+5.2f}" if s.get("h2") is not None else "  . "
    flag = "  <<< +EV STABLE" if s.get("stable") and s["ev_lo"] > 0 else (
           "  < +EV" if s["ev_lo"] > 0 else "")
    return (f"  {tag:34} n={s['n']:4} mean {s['mean']:+5.2f} ev_lo {s['ev_lo']:+5.2f} "
            f"win {s['win']:.2f} ghost {s['ghost']:.2f} | h1 {h1} h2 {h2}{fr}{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--friction", type=float, default=1.5)
    ap.add_argument("--exit", default="ride", choices=list(pe.EXITS))
    args = ap.parse_args()

    rows = pe.load_rows()
    if not rows:
        print("no forward_obs with fwd_min/max."); return
    span_days = max((rows[-1]["ts"] - rows[0]["ts"]) / 86400.0, 1e-9)
    print(f"forward_obs: {len(rows)} rows over {span_days:.1f} days "
          f"(exit={args.exit}, friction={args.friction}%)\n")

    # ---- 1) FIRE-RATE by regime over the deep_pool-admitted cohort -------------
    admitted = []           # (regime, row, rule, ret)
    for r in rows:
        ok, rule = pe.deep_pool_admit(r)
        if not ok:
            continue
        reg = pe.lab.regime_of(r.get("agg"))
        admitted.append((reg, r, rule, pe.realized(r, args.exit, args.friction)))

    by_reg = defaultdict(list)
    for reg, r, rule, ret in admitted:
        by_reg[reg].append(ret)
    print("=== 1) deep_pool-ADMITTED cohort by regime (the fire-rate picture) ===")
    tot = len(admitted)
    for reg in ("euphoria", "aggressive", "sniper", "normal", "dead"):
        v = by_reg.get(reg, [])
        share = 100.0 * len(v) / tot if tot else 0
        tag = f"{reg:11} [{'KEPT' if reg in pe.KEPT else 'SKIP'}]"
        print(fmt(f"{tag} {share:4.0f}% of fires", _split_stats(v, len(v)//2),
                  fires_per_day=len(v)/span_days))
    kept_n = sum(len(by_reg.get(k, [])) for k in pe.KEPT)
    norm_n = len(by_reg.get("normal", []))
    print(f"\n  KEPT fires/day = {kept_n/span_days:.1f}  |  normal fires/day = {norm_n/span_days:.1f} "
          f"(skipped today)  |  ratio {norm_n/max(kept_n,1):.1f}x")

    # ---- 2) search a TIGHT +EV sub-slice INSIDE normal ------------------------
    print("\n=== 2) sub-slices INSIDE `normal` (current scorer + ride exit) ===")
    norm = [(r, rule, ret) for (reg, r, rule, ret) in admitted if reg == "normal"]
    def slice_stats(pred):
        rets = [ret for (r, rule, ret) in norm if pred(r, rule)]
        return _split_stats(rets, len(rets)//2), len(rets)/span_days
    cands = [
        ("ALL normal deep_pool (baseline)",      lambda r, rule: True),
        ("strict_filling only",                   lambda r, rule: rule == "strict_filling"),
        ("filling (any)",                         lambda r, rule: "filling" in rule),
        ("bs>=1.5",                               lambda r, rule: (r.get("bs") or 0) >= 1.5),
        ("bs>=2.0",                               lambda r, rule: (r.get("bs") or 0) >= 2.0),
        ("strict_filling & bs>=1.5",              lambda r, rule: rule == "strict_filling" and (r.get("bs") or 0) >= 1.5),
        ("liq 100-400k (sweet spot)",             lambda r, rule: 100_000 <= (r.get("liq") or 0) <= 400_000),
        ("strict_filling & liq>=100k",            lambda r, rule: rule == "strict_filling" and (r.get("liq") or 0) >= 100_000),
        ("strict_filling & bs>=1.5 & liq>=100k",  lambda r, rule: rule == "strict_filling" and (r.get("bs") or 0) >= 1.5 and (r.get("liq") or 0) >= 100_000),
        ("score>=65 (live gate)",                 lambda r, rule: (r.get("score") or 0) >= 65),
        ("score>=65 & strict_filling",            lambda r, rule: (r.get("score") or 0) >= 65 and rule == "strict_filling"),
        ("score>=65 & bs>=1.5",                   lambda r, rule: (r.get("score") or 0) >= 65 and (r.get("bs") or 0) >= 1.5),
        ("vol_accel>=1.5 (S89 normal edge)",      lambda r, rule: (r.get("vacc") or 0) >= 1.5),
        ("strict_filling & vacc>=1.5",            lambda r, rule: rule == "strict_filling" and (r.get("vacc") or 0) >= 1.5),
    ]
    results = []
    for tag, pred in cands:
        s, fpd = slice_stats(pred)
        results.append((tag, s, fpd))
        print(fmt(tag, s, fires_per_day=fpd))

    # ---- 3) reference: the KEPT cohort + days-to-n>=15 with each lift ----------
    print("\n=== 3) gate impact (KEPT baseline vs adding the best normal slice) ===")
    kept_rets = [ret for (reg, r, rule, ret) in admitted if reg in pe.KEPT]
    ks = _split_stats(kept_rets, len(kept_rets)//2)
    print(fmt("KEPT (euph/aggr/sniper) — gate cohort", ks, fires_per_day=kept_n/span_days))
    kept_fpd = kept_n / span_days
    # live fire-rate is a fraction of candidate fire-rate (not every candidate becomes a live
    # close — concurrency caps, dupes, RugCheck). Use the live-vs-candidate ratio from S94b
    # (~live n=5 over the gate's life) as a rough discount; report candidate-rate days as the
    # OPTIMISTIC bound and note the live discount.
    print("\n  days to reach n>=15 (CANDIDATE rate; live rate is lower — see note):")
    for tag, s, fpd in results:
        if s is None or s["ev_lo"] <= 0 or not s.get("stable"):
            continue
        combined = kept_fpd + fpd
        d_now  = 15 / max(kept_fpd, 1e-9)
        d_lift = 15 / max(combined, 1e-9)
        print(f"    + {tag:34} -> {combined:5.1f} fires/d  (~{d_lift:4.1f}d vs {d_now:4.1f}d KEPT-only)")

    print("\nNOTE: candidate fire-rate >> live close-rate (concurrency caps / dupes / RugCheck).")
    print("      A +EV STABLE normal slice is the unlock: it adds gate-eligible fires in the")
    print("      regime that's 75-85% of the tape, without re-admitting the -EV normal bulk.")
    print("      Nothing applied — if a slice clears, the live change = add it to the deep_pool")
    print("      admission for `normal` ONLY (canary'd), NOT remove normal from the skip set wholesale.")


if __name__ == "__main__":
    main()
