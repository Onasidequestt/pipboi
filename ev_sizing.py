"""EV-weighted position sizing — DORMANT (not wired live).

Implements the §G.1 adaptive seam: scale a position's size by the admitting rule's
*measured* edge (`ev_lo` from the strategy brain) instead of blind confidence alone.
Fractional-Kelly style — bigger size for rules with a proven, tight lower-bound edge;
HALF size for unproven/negative-edge rules so the fleet bleeds slower while still
sampling. The multiplier is clamped so it can never blow up a position.

WHY ev_lo (not ev): ev_lo = mean − 1.64·SE is the variance-aware lower bound the brain
already uses for promotion. Sizing on the lower bound means a high-variance "lucky"
rule (wide SE → low ev_lo) is NOT rewarded — only a genuinely tight, proven edge is.

──────────────────────────────────────────────────────────────────────────────
INTEGRATION (NOT YET APPLIED — ships dormant on purpose):

  main.py, once at module top:
      from ev_sizing import ev_size_multiplier
      _EV_WEIGHTED_SIZING_ENABLED = False   # flag — flip to True only AFTER deep_pool
                                            # proves +EV live (handoff §G.1)

  main.py, in the SIZE block, right after `_size_frac` is computed:
      if _EV_WEIGHTED_SIZING_ENABLED:
          _rule = sig.get("brain_rule_name") or sig.get("insane_tier") or sig.get("play")
          _size_frac *= ev_size_multiplier(_rule)

  Flag False (default) = the block is skipped = byte-for-byte current sizing.
  The multiplier only re-weights an *already-decided* entry's size — it never adds an
  entry, never bypasses RugCheck/risk/caps. With ev_lo≤0 it can only SHRINK size.
──────────────────────────────────────────────────────────────────────────────
"""
import json
import time
from pathlib import Path

_BRAIN_PATH = Path(__file__).parent / "shared_memory" / "strategy_brain.json"

# Fractional-Kelly map from measured ev_lo (%) → size multiplier.
#   ev_lo ≤ 0      → floor (0.5×): unproven/negative edge, half size, never zero.
#   ev_lo = _REF_EV → 1.0× (neutral; current sizing). ≈ deep_pool_quality's lower bound.
#   ev_lo ≥ high   → cap (1.5×): a genuinely proven, tight edge earns up to +50%.
_MIN_MULT = 0.5
_MAX_MULT = 1.5
_REF_EV   = 4.0    # ev_lo (%) that maps to 1.0×
_KELLY_FR = 0.5    # fractional-Kelly damping (half-Kelly)

# ── S70: fraction-of-cap sizing (the real lever — see ev_size_fraction_of_cap) ──────
# The ±50% multiplier above is the wrong abstraction: it re-weights the C² conf-based
# size, but for fresh discovery tokens C² already crushes the base to ~1.5% of wallet
# (no trade history → default conf ~0.6 → conf² ≈ 0.1). Multiplying a crushed base by
# ≤1.5× is still ~2%. The fix is to size brain-VALIDATED entries by the RULE's measured
# edge directly — a fraction of the play's size_cap — bypassing the (irrelevant, defaulted)
# token confidence. ev_lo is the variance-aware lower bound, so this is already conservative.
#   ev_lo ≤ 0      → _FRAC_FLOOR-ish (degraded edge → tiny size, never zero)
#   ev_lo = _FRAC_FULL_EV → 1.0  (full cap — a genuinely proven, tight edge earns the cap)
# Linear ramp between, clamped [_FRAC_MIN, 1.0]. At a 15% cap this turns deep_pool_filling
# (ev_lo +6) from ~1.5% → ~12% of wallet — the ~5–8× step that turns months into weeks.
_FRAC_MIN     = 0.08   # hard floor — even a degraded admitted rule bets something tiny
_FRAC_AT_ZERO = 0.15   # fraction of cap at ev_lo = 0 (the y-intercept of the ramp)
_FRAC_FULL_EV = 8.0    # ev_lo (%) at which a rule earns the FULL size cap (1.0)

# Live trade tags → brain candidate name. The deep_pool entry path trades the brain's
# `deep_pool_quality` rule but tags positions `insane_tier="deep_pool"`. brain_rule trades
# already carry the admitting candidate's own name, so they pass through unchanged.
_RULE_ALIAS = {
    "deep_pool": "deep_pool_quality",
}

_cache = {"ts": 0.0, "data": {}}
_CACHE_TTL = 30.0


