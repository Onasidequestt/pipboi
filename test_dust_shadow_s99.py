#!/usr/bin/env python3
"""Unit tests for the S99 dust shadow executor + route-depth feature + shadow gate.

READ-ONLY, no network, no fleet contact. Run: python3 test_dust_shadow_s99.py
Covers: (1) dust sizing override + cap arithmetic, (2) route-depth roundtrip math,
(3) the REAL prestige live-gate dust EXCLUSION (monkeypatched _closes — exercises the
shipped code), (4) the dust_gate shadow-gate math (return-%, return-% ghost, sellable filter).
"""
import sys

PASS = 0
FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  <<< FAIL")


# ── (1) dust sizing override + cap arithmetic (mirrors main.py constants/formula) ──
print("\n[1] dust sizing override + concurrent-cap exemption")
MIN_POSITION_SOL = 0.003
_DUST_SHADOW_SOL = 0.01
_DUST_MAX_CONCURRENT = 8

def dust_size():
    return round(max(MIN_POSITION_SOL, _DUST_SHADOW_SOL), 6)

check("dust size lands on ~◎0.01 (above the MIN_POSITION_SOL floor)", dust_size() == 0.01)
check("dust size never below the Jupiter MIN_POSITION_SOL floor", dust_size() >= MIN_POSITION_SOL)

# concurrent-cap exemption: real-slot count excludes dust positions
positions = {
    "A": {"dust_shadow": True},
    "B": {"dust_shadow": True},
    "C": {},                      # real
    "D": {"dust_shadow": False},  # real
}
_real_open = sum(1 for p in positions.values() if not p.get("dust_shadow"))
_dust_open = sum(1 for p in positions.values() if p.get("dust_shadow"))
check("real-slot count excludes dust positions (2 real of 4)", _real_open == 2)
check("dust positions counted separately for their own cap (2)", _dust_open == 2)
check("dust cap blocks a new dust entry only at the dust ceiling",
      (_dust_open >= _DUST_MAX_CONCURRENT) is False)
check("dust cap WOULD block when full", (8 >= _DUST_MAX_CONCURRENT) is True)


# ── (2) route-depth roundtrip math (mirrors execute_buy honeypot capture) ──
print("\n[2] route-depth roundtrip capture math")
def roundtrip_pct(sell_out_lamports, sol_lamports_in):
    _roundtrip = (sell_out_lamports / sol_lamports_in) if sol_lamports_in > 0 else 0.0
    return round((_roundtrip - 1.0) * 100, 3)

check("perfect round-trip → 0% friction", roundtrip_pct(1_000_000_000, 1_000_000_000) == 0.0)
check("3% exit loss → −3.0%", roundtrip_pct(970_000_000, 1_000_000_000) == -3.0)
check("honeypot (no sell value) → −100%", roundtrip_pct(0, 1_000_000_000) == -100.0)
check("zero-SOL-in guard → −100% (degenerate, can't happen: size ≥ MIN_POSITION_SOL)", roundtrip_pct(500, 0) == -100.0)


# ── (3) REAL live-gate dust EXCLUSION — monkeypatch _closes, run the shipped gate ──
print("\n[3] prestige live-gate EXCLUDES dust_shadow closes (real shipped code)")
import prestige_tracker as pt

# A dust close in a KEPT regime (sniper) with a positive pnl — if NOT excluded it would
# wrongly count toward the live arming gate. Plus a real deep_pool close that SHOULD count.
synthetic = [
    {"play": "deep_pool", "regime": "sniper", "pnl_sol": 0.0005, "pnl": 0.07, "dust_shadow": True},   # dust → must be dropped
    {"play": "deep_pool", "regime": "sniper", "pnl_sol": 0.0009, "pnl": 0.13},                          # real → counts
    {"play": "deep_pool", "regime": "normal", "pnl_sol": -0.02, "pnl": -3.0},                           # stale skip-regime → dropped
    {"play": "deep_pool", "regime": "normal", "pnl_sol": 0.001, "pnl": 0.15, "normal_slice": True},     # tight slice → counts
]
_orig_closes = pt._closes
pt._closes = lambda b: synthetic if b == 1 else []
try:
    n, net, ghosts, gr = pt._fleet_deep_pool_stats()
finally:
    pt._closes = _orig_closes

check("dust close EXCLUDED from live gate n (n=2: 1 real sniper + 1 normal-slice)", n == 2)
check("dust pnl_sol NOT folded into live-gate net", abs(net - (0.0009 + 0.001)) < 1e-9)
check("stale normal-bulk close still dropped (regime filter intact)", n == 2)
check("no ghosts counted (none qualify)", ghosts == 0)


# ── (4) dust_gate shadow-gate math (return-%, return-% ghost, sellable filter) ──
print("\n[4] dust_gate shadow-gate math (size-normalized)")
import dust_gate as dg

