#!/usr/bin/env python3
"""
strategy_brain.py — the ever-evolving CORE STRATEGY engine (shadow → guarded live).

Evidence (backtest.py, 198 token-days real OHLCV): pure price-action — momentum OR
reversion, ANY parameters — is structurally -EV after trading cost. So the core
strategy is NOT a price-weight optimizer. It is an evidence loop that:

  1. holds a registry of candidate ENTRY RULES — price rules AND orthogonal rules
     (whale flow, pool quality, liquidity dynamics) that price candles can't see;
  2. scores every candidate on PAPER against signal_lab.py's live forward returns,
     by market regime;
  3. PROMOTES a candidate to "live" only when its paper-EV is positive AND beats the
     incumbent by a margin over a minimum sample — autonomy model (c)→(b):
     shadow-validate first, then auto-promote within guardrails;
  4. AUTO-ROLLS-BACK if a promoted rule's paper-EV later goes negative.

It deploys NOTHING until a rule earns it on paper. With today's data it will
(correctly) keep the conservative incumbent and promote nothing — its job right now
is to refuse losers and arm itself for the first real winner.

  python3 strategy_brain.py                 # status: current live rule + candidate ranking
  python3 strategy_brain.py --evaluate      # re-score all candidates on signal_lab data
  python3 strategy_brain.py --evolve        # evaluate + apply promotion/rollback if warranted
  python3 strategy_brain.py --horizon 60    # forward-return horizon (min)

State lives in shared_memory/strategy_brain.json. The "live" rule is published there
for main.py to read once the wire-to-live step is taken (gated on a +EV promotion).
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

import signal_lab as sl  # reuse the validated forward-return machinery
sys.path.insert(0, str(Path(__file__).parent / "paper_lab"))
import lab  # REALOBJ: realized_return — the realizable-exit model the brain now learns on
import honest_objective as _ho  # S121 V2: the ONE shared wallet-true objective (Draft #1 calibrated haircut)

BASE = Path(__file__).parent
STATE = BASE / "shared_memory" / "strategy_brain.json"

ROUND_TRIP_COST = 1.0   # % — kept ONLY as the legacy PRICE-objective diagnostic (see REALOBJ)
MIN_SAMPLES     = 30    # paired obs before a rule is trustworthy for SHADOW ranking
PROMOTE_MARGIN  = 0.5   # % EV the candidate must beat the incumbent by to shadow-promote
PROMOTE_MIN_EV  = 0.0   # candidate paper-EV LOWER-BOUND must exceed this to shadow-promote
MIN_WR_PROMOTE  = 40.0  # a primary rule must not be a fat-tail coin-flip (anti-whale_flow)

# ── LIVE-READINESS BAR (real money) — deliberately stricter than shadow ────────
# A shadow-promoted rule is the brain's current belief. Wiring it to LIVE capital
# requires clearing ALL of these. Set after seeing that a single quiet-afternoon
# n=33 / 82% WR sample can mislead — real money waits for breadth across time/regime.
LIVE_MIN_SAMPLES   = 100    # paired forward-obs (≈ a day-plus of data, multiple regimes)
LIVE_MIN_EV        = 2.0    # % paper-EV, clearly above the ~1% cost wall + noise
LIVE_MIN_WR        = 55.0   # % win rate floor (don't rely on one fat tail)
LIVE_MIN_AGE_HRS   = 24.0   # rule must have held the bar across ≥24h of logging (spans regimes)


def _live_ready(cand: dict, span_hrs: float) -> tuple[bool, str]:
    """Is this candidate proven enough to risk real SOL? Returns (ok, reason)."""
    if cand["n"] < LIVE_MIN_SAMPLES:
        return False, f"n={cand['n']} < {LIVE_MIN_SAMPLES}"
    if cand["ev"] < LIVE_MIN_EV:
        return False, f"EV {cand['ev']:+.2f}% < +{LIVE_MIN_EV}%"
    if cand["wr"] < LIVE_MIN_WR:
        return False, f"WR {cand['wr']:.0f}% < {LIVE_MIN_WR:.0f}%"
    if cand.get("ev_lo", -1.0) <= 0.0:
        return False, f"EV lower-bound {cand.get('ev_lo', 0):+.2f}% ≤ 0 (fragile/fat-tail)"
    # Temporal stability: the edge must clear the EV bar in EACH chronological half
    # independently — not merely pool to +EV on one lucky window. "Both halves > 0" is
    # too weak: momentum_breakout was H1 +0.9% / H2 +17% (both positive, but a recent-
    # window/hot-regime artifact). Requiring each half ≥ LIVE_MIN_EV rejects that, the
    # decaying incumbent (H1 +14.6 / H2 −8.3), and passes the deep_pool rules (worst
    # half +3.3). Absent half data (a summary-only dict) fails closed — unknown ≠ proven.
    eh1, eh2 = cand.get("ev_h1"), cand.get("ev_h2")
    if eh1 is None or eh2 is None or eh1 < LIVE_MIN_EV or eh2 < LIVE_MIN_EV:
        return False, (f"unstable across time — each half must be ≥{LIVE_MIN_EV}% "
                       f"(H1 {eh1 if eh1 is not None else '?'} / H2 {eh2 if eh2 is not None else '?'})")
    if span_hrs < LIVE_MIN_AGE_HRS:
        return False, f"only {span_hrs:.1f}h of data < {LIVE_MIN_AGE_HRS:.0f}h"
    return True, "clears all live-readiness gates (incl. temporal stability)"


def live_ready_rule(ev: dict):
    """EXPLOITATION selector: the best (highest EV-lo) candidate that clears the FULL
    live-readiness bar — or None. Deliberately DISTINCT from the EV-greedy shadow
    `live_rule` (exploration): the shadow leader is often a high-EV / low-n rule that
    will never reach n≥100 (e.g. deep_pool_strict n=57 +9.87% EV-lo), while a quieter
    high-n rule (deep_pool_momentum n=124) clears every gate but span. Scanning ALL
    candidates for the best PROVEN one is what makes wire-to-live actually fire when a
    rule is ready, instead of waiting on a label that keeps chasing the highest point EV.
    Returns (name, candidate_dict) or None."""
    span = ev.get("span_hrs", 0.0)
    ready = [(n, c) for n, c in ev.get("candidates", {}).items() if _live_ready(c, span)[0]]
    if not ready:
        return None
    ready.sort(key=lambda kv: -kv[1].get("ev_lo", 0.0))
    return ready[0]

# ── Candidate entry rules (over signal_lab feature rows) ──────────────────────
# Each rule maps a logged feature snapshot → enter? Add new hypotheses here; the
# engine validates them automatically. Orthogonal rules are where the edge may live.
def _num(r, k, d=0.0):
    v = r.get(k, d)
    return v if isinstance(v, (int, float)) else d

CANDIDATES = {
    # incumbent: the current bot's gate (composite score ≥ regime threshold ~80)
    "incumbent_score80":  lambda r: _num(r, "score") >= 80,
    # price theses — backtest says -EV; kept so the engine keeps proving they're not the answer
    "momentum_breakout":  lambda r: _num(r, "m5") >= 2.0 and _num(r, "vacc", 1) >= 1.5,
    "reversion_washout":  lambda r: -10 <= _num(r, "m5") <= -3 and _num(r, "vacc", 1) >= 1.5,
    # orthogonal theses — what price candles can't see (the real hope)
    "whale_flow":         lambda r: _num(r, "whale") == 1,
    "whale_plus_momentum":lambda r: _num(r, "whale") == 1 and _num(r, "m5") >= 1.0,
    "liq_filling":        lambda r: _num(r, "lqv") > 0 and _num(r, "m5") >= 0,
    "deep_pool_momentum": lambda r: _num(r, "m5") >= 2.0 and _num(r, "liq_mc") >= 0.05,
    # S61 leads — "deep, SELLABLE pool + mild momentum + balanced buy pressure" was the
    # one theme whose EV survived the realizability (liq≥$50k) filter in an in-sample grid
    # search (the n=99 / WR 60.6% / EV-lo +4.3% standout). Added so the brain validates them
    # FORWARD (out-of-sample) toward the live bar — grid-search winners are overfit until proven.
    "deep_pool_quality":  lambda r: _num(r, "m5") >= 1.0 and _num(r, "liq_mc") >= 0.10 and _num(r, "bs") >= 1.0,
    # S70 — deep_pool_quality + the pool NOT draining at entry (lqv≥0). Forward-validates
    # the live drain-at-entry exitability guard (observer._DEEP_POOL_MAX_DRAIN): on 149
    # in-sample deep_pool obs, dropping the lqv<0 cohort lifted EV +5.07→+5.83% and cut the
    # ≤−20% mid-hold deep-dip rate 4%→0% (the ghost predictor). If this beats deep_pool_quality
    # forward at acceptable n, the non-draining variant is the one to admit/wire.
    "deep_pool_calm":     lambda r: _num(r, "m5") >= 1.0 and _num(r, "liq_mc") >= 0.10 and _num(r, "bs") >= 1.0 and _num(r, "lqv") >= 0.0,
    # S70 — deep_pool_quality + the pool ACTIVELY FILLING at entry (lqv>0.01: LP being added
    # while the token moves with buy pressure = real accumulation/conviction). The strongest
    # sub-edge found: in-sample n=37 EV +14.4% WR 81%, BOTH halves +EV (H1+8.2/H2+20.3 = stable),
    # +12.1% even with the top winsor row dropped (not a fat-tail mirage — passes the whale_flow
    # test that killed phantom edges). ~3× the deep_pool_quality baseline (+4.4%). NOT used to
    # gate entry (filling is rare — n=37 — and the grind needs volume; flat pools are still +3%);
    # this candidate MEASURES it so it becomes the #1 EV-weighted-sizing lever once live proves +EV
    # (bet bigger on filling pools). The drain guard already rejects the lqv<0 tail.
    "deep_pool_filling":  lambda r: _num(r, "m5") >= 1.0 and _num(r, "liq_mc") >= 0.10 and _num(r, "bs") >= 1.0 and _num(r, "lqv") > 0.01,
    # S70 hunt — the ELITE cohort: deep_pool_strict (m5≥2) + actively filling pool (lqv>0.01).
    # Strongest edge in the system: n=36 EV +14.7% ev_lo +7.5% WR 81%, both halves +12/+17
    # (stable), +12.2% even dropping the top winsor row. Strong momentum + accumulating LP +
    # deep pool + buy pressure all at once. Highest ev_lo of any candidate → the #1 EV-sizing
    # target (sizes biggest under ev_size_fraction_of_cap). Fields all present in the live feats.
    "deep_pool_strict_filling": lambda r: _num(r, "m5") >= 2.0 and _num(r, "liq_mc") >= 0.10 and _num(r, "bs") >= 1.0 and _num(r, "lqv") > 0.01,
    # S70 hunt — orthogonal dimension: deep_pool + volume ACCELERATION (vacc≥2 = volume doubling
    # vs its rolling avg = fresh breakout). n=30 EV +8.9% ev_lo +2.2% WR 63%, both halves +11/+7.
    # Just clears the bar (borderline n/ev_lo) — kept as a forward-validated probe; vacc is a
    # different signal axis than lqv/momentum so it may catch movers the others miss.
    "deep_pool_accel":    lambda r: _num(r, "m5") >= 1.0 and _num(r, "liq_mc") >= 0.10 and _num(r, "bs") >= 1.0 and _num(r, "lqv") >= 0.0 and _num(r, "vacc", 1.0) >= 2.0,
    "deep_pool_strict":   lambda r: _num(r, "m5") >= 2.0 and _num(r, "liq_mc") >= 0.10 and _num(r, "bs") >= 1.0,
    "quality_only":       lambda r: _num(r, "liq_mc") >= 0.15,
    # S62 probes — isolate WHICH dimension drives the deep_pool_quality edge, each a
    # single-variable change off that proven base (m5≥1 & liq_mc≥0.10 & bs≥1). The brain
    # forward-validates them; the temporal-stability + n≥100 + EV-lo bar guard against the
    # multiple-comparisons trap (a lucky variant is caught the way momentum_breakout was).
    #   deep_pool_buy:     drop momentum — is "deep sellable pool + clear buy dominance"
    #                      enough on its own? RESULT (S62, n=236): −0.29% EV = −EV. MOMENTUM
    #                      IS NECESSARY (with quality_only ~0, this isolates m5 as load-bearing).
    "deep_pool_buy":          lambda r: _num(r, "liq_mc") >= 0.10 and _num(r, "bs") >= 1.3,
    #   deep_pool_fresh:   add volume ACCELERATION on top of deep_pool_quality. RESULT (n=42):
    #                      one-window artifact (H1+1/H2+18, WR 43%) — vol-accel adds no robust edge.
    "deep_pool_fresh":        lambda r: _num(r, "m5") >= 1.0 and _num(r, "liq_mc") >= 0.10 and _num(r, "vacc", 1) >= 1.5,
    #   deep_pool_quality_strong: firmer buy dominance (bs≥1.5 vs 1.0). RESULT (n=32): fragile
    #                      (EV-lo −1.4, WR 38%) — tightening bs shrinks n + hurts; bs≥1.0 is right.
    "deep_pool_quality_strong": lambda r: _num(r, "m5") >= 1.0 and _num(r, "liq_mc") >= 0.10 and _num(r, "bs") >= 1.5,
    # disciplined selectivity (the Bot-3 model that's the one live bright spot)
    "blue_chip_discipline": lambda r: _num(r, "score") >= 80 and _num(r, "bs") >= 1.3,
    # S62 — the score gate IS predictive: EV rises monotonically by score bucket on sellable
    # obs ([0,50)=−1.8% → [65,80)=+2.2% → [80,95)=+7.3%/WR68%), then [95,200) flips −17%
    # (n=5, likely too-perfect = wash-traded pumps that rug). incumbent_score80's "decay" was
    # 2 sellable-token rugs (−75%/−71%) + that ≥95 tail. These two forward-validate the fix:
    #   score_band: enter the [80,95) sweet spot, EXCLUDE the rug-prone ≥95 over-cooked band.
    "score_band":         lambda r: 80 <= _num(r, "score") < 95,
    #   score_deep: the bot's gate + realizability (deep sellable pool) — best of both leads.
    "score_deep":         lambda r: _num(r, "score") >= 80 and _num(r, "liq_mc") >= 0.10,
    # S68 probe — deep_pool_scored: layer the bot's composite score gate (≥65, the S66 sweet
    # spot that had the same EV as ≥80 with 4× the sample → tighter EV-lo) ON TOP of the
    # strongest structural edge (deep_pool_strict: m5≥2 & liq_mc≥0.10 & bs≥1). Tests whether
    # the score adds orthogonal signal beyond pool-depth+momentum+buy-pressure. If ev_lo rises
    # vs deep_pool_strict at acceptable n, the score is a real EV multiplier worth gating on;
    # if not, structure already captures it. Subset of deep_pool_strict → expect smaller n.
    "deep_pool_scored":   lambda r: _num(r, "m5") >= 2.0 and _num(r, "liq_mc") >= 0.10 and _num(r, "bs") >= 1.0 and _num(r, "score") >= 65,
    # S63 — measure the DEBATER (the live 3-persona veto on every INSANE signal, main.py:
    # debater.evaluate). It runs on faith — never forward-validated. signal_lab now records
    # the REAL live verdict per row as `dbt` (1=passed, -1=vetoed, absent=not evaluated).
    # These two split the gate so the brain answers: are debater-PASSED sigs actually +EV,
    # and would debater-VETOED sigs have won? If veto-EV ≈ pass-EV (or higher), the hand-
    # tuned gate is noise/harmful. Population is only standard-path INSANE sigs (small n).
    "debater_pass":       lambda r: _num(r, "dbt") >= 1,
    "debater_veto":       lambda r: _num(r, "dbt") <= -1,
}


# ── Evaluation ────────────────────────────────────────────────────────────────

def _ev_stats(pnls: list) -> tuple:
    """Mean EV, variance-aware one-sided 95% lower bound, and SE. Fat-tailed rules
    (a few huge winners, low WR) get a wide SE → ev_lo collapses below 0, so they
    can't promote on a lucky point estimate. This is the anti-whale_flow guard."""
    n = len(pnls)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean = sum(pnls) / n
    if n > 1:
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        se = (var ** 0.5) / (n ** 0.5)
    else:
        se = 0.0
    ev_lo = mean - 1.64 * se
    return round(mean, 3), round(ev_lo, 3), round(se, 3)


