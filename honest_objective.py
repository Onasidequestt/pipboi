#!/usr/bin/env python3
"""
honest_objective.py — S121 V2: the ONE wallet-true objective (shared ruler).

WHY (S115 BRAIN-6H, run-long calibration): the brain/GA learn on a realizable shadow that
OVER-CREDITS the wallet by a stable ~3.2pp (shadow −0.46% vs live wallet −3.70% on the deep_pool
cohort). Root: the trail model (`lab.realized_return`, `_REAL_EXIT`) assumes ~retention·peak fills
with no whipsaw (EVOLVE-12's documented optimism), and `fit_friction()` fits the INSTANT round-trip
(a floor). Anything ranked on that metric is ranked on fiction.

THIS MODULE is the single source of truth so the brain, the GA (`gene_evolve_*`), `prove_edge`, and
`lane_watch` all rank on the SAME honest number — no per-tool drift (Phase 3.2).

`realizable_return(obs, exit_policy=None)`  → wallet-honest realized %: ride-exit over the recorded
    fwd path − depth-scaled friction (fit from the live route ledger) − a ghost/deep-drawdown haircut
    − a CALIBRATED gain-side fill factor (Draft #1) fit so the deep_pool paper shadow mean ≈ the live
    proceeds-pnl mean. The haircut is FLOORED at "no boost" — it may never FLATTER the wallet.
`calibration_gap()`  → {shadow_mean, wallet_mean, gap_pp, ...}: the live shadow↔wallet gap, the
    tripwire `reconcile_harness` watches (FAIL when |gap| > 1pp) so the ruler stays honest as regimes
    shift.

SAFE-DIRECTION: changes only what the brain/GA BELIEVE — it writes NO ev_sizing.json; arming
(arm_genes/prestige/lane_watch) reads LIVE realized closes and is fail-closed.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "paper_lab"))
import lab  # paper_lab/lab.py — realized_return (read-only, stdlib)

import prestige_tracker as _pt   # _is_ghost (pure dict fn; single ghost def)

# ── shared constants (kept identical to strategy_brain's REALOBJ so behaviour is unchanged ex-haircut)
ROUND_TRIP_COST   = 1.0                                              # legacy flat friction fallback %
SELL_FLOOR        = 50_000.0                                          # realizability floor (signal_lab.SELL_FLOOR)
_REAL_EXIT        = {"type": "trail", "act": 10.0, "retention": 0.75, "sl": 10.0}  # ≈ live deep_pool ride
_GHOST_FWDMIN     = -90.0                                             # fwd_min ≤ this ⇒ ghost → near-total loss
_DEEP_DD_FWDMIN   = -50.0                                             # Draft #1(b): deep drawdown the endpoint under-counts
_DEEP_DD_FACTOR   = 0.5                                               # extra gain-side penalty on those rows
_FRICTION_FLOOR   = 0.3                                               # min round-trip % (network+priority floor)
_DP_PLAYS         = {"deep_pool", "brain_rule"}
_FORWARD_OBS      = BASE / "shared_memory" / "forward_obs.jsonl"
_MIN_WALLET_N     = 12                                                # below this the wallet cohort is too thin to calibrate
_FILL_MIN         = 0.40                                              # floor: a fill factor is fill OPTIMISM, never
                                                                      # "edge is dead" (that's lane_watch/kill_criterion's
                                                                      # job). A disproven cohort drives the raw fit→0;
                                                                      # flooring keeps the global haircut from nuking
                                                                      # legitimate +EV lanes' paper estimates.
_FILL_DEFAULT     = 0.55                                              # conservative UNCALIBRATED value (Draft #1 illustrative)
_CACHE_TTL        = 600.0                                             # re-fit friction + fill at most every 10 min

_FRIC_CACHE = {"ts": 0.0, "fn": None}
_FILL_CACHE = {"ts": 0.0, "f": None}


# ── friction(depth) — fit from the live route ledger (identical to strategy_brain._fit_friction) ────
def fit_friction(force: bool = False):
    """Round-trip friction% as a fn of pool depth, OLS of route cost on log10(liq) from the live route
    ledger, pooled across bots. Falls back to flat ROUND_TRIP_COST when route data is thin. Cached."""
    now = time.monotonic()
    if not force and _FRIC_CACHE["fn"] is not None and (now - _FRIC_CACHE["ts"]) < _CACHE_TTL:
        return _FRIC_CACHE["fn"]
    xs, ys = [], []
    for b in (1, 2, 3):
        dbp = BASE / "bots" / f"bot{b}" / "trades.db"
        if not dbp.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
            for (d,) in con.execute("SELECT data FROM trades WHERE event='close'"):
                r = json.loads(d)
                rr, liq = r.get("route_roundtrip_pct"), r.get("entry_liq")
                if rr is None or not liq or liq <= 0:
                    continue
                xs.append(math.log10(liq)); ys.append(max(0.0, -float(rr)))
            con.close()
        except Exception:
            continue
    n = len(xs)
    if n < 10:
        fn = (lambda liq: ROUND_TRIP_COST)
    else:
        mx = sum(xs) / n; my = sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx) if sxx else 0.0
        intercept = my - slope * mx
        def fn(liq, _i=intercept, _s=slope):
            if not liq or liq <= 0:
                return ROUND_TRIP_COST
            return max(0.0, min(8.0, _i + _s * math.log10(liq)))
    _FRIC_CACHE.update(ts=now, fn=fn)
    return fn


def _raw_realizable(obs, cost_fn) -> float:
    """The realizable model WITHOUT the fill factor (friction + ghost + deep-drawdown haircut only).
    This is what the fill factor is calibrated against."""
    fmin, fmax = obs.get("fwd_min"), obs.get("fwd_max")
    fric = max(_FRICTION_FLOOR, cost_fn(obs.get("liq") or 0.0))
    ret = lab.realized_return({"fwd": obs["fwd"], "fwd_min": fmin, "fwd_max": fmax},
                              _REAL_EXIT, friction_pct=fric)
    if fmin is not None:
        if fmin <= _GHOST_FWDMIN:
            ret = min(ret, fmin - fric)                       # ghost → near-total loss
        elif fmin <= _DEEP_DD_FWDMIN and ret > 0:
            ret = min(ret, ret * _DEEP_DD_FACTOR)             # Draft#1(b): deep drawdown the endpoint under-counts
    return ret


# ── live wallet cohort + deep_pool shadow cohort (for the fill-factor calibration) ──────────────────
def _live_dp_returns():
    """Live deep_pool/brain_rule proceeds-pnl return-% (pnl_sol/size_sol·100), ghost-excluded, sized only."""
    out = []
    for b in (1, 2, 3):
        dbp = BASE / "bots" / f"bot{b}" / "trades.db"
        if not dbp.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
            for (d,) in con.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid"):
                r = json.loads(d)
                if (r.get("play") or r.get("tier")) not in _DP_PLAYS:
                    continue
                if _pt._is_ghost(r):
                    continue
                sz = r.get("size_sol") or 0.0
                if sz <= 0:
                    continue
                out.append((r.get("pnl_sol", 0.0) or 0.0) / sz * 100.0)
            con.close()
        except Exception:
            continue
    return out


def _dp_shadow_rows(limit: int = 60_000):
    """forward_obs rows admitted by the deep_pool predicate (sellable + complete path)."""
    if not _FORWARD_OBS.exists():
        return []
    try:
        import prove_edge as _pe           # lazy — avoids any import cycle at module load
    except Exception:
        return []
    rows = []
    try:
        with open(_FORWARD_OBS) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("fwd") is None or (r.get("liq") or 0) < SELL_FLOOR:
                    continue
                ok, _ = _pe.deep_pool_admit(r)
                if ok:
                    rows.append(r)
    except Exception:
        return []
    return rows[-limit:]


def fit_fill_factor(force: bool = False) -> float:
    """Calibrate a multiplicative GAIN-side fill factor (Draft #1) so the deep_pool paper shadow mean
    ≈ the live wallet mean. Clamped to [0, 1] — it may never FLATTER the wallet (f=1 = no haircut).
    Cached; recomputed every _CACHE_TTL. Thin/insufficient data → 1.0 (safe: never over-penalise)."""
    now = time.monotonic()
    if not force and _FILL_CACHE["f"] is not None and (now - _FILL_CACHE["ts"]) < _CACHE_TTL:
        return _FILL_CACHE["f"]
    f = _FILL_DEFAULT
    try:
        wallet = _live_dp_returns()
        if len(wallet) >= _MIN_WALLET_N:
            cost_fn = fit_friction()
            shadow = [_raw_realizable(r, cost_fn) for r in _dp_shadow_rows()]
            if shadow:
                wallet_mean = sum(wallet) / len(wallet)
                shadow_mean = sum(shadow) / len(shadow)
                pos_contrib = sum(s for s in shadow if s > 0) / len(shadow)  # avg positive contribution to the mean
                if pos_contrib > 1e-9 and shadow_mean > wallet_mean:
                    f = 1.0 - (shadow_mean - wallet_mean) / pos_contrib
                # else shadow already ≤ wallet → no haircut (never flatter) → 1.0
                else:
                    f = 1.0
                f = max(_FILL_MIN, min(1.0, f))    # floor: optimism haircut, never an edge-kill
    except Exception:
        f = _FILL_DEFAULT
    _FILL_CACHE.update(ts=now, f=f)
    return f


# ── the shared objective ────────────────────────────────────────────────────────────────────────
def realizable_return(obs, exit_policy=None, cost_fn=None, fill_factor=None) -> float:
    """Wallet-honest realized % for one forward_obs row. Pass cost_fn/fill_factor to fit ONCE for a
    batch (the per-obs path then does no I/O). exit_policy overrides the default ride for lane exits."""
    if cost_fn is None:
        cost_fn = fit_friction()
    if fill_factor is None:
        fill_factor = fit_fill_factor()
    ex = exit_policy or _REAL_EXIT
    fmin, fmax = obs.get("fwd_min"), obs.get("fwd_max")
    fric = max(_FRICTION_FLOOR, cost_fn(obs.get("liq") or 0.0))
    ret = lab.realized_return({"fwd": obs["fwd"], "fwd_min": fmin, "fwd_max": fmax},
                              ex, friction_pct=fric)
    if ret > 0:
        ret *= fill_factor                                   # Draft#1(a): gain-side fill quality
    if fmin is not None:
        if fmin <= _GHOST_FWDMIN:
            ret = min(ret, fmin - fric)
        elif fmin <= _DEEP_DD_FWDMIN and ret > 0:
            ret = min(ret, ret * _DEEP_DD_FACTOR)
    return ret


def ev_lo(rets):
    """ev_lo = mean − 1.64·SE (population stdev / sqrt(n)) — the shared bound (kill_criterion/runner_watch/lab)."""
    n = len(rets)
    if n == 0:
        return 0.0, 0.0, 0
    mean = sum(rets) / n
    if n >= 2:
        var = sum((x - mean) ** 2 for x in rets) / n
        se = math.sqrt(var) / math.sqrt(n)
    else:
        se = float("inf")
    return mean - 1.64 * se, mean, n


def calibration_gap() -> dict:
    """Live shadow↔wallet gap on the deep_pool cohort (the reconcile_harness tripwire). gap_pp =
    shadow_mean − wallet_mean (positive = the model still over-credits the wallet)."""
    cost_fn = fit_friction(force=True)
    f = fit_fill_factor(force=True)
    wallet = _live_dp_returns()
    rows = _dp_shadow_rows()
    shadow = [realizable_return(r, cost_fn=cost_fn, fill_factor=f) for r in rows]
    wm = (sum(wallet) / len(wallet)) if wallet else 0.0
    sm = (sum(shadow) / len(shadow)) if shadow else 0.0
    return {
        "shadow_mean": round(sm, 3), "wallet_mean": round(wm, 3),
        "gap_pp": round(sm - wm, 3), "abs_gap_pp": round(abs(sm - wm), 3),
        "n_wallet": len(wallet), "n_shadow": len(shadow),
        "fill_factor": round(f, 3), "calibratable": len(wallet) >= _MIN_WALLET_N,
    }


if __name__ == "__main__":
    g = calibration_gap()
    print("=" * 64)
    print("  HONEST OBJECTIVE — calibration")
    print("=" * 64)
    print(f"  fill_factor (Draft#1):  {g['fill_factor']}   (1.0 = no haircut / thin data)")
    print(f"  shadow_mean (paper):    {g['shadow_mean']:+.3f}%   n={g['n_shadow']}")
    print(f"  wallet_mean (live dp):  {g['wallet_mean']:+.3f}%   n={g['n_wallet']}")
    print(f"  gap_pp (shadow−wallet): {g['gap_pp']:+.3f}pp   |gap|={g['abs_gap_pp']:.3f}pp")
    print(f"  calibratable:           {g['calibratable']}  (need n_wallet ≥ {_MIN_WALLET_N})")
    print(f"  → target |gap| < 1.0pp (reconcile_harness FAILs above that)")
    print("=" * 64)