def _load_candidates() -> dict:
    """Return {rule_name: stats_dict} from the brain's last_eval. Never raises."""
    now = time.time()
    if now - _cache["ts"] < _CACHE_TTL and _cache["data"]:
        return _cache["data"]
    data = {}
    try:
        j = json.loads(_BRAIN_PATH.read_text())
        cands = (j.get("last_eval", {}) or {}).get("candidates", {})
        if isinstance(cands, dict):
            data = cands
        elif isinstance(cands, list):  # tolerate a list schema too
            data = {c.get("name"): c for c in cands if c.get("name")}
    except Exception:
        data = {}
    _cache["ts"] = now
    _cache["data"] = data
    return data


def ev_size_multiplier(rule_name, default: float = 1.0) -> float:
    """Size multiplier in [_MIN_MULT, _MAX_MULT] for the admitting rule.

    Never raises. Unknown rule / no brain data / no ev_lo → `default` (1.0 = current
    sizing), so a brain hiccup can never change sizing when this is wired on.
    """
    try:
        if not rule_name:
            return default
        name = _RULE_ALIAS.get(rule_name, rule_name)
        c = _load_candidates().get(name)
        if not c:
            return default
        ev_lo = c.get("ev_lo")
        if ev_lo is None:
            return default
        raw = 1.0 + _KELLY_FR * (float(ev_lo) - _REF_EV) / _REF_EV
        return round(max(_MIN_MULT, min(_MAX_MULT, raw)), 4)
    except Exception:
        return default


def _frac_for_ev(ev_lo: float) -> float:
    """Pure map: rule ev_lo (%) → target fraction of the play's size cap, clamped."""
    raw = _FRAC_AT_ZERO + (ev_lo / _FRAC_FULL_EV) * (1.0 - _FRAC_AT_ZERO)
    return round(max(_FRAC_MIN, min(1.0, raw)), 4)


def ev_size_fraction_of_cap(rule_name, default: float = None):
    """Target fraction (0,1] of a play's size_cap for a brain-VALIDATED entry, driven by
    the rule's measured ev_lo. This is the rule-edge sizing lever (replaces conf²/C² for
    validated entries). Returns `default` (None ⇒ caller keeps its existing C² sizing) when
    the rule is unknown / has no brain data / no ev_lo — so a brain hiccup never changes
    sizing. Never raises. The result still gets multiplied by the cap + market vol_scale +
    headroom downstream, so it can only ever size WITHIN the existing risk envelope."""
    try:
        if not rule_name:
            return default
        name = _RULE_ALIAS.get(rule_name, rule_name)
        c = _load_candidates().get(name)
        if not c:
            return default
        ev_lo = c.get("ev_lo")
        if ev_lo is None:
            return default
        return _frac_for_ev(float(ev_lo))
    except Exception:
        return default


# ── S75: strong-AND-stable size-up gate (the "only size up the proven sub-edge" lever) ──
# ev_size_fraction_of_cap sizes ANY rule by its ev_lo (even a weak +1). For sizing up real
# capital we want a stricter test: only the brain's CURRENTLY strong AND temporally-stable
# sub-cohorts (deep_pool_filling / _strict / _strict_filling — ev_lo +5..+7.6, both halves
# positive). A rule that drops below the bar OR whose 2nd-half edge goes negative (decay,
# like deep_pool_quality's H2 −1.2) auto-falls-through to `default` (None ⇒ caller keeps the
# small C² sizing). This is "talk to the brain to size up when confident in a LARGE return,
# and stop the moment the brain says the edge weakened." Never raises.
_STRONG_EV_BAR = 3.0    # ev_lo (%) a sub-rule must clear to earn size-up
# S86: per-regime obs floor for the REGIME-CONDITIONAL size-up path (variance guard — a thin
# regime whose mean looks huge but whose ev_lo is noisy must NOT earn real SOL).
_STRONG_REGIME_MIN_N = 8

