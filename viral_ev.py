#!/usr/bin/env python3
"""
viral_ev.py — does the on-chain VIRALITY signature predict realizable EV? (S90)

The evidence step before paying for LunarCrush. The "Jotchua signature" (vol-accel + sustained
buy-pressure + still-climbing + sellable + filling) is computed PURELY from fields already logged
in shared_memory/forward_obs.jsonl — no new logging, no waiting, no API. It then asks the only
question that matters: does a higher virality score sort the 30-min forward return (`fwd`) higher?
And does the COMPOSITE beat vol-accel ALONE (the current lead scorer dim)? If yes, the off-chain
LunarCrush layer is worth measuring next. If no, the paid signal probably won't help either.

READ-ONLY. Pure stdlib. Mirrors virality_probe.py's gates (hard $30k liq floor, volume-credibility
on the accel/buy ratios) so micro-pool noise can't fake a score. Run: python3 viral_ev.py
"""
import json
import math
from pathlib import Path

OBS = Path(__file__).parent / "shared_memory" / "forward_obs.jsonl"
_LIQ_FLOOR = 30_000.0


def viral_score(r: dict) -> float:
    """0–100 Jotchua-shape score from forward_obs fields. Mirrors virality_probe weights."""
    liq = r.get("liq", 0) or 0
    v1h = r.get("v1h", 0) or 0
    cred = min(v1h / 10_000.0, 1.0)                     # volume credibility 0–1
    s = 0.0
    # vol-acceleration (LEAD) — vacc already logged, credibility-scaled
    vacc = r.get("vacc", 0) or 0
    s += 30 * min(vacc / 2.0, 1.0) * cred
    # buy pressure (5m ratio, needs a real sample)
    tx5 = (r.get("b5", 0) or 0) + (r.get("s5", 0) or 0)
    bs = (r.get("bs", 0.5) or 0.5) if tx5 >= 8 else 0.5
    s += 20 * max(0.0, (bs - 0.5) / 0.3)
    # live multi-leg: 1h climbing, 5m not actively dumping
    m1h = r.get("m1h", 0) or 0
    m5 = r.get("m5", 0) or 0
    leg = 0.0
    if m1h > 5: leg += 0.6
    elif m1h < -3: leg -= 0.3
    if m5 > -2: leg += 0.4
    s += 25 * max(0.0, min(leg, 1.0))
    # sellability (saturating) + hard ghost gate
    if liq >= _LIQ_FLOOR:
        s += 15 * min((liq - _LIQ_FLOOR) / 90_000.0, 1.0)
    # liquidity FILLING bonus (lqv > 0 = LP added while trading)
    lqv = r.get("lqv", 0) or 0
    s += 10 * max(0.0, min(lqv / 0.05, 1.0))
    if liq < _LIQ_FLOOR:
        s = min(s, 22.0)                                # ghost zone capped
    return max(0.0, min(100.0, s))


def _stats(vals):
    n = len(vals)
    if n == 0:
        return 0, 0.0, 0.0, 0.0
    mean = sum(vals) / n
    win = 100.0 * sum(1 for v in vals if v > 0) / n
    if n > 1:
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
        lo = mean - 1.64 * sd / math.sqrt(n)           # one-sided 95% lower bound
    else:
        lo = mean
    return n, mean, win, lo


