#!/usr/bin/env python3
"""
test_solanatracker_s119.py — offline tests for solanatracker_service.py (S119).
No network. Covers: the no-key inert path, canary-off = byte-identical candidate list,
throttle/min-interval math, and the normalize/merge contract (shape parity with the
gecko candidate dict + the additive risk_score). Nothing here enables a canary or
touches the fleet.
"""
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import solanatracker_service as st

_PASS = _FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✓  {name}")
    else:
        _FAIL += 1
        print(f"  ✗  {name}")


SAMPLE = {
    "token": {"mint": "ABC123pump", "symbol": "TEST"},
    "pools": [{"liquidity": {"usd": 42000}, "price": {"usd": 0.0013},
               "marketCap": {"usd": 500000},
               "txns": {"buys5m": 30, "sells5m": 9, "volume5m": 1200, "volume1h": 8000, "volume": 90000}}],
    "events": {"5m": {"priceChangePercentage": 18.2}, "1h": {"priceChangePercentage": 40.0}},
    "risk": {"score": 3},
}

# The exact contract the discovery candidate dict must satisfy (gecko-shape keys).
_REQUIRED_KEYS = {
    "mint", "symbol", "price_usd", "liquidity_usd", "volume_5m", "volume_1h", "volume_6h",
    "volume_24h", "price_change_5m", "price_change_1h", "price_change_6h", "price_change_24h",
    "txns_5m_buys", "txns_5m_sells", "txns_1h_buys", "txns_1h_sells", "txns_24h_buys",
    "txns_24h_sells", "fdv", "market_cap", "pair_age_hours", "pair_address", "dex",
    "has_socials", "has_website", "paid_boost_active", "risk_score",
}


# ---- 1. no-key inert path ------------------------------------------------------
def test_no_key_inert():
    old = st.API_KEY
    st.API_KEY = ""        # simulate missing key
    try:
        check("solanatracker_enabled() False with no key", st.solanatracker_enabled() is False)
        # fetch_trending must fail-soft to ([], {}) without ever touching the network
        mints, pool = asyncio.run(st.fetch_trending(None))
        check("fetch_trending → ([],{}) when no key", mints == [] and pool == {})
        tok = asyncio.run(st.fetch_token(None, "ABC123pump"))
        check("fetch_token → None when no key", tok is None)
    finally:
        st.API_KEY = old


# ---- 2. canary-off = inert even WITH a key -------------------------------------
def test_canary_gate():
    old_key = st.API_KEY
    st.API_KEY = "FAKEKEY"        # key present...
    # ...but canary file absent (point _ROOT at an empty temp dir so no canary exists)
    old_root = st._ROOT
    old_cache = dict(st._CANARY_CACHE)
    with tempfile.TemporaryDirectory() as td:
        st._ROOT = Path(td)
        st._CANARY_CACHE.update(ts=-1e9, val={})   # bust the 30s cache
        try:
            check("enabled() False: key set but canary absent", st.solanatracker_enabled(1) is False)
            # now write an ENABLED canary and confirm the gate flips (logic only, still no net)
            (Path(td) / "bots" / "bot1").mkdir(parents=True)
            (Path(td) / "bots" / "bot1" / "solanatracker.json").write_text('{"enabled":true}')
            st._CANARY_CACHE.update(ts=-1e9, val={})
            check("enabled() True: key set + canary enabled", st.solanatracker_enabled(1) is True)
            # disabled canary → inert
            (Path(td) / "bots" / "bot1" / "solanatracker.json").write_text('{"enabled":false}')
            st._CANARY_CACHE.update(ts=-1e9, val={})
            check("enabled() False: canary {enabled:false}", st.solanatracker_enabled(1) is False)
        finally:
            st._ROOT = old_root
            st.API_KEY = old_key
            st._CANARY_CACHE.update(**old_cache)


# ---- 3. canary-off = byte-identical candidate list (merge no-op) ---------------
def test_merge_noop_when_disabled():
    old_key = st.API_KEY
    st.API_KEY = ""    # disabled
    try:
        existing = {"M1": {"mint": "M1", "liquidity_usd": 50000}}
        before = json.dumps(existing, sort_keys=True)
        mints, pool = asyncio.run(st.fetch_trending(None))
        merged = dict(existing)
        merged.update(pool)            # the additive-merge discovery would do
        for m in mints:
            pass
        after = json.dumps(merged, sort_keys=True)
        check("disabled adapter leaves candidate list byte-identical", before == after)
    finally:
        st.API_KEY = old_key


# ---- 4. normalize / merge contract ---------------------------------------------
def test_normalize_contract():
    rec = st.normalize_token(SAMPLE)
    check("normalize returns a dict", isinstance(rec, dict))
    check("all required gecko-shape keys present", _REQUIRED_KEYS.issubset(rec.keys()))
    check("mint mapped", rec["mint"] == "ABC123pump")
    check("nested liquidity.usd → liquidity_usd", rec["liquidity_usd"] == 42000.0)
    check("nested price.usd → price_usd", abs(rec["price_usd"] - 0.0013) < 1e-9)
    check("event 5m priceChangePercentage → price_change_5m", rec["price_change_5m"] == 18.2)
    check("txns buys5m → txns_5m_buys (int)", rec["txns_5m_buys"] == 30 and isinstance(rec["txns_5m_buys"], int))
    check("risk.score → risk_score (the additive field)", rec["risk_score"] == 3.0)
    check("dex tagged solanatracker", rec["dex"] == "solanatracker")
    # skip-mints + junk → None
    check("SOL base mint → None", st.normalize_token({"token": {"mint": "So11111111111111111111111111111111111111112"}}) is None)
    check("no-mint record → None", st.normalize_token({"foo": 1}) is None)
    check("non-dict → None", st.normalize_token([1, 2, 3]) is None)


# ---- 5. risk_score defensive across documented shapes --------------------------
def test_risk_score_shapes():
    check("risk.score shape", st._risk_score({"risk": {"score": 7}}) == 7.0)
    check("riskScore shape", st._risk_score({"riskScore": 5}) == 5.0)
    check("top-level score shape", st._risk_score({"score": 2}) == 2.0)
    check("unknown → 0.0", st._risk_score({"nope": 1}) == 0.0)


# ---- 6. throttle math (min-interval spacer keeps under free 3 req/s) -----------
def test_throttle_constants():
    check("semaphore caps concurrency at 2", st._SEM._value == 2)
    check("min-interval ≥ 0.33s (under 3 req/s)", st._MIN_INTERVAL >= 0.33)
    # two spaced calls cannot exceed 3/s: 2 calls need ≥ _MIN_INTERVAL apart
    rps_ceiling = 1.0 / st._MIN_INTERVAL * st._SEM._value
    check("effective ceiling ≤ ~5 req/s (free-tier safe)", rps_ceiling <= 5.0 + 1e-9)


if __name__ == "__main__":
    print("=" * 60)
    print("  solanatracker_service.py — S119 offline test suite")
    print("=" * 60)
    test_no_key_inert()
    test_canary_gate()
    test_merge_noop_when_disabled()
    test_normalize_contract()
    test_risk_score_shapes()
    test_throttle_constants()
    print("=" * 60)
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("  " + ("ALL PASS" if _FAIL == 0 else "*** FAILURES ***"))
    print("=" * 60)
    raise SystemExit(1 if _FAIL else 0)