# ── REALOBJ — realizable-PROCEEDS objective (operator: "fix the measurement first") ──────────
# The brain used to score every rule on  fwd − ROUND_TRIP_COST  (flat 1%, hold-to-endpoint).
# That is a PRICE upper bound: depth-blind on friction (the REAL round-trip is ~2% in a $75k
# pool, ~0% in a >$1M pool — measured from the route ledger) and pessimistic on the exit (the
# fleet rides a trail, not a hold). Both errors mis-RANK the promotion queue. The brain now
# learns on the SAME realizable model the research tools use (lab.realized_return ride-exit)
# − a friction(depth) curve FIT FROM THE REAL route ledger − a ghost haircut. The old price
# objective is kept per-candidate as the `price_ev` diagnostic (the unsellability/exit gap).
# Proof: research/realizable_objective.py. Safe-direction: this only makes what the brain
# BELIEVES honester — it writes NO ev_sizing.json and the LIVE-readiness bar + the separate
# arming/deploy gates are unchanged and fail-closed.
_REAL_EXIT      = {"type": "trail", "act": 10.0, "retention": 0.75, "sl": 10.0}  # ≈ live deep_pool ride
_GHOST_FWDMIN   = -90.0    # fwd_min ≤ this ⇒ ghost/unsellable → conservative near-total loss
_FRICTION_FLOOR = 0.3      # min round-trip % even for the deepest pool (network+priority floor)


