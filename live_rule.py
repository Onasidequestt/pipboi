#!/usr/bin/env python3
"""
live_rule.py — the wire-to-live bridge between strategy_brain and the live entry path.

WHAT IT IS
----------
The brain (strategy_brain.py) forward-validates candidate entry RULES on paper and,
via `live_ready_rule()`, names the single best candidate that has cleared the FULL
live-readiness bar (n≥100, EV≥+2%, WR≥55%, EV-lo>0, ≥24h span). This module reads that
verdict and exposes it to main.py's `execute_buy()` as an extra entry filter.

WHY A SEPARATE MODULE
---------------------
Keeps the risky on-the-hot-path logic (predicate evaluation on a live signal) OUT of
main.py — it's a tiny, independently TESTED unit here. main.py's change is then a
3-line guarded import + one dormant `if` block. This module also owns all the failure
handling so a brain-state hiccup can NEVER break the running fleet.

SAFETY MODEL
------------
- `active()` returns a rule ONLY when the brain says a candidate is truly live-ready;
  otherwise None (→ main.py applies no filter → current behavior).
- The filter can ONLY make entries MORE selective (skip), never add or upsize a trade,
  so it cannot increase risk.
- Every path fails toward CURRENT BEHAVIOR: a missing/corrupt brain file, an unknown
  rule, or a predicate error all resolve to "don't filter" rather than crash or block
  the fleet. (The deliberate exception: a `liq_mc`-dependent rule with liq_mc absent
  from the sig evaluates the predicate honestly → that term is 0 → rule won't pass →
  skip; that's the safe/selective direction, and the wiring checklist below removes it.)
- 30s in-process cache so per-buy calls don't hit disk in the hot path.

TO ACTUALLY GO LIVE (operator, once `python3 strategy_brain.py --evolve` prints
✅ READY FOR REAL SOL):
  1. Enrich the live signal with liq/mcap at the three sig-build sites in main.py
     (deep_pool_* rules need it):  sig["liq_mc"] = liq/mc  (0.0 when mc unknown).
  2. Set  _LIVE_RULE_ENABLED = True  in main.py.
  3. ./run.sh  (the flag + this module load on restart).
  4. Watch for `[LIVE-RULE] … skip` lines — confirms the filter is selecting.
Reverting is the one constant back to False + restart.

USAGE / TEST
------------
    python3 live_rule.py             # report the current live-ready verdict
    python3 live_rule.py --selftest  # unit tests (no fleet/brain dependency on state)
"""
from __future__ import annotations

import json
import time
from typing import Optional

import strategy_brain as sb   # single source of truth for CANDIDATES + readiness bar

_CACHE_TTL_S = 30.0
_cache: tuple[float, Optional["_LiveRule"]] = (0.0, None)


# ── Map a LIVE signal dict (main.py) → the feature row CANDIDATES predicates expect ──
# The predicate keys (score/m5/bs/liq_mc/vacc/whale/lqv) come from signal_lab feature
# rows; the live `signal` dict uses different names. Defaults are chosen so a MISSING
# field pushes a rule toward NOT passing (more selective = safe), except neutral signals
# (vacc=1.0, whale=0, lqv=0.0) which are genuine "no info" values.
def _features_from_sig(sig: dict) -> dict:
    def num(*keys, default=0.0):
        for k in keys:
            v = sig.get(k)
            if isinstance(v, (int, float)):
                return v
        return default
    return {
        "score":  num("validation_score", "score"),
        "m5":     num("momentum_5m", "price_change_5m"),
        "bs":     num("buy_sell_ratio"),
        "liq_mc": num("liq_mc", default=0.0),          # needs enrichment (see header step 1)
        "vacc":   num("vol_accel", "vacc", default=1.0),
        "whale":  num("whale", default=0),
        "lqv":    num("liq_velocity", "lqv", default=0.0),
    }


class _LiveRule:
    def __init__(self, name: str, predicate, cand: dict):
        self.name = name
        self.cand = cand            # {n, wr, ev, ev_lo, ...} for logging/telemetry
        self._pred = predicate

    def passes(self, sig: dict) -> bool:
        """True = allow the entry, False = skip it. On any predicate error, allow
        (fail toward current behavior — never block the fleet on a code fault)."""
        try:
            return bool(self._pred(_features_from_sig(sig)))
        except Exception:
            return True


def _brain_eval() -> Optional[dict]:
    """Last evaluation the brain persisted (candidates + span_hrs), or None."""
    try:
        st = json.loads(sb.STATE.read_text())
        ev = st.get("last_eval") or {}
        return ev if ev.get("candidates") else None
    except Exception:
        return None


def ready_verdict() -> tuple[Optional[str], dict]:
    """(rule_name, candidate_dict) of the live-ready rule, or (None, {})."""
    ev = _brain_eval()
    if not ev:
        return None, {}
    rr = sb.live_ready_rule(ev)
    return (rr[0], rr[1]) if rr else (None, {})


