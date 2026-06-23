#!/usr/bin/env python3
"""
test_honest_objective_s121.py — Phase 3.4 (V2): the shared wallet-true objective.

Offline, deterministic: monkeypatches the data readers so no live DB / forward_obs is touched.
Run: python3 test_honest_objective_s121.py
"""
import honest_objective as HO

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


# ─────────────────────────────────────────────────────────────────────────────
print("realizable_return — fill-quality haircut on the GAIN side (Draft #1a)")
cost = lambda liq: 1.0           # flat 1% friction for deterministic math
obs_win = {"fwd": 20.0, "fwd_min": -2.0, "fwd_max": 30.0, "liq": 200_000}
# trail act10/ret0.75: gross = max(20, 30*0.75=22.5)=22.5; −1 friction = 21.5
full = HO.realizable_return(obs_win, cost_fn=cost, fill_factor=1.0)
half = HO.realizable_return(obs_win, cost_fn=cost, fill_factor=0.5)
check("fill=1.0 reads ~21.5 (no haircut)", abs(full - 21.5) < 1e-6)
check("fill=0.5 halves the gain (peak-fill optimism removed)", abs(half - 10.75) < 1e-6)
check("haircut never flatters (half < full on a winner)", half < full)

print("realizable_return — ghost obs → near-total loss")
obs_ghost = {"fwd": -95.0, "fwd_min": -97.0, "fwd_max": 3.0, "liq": 80_000}
g = HO.realizable_return(obs_ghost, cost_fn=cost, fill_factor=1.0)
check("ghost (fwd_min ≤ −90) reads ≤ −90", g <= -90.0)

print("realizable_return — deep drawdown the endpoint under-counts (Draft #1b)")
obs_dd = {"fwd": 15.0, "fwd_min": -60.0, "fwd_max": 25.0, "liq": 150_000}
# gross = max(15, 25*0.75=18.75)=18.75; −1 = 17.75; fmin −60 ≤ −50 & ret>0 → ×0.5 = 8.875
dd = HO.realizable_return(obs_dd, cost_fn=cost, fill_factor=1.0)
check("deep-drawdown winner is halved (8.875)", abs(dd - 8.875) < 1e-6)

print("realizable_return — losses are realized as-is (not haircut on the gain side)")
obs_loss = {"fwd": -4.0, "fwd_min": -6.0, "fwd_max": 1.0, "liq": 120_000}
# trail: fmax 1 < act 10 and fmin −6 > −sl(10) → gross = fwd = −4; −1 = −5
loss_full = HO.realizable_return(obs_loss, cost_fn=cost, fill_factor=1.0)
loss_half = HO.realizable_return(obs_loss, cost_fn=cost, fill_factor=0.5)
check("loss unaffected by fill_factor (gain-side only)", abs(loss_full - loss_half) < 1e-9 and loss_full < 0)

print("ev_lo — shared bound (mean − 1.64·SE)")
lo, mean, n = HO.ev_lo([10.0, 10.0, 10.0])
check("zero-variance ev_lo == mean", abs(lo - 10.0) < 1e-9 and n == 3)
lo2, m2, n2 = HO.ev_lo([])
check("empty ev_lo → (0,0,0)", lo2 == 0 and n2 == 0)

print("fit_fill_factor — calibration converges + clamps [FILL_MIN, 1] (Draft #1)")
_orig_live, _orig_rows, _orig_raw, _orig_fric = (
    HO._live_dp_returns, HO._dp_shadow_rows, HO._raw_realizable, HO.fit_friction)
try:
    HO.fit_friction = lambda force=False: (lambda liq: 1.0)
    HO._raw_realizable = lambda obs, cost_fn: obs["_v"]   # shadow value is whatever we inject

    # (1) thin wallet → conservative default, never crashes
    HO._live_dp_returns = lambda: [-5.0] * 3
    HO._dp_shadow_rows = lambda: [{"_v": 2.0}]
    check("thin wallet → _FILL_DEFAULT", abs(HO.fit_fill_factor(force=True) - HO._FILL_DEFAULT) < 1e-9)

    # (2) shadow over-credits wallet → haircut < 1, clamped at FILL_MIN floor (disproven-cohort case)
    HO._live_dp_returns = lambda: [-5.71] * 20
    HO._dp_shadow_rows = lambda: [{"_v": 10.0}] * 5 + [{"_v": -8.0}] * 5   # mean +1, big positive contrib
    f2 = HO.fit_fill_factor(force=True)
    check("over-crediting shadow → f in [FILL_MIN, 1)", HO._FILL_MIN <= f2 < 1.0)

    # (3) shadow already ≤ wallet → never flatter (f = 1.0)
    HO._live_dp_returns = lambda: [8.0] * 20
    HO._dp_shadow_rows = lambda: [{"_v": 1.0}] * 10
    check("shadow ≤ wallet → f = 1.0 (never flatter)", abs(HO.fit_fill_factor(force=True) - 1.0) < 1e-9)

    # (4) no data anywhere → never crashes, safe default
    HO._live_dp_returns = lambda: []
    HO._dp_shadow_rows = lambda: []
    check("no data → fit_fill_factor returns a finite default", 0.0 <= HO.fit_fill_factor(force=True) <= 1.0)

    # (5) calibration_gap with no data → no crash, coherent dict
    g = HO.calibration_gap()
    check("calibration_gap on empty data returns coherent dict", set(["gap_pp", "fill_factor", "calibratable"]).issubset(g))
finally:
    HO._live_dp_returns, HO._dp_shadow_rows, HO._raw_realizable, HO.fit_friction = (
        _orig_live, _orig_rows, _orig_raw, _orig_fric)

# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*60}")
raise SystemExit(1 if FAIL else 0)
