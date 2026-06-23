#!/usr/bin/env python3
"""DUST SHADOW GATE (S99) — the size-NORMALIZED evidence gate the dust executor feeds.

READ-ONLY. Arms NOTHING. The live deploy gate (prestige_tracker._fleet_deep_pool_stats)
measures the deep_pool edge in ABSOLUTE SOL (net = Σ pnl_sol, ghost = pnl_sol < −0.02) and
is starved by FIRE-RATE — deep_pool skips dead/normal and the market is `normal` 75-85% of
the tape, so it fires ~0×/day and freezes at n=5/15. The S99 dust executor fires the IDENTICAL
deep_pool predicate across those skipped regimes at ~◎0.01 to manufacture real on-chain closes
(genuine friction, genuine ghosts) WITHOUT risking capital. Because we log size_sol (S80), the
edge reads as RETURN-% (pnl_sol/size_sol), which normalizes away the size difference — so a
dust close and a real close are directly comparable on a per-trade-EV basis.

This tool reads the dust + real deep_pool/brain_rule closes, size-normalizes them, and mirrors
the live gate's three bars in return-% terms:
    n ≥ MIN_CLOSES          (volume)
    mean return-% ≥ 0       (is the edge +EV? — the absolute-net analog, size-independent)
    ghost-rate ≤ GHOST_MAX  (exitability) — ghost judged by RETURN-% (catches dust ghosts that
                            the live gate's absolute −0.02 floor would miss)

It is DELIBERATELY separate from the arming gate: EV-sizing on REAL capital still arms only on
the live gate's REAL-capital evidence (prestige_tracker excludes dust_shadow). This shadow gate
tells you EARLY whether to expect +EV as the dust closes pile up. THE ONE OPERATOR RULE intact —
it writes no ev_sizing.json, touches no bot, signs nothing.

  python3 dust_gate.py                  # full shadow-gate readout (dust + real, by regime)
  python3 dust_gate.py --sellable-only  # mature n on the route-clean sample (drop entry-ghosts)
  python3 dust_gate.py --min-roundtrip -10   # sellable = entry round-trip cost ≥ this % (default −10)

⚠ HONEST PROXY CAVEAT: dust pays outsized fee/impact drag vs a scaled entry → its return-% runs
slightly CONSERVATIVE (a real +EV edge reads marginally worse, never better). Safe-direction:
the shadow gate is HARDER to pass on dust than the true edge warrants, so a PASS here is real.
"""
import sys
from statistics import mean, pstdev

from prestige_tracker import (
    _closes, BOTS, _DP_PLAYS, MIN_CLOSES, GHOST_MAX,
    _fleet_deep_pool_stats,
)

_GHOST_RET = -0.50   # return-% ghost floor: pnl≈0 AND lost >50% of capital = unsellable (size-independent)


def _ret(r):
    """Size-normalized realized return (pnl_sol / size_sol). None when size_sol is missing/0."""
    sz = r.get("size_sol") or 0.0
    if sz <= 0:
        return None
    return (r.get("pnl_sol", 0.0) or 0.0) / sz


def _is_ghost_ret(r, ret):
    """Ghost in RETURN-% terms — catches dust ghosts the live gate's absolute −0.02 floor misses."""
    if r.get("ghost"):
        return True
    return abs(r.get("pnl", 0.0)) < 1e-9 and ret is not None and ret < _GHOST_RET


def _sellable(r, min_roundtrip):
    """True when the entry route was clean enough to be exitable (route-depth feature, S99).

    route_roundtrip_pct = immediate buy→sell friction% at entry (0 = perfect, negative = exit
    costs you). None = pre-S99 row (no route logged) — kept (can't prove it was a trap)."""
    rt = r.get("route_roundtrip_pct")
    if rt is None:
        return True
    return rt >= min_roundtrip


def _collect(sellable_only=False, min_roundtrip=-10.0):
    """All deep_pool/brain_rule closes with a size_sol, size-normalized + tagged dust/real."""
    rows = []
    for b in BOTS:
        for r in _closes(b):
            play = r.get("play") or r.get("tier") or r.get("insane_tier")
            if play not in _DP_PLAYS:
                continue
            ret = _ret(r)
            if ret is None:
                continue   # pre-S80 row without the return-% denominator
            if sellable_only and not _sellable(r, min_roundtrip):
                continue
            rows.append({
                "bot": b,
                "ret": ret,
                "ghost": _is_ghost_ret(r, ret),
                "dust": bool(r.get("dust_shadow")),
                "regime": r.get("regime") or "?",
                "rule": r.get("deep_pool_strong_rule") or play,
                "rt": r.get("route_roundtrip_pct"),
            })
    return rows


