#!/usr/bin/env python3
"""
rule_robustness.py — is a brain candidate's edge REAL or a window artifact?

WHY THIS EXISTS
---------------
strategy_brain ranks candidates by their EV-lower-bound on the CURRENT pooled window.
That answers "is it +EV right now with enough breadth" — but NOT "is the edge stable
over TIME." The deep_pool_* rules are in-sample grid-search winners; the handoff warns
they're overfit until proven out-of-sample. The single highest-stakes decision in this
project is flipping a rule to LIVE SOL, and that hinges on exactly this: does the edge
hold in the recent half of data, or was it front-loaded in one lucky window?

This tool splits the SAME realizable (sellable-only, liq≥$50k) matured observations the
brain scores on into chronological halves + per regime, and flags decay / one-window /
single-regime signatures that the pooled EV hides. Read-only. Zero trade risk.

VERDICTS
--------
  STABLE +EV    both time-halves mean-EV > 0 (robust if both EV-lo > 0 too)
  DECAYING ⚠    older half +EV, recent half ≤ 0 — edge fading, DO NOT wire
  EMERGING      recent half +EV, older half ≤ 0 — promising but young, wait for more
  -EV           both halves ≤ 0
  thin          a half has < MIN_HALF obs — not enough to judge stability yet
A rule is only trustworthy for live SOL when it is STABLE across halves AND not
dependent on a single regime.

USAGE
-----
    python3 rule_robustness.py                # all candidates with n ≥ 20, horizon 30m
    python3 rule_robustness.py --horizon 60
    python3 rule_robustness.py --min-n 30
"""
from __future__ import annotations

import argparse

import signal_lab as sl
import strategy_brain as sb

MIN_HALF = 15   # obs per time-half below which stability can't be judged


def _stats(pnls: list) -> dict:
    if not pnls:
        return {"n": 0, "wr": 0.0, "ev": 0.0, "ev_lo": 0.0}
    ev, ev_lo, _ = sb._ev_stats(pnls)
    wins = sum(1 for p in pnls if p > 0)
    return {"n": len(pnls), "wr": round(100 * wins / len(pnls), 1), "ev": ev, "ev_lo": ev_lo}


def _verdict(h1: dict, h2: dict) -> str:
    if h1["n"] < MIN_HALF or h2["n"] < MIN_HALF:
        return "thin (halves too small)"
    a, b = h1["ev"], h2["ev"]
    if a > 0 and b > 0:
        robust = h1["ev_lo"] > 0 and h2["ev_lo"] > 0
        return "STABLE +EV (robust)" if robust else "STABLE +EV (point-est)"
    if a > 0 >= b:
        return "DECAYING ⚠ (recent half ≤0)"
    if b > 0 >= a:
        return "EMERGING (recent only)"
    return "-EV (both halves)"


def analyze(horizon: int, min_n: int) -> None:
    sl.harvest_durable(horizon)
    fwd_all = sl.load_matured(horizon)
    fwd = [r for r in fwd_all if (r.get("liq") or 0) >= sl.SELL_FLOOR]
    fwd.sort(key=lambda r: r["ts"])
    span = ((fwd[-1]["ts"] - fwd[0]["ts"]) / 3600.0) if fwd else 0.0

    print("=" * 92)
    print(f"  RULE ROBUSTNESS — chronological-split stability  |  horizon {horizon}m")
    print(f"  realizable (sellable-only) obs: {len(fwd)}  ·  span {span:.1f}h  ·  "
          f"split at the midpoint by time")
    print("=" * 92)
    print(f"  {'rule':<20} {'n':>4} {'EV':>7} {'EVlo':>7} │ {'H1n':>4} {'H1EV':>7} │ "
          f"{'H2n':>4} {'H2EV':>7} │ verdict")
    print("  " + "-" * 90)

    rows = []
    for name, rule in sb.CANDIDATES.items():
        try:
            matched = [r for r in fwd if rule(r)]
        except Exception:
            matched = []
        if len(matched) < min_n:
            continue
        pnls = [r["fwd"] - sb.ROUND_TRIP_COST for r in matched]
        full = _stats(pnls)
        mid = len(matched) // 2
        h1 = _stats([r["fwd"] - sb.ROUND_TRIP_COST for r in matched[:mid]])
        h2 = _stats([r["fwd"] - sb.ROUND_TRIP_COST for r in matched[mid:]])
        # per-regime EV
        reg = {}
        for rg in ("hot", "normal", "dead"):
            rp = [r["fwd"] - sb.ROUND_TRIP_COST for r in matched if sl._regime_of(r) == rg]
            if rp:
                reg[rg] = round(sum(rp) / len(rp), 1)
        rows.append((name, full, h1, h2, _verdict(h1, h2), reg))

    rows.sort(key=lambda x: -x[1]["ev_lo"])
    for name, full, h1, h2, verdict, reg in rows:
        print(f"  {name:<20} {full['n']:>4} {full['ev']:>+7.2f} {full['ev_lo']:>+7.2f} │ "
              f"{h1['n']:>4} {h1['ev']:>+7.2f} │ {h2['n']:>4} {h2['ev']:>+7.2f} │ {verdict}")
        if reg:
            print(f"  {'':<20} regime EV: " + "  ".join(f"{k}={v:+.1f}%" for k, v in reg.items()))

    print("  " + "-" * 90)
    print("  Read: a live-wire candidate should be STABLE across halves AND not reliant on one")
    print("  regime. DECAYING = the in-sample edge is fading out-of-sample — do NOT wire it.")
    print("=" * 92)


def main() -> None:
    ap = argparse.ArgumentParser(description="Temporal-split robustness of brain candidate rules.")
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--min-n", type=int, default=20, help="min full matched obs to include a rule")
    a = ap.parse_args()
    analyze(a.horizon, a.min_n)


if __name__ == "__main__":
    main()