def active(use_cache: bool = True) -> Optional[_LiveRule]:
    """The live-ready rule as an applyable filter, or None (→ no filtering). Never raises."""
    global _cache
    if use_cache and (time.time() - _cache[0]) < _CACHE_TTL_S:
        return _cache[1]
    rule = None
    try:
        name, cand = ready_verdict()
        if name:
            pred = sb.CANDIDATES.get(name)
            if pred is not None:
                rule = _LiveRule(name, pred, cand)
    except Exception:
        rule = None
    _cache = (time.time(), rule)
    return rule


# ── S67: brain-driven entry registry (admission, not filtering) ──────────────────
# `active()` above is the strict 24h-span FILTER (narrows entries once a rule is fully
# live-ready). The registry below is the complementary ADMISSION layer: it lets the
# observer enter LIQUID movers matching ANY brain candidate that clears a robustness
# bar — so the fleet trades every proven rule in any market, and new rules the brain
# validates auto-become tradeable with zero code change. The bar is deliberately the
# robustness standard (the same evidence that justified the deep_pool path), NOT the
# 24h-span live-ready gate — admission widens the net; the filter (active()) tightens it.
ADMIT_MIN_EV_LO   = 2.0  # % — EV lower-bound clearly above the ~1% cost wall + noise
ADMIT_MIN_N       = 30   # paired forward-obs sample floor
ADMIT_HALF_MIN_EV = 1.5  # % — EACH chronological half must clear this (temporal stability).
                         # Mirrors the brain's robustness intent (live-ready uses 2.0): rejects
                         # the one-window lottery pattern (e.g. H1 +1% / H2 +15%, WR 41%) where a
                         # single window carries the whole edge — exactly what one-window means.
# Rules whose predicate needs a feature the observer can't supply reliably pre-trade
# (whale/lqv flow, negative-momentum washout, post-trade debater verdict). They have
# their own dedicated paths or are n=0; excluding them keeps admission honest.
_ADMIT_EXCLUDE = {"whale_flow", "whale_plus_momentum", "liq_filling",
                  "reversion_washout", "debater_pass", "debater_veto"}


def admit_rules(use_cache: bool = True) -> list:
    """Brain candidates currently clearing the ADMISSION bar (robust +EV, temporally
    stable, enough sample), sorted best-EV-lo first. Each item: {name, predicate,
    ev_lo, wr, n}. Never raises → returns [] on any error (fail toward no admission)."""
    try:
        ev = _brain_eval()
        if not ev:
            return []
        out = []
        for name, cand in (ev.get("candidates") or {}).items():
            if name in _ADMIT_EXCLUDE:
                continue
            pred = sb.CANDIDATES.get(name)
            if pred is None:
                continue
            n   = cand.get("n", 0)
            elo = cand.get("ev_lo")
            h1  = cand.get("ev_h1", 0.0)
            h2  = cand.get("ev_h2", 0.0)
            if elo is None or n < ADMIT_MIN_N or elo < ADMIT_MIN_EV_LO:
                continue
            if h1 < ADMIT_HALF_MIN_EV or h2 < ADMIT_HALF_MIN_EV:   # temporal stability gate
                continue
            out.append({"name": name, "predicate": pred,
                        "ev_lo": elo, "wr": cand.get("wr", 0), "n": n})
        out.sort(key=lambda d: -d["ev_lo"])
        return out
    except Exception:
        return []


def match_rule(features: dict, rules: Optional[list] = None) -> Optional[dict]:
    """Best (highest ev_lo) admitted rule whose predicate matches `features`, or None.
    Never raises (a predicate fault skips that rule, fails toward no match)."""
    rules = admit_rules() if rules is None else rules
    for r in rules:               # already sorted ev_lo-desc → first match is best
        try:
            if r["predicate"](features):
                return r
        except Exception:
            continue
    return None


# ── CLI / self-test ─────────────────────────────────────────────────────────────
def _report() -> None:
    name, cand = ready_verdict()
    if name:
        print(f"✅ LIVE-READY rule: {name}  "
              f"(n={cand.get('n')}, EV {cand.get('ev'):+.2f}%, WR {cand.get('wr')}%, "
              f"EV-lo {cand.get('ev_lo'):+.2f}%)")
        print(f"   main.py would apply this as an entry filter if _LIVE_RULE_ENABLED=True.")
    else:
        print("⛔ No rule is live-ready — active() returns None → main.py applies NO filter "
              "(current behavior). Run `python3 strategy_brain.py --evolve` for the gap.")


