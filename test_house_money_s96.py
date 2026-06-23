#!/usr/bin/env python3
"""S96 — house-money de-risk ("paid off" exit) test suite.

Covers the goal: works across all 5 regimes, banks the cost basis to GUARANTEE green,
and sizes how much RIDES vs BANKS by confidence × regime-EV (the +EV conviction).

READ-ONLY on the live fleet: monkeypatches the canary paths to temp files and stubs
_save_positions so no real positions.json / canary is touched. Run: python3 test_house_money_s96.py
"""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import stoic_strategy as ss

REGIMES = ["euphoria", "aggressive", "sniper", "normal", "dead"]
_fails = []


def _check(name, cond, detail=""):
    tag = "✅" if cond else "❌"
    print(f"  {tag} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _fails.append(name)


# ─────────────────────────────────────────────────────────────────────────────
# 1. _house_money_plan — the pure conviction→sizing map
# ─────────────────────────────────────────────────────────────────────────────
def test_plan_all_regimes():
    print("\n[1] _house_money_plan across all 5 regimes @ +100% (conf 0.9 = high):")
    rows = {}
    for r in REGIMES:
        f, conv, rmult = ss._house_money_plan(100.0, 0.9, r)
        rows[r] = (f, conv, rmult)
        banked_basis = f * 2.0  # current value = 2× basis at +100%; banked = f × value
        print(f"      {r:11s}: sell {f*100:5.1f}%  conviction {conv:.2f}  recover {rmult:.2f}×  "
              f"→ banks {banked_basis:.2f}× cost basis, rides {(1-f)*100:.0f}% free")
        # every regime must produce a valid, in-bounds fraction
        _check(f"{r}: fraction within [{ss._HM_MIN_SELL},{ss._HM_MAX_SELL}]",
               ss._HM_MIN_SELL <= f <= ss._HM_MAX_SELL, f"f={f}")
        # GUARANTEED GREEN: banked SOL >= the initial stake (recover_mult >= 1.0)
        _check(f"{r}: guaranteed green (banks >= 1.0× basis)", banked_basis >= 1.0 - 1e-9,
               f"banked {banked_basis:.3f}× basis")

    # +EV ordering: as regime EV falls (euph→aggr→sniper→normal→dead), we BANK MORE (ride less)
    fracs = [rows[r][0] for r in REGIMES]
    mono = all(fracs[i] <= fracs[i + 1] + 1e-9 for i in range(len(fracs) - 1))
    _check("monotonic: lower-EV regime banks MORE / rides LESS", mono,
           " ".join(f"{r}={f:.2f}" for r, f in zip(REGIMES, fracs)))
    # high-conviction +EV regime rides the MAX tail (euphoria banks ~exactly cost basis = 50%)
    _check("euphoria high-conf banks ~cost basis only (≈50%, rides max)",
           abs(rows["euphoria"][0] - 0.5) < 0.02, f"euph f={rows['euphoria'][0]}")
    _check("dead regime de-risks HARDER than euphoria",
           rows["dead"][0] > rows["euphoria"][0] + 0.1,
           f"dead {rows['dead'][0]:.2f} vs euph {rows['euphoria'][0]:.2f}")


def test_plan_confidence_gradient():
    print("\n[2] confidence gradient @ +100%, fixed regime=aggressive (higher conf → ride more):")
    prev = None
    for c in [0.40, 0.55, 0.70, 0.85, 1.00]:
        f, conv, rmult = ss._house_money_plan(100.0, c, "aggressive")
        print(f"      conf {c:.2f}: sell {f*100:5.1f}%  conviction {conv:.2f}  recover {rmult:.2f}×")
        if prev is not None:
            _check(f"conf {c:.2f} banks ≤ lower conf (higher conf rides more)", f <= prev + 1e-9,
                   f"f={f} prev={prev}")
        prev = f


def test_plan_edges():
    print("\n[3] edge cases — clamps, missing fields, the Jotchua trade:")
    # never sell >max even at a low trigger + lowest conviction (no over-100% sell)
    f, _, _ = ss._house_money_plan(40.0, 0.40, "dead")
    _check("low-trigger × low-conviction clamped to _HM_MAX_SELL", f <= ss._HM_MAX_SELL + 1e-9, f"f={f}")
    # huge winner: bank floor honoured, most of it rides
    f, _, rmult = ss._house_money_plan(1000.0, 0.9, "euphoria")
    _check("+1000% banks ≥ floor and rides the rest", f >= ss._HM_MIN_SELL - 1e-9 and f < 0.5,
           f"f={f}")
    # missing confidence → neutral default (no crash)
    f, conv, _ = ss._house_money_plan(100.0, None, "normal")
    _check("missing confidence handled (neutral)", 0.0 < f <= ss._HM_MAX_SELL, f"f={f} conv={conv}")
    # the actual Jotchua-shape: +140%, decent conf, euphoria → keep a big free tail
    f, conv, rmult = ss._house_money_plan(140.0, 0.8, "euphoria")
    print(f"      Jotchua-shape (+140%, conf .8, euphoria): sell {f*100:.0f}% "
          f"(recover {rmult:.2f}× basis), ride {(1-f)*100:.0f}% free")
    _check("Jotchua: banks >= cost basis AND keeps a large tail riding",
           f * 2.4 >= 1.0 - 1e-9 and (1 - f) >= 0.45, f"f={f}, banked {f*2.4:.2f}× basis")


# ─────────────────────────────────────────────────────────────────────────────
# Integration: drive check_exits with the canary on
# ─────────────────────────────────────────────────────────────────────────────
def _mk_pos(entry=1.0, regime="euphoria", confidence=0.9, tier=None,
            momentum=0.0, entry_liq=0.0, ride=False):
    return {
        "entry_price": entry, "size_sol": 0.1, "size_usd": 15.0,
        "momentum_at_entry": momentum, "volume_5m_at_entry": 0.0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "peak_price": entry, "trail_active": False, "price_history": [entry],
        "stack_count": 0, "tp1_taken": False, "remaining_fraction": 1.0,
        "regime": regime, "confidence": confidence, "ride": ride,
        "insane_tier": tier, "mode": "insane", "entry_liq": entry_liq,
    }


def _fresh_strategy(tmp):
    """A StoicStrategy with disk writes stubbed and the house-money canary enabled."""
    (tmp / "house_money.json").write_text(json.dumps({"enabled": True}))
    ss._HOUSE_MONEY_PATH = tmp / "house_money.json"
    ss._house_money_cache = (0.0, None)
    s = ss.StoicStrategy()
    s._save_positions = lambda: None  # never touch disk
    return s


def test_integration_fires_and_is_one_shot():
    print("\n[4] integration: check_exits banks once at +100%, then never again:")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s = _fresh_strategy(tmp)
        mint = "So1111111111111111111111111111111111111111"
        s.positions = {mint: _mk_pos(regime="euphoria", confidence=0.9)}
        prices = {mint: 2.0}  # +100%

        ex1 = s.check_exits(prices, cycle=1)
        hm = [e for e in ex1 if e.get("house_money")]
        _check("house-money partial emitted at +100%", len(hm) == 1, f"got {len(hm)}")
        if hm:
            f_expected, _, _ = ss._house_money_plan(100.0, 0.9, "euphoria")
            _check("fraction matches the plan", abs(hm[0]["fraction"] - f_expected) < 1e-9,
                   f"{hm[0]['fraction']} vs {f_expected}")
            _check("exit_type=partial (routes through execute_partial_sell)",
                   hm[0]["exit_type"] == "partial")
        p = s.positions[mint]
        _check("house_money_taken latched", p.get("house_money_taken") is True)
        _check("remaining_fraction multiplied down",
               abs(p["remaining_fraction"] - round(1 - f_expected, 4)) < 1e-3,
               f"rem={p['remaining_fraction']}")

        # second cycle, still +100% → must NOT bank again (one-shot)
        ex2 = s.check_exits(prices, cycle=2)
        _check("no second house-money bank (one-shot guard)",
               not any(e.get("house_money") for e in ex2))


def test_integration_all_regimes():
    print("\n[5] integration: fires across ALL 5 regimes, fraction sized by +EV:")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s = _fresh_strategy(tmp)
        last = None
        for r in REGIMES:
            mint = f"Mint{r}1111111111111111111111111111111111"[:44]
            s.positions = {mint: _mk_pos(regime=r, confidence=0.9)}
            ex = s.check_exits({mint: 2.0}, cycle=1)
            hm = [e for e in ex if e.get("house_money")]
            ok = len(hm) == 1
            frac = hm[0]["fraction"] if hm else None
            _check(f"{r}: banks at +100% (sized {frac})", ok, f"exits={len(ex)}")
            if frac is not None and last is not None:
                _check(f"{r}: banks ≥ higher-EV regime (rides less in lower EV)",
                       frac >= last - 1e-9, f"{frac} vs {last}")
            if frac is not None:
                last = frac


def test_integration_composes_with_ride():
    print("\n[6] integration: composes with the momentum-gem RIDE (banks even while riding):")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s = _fresh_strategy(tmp)
        # turn the momentum-gem ride canary ON too
        (tmp / "momentum_gem_ride.json").write_text(json.dumps({"enabled": True}))
        ss._MOMENTUM_GEM_RIDE_PATH = tmp / "momentum_gem_ride.json"
        ss._momentum_gem_ride_cache = (0.0, False)
        mint = "Ride111111111111111111111111111111111111111"[:44]
        # a Jotchua-class gem entry: tier=gem, entered on momentum, $100k pool (in the _MG band)
        s.positions = {mint: _mk_pos(regime="euphoria", confidence=0.85, tier="gem",
                                     momentum=6.6, entry_liq=100_000.0, ride=True)}
        ex = s.check_exits({mint: 2.0}, cycle=1)
        hm = [e for e in ex if e.get("house_money")]
        _check("ride is active AND house-money still banks the cost basis", len(hm) == 1,
               f"got {len(hm)} house-money exits")
        # confirm the position was actually flagged as a ride this cycle (remap ran)
        _check("position remapped to ride (take_profit=trail-activate)",
               s.positions[mint].get("ride") is True)


if __name__ == "__main__":
    print("=" * 74)
    print("S96 HOUSE-MONEY DE-RISK — TEST SUITE")
    print("=" * 74)
    test_plan_all_regimes()
    test_plan_confidence_gradient()
    test_plan_edges()
    test_integration_fires_and_is_one_shot()
    test_integration_all_regimes()
    test_integration_composes_with_ride()
    print("\n" + "=" * 74)
    if _fails:
        print(f"❌ {len(_fails)} FAILURE(S): " + "; ".join(_fails))
        raise SystemExit(1)
    print("✅ ALL TESTS PASSED")