# S121 V2 (Phase 3.2): friction + the realizable objective now come from honest_objective — the ONE
# shared wallet-true ruler the brain, the GA, prove_edge, and lane_watch all consume (no per-tool
# drift). honest_objective.realizable_return adds the Draft #1 CALIBRATED gain-side fill-quality +
# deep-drawdown haircut on top of the friction-fit the brain used to do here, so the brain stops
# over-crediting the wallet ~3.2pp. Safe-direction: still only changes what the brain BELIEVES.
def _fit_friction():
    return _ho.fit_friction()


def _realizable_pnl(r, cost_fn, fill_factor=None):
    return _ho.realizable_return(r, cost_fn=cost_fn, fill_factor=fill_factor)


def evaluate(horizon_min: int) -> dict:
    """Score every candidate on paper against REALIZABLE-PROCEEDS returns (REALOBJ)."""
    sl.harvest_durable(horizon_min)          # persist matured rows → durable 24h span clock
    fwd_all = sl.load_matured(horizon_min)
    span_hrs = ((max(r["ts"] for r in fwd_all) - min(r["ts"] for r in fwd_all)) / 3600.0) if fwd_all else 0.0
    # Realizability: only score on tokens that were sellable at signal time. A +EV
    # that lives in $500-liquidity pools is the ghost-close bleed, not edge — this
    # single filter is what makes the brain measure CAPTURABLE PnL instead of phantom
    # price moves (it correctly zeroes out whale_flow's $549-median-liq "+20% EV").
    fwd = sorted((r for r in fwd_all if (r.get("liq") or 0) >= sl.SELL_FLOOR), key=lambda r: r["ts"])
    _cost_fn = _fit_friction()   # REALOBJ: depth-scaled friction from the live route ledger
    results = {}
    for name, rule in CANDIDATES.items():
        try:
            sub = [r for r in fwd if rule(r)]   # already in ts order (fwd is sorted)
        except Exception:
            sub = []
        pnls = [_realizable_pnl(r, _cost_fn) for r in sub]         # REALOBJ: the objective the brain LEARNS on
        price_pnls = [r["fwd"] - ROUND_TRIP_COST for r in sub]     # diagnostic: legacy PRICE objective (unsellability/exit gap)
        if pnls:
            n = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            ev, ev_lo, se = _ev_stats(pnls)
            # per-regime EV for context (S85: 5-band ladder — matches live; surfaces the sniper edge)
            # S86: ALSO emit by_regime_lo = variance-aware per-regime lower bound (ev_lo = mean−1.64·SE)
            # + n. This is what regime-aware EV-sizing reads: the BLENDED ev_lo/both-halves are
            # poisoned by normal/dead obs the fleet no longer trades (S85 regime-mix artifact —
            # strict_filling blended ev_lo 2.7 / h2 −1.85 FAILS the size-up bar even though sniper
            # +28 / euph +21 / aggr +11 in isolation), so without this the SIZE gene would never
            # express post-gate. n lets the sizer refuse a thin/noisy regime. Diagnostic-only:
            # by_regime_lo is NOT read by promotion (_live_ready) — zero change to what goes live.
            by_reg = {}
            by_reg_lo = {}
            for reg in ("euphoria", "aggressive", "normal", "sniper", "dead"):
                rp = [_realizable_pnl(r, _cost_fn) for r in sub if sl._regime_of(r) == reg]   # REALOBJ
                if rp:
                    by_reg[reg] = round(sum(rp) / len(rp), 2)
                    _rev, _rev_lo, _rse = _ev_stats(rp)
                    by_reg_lo[reg] = {"ev_lo": _rev_lo, "ev": _rev, "n": len(rp)}
            # chronological-half EVs — temporal-stability signal for the live bar. An
            # in-sample grid winner whose edge lives in one time-window (momentum_breakout:
            # H1 +0.9% / H2 +17%) is overfit; the pooled EV/EV-lo hides it. _live_ready()
            # requires BOTH halves +EV so a decaying/one-window rule can't reach real SOL.
            mid = n // 2
            ev_h1 = round(sum(pnls[:mid]) / mid, 2) if mid else 0.0
            ev_h2 = round(sum(pnls[mid:]) / (n - mid), 2) if (n - mid) else 0.0
            pev, pev_lo, _pse = _ev_stats(price_pnls)   # REALOBJ: legacy price objective, diagnostic only
            results[name] = {"n": n, "wr": round(100 * wins / n, 1),
                             "ev": ev, "ev_lo": ev_lo, "se": se,
                             "ev_h1": ev_h1, "ev_h2": ev_h2,
                             "by_regime": by_reg, "by_regime_lo": by_reg_lo,
                             # REALOBJ diagnostics: the OLD price objective + the gap it overstated by
                             "price_ev": pev, "price_ev_lo": pev_lo, "gap": round(pev - ev, 3)}
        else:
            results[name] = {"n": 0, "wr": 0.0, "ev": 0.0, "ev_lo": 0.0, "se": 0.0,
                             "ev_h1": 0.0, "ev_h2": 0.0, "by_regime": {}, "by_regime_lo": {},
                             "price_ev": 0.0, "price_ev_lo": 0.0, "gap": 0.0}
    return {
        "ts": time.time(),
        "horizon_min": horizon_min,
        "paired_obs": len(fwd),            # sellable (scored) observations
        "paired_obs_all": len(fwd_all),    # all matured observations (span basis)
        "span_hrs": round(span_hrs, 1),
        "candidates": results,
    }


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"live_rule": "incumbent_score80", "promoted_from": None,
            "promotion_log": [], "last_eval": {}}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(STATE)


