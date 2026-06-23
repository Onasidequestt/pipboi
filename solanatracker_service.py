#!/usr/bin/env python3
"""SolanaTracker discovery/risk adapter (PIPE12 R-A1) — DEFAULT-OFF, fleet-safe.

WHY (the binding constraint, every recent session): free GeckoTerminal self-429s and slices
the deep SELLABLE pools off the capped candidate list (S91), so even a validated edge can't
fire enough to prove (RUNNEREDGE fired only 32× the last time it was live). And the pipeline
learns a token is unsellable only at the SELL — the ghost class. SolanaTracker Advanced
(€50/mo, 200k req/mo, NO rate limit) ends the rate-limit starvation outright AND ships a
pre-entry risk score (1-10 rug/sniper) + graduation events — exitability data the count-based
sidecar is structurally blind to.

DISCIPLINE (mirrors execwire/jupiter_ultra + the per-bot canary pattern):
  - INERT unless BOTH (a) SOLANATRACKER_API_KEY is set AND (b) the per-bot canary
    bots/botN/solanatracker.json {"enabled":true} exists → solanatracker_enabled() is the
    SINGLE gate. No key OR no canary ⇒ every public function no-ops / returns empty.
  - FAIL-SOFT: any HTTP/parse error returns ([], {}) / None — it can NEVER raise into the
    discovery loop or block a cycle (same contract as discovery._gecko_page).
  - Emits the EXACT candidate dict shape discovery._normalize_gecko_pool produces, PLUS a new
    `risk_score` field + dex="solanatracker", so it's a DROP-IN additive discovery source.
  - Rate-limited (free tier = 3 req/s): a semaphore + min-interval spacer keeps it under the
    free limit so the validate-first run can't burn the 2,500-req/mo budget in a burst.

VALIDATE-FIRST — nothing here is wired into the live fleet yet. Run
    python3 research/solanatracker/validate.py
on a FREE key (2,500 req) to confirm reachability + the field mapping + risk-score coverage
BEFORE the €50/mo and BEFORE wiring fetch_trending() into discovery_service._poll_discovery.
The field mapping below follows SolanaTracker's documented data API shape but is marked where
a live response should confirm it — the validator dumps the raw JSON so the map is verified,
not assumed.

ENABLE (after validation):
    # 1. free key from solanatracker.io → .env:  SOLANATRACKER_API_KEY=...
    # 2. echo '{"enabled":true}' > bots/bot1/solanatracker.json   (Bot1 A/B; bot2/3 = control)
    # 3. wire `fetch_trending` into discovery_service._poll_discovery (one additive merge,
    #    reserve-slotted like the S91 BC fix) + restart.  See research/solanatracker/README.md.
REVERT: rm bots/bot*/solanatracker.json  (hot — adapter goes inert; or unset the key).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

try:
    import httpx  # the fleet already depends on httpx (discovery.py)
except Exception:  # pragma: no cover - httpx always present in the fleet venv
    httpx = None

_ROOT = Path(__file__).resolve().parent
API_KEY = os.getenv("SOLANATRACKER_API_KEY", "")

# Free tier = data.solanatracker.io; Advanced/paid may issue a dedicated host — keep it
# overridable via env so upgrading is a one-line .env change, no code edit.
BASE = os.getenv("SOLANATRACKER_BASE", "https://data.solanatracker.io").rstrip("/")

# Free tier is 3 req/s. Stay well under it: 2 concurrent + a 0.4s min spacing between calls.
_SEM = asyncio.Semaphore(2)
_MIN_INTERVAL = 0.40
_last_call = 0.0

# Same skip-set as discovery._SKIP_MINTS (never trade the base/quote tokens).
_SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",   # ETH (Wormhole)
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",    # mSOL
}

_CANARY_CACHE: dict = {"ts": 0.0, "val": {}}
_CANARY_TTL = 30.0  # 30s hot-reload, same as every other canary


def _bot_id() -> int:
    try:
        return int(os.getenv("BOT_ID", "1"))
    except Exception:
        return 1


def _canary(bot_id: int | None = None) -> dict:
    """Read bots/botN/solanatracker.json (30s cache). Missing/invalid ⇒ {} (disabled)."""
    now = time.monotonic()
    if now - _CANARY_CACHE["ts"] < _CANARY_TTL:
        return _CANARY_CACHE["val"]
    bid = bot_id if bot_id is not None else _bot_id()
    val: dict = {}
    try:
        fp = _ROOT / f"bots/bot{bid}/solanatracker.json"
        if fp.exists():
            val = json.loads(fp.read_text()) or {}
    except Exception:
        val = {}
    _CANARY_CACHE.update(ts=now, val=val)
    return val


def solanatracker_enabled(bot_id: int | None = None) -> bool:
    """THE single gate: a key is set AND this bot's canary says enabled. Else fully inert."""
    if not API_KEY or httpx is None:
        return False
    return bool(_canary(bot_id).get("enabled"))