# _ret = pnl_sol / size_sol (size-independent)
check("_ret normalizes by size (dust +5% reads same as real +5%)",
      abs(dg._ret({"pnl_sol": 0.0005, "size_sol": 0.01}) - 0.05) < 1e-9
      and abs(dg._ret({"pnl_sol": 0.05, "size_sol": 1.0}) - 0.05) < 1e-9)
check("_ret None when size_sol missing/0", dg._ret({"pnl_sol": -0.01}) is None and dg._ret({"pnl_sol": -0.01, "size_sol": 0}) is None)

# return-% ghost catches a DUST ghost (−0.01 absolute, but −100% return) the live −0.02 floor misses
_dust_ghost = {"pnl_sol": -0.01, "size_sol": 0.01, "pnl": 0.0}
check("dust ghost caught by return-% floor (−100% < −50%)", dg._is_ghost_ret(_dust_ghost, dg._ret(_dust_ghost)) is True)
check("dust ghost would EVADE the live absolute −0.02 floor (proves why size-norm matters)",
      (abs(_dust_ghost["pnl"]) < 1e-9 and _dust_ghost["pnl_sol"] < -0.02) is False)
# a small real loss (−3%) is NOT a ghost
_small_loss = {"pnl_sol": -0.0003, "size_sol": 0.01, "pnl": -0.04}
check("small −3% loss is NOT a ghost", dg._is_ghost_ret(_small_loss, dg._ret(_small_loss)) is False)
# explicit ghost flag honored
check("explicit ghost:True honored", dg._is_ghost_ret({"ghost": True}, -0.1) is True)

# sellable filter: route_roundtrip_pct gate; None (pre-S99) kept
check("sellable: clean route (−2% ≥ −10%) kept", dg._sellable({"route_roundtrip_pct": -2.0}, -10.0) is True)
check("sellable: bad route (−40% < −10%) dropped", dg._sellable({"route_roundtrip_pct": -40.0}, -10.0) is False)
check("sellable: missing route (pre-S99) kept (can't prove trap)", dg._sellable({}, -10.0) is True)

# _stats: mean over CLEAN rows; ghost excluded from mean but counted in rate
_rows = [
    {"ret": 0.10, "ghost": False},
    {"ret": -0.04, "ghost": False},
    {"ret": -1.0, "ghost": True},   # ghost: excluded from mean, counted in rate
]
n, ghosts, gr, m, ev_lo = dg._stats(_rows)
check("_stats n counts all rows (3)", n == 3)
check("_stats ghost-rate = 1/3", abs(gr - 1/3) < 1e-9)
check("_stats mean over clean only ((0.10−0.04)/2 = 0.03)", abs(m - 0.03) < 1e-9)
check("_stats ev_lo ≤ mean (lower bound)", ev_lo <= m)


# ── (5) observer admission truth-table (replicates the exact inline predicate, S99) ──
print("\n[5] observer dust-lane admission truth table")
SKIP = {"dead", "normal"}

def admit(regime, dust_on, normaldp_on, filling=True, bs=2.0):
    """Mirror observer.score_tokens deep_pool admission for dust/real classification.
    Returns (admitted, is_dust). Assumes the token passes the quality gates (drain/m5/liq_mc/strong)."""
    _dp_normal_slice   = (regime == "normal" and normaldp_on)
    _runs              = (regime not in SKIP) or _dp_normal_slice or dust_on   # outer guard
    _tok_is_real_slice = (_dp_normal_slice and filling and bs >= 1.5)
    _dropped           = _dp_normal_slice and (not _tok_is_real_slice) and (not dust_on)  # line-1088
    _admitted          = _runs and not _dropped
    _is_dust           = dust_on and regime in SKIP and not _tok_is_real_slice
    return _admitted, _is_dust

# dust OFF → byte-for-byte current behaviour
check("OFF: kept regime (sniper) admits as REAL", admit("sniper", False, False) == (True, False))
check("OFF: dead regime NOT admitted (no dust)", admit("dead", False, False) == (False, False))
check("OFF: normal + no normal_dp NOT admitted", admit("normal", False, False) == (False, False))
check("OFF: normal + normal_dp tight-slice admits REAL", admit("normal", False, True, filling=True, bs=2.0) == (True, False))
check("OFF: normal + normal_dp NON-slice dropped", admit("normal", False, True, filling=False, bs=1.0) == (False, False))
# dust ON
check("ON: kept regime still REAL (never dusted)", admit("sniper", True, False) == (True, False))
check("ON: dead regime → DUST", admit("dead", True, False) == (True, True))
check("ON: normal (no normal_dp) → DUST (full skipped tape)", admit("normal", True, False, filling=False, bs=1.0) == (True, True))
check("ON: normal + normal_dp tight-slice stays REAL", admit("normal", True, True, filling=True, bs=2.0) == (True, False))
check("ON: normal + normal_dp NON-slice → DUST (co-enabled: covers normal, not just dead)",
      admit("normal", True, True, filling=False, bs=1.0) == (True, True))


print(f"\n{'='*50}\n  {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