def _trustworthy(c: dict) -> bool:
    return c["n"] >= MIN_SAMPLES


def evolve(horizon_min: int, apply: bool) -> dict:
    """Decide promotion / rollback. With apply=False, report only."""
    st = _load_state()
    ev = evaluate(horizon_min)
    st["last_eval"] = ev
    cands = ev["candidates"]
    live = st["live_rule"]
    live_ev = cands.get(live, {}).get("ev", 0.0)
    live_ev_lo = cands.get(live, {}).get("ev_lo", 0.0)
    live_n = cands.get(live, {}).get("n", 0)
    # The bar a challenger must clear: the incumbent's robust EV LOWER-BOUND, but
    # only when the incumbent is itself trustworthy. An unproven incumbent (n<MIN)
    # has a noisy estimate that must not gate out a genuinely-validated candidate —
    # fall back to "must be robustly +EV".
    live_trust = _trustworthy(cands.get(live, {}))
    bar = max(live_ev_lo, PROMOTE_MIN_EV) if live_trust else PROMOTE_MIN_EV

    decision = {"action": "hold", "reason": "", "live_rule": live}

    # Candidate ranking among trustworthy rules whose EV LOWER-BOUND is +EV and
    # whose WR clears the coin-flip floor (excludes fat-tail phantom rules). Ranking
    # by ev_lo, not point ev, so a wide-variance rule can't leapfrog on luck.
    ranked = sorted(
        ((n, c) for n, c in cands.items()
         if _trustworthy(c) and c["ev_lo"] > PROMOTE_MIN_EV and c["wr"] >= MIN_WR_PROMOTE),
        key=lambda kv: -kv[1]["ev_lo"],
    )

    # AUTO-ROLLBACK: a previously promoted live rule has gone -EV with enough data
    if st.get("promoted_from") and _trustworthy(cands.get(live, {})) and live_ev <= 0:
        prev = st["promoted_from"]
        decision = {"action": "rollback", "from": live, "to": prev,
                    "reason": f"live '{live}' paper-EV {live_ev:+.2f}% ≤ 0 over n={live_n} — reverting",
                    "live_rule": prev}
        if apply:
            st["promotion_log"].append({"ts": time.time(), **decision})
            st["live_rule"] = prev
            st["promoted_from"] = None
            _save_state(st)
        return decision

    # PROMOTE: best trustworthy candidate clears EV>0 AND beats incumbent by margin
    if ranked:
        best_name, best = ranked[0]
        if best_name != live and best["ev_lo"] > bar + PROMOTE_MARGIN:
            _bar_desc = (f"trustworthy live '{live}' EV-lo {live_ev_lo:+.2f}%"
                         if live_trust else f"+EV floor (live '{live}' unproven, n={live_n})")
            decision = {"action": "promote", "from": live, "to": best_name,
                        "reason": (f"'{best_name}' EV-lo {best['ev_lo']:+.2f}% (EV {best['ev']:+.2f}%, "
                                   f"n={best['n']}, WR {best['wr']}%) clears {_bar_desc} "
                                   f"by ≥{PROMOTE_MARGIN}% — promoting (shadow→live)"),
                        "live_rule": best_name}
            if apply:
                st["promotion_log"].append({"ts": time.time(), **decision})
                st["promoted_from"] = live
                st["live_rule"] = best_name
                _save_state(st)
            return decision

    decision["reason"] = (
        f"no candidate is both +EV and ≥{PROMOTE_MARGIN}% better than live "
        f"'{live}' ({live_ev:+.2f}%) with n≥{MIN_SAMPLES} — keeping incumbent."
    )
    # Live-readiness (EXPLOITATION, decoupled from the EV-greedy shadow label): is ANY
    # candidate proven enough to risk real SOL? Pick the best PROVEN one, not the best
    # point-EV one — see live_ready_rule(). Falls back to reporting the closest contender.
    lrr = live_ready_rule(ev)
    if lrr:
        decision["live_candidate"] = lrr[0]
        decision["live_ready"] = True
        decision["live_reason"] = (f"clears all live-readiness gates "
                                   f"(EV-lo {lrr[1].get('ev_lo', 0):+.2f}%, n={lrr[1]['n']}, WR {lrr[1]['wr']}%)")
    else:
        # Not ready — report the MOST INFORMATIVE contender: the best (EV-lo) rule that
        # clears every gate EXCEPT the data-span clock (the current universal blocker),
        # so the operator watches the right rule and sees the real remaining gap, not a
        # high-EV/low-n rule that's failing for an unrelated reason.
        span = ev["span_hrs"]
        near = sorted(
            ((n, c) for n, c in cands.items()
             if c["n"] >= LIVE_MIN_SAMPLES and c["ev"] >= LIVE_MIN_EV
             and c["wr"] >= LIVE_MIN_WR and c.get("ev_lo", -1.0) > 0.0
             and c.get("ev_h1", -99.0) >= LIVE_MIN_EV and c.get("ev_h2", -99.0) >= LIVE_MIN_EV),
            key=lambda kv: -kv[1]["ev_lo"])
        if near:
            nm, c = near[0]
            decision["live_candidate"] = nm
            decision["live_ready"] = False
            decision["live_reason"] = (f"clears n/EV/WR/EV-lo/stability (n={c['n']}, EV-lo {c['ev_lo']:+.2f}%, "
                                       f"WR {c['wr']}%, H1 {c.get('ev_h1',0):+.1f}%/H2 {c.get('ev_h2',0):+.1f}%); "
                                       f"needs span {span:.1f}h → {LIVE_MIN_AGE_HRS:.0f}h")
        else:
            best_for_live = ranked[0] if ranked else (live, cands.get(live, {}))
            lr_ok, lr_why = _live_ready(best_for_live[1], span) if best_for_live[1] else (False, "no data")
            decision["live_candidate"] = best_for_live[0]
            decision["live_ready"] = lr_ok
            decision["live_reason"] = lr_why
    if apply:
        _save_state(st)
    return decision


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_status(ev: dict, st: dict) -> None:
    print("=" * 66)
    print("  STRATEGY BRAIN — shadow evaluation (autonomy: c→b)")
    print(f"  live rule: {st['live_rule']}   |   paired forward-obs: {ev['paired_obs']}"
          f"   |   horizon {ev['horizon_min']}m")
    print("=" * 66)
    if ev["paired_obs"] == 0:
        print("\n  No forward-return data yet. signal_lab.py must run long enough to")
        print("  pair signals with prices ~2× the horizon ahead. Keep it logging.")
        return
    print(f"  realizable (sellable-only) obs: {ev['paired_obs']}  ·  span {ev['span_hrs']}h"
          f"  ·  all matured: {ev.get('paired_obs_all', ev['paired_obs'])}")
    print("  EV = REALIZABLE (ride-exit − friction(depth) − ghost) · priceEV = old fwd−1% (diagnostic)")
    print(f"\n  {'candidate':<20}{'n':>5}{'WR':>6}{'realEV':>8}{'EV-lo':>8}{'priceEV':>9}  {'verdict'}")
    print("  " + "-" * 70)
    rank = sorted(ev["candidates"].items(),
                  key=lambda kv: (-kv[1].get("ev_lo", -9e9) if kv[1]["n"] >= MIN_SAMPLES else 1e9))
    for name, c in rank:
        elo = c.get("ev_lo", 0.0)
        if c["n"] == 0:
            v = "no matches (unsellable/none)"
        elif c["n"] < MIN_SAMPLES:
            v = f"need {MIN_SAMPLES - c['n']} more"
        elif elo > 0 and c["wr"] >= MIN_WR_PROMOTE:
            _stable = c.get("ev_h1", -99.0) >= LIVE_MIN_EV and c.get("ev_h2", -99.0) >= LIVE_MIN_EV
            v = ("+EV robust ← candidate" if _stable
                 else f"+EV but ⚠ one-window (H1{c.get('ev_h1',0):+.0f}/H2{c.get('ev_h2',0):+.0f})")
        elif c["ev"] > 0:
            v = "+EV but fragile (EV-lo≤0 / low WR)"
        else:
            v = "-EV"
        star = "▶ " if name == st["live_rule"] else "  "
        print(f"  {star}{name:<18}{c['n']:>5}{c['wr']:>5.1f}%{c['ev']:>+7.2f}%{elo:>+7.2f}%"
              f"{c.get('price_ev', 0.0):>+8.2f}%  {v}")
    print("\n  Promotion gate: paper-EV > 0  AND  ≥%.1f%% better than live  AND  n ≥ %d."
          % (PROMOTE_MARGIN, MIN_SAMPLES))
    if st.get("promotion_log"):
        last = st["promotion_log"][-1]
        print(f"  Last action: {last.get('action')} → {last.get('live_rule')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ever-evolving core-strategy engine")
    ap.add_argument("--evaluate", action="store_true", help="re-score candidates (no state change)")
    ap.add_argument("--evolve", action="store_true", help="evaluate + apply promotion/rollback")
    ap.add_argument("--horizon", type=int, default=30)
    a = ap.parse_args()

    if a.evolve:
        d = evolve(a.horizon, apply=True)
        print(f"[brain] {d['action'].upper()}: {d['reason']}")
        print(f"[brain] shadow live rule: {d['live_rule']}  (exploration / EV-greedy)")
        # ALWAYS report the wire-to-live verdict, on every action (promote/rollback/hold)
        # — this is what the operator reads to know whether real SOL is at stake. It uses
        # the EXPLOITATION selector (best PROVEN candidate), NOT the shadow label, so it
        # fires the moment any rule clears the bar. Nothing auto-wires unless this says READY.
        flag = "✅ READY FOR REAL SOL" if d.get("live_ready") else "⛔ NOT live-ready"
        print(f"[brain] wire-to-live check on '{d.get('live_candidate', d['live_rule'])}': "
              f"{flag} — {d.get('live_reason', 'no data')}")
        return
    # status / evaluate both just show the table (evaluate is the same minus apply)
    st = _load_state()
    ev = evaluate(a.horizon)
    st["last_eval"] = ev
    _save_state(st)
    _print_status(ev, st)


if __name__ == "__main__":
    main()