def _selftest() -> int:
    fails = []

    # 1. feature mapping
    f = _features_from_sig({"validation_score": 84, "momentum_5m": 3.2,
                            "buy_sell_ratio": 1.4, "liq_mc": 0.12})
    if not (f["score"] == 84 and f["m5"] == 3.2 and f["bs"] == 1.4 and f["liq_mc"] == 0.12):
        fails.append(f"feature map wrong: {f}")
    if _features_from_sig({})["vacc"] != 1.0:
        fails.append("missing vacc should default neutral 1.0")
    if _features_from_sig({})["liq_mc"] != 0.0:
        fails.append("missing liq_mc should default 0.0 (selective)")

    # 2. live_ready_rule selects the best PROVEN rule from a synthetic eval, ignoring a
    #    higher-EV but low-n rule (the deep_pool_strict trap) and a span-blocked one.
    synth = {"span_hrs": 30.0, "candidates": {
        "deep_pool_strict":   {"n": 57,  "wr": 80.0, "ev": 15.0, "ev_lo": 9.0, "ev_h1": 12.0, "ev_h2": 18.0},  # n<100
        "deep_pool_quality":  {"n": 120, "wr": 60.0, "ev": 8.0,  "ev_lo": 4.3, "ev_h1": 7.0,  "ev_h2": 9.0},   # READY (stable)
        "deep_pool_momentum": {"n": 130, "wr": 58.0, "ev": 5.5,  "ev_lo": 1.7, "ev_h1": 8.0,  "ev_h2": 3.0},   # READY (lower ev_lo)
        "momentum_breakout":  {"n": 130, "wr": 55.0, "ev": 9.0,  "ev_lo": 5.0, "ev_h1": 0.9,  "ev_h2": 17.0},  # high ev_lo but UNSTABLE
        "quality_only":       {"n": 900, "wr": 20.0, "ev": 0.3,  "ev_lo": -0.3, "ev_h1": 1.0, "ev_h2": -0.7},  # fragile
    }}
    rr = sb.live_ready_rule(synth)
    # deep_pool_quality must win: momentum_breakout has higher ev_lo (5.0) but is UNSTABLE
    # (H1 +0.9 / H2 +17) → the stability gate must exclude it. This is the whole point.
    if not rr or rr[0] != "deep_pool_quality":
        fails.append(f"live_ready_rule should pick deep_pool_quality (stable), got {rr[0] if rr else None}")

    # 3. span gate blocks everything when span < 24h (the current real-world state)
    synth_lowspan = dict(synth, span_hrs=10.0)
    if sb.live_ready_rule(synth_lowspan) is not None:
        fails.append("live_ready_rule must return None when span < 24h")

    # 4. predicate eval on a live-style sig: deep_pool_momentum needs m5≥2 & liq_mc≥0.05
    rule = _LiveRule("deep_pool_momentum", sb.CANDIDATES["deep_pool_momentum"], {})
    if not rule.passes({"momentum_5m": 3.0, "liq_mc": 0.08}):
        fails.append("deep_pool_momentum should PASS m5=3 liq_mc=0.08")
    if rule.passes({"momentum_5m": 3.0, "liq_mc": 0.0}):
        fails.append("deep_pool_momentum should SKIP when liq_mc=0 (not enriched)")
    if rule.passes({"momentum_5m": 1.0, "liq_mc": 0.08}):
        fails.append("deep_pool_momentum should SKIP m5=1 (<2)")

    # 5. active() against the REAL current brain state must be None (nothing live-ready)
    if active(use_cache=False) is not None:
        fails.append("active() should be None now (no rule live-ready) — SAFETY")

    # 6. admission registry: match_rule picks the highest-ev_lo matching rule, and the
    #    predicate gate is honest (m5=1.5 fails the strict rule, falls to the quality rule).
    synth_rules = [
        {"name": "deep_pool_strict",  "predicate": sb.CANDIDATES["deep_pool_strict"],  "ev_lo": 9.0},
        {"name": "deep_pool_quality", "predicate": sb.CANDIDATES["deep_pool_quality"], "ev_lo": 3.8},
    ]
    m = match_rule({"m5": 2.5, "liq_mc": 0.12, "bs": 1.4}, synth_rules)
    if not m or m["name"] != "deep_pool_strict":
        fails.append(f"match_rule should pick deep_pool_strict (best ev_lo, m5=2.5 matches), got {m}")
    m2 = match_rule({"m5": 1.5, "liq_mc": 0.12, "bs": 1.4}, synth_rules)
    if not m2 or m2["name"] != "deep_pool_quality":
        fails.append(f"match_rule should fall to deep_pool_quality (m5=1.5<2 fails strict), got {m2}")
    if match_rule({"m5": 0.5, "liq_mc": 0.02, "bs": 0.5}, synth_rules) is not None:
        fails.append("match_rule should return None when no rule's predicate matches")

    if fails:
        print("SELFTEST FAILED:")
        for f_ in fails:
            print("  ✗", f_)
        return 1
    print("SELFTEST OK — 6/6 groups passed (mapping, selector, span gate, predicate, real-state safety, admission registry)")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    _report()