def _stats(rows):
    """(n, ghosts, ghost_rate, mean_ret, ev_lo) over a row set. Mean over CLEAN (non-ghost) rows."""
    n = len(rows)
    ghosts = sum(1 for r in rows if r["ghost"])
    clean = [r["ret"] for r in rows if not r["ghost"]]
    gr = (ghosts / n) if n else 0.0
    if not clean:
        return n, ghosts, gr, 0.0, 0.0
    m = mean(clean)
    if len(clean) > 1:
        se = pstdev(clean) / (len(clean) ** 0.5)
        ev_lo = m - 1.64 * se
    else:
        ev_lo = m
    return n, ghosts, gr, m, ev_lo


def _line(label, rows):
    n, ghosts, gr, m, ev_lo = _stats(rows)
    print(f"   {label:<22} n={n:<4} mean={m:+.3%}  ev_lo={ev_lo:+.3%}  ghost={gr:.0%} ({ghosts}/{n})")
    return n, gr, m


def main():
    sellable_only = "--sellable-only" in sys.argv
    min_rt = -10.0
    if "--min-roundtrip" in sys.argv:
        try:
            min_rt = float(sys.argv[sys.argv.index("--min-roundtrip") + 1])
        except Exception:
            pass

    rows = _collect(sellable_only=sellable_only, min_roundtrip=min_rt)
    dust = [r for r in rows if r["dust"]]
    real = [r for r in rows if not r["dust"]]

    print("\n══════════════════════════════════════════════════════════════════════")
    print("  DUST SHADOW GATE (S99) — size-normalized deep_pool evidence  [READ-ONLY]")
    if sellable_only:
        print(f"  filter: SELLABLE-ONLY (entry route-roundtrip ≥ {min_rt:.0f}%)")
    print("══════════════════════════════════════════════════════════════════════")

    print("\n  COHORT (return-%, size-normalized):")
    n_all, gr_all, m_all = _line("ALL deep_pool", rows)
    _line("  · dust lane", dust)
    _line("  · real capital", real)

    # per-regime split (the whole point — the dust lane samples the skipped tape)
    regimes = sorted({r["regime"] for r in rows})
    if regimes:
        print("\n  BY REGIME (all):")
        for rg in regimes:
            _line(f"  {rg}", [r for r in rows if r["regime"] == rg])

    # per-strong-rule split
    rules = sorted({r["rule"] for r in rows})
    if len(rules) > 1:
        print("\n  BY SUB-RULE (all):")
        for ru in rules:
            _line(f"  {ru[:20]}", [r for r in rows if r["rule"] == ru])

    # ── the shadow gate verdict (mirrors the live gate's bars, size-normalized) ──
    n, ghosts, gr, m, ev_lo = _stats(rows)
    g_n   = n >= MIN_CLOSES
    g_net = m >= 0
    g_gh  = gr <= GHOST_MAX
    print("\n  SHADOW GATE (size-normalized; arms NOTHING — the live gate stays the gatekeeper):")
    print(f"   [{'PASS' if g_n   else 'WAIT'}]  closes n≥{MIN_CLOSES}        {n}")
    print(f"   [{'PASS' if g_net else 'WAIT'}]  mean return ≥ 0       {m:+.3%}  (ev_lo {ev_lo:+.3%})")
    print(f"   [{'PASS' if g_gh  else 'WAIT'}]  ghost-rate ≤ {GHOST_MAX:.0%}      {gr:.0%} ({ghosts}/{n})")
    ready = g_n and g_net and g_gh
    print(f"\n  → shadow verdict: {'✅ EDGE LOOKS +EV (size-normalized)' if ready else '⏳ NOT YET'}"
          f"{'' if ready else ' — binding: ' + ', '.join(w for ok,w in ((g_n,'n'),(g_net,'mean≥0'),(g_gh,'ghost≤10%')) if not ok)}")

    # ── the REAL live arming gate, for side-by-side (this is what actually arms EV-sizing) ──
    ln, lnet, lgh, lgr = _fleet_deep_pool_stats()
    print("\n  LIVE ARMING GATE (real-capital only — dust EXCLUDED; absolute ◎):")
    print(f"   n={ln}/{MIN_CLOSES}   net=◎{lnet:+.4f}   ghost={lgr:.0%} ({lgh}/{ln})")
    print("   (EV-sizing arms ONLY off this — dust accelerates your READ, never the arming.)\n")


if __name__ == "__main__":
    main()