def _quintile_report(title, rows, keyfn):
    rows = [r for r in rows if r.get("fwd") is not None and keyfn(r) is not None]
    rows.sort(key=keyfn)
    n = len(rows)
    if n < 25:
        print(f"\n  {title}: only n={n} — too few to quintile.")
        return None
    qs = [rows[i * n // 5:(i + 1) * n // 5] for i in range(5)]
    print(f"\n  {title}  (n={n})")
    print(f"    {'band':<6} {'n':>5} {'score':>12} {'fwd EV':>9} {'win%':>6} {'fwd_lo':>8}")
    q1ev = q5ev = None
    for i, q in enumerate(qs, 1):
        sc = [keyfn(r) for r in q]
        nn, mean, win, lo = _stats([r["fwd"] for r in q])
        rng = f"{min(sc):.0f}–{max(sc):.0f}" if max(sc) > 5 else f"{min(sc):.2f}–{max(sc):.2f}"
        print(f"    Q{i:<5} {nn:>5} {rng:>12} {mean:>+8.2f}% {win:>5.0f}% {lo:>+7.2f}")
        if i == 1: q1ev = mean
        if i == 5: q5ev = mean
    if q1ev is not None and q5ev is not None:
        print(f"    → Q5−Q1 spread: {q5ev - q1ev:+.2f}%  (the sorting power — higher = the signal works)")
    return (q5ev - q1ev) if (q1ev is not None and q5ev is not None) else None


def main():
    if not OBS.exists():
        print("no forward_obs.jsonl"); return
    rows = []
    for line in OBS.read_text().splitlines():
        if line.strip():
            try: rows.append(json.loads(line))
            except Exception: pass
    for r in rows:
        r["_viral"] = viral_score(r)
    sellable = [r for r in rows if (r.get("liq", 0) or 0) >= _LIQ_FLOOR]

    print("═" * 72)
    print(f"  VIRALITY → EV   ·   {len(rows):,} obs   ·   {len(sellable):,} on sellable pools (≥$30k)")
    print("═" * 72)
    print("  Question: does the on-chain virality score sort the 30-min forward return?")
    print("  (`fwd` is raw/un-fee'd — a relative sorter, not a net-EV promise.)")

    # 1. the headline: virality composite on the cohort that matters (sellable)
    spread_v = _quintile_report("VIRALITY composite — SELLABLE pools (≥$30k)", sellable, lambda r: r["_viral"])
    # 2. baseline: does the composite beat vol-accel ALONE (current lead dim)?
    spread_a = _quintile_report("vol-accel ALONE — SELLABLE pools (baseline)", sellable, lambda r: (r.get("vacc") or 0))
    # 3. all obs (incl. the micro-pool ghost flood) for context
    _quintile_report("VIRALITY composite — ALL obs (incl. <$30k ghost zone)", rows, lambda r: r["_viral"])

    # 4. recency stability on the sellable cohort (avoid overfit — split chronologically)
    sell_t = sorted([r for r in sellable if r.get("ts")], key=lambda r: r["ts"])
    if len(sell_t) >= 50:
        half = len(sell_t) // 2
        for label, chunk in (("FIRST half", sell_t[:half]), ("SECOND half (recent)", sell_t[half:])):
            top = [r for r in chunk if r["_viral"] >= 50]
            bot = [r for r in chunk if r["_viral"] < 50]
            _, tm, tw, tlo = _stats([r["fwd"] for r in top])
            _, bm, bw, blo = _stats([r["fwd"] for r in bot])
            print(f"\n  {label}: viral≥50 fwd {tm:+.2f}% (n={len(top)}, lo {tlo:+.2f}) | "
                  f"viral<50 {bm:+.2f}% (n={len(bot)})")

    # verdict
    print("\n" + "─" * 72)
    if spread_v is None:
        print("  VERDICT: too few sellable obs to judge — let forward_obs accumulate.")
    else:
        beats = (spread_a is not None and spread_v > spread_a + 0.3)
        if spread_v > 1.0 and beats:
            print(f"  VERDICT: ✅ virality SORTS EV (Q5−Q1 {spread_v:+.2f}%) AND beats vol-accel alone")
            print(f"           ({spread_a:+.2f}%) → the composite adds signal. Off-chain (LunarCrush)")
            print( "           layer is worth measuring next; consider social as a SIZING multiplier.")
        elif spread_v > 1.0:
            print(f"  VERDICT: ~ virality sorts EV (Q5−Q1 {spread_v:+.2f}%) but does NOT clearly beat")
            print(f"           vol-accel alone ({spread_a:+.2f}%) → the on-chain composite ≈ the lead dim")
            print( "           already does. Paying for off-chain social is hard to justify yet.")
        else:
            print(f"  VERDICT: ✗ virality does NOT sort realizable EV here (Q5−Q1 {spread_v:+.2f}%).")
            print( "           The paid off-chain (LunarCrush) layer is unlikely to help — save the $29.")
    print("─" * 72)


if __name__ == "__main__":
    main()
