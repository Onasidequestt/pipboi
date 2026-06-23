#!/usr/bin/env python3
"""DexScreener-led discovery feed — the $0 fix for the S91 candidate-stream starvation.

WHY (measured this session): free GeckoTerminal is ~30 req/min and 429s a 20-call burst
(6/20 succeed) → the deep SELLABLE pools get sliced off the capped candidate list (S91), so
even a validated edge (RUNNEREDGE, the vacc-key slice) can't fire enough to prove. DexScreener
is **free, no key, ~300 req/min** (empirically 40 calls/0.5s, 0×429 — ~10× Gecko's budget) and
is ALREADY a fleet dependency (`dexscreener.py`). Leaning discovery on it widens the stream for
$0 — the alternative to paying SolanaTracker €50/mo for rate-limit relief.

DISCIPLINE (mirrors solanatracker_service / the canary pattern):
  - INERT unless the per-bot canary bots/botN/dex_discovery.json {"enabled":true} exists →
    dex_discovery_on() is the single gate. No canary ⇒ every function no-ops / returns empty.
  - FAIL-SOFT: any HTTP/parse error returns ([], {}) — it can NEVER raise into the discovery
    loop or stall a cycle (same contract as discovery._gecko_page).
  - Emits the EXACT candidate dict shape discovery._normalize_gecko_pool produces, dex=
    "dexscreener", so it's a DROP-IN ADDITIVE source — reserve-slotted into discovery_service
    the same way the S91 BC fix is (it never floods the cap).
  - Self-throttled well under 300/min (a Semaphore + tiny spacer) so it stays free forever.

VALIDATE-FIRST — NOT wired into live discovery yet. Run
    python3 research/dexscreener/coverage_ab.py
to measure whether DexScreener surfaces the sellable pools gecko is MISSING (the decision data)
BEFORE wiring fetch_candidates into discovery_service._poll_discovery + restart. See the README.

ENABLE (after the coverage A/B looks good):
    echo '{"enabled":true}' > bots/bot1/dex_discovery.json     # Bot1 A/B; bot2/3 = control
    # then the one additive merge in discovery_service._poll_discovery (README) + restart.
REVERT: rm bots/bot*/dex_discovery.json  (hot — feed goes inert).
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

_ROOT = Path(__file__).resolve().parent
BASE = "https://api.dexscreener.com"

# DexScreener free: 300/min on pairs/tokens, 60/min on profiles/boosts. Stay well under:
_SEM = asyncio.Semaphore(3)
_MIN_INTERVAL = 0.25
_last = 0.0

_SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",   # ETH
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",    # mSOL
}

_CANARY_CACHE = {"ts": -1e9, "val": {}}   # S121: −sentinel so the FIRST call reads the canary
                                          # immediately (time.monotonic() starts ~0 on macOS, so a
                                          # 0.0 seed left the canary "off" for a process's first 30s).


def _bot_id() -> int:
    import os
    try:
        return int(os.getenv("BOT_ID", "1"))
    except Exception:
        return 1


def dex_discovery_on(bot_id: int | None = None) -> bool:
    """The single gate: bots/botN/dex_discovery.json {"enabled":true}. 30s cache."""
    if httpx is None:
        return False
    now = time.monotonic()
    if now - _CANARY_CACHE["ts"] >= 30.0:
        val = {}
        try:
            fp = _ROOT / f"bots/bot{bot_id if bot_id is not None else _bot_id()}/dex_discovery.json"
            if fp.exists():
                val = json.loads(fp.read_text()) or {}
        except Exception:
            val = {}
        _CANARY_CACHE.update(ts=now, val=val)
    return bool(_CANARY_CACHE["val"].get("enabled"))


async def _get(client, path: str, timeout: float = 12.0):
    """Rate-limited, fail-soft GET → parsed JSON or None (never raises)."""
    global _last
    if httpx is None:
        return None
    try:
        async with _SEM:
            gap = _MIN_INTERVAL - (time.monotonic() - _last)
            if gap > 0:
                await asyncio.sleep(gap)
            _last = time.monotonic()
            r = await client.get(f"{BASE}/{path.lstrip('/')}",
                                 headers={"accept": "application/json"}, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _pair_age_hours(created_ms) -> float:
    try:
        created = float(created_ms) / 1000.0
        return round(max((datetime.now(timezone.utc).timestamp() - created) / 3600.0, 0.0), 1)
    except Exception:
        return 9999.0


def normalize_pair(p: dict) -> dict | None:
    """Map a DexScreener pair → our candidate dict (discovery._normalize_gecko_pool shape)."""
    if not isinstance(p, dict) or p.get("chainId") != "solana":
        return None
    base = p.get("baseToken") or {}
    mint = base.get("address") or ""
    if not mint or mint in _SKIP_MINTS:
        return None
    liq = (p.get("liquidity") or {})
    vol = (p.get("volume") or {})
    pc = (p.get("priceChange") or {})
    tx = (p.get("txns") or {})
    m5t, h1t, h24t = (tx.get("m5") or {}), (tx.get("h1") or {}), (tx.get("h24") or {})

    def _f(d, k):
        try:
            return float(d.get(k) or 0)
        except Exception:
            return 0.0

    def _i(d, k):
        try:
            return int(d.get(k) or 0)
        except Exception:
            return 0

    return {
        "mint":              mint,
        "symbol":            base.get("symbol") or "",
        "price_usd":         _f(p, "priceUsd"),
        "liquidity_usd":     _f(liq, "usd"),
        "volume_5m":         _f(vol, "m5"),
        "volume_1h":         _f(vol, "h1"),
        "volume_6h":         _f(vol, "h6"),
        "volume_24h":        _f(vol, "h24"),
        "price_change_5m":   _f(pc, "m5"),
        "price_change_1h":   _f(pc, "h1"),
        "price_change_6h":   _f(pc, "h6"),
        "price_change_24h":  _f(pc, "h24"),
        "txns_5m_buys":      _i(m5t, "buys"),
        "txns_5m_sells":     _i(m5t, "sells"),
        "txns_1h_buys":      _i(h1t, "buys"),
        "txns_1h_sells":     _i(h1t, "sells"),
        "txns_24h_buys":     _i(h24t, "buys"),
        "txns_24h_sells":    _i(h24t, "sells"),
        "fdv":               _f(p, "fdv"),
        "market_cap":        _f(p, "marketCap"),
        "pair_age_hours":    _pair_age_hours(p.get("pairCreatedAt")),
        "pair_address":      p.get("pairAddress") or "",
        "dex":               "dexscreener",
        "has_socials":       bool((p.get("info") or {}).get("socials")),
        "has_website":       bool((p.get("info") or {}).get("websites")),
        "paid_boost_active": bool(p.get("boosts")),
    }


def _best_pair_per_token(pairs: list) -> dict:
    """Collapse many pairs → the deepest Solana pool per mint (sellability = depth)."""
    best: dict = {}
    for p in pairs or []:
        rec = normalize_pair(p)
        if not rec or rec["liquidity_usd"] <= 0:
            continue
        cur = best.get(rec["mint"])
        if cur is None or rec["liquidity_usd"] > cur["liquidity_usd"]:
            best[rec["mint"]] = rec
    return best


async def _trending_addresses(client) -> list:
    """Solana token addresses from the free trending-intent endpoints (boosts + new profiles)."""
    addrs: list = []
    for path in ("token-boosts/top/v1", "token-boosts/latest/v1", "token-profiles/latest/v1"):
        data = await _get(client, path)
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and it.get("chainId") == "solana":
                    a = it.get("tokenAddress")
                    if a and a not in addrs:
                        addrs.append(a)
    return addrs


async def fetch_candidates(client, extra_terms: tuple = ("raydium", "pumpswap")) -> tuple:
    """DexScreener candidate stream → (mints, pool_data) in discovery's candidate shape.
    Fail-soft → ([], {}) when disabled/empty/error. Sources: trending-intent boosts+profiles
    (batch-resolved to full pairs) + a couple of dex search terms for breadth. ~6-8 calls."""
    if not dex_discovery_on():
        return [], {}
    pool: dict = {}

    # 1) trending-intent tokens → batch-resolve to full pairs (30 addrs/call)
    addrs = await _trending_addresses(client)
    for i in range(0, len(addrs), 30):
        chunk = ",".join(addrs[i:i + 30])
        data = await _get(client, f"latest/dex/tokens/{chunk}")
        if isinstance(data, dict):
            pool.update(_best_pair_per_token(data.get("pairs") or []))

    # 2) breadth via search (full pair data inline)
    for term in extra_terms:
        data = await _get(client, f"latest/dex/search?q={term}")
        if isinstance(data, dict):
            for m, rec in _best_pair_per_token(data.get("pairs") or []).items():
                pool.setdefault(m, rec)   # don't override a deeper trending pair

    return list(pool.keys()), pool


if __name__ == "__main__":
    print("httpx:", httpx is not None, "| enabled (no canary):", dex_discovery_on())
    if httpx:
        async def _demo():
            async with httpx.AsyncClient() as c:
                # bypass the canary for a one-off demo by forcing the cache
                _CANARY_CACHE.update(ts=time.monotonic(), val={"enabled": True})
                mints, pool = await fetch_candidates(c)
                sellable = [r for r in pool.values() if r["liquidity_usd"] >= 30000]
                print(f"fetched {len(mints)} candidates, {len(sellable)} sellable (>=$30k)")
                for r in sorted(sellable, key=lambda x: -x["liquidity_usd"])[:5]:
                    print(f"  {r['symbol'][:8]:8} ${r['liquidity_usd']:>12,.0f}  "
                          f"m5 {r['price_change_5m']:+.1f}%  bs {r['txns_5m_buys']}/{r['txns_5m_sells']}")
        asyncio.run(_demo())