def ev_strong_fraction_of_cap(rule_name, regime: str = None, default: float = None):
    """Fraction of cap ONLY if `rule_name` is a strong, stable edge right now. Two paths:

    S86 REGIME-CONDITIONAL (when `regime` is given): size by the brain's regime-ISOLATED
    lower bound `by_regime_lo[regime]` when PROVEN — n ≥ _STRONG_REGIME_MIN_N AND ev_lo ≥
    _STRONG_EV_BAR. Fixes the S85 regime-mix artifact: the BLENDED ev_lo/both-halves are
    dragged under the bar by normal/dead obs the fleet no longer trades (strict_filling
    blended ev_lo 2.7 / h2 −1.85 FAILS even though euphoria ev_lo +9 in isolation), so
    without this the SIZE gene could NEVER express post-gate. The 1.64·SE lower bound +
    n-floor keep it honest (sniper +28 mean on n=5 → ev_lo −2.8 → correctly refused).
    Strictly ADDITIVE: an unproven regime falls through to the legacy bar, so expression is
    never WORSE than today.

    LEGACY blended (no regime / regime unproven): ev_lo ≥ _STRONG_EV_BAR AND both
    chronological halves (ev_h1, ev_h2) ≥ 0. Otherwise → `default`. Never raises."""
    try:
        if not rule_name:
            return default
        name = _RULE_ALIAS.get(rule_name, rule_name)
        c = _load_candidates().get(name)
        if not c:
            return default
        # ── S86 regime-conditional path (the size-up UNLOCK for the kept regimes) ──
        if regime:
            rl = (c.get("by_regime_lo") or {}).get(regime)
            if rl is not None:
                try:
                    if int(rl.get("n", 0)) >= _STRONG_REGIME_MIN_N and float(rl.get("ev_lo")) >= _STRONG_EV_BAR:
                        return _frac_for_ev(float(rl["ev_lo"]))
                except (TypeError, ValueError):
                    pass
            # regime given but NOT proven → fall through to the blended bar (never worse than today)
        # ── legacy blended strong+stable bar (unchanged behavior) ──
        ev_lo = c.get("ev_lo")
        if ev_lo is None or float(ev_lo) < _STRONG_EV_BAR:
            return default
        h1, h2 = c.get("ev_h1"), c.get("ev_h2")
        if h1 is not None and float(h1) < 0.0:
            return default
        if h2 is not None and float(h2) < 0.0:
            return default
        return _frac_for_ev(float(ev_lo))
    except Exception:
        return default


def _selftest() -> bool:
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and cond
        print(("  ok  " if cond else "  FAIL ") + msg)

    # Pure-math invariants (independent of live brain state)
    def mult_for(ev):  # bypass cache/file: emulate the map directly
        raw = 1.0 + _KELLY_FR * (ev - _REF_EV) / _REF_EV
        return round(max(_MIN_MULT, min(_MAX_MULT, raw)), 4)

    chk(mult_for(-5.0) == _MIN_MULT, f"ev_lo −5% → floor {_MIN_MULT}× (negative edge shrinks)")
    chk(abs(mult_for(_REF_EV) - 1.0) < 1e-9, f"ev_lo {_REF_EV}% → 1.0× (neutral)")
    chk(mult_for(20.0) == _MAX_MULT, f"ev_lo 20% → cap {_MAX_MULT}× (clamped)")
    chk(mult_for(0.0) >= _MIN_MULT and mult_for(0.0) < 1.0, "ev_lo 0% → between floor and 1.0×")
    # Never-raise + default behaviour
    chk(ev_size_multiplier(None) == 1.0, "None rule → 1.0× default")
    chk(ev_size_multiplier("does_not_exist") == 1.0, "unknown rule → 1.0× default")
    chk(_MIN_MULT <= ev_size_multiplier("deep_pool") <= _MAX_MULT, "deep_pool→deep_pool_quality alias resolves, in range")

    # Fraction-of-cap map (the real lever)
    chk(_frac_for_ev(_FRAC_FULL_EV) == 1.0, f"ev_lo {_FRAC_FULL_EV}% → full cap (1.0)")
    chk(abs(_frac_for_ev(0.0) - _FRAC_AT_ZERO) < 1e-9, f"ev_lo 0% → {_FRAC_AT_ZERO} of cap")
    chk(_frac_for_ev(-5.0) == _FRAC_MIN, f"ev_lo −5% → floor {_FRAC_MIN} (degraded edge shrinks, never 0)")
    chk(_frac_for_ev(20.0) == 1.0, "ev_lo 20% → clamped to full cap")
    chk(_frac_for_ev(6.0) > _frac_for_ev(2.0) > _frac_for_ev(0.0), "monotonic in ev_lo")
    chk(ev_size_fraction_of_cap(None) is None, "None rule → None (caller keeps C² sizing)")
    chk(ev_size_fraction_of_cap("does_not_exist") is None, "unknown rule → None default")

    # Live read against the real brain (informational)
    cands = _load_candidates()
    print(f"  -- live brain candidates: {len(cands)} --")
    for nm in sorted(cands):
        c = cands[nm]
        _fr = ev_size_fraction_of_cap(nm)
        _frs = f"{_fr:.2f}cap" if _fr is not None else "  —  "
        print(f"     {nm:22} ev_lo={c.get('ev_lo')!s:>8}  → {ev_size_multiplier(nm)}×  | frac {_frs}")
    print(("SELFTEST PASS" if ok else "SELFTEST FAIL"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