async def _get(client, path: str, params: dict | None = None, timeout: float = 12.0):
    """Rate-limited, fail-soft GET. Returns parsed JSON or None (never raises)."""
    global _last_call
    if not API_KEY or httpx is None:
        return None
    url = f"{BASE}/{path.lstrip('/')}"
    headers = {"x-api-key": API_KEY}
    try:
        async with _SEM:
            # min-interval spacer so a fan-out stays under the free 3 req/s ceiling
            gap = _MIN_INTERVAL - (time.monotonic() - _last_call)
            if gap > 0:
                await asyncio.sleep(gap)
            _last_call = time.monotonic()
            r = await client.get(url, params=params or {}, headers=headers, timeout=timeout)
        if r.status_code == 429:
            # honour Retry-After once, then give up softly (no cycle-killing loop)
            ra = r.headers.get("Retry-After")
            try:
                await asyncio.sleep(min(float(ra), 5.0) if ra else 1.0)
            except Exception:
                await asyncio.sleep(1.0)
            r = await client.get(url, params=params or {}, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _risk_score(raw: dict) -> float:
    """Pull the 1-10 rug/sniper risk score. Defensive across documented shapes
    ({"risk": {"score": N}} | {"riskScore": N} | top-level "score"). 0.0 = unknown."""
    for path in (("risk", "score"), ("riskScore",), ("score",)):
        cur = raw
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok:
            try:
                return float(cur)
            except Exception:
                pass
    return 0.0


def _f(d: dict, *keys, default=0.0) -> float:
    """First present nested/flat key → float; tolerant of SolanaTracker's nested {usd:N}."""
    for k in keys:
        if isinstance(k, (list, tuple)):
            cur = d
            ok = True
            for kk in k:
                if isinstance(cur, dict) and kk in cur:
                    cur = cur[kk]
                else:
                    ok = False
                    break
            if ok:
                try:
                    return float(cur)
                except Exception:
                    pass
        elif isinstance(d, dict) and k in d:
            try:
                return float(d[k])
            except Exception:
                pass
    return default


def normalize_token(raw: dict) -> dict | None:
    """Map a SolanaTracker token record → our candidate dict (discovery._normalize_gecko_pool
    shape) + `risk_score`. Returns None if no usable mint. DEFENSIVE — confirm field names
    against research/solanatracker/validate.py's raw dump before trusting in production.

    Expected documented shape (token-summary): {
        "token": {"mint"/"address", "symbol", "name"},
        "pools": [{"liquidity":{"usd"}, "price":{"usd"}, "marketCap":{"usd"},
                   "txns":{...}, "poolId"/"address", ...}],
        "events": {"5m":{"priceChangePercentage"}, "1h":{...}, ...},
        "risk": {"score": 1-10, "rugged": bool},
    }
    """
    if not isinstance(raw, dict):
        return None
    tok = raw.get("token") if isinstance(raw.get("token"), dict) else raw
    mint = (tok.get("mint") or tok.get("address") or raw.get("mint") or raw.get("address") or "")
    if not mint or mint in _SKIP_MINTS:
        return None

    pools = raw.get("pools") if isinstance(raw.get("pools"), list) else []
    pool = pools[0] if pools else {}
    ev = raw.get("events") if isinstance(raw.get("events"), dict) else {}

    def _ev(window: str) -> float:
        w = ev.get(window) if isinstance(ev.get(window), dict) else {}
        return _f(w, "priceChangePercentage", "priceChange", default=0.0)

    txns = pool.get("txns") if isinstance(pool.get("txns"), dict) else {}

    return {
        "mint":              mint,
        "symbol":            tok.get("symbol") or "",
        "price_usd":         _f(pool, ("price", "usd"), "priceUsd", "price"),
        "liquidity_usd":     _f(pool, ("liquidity", "usd"), "liquidityUsd", "liquidity"),
        "volume_5m":         _f(pool, ("txns", "volume5m"), "volume_5m", default=0.0),
        "volume_1h":         _f(pool, ("txns", "volume1h"), "volume_1h", default=0.0),
        "volume_6h":         0.0,
        "volume_24h":        _f(pool, ("txns", "volume"), "volume24h", "volume", default=0.0),
        "price_change_5m":   _ev("5m"),
        "price_change_1h":   _ev("1h"),
        "price_change_6h":   _ev("6h"),
        "price_change_24h":  _ev("24h"),
        "txns_5m_buys":      int(_f(txns, "buys5m", default=0.0)),
        "txns_5m_sells":     int(_f(txns, "sells5m", default=0.0)),
        "txns_1h_buys":      int(_f(txns, "buys1h", default=0.0)),
        "txns_1h_sells":     int(_f(txns, "sells1h", default=0.0)),
        "txns_24h_buys":     int(_f(txns, "buys", default=0.0)),
        "txns_24h_sells":    int(_f(txns, "sells", default=0.0)),
        "fdv":               _f(pool, ("marketCap", "usd"), "fdv", default=0.0),
        "market_cap":        _f(pool, ("marketCap", "usd"), "marketCap", default=0.0),
        "pair_age_hours":    9999.0,
        "pair_address":      pool.get("poolId") or pool.get("address") or "",
        "dex":               "solanatracker",
        "has_socials":       False,
        "has_website":       False,
        "paid_boost_active": False,
        # ── NEW (the reason to pay): pre-entry rug/sniper risk 1-10. 0 = unknown.
        "risk_score":        _risk_score(raw),
    }


async def fetch_trending(client, timeframe: str = "5m", limit: int = 100):
    """Fetch trending tokens → (mints, pool_data) in discovery's candidate shape.
    Fail-soft: returns ([], {}) when disabled or on any error. The endpoint path is
    overridable via SOLANATRACKER_TRENDING_PATH (confirm the exact route with validate.py)."""
    if not solanatracker_enabled():
        return [], {}
    path = os.getenv("SOLANATRACKER_TRENDING_PATH", f"tokens/trending/{timeframe}")
    data = await _get(client, path)
    if data is None:
        return [], {}
    # accept either a bare list or {"data"/"tokens": [...]}
    rows = data if isinstance(data, list) else (data.get("data") or data.get("tokens") or [])
    mints, pool_data = [], {}
    for raw in rows[: max(1, limit)]:
        rec = normalize_token(raw)
        if rec and rec["liquidity_usd"] > 0:
            mints.append(rec["mint"])
            pool_data[rec["mint"]] = rec
    return mints, pool_data


async def fetch_token(client, mint: str):
    """Single-token lookup → candidate dict (incl. risk_score) or None. For S98 rug_screen /
    a fast pre-entry risk first-pass. Fail-soft."""
    if not solanatracker_enabled():
        return None
    data = await _get(client, f"tokens/{mint}")
    if data is None:
        return None
    return normalize_token(data)


def _probe() -> int:
    """--probe (S119): exercise the free tier (if a key is present) and print shape/latency/
    coverage. With no key it is fully inert and points at the full validator. Never wires
    anything live; touches no canary, no fleet."""
    print("=" * 64)
    print("  SolanaTracker adapter — PROBE")
    print("=" * 64)
    print(f"  key set: {bool(API_KEY)}   base: {BASE}")
    if not API_KEY or httpx is None:
        print("  → INERT (no SOLANATRACKER_API_KEY / httpx). Nothing fetched.")
        print("  For the full free-tier 2,500-req validation (shape/coverage vs gecko), set the")
        print("  key then run:  python3 research/solanatracker/validate.py")
        print("=" * 64)
        return 0

    async def _run():
        # Probe ignores the per-bot canary on purpose (key-gated diagnostic only).
        async with httpx.AsyncClient() as client:
            path = os.getenv("SOLANATRACKER_TRENDING_PATH", "tokens/trending/5m")
            t0 = time.monotonic()
            data = await _get(client, path)
            dt = (time.monotonic() - t0) * 1000.0
            if data is None:
                print(f"  → fetch returned None (non-200/parse/timeout) in {dt:.0f}ms")
                return
            rows = data if isinstance(data, list) else (data.get("data") or data.get("tokens") or [])
            recs = [r for r in (normalize_token(x) for x in rows) if r]
            with_liq = [r for r in recs if r["liquidity_usd"] > 0]
            with_risk = [r for r in recs if r["risk_score"] > 0]
            print(f"  → {len(rows)} raw rows in {dt:.0f}ms · {len(recs)} normalized · "
                  f"{len(with_liq)} with liq>0 · {len(with_risk)} with risk_score")
            if recs:
                print("  sample:", json.dumps({k: recs[0][k] for k in
                      ("mint", "symbol", "liquidity_usd", "price_change_5m", "risk_score")}, indent=2))

    try:
        asyncio.run(_run())
    except Exception as e:
        print(f"  → probe error (fail-soft): {e}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    import sys
    if "--probe" in sys.argv:
        raise SystemExit(_probe())
    # tiny self-check (no network): default-OFF + normalizer on a synthetic record
    print("API key set:        ", bool(API_KEY))
    print("enabled (no canary):", solanatracker_enabled())
    sample = {
        "token": {"mint": "ABC123pump", "symbol": "TEST"},
        "pools": [{"liquidity": {"usd": 42000}, "price": {"usd": 0.0013},
                   "marketCap": {"usd": 500000}, "txns": {"buys5m": 30, "sells5m": 9}}],
        "events": {"5m": {"priceChangePercentage": 18.2}},
        "risk": {"score": 3},
    }
    rec = normalize_token(sample)
    print("normalize sample → ", json.dumps(rec, indent=2))
