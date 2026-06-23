"""
Multi-source viral token discovery for INSANE mode.

Seven parallel sources feed the discovered-token pool every DISCOVERY_REFRESH_CYCLES:

  1. Bonding Curve  — Helius WebSocket, pump.fun program logs. Real-time whale
                      buy signals BEFORE any trending list sees them. Pre-trend.
                      Zero extra API calls — events parsed from log stream.

  2. GeckoTerminal  — free, no key. Solana trending pools ranked by on-chain activity.
                      3 pages × ~20 pools = up to 60 candidates.

  3. Birdeye        — public-api.birdeye.so. No API key required for the public tier.
                      Trending + high-volume token lists. Optional BIRDEYE_API_KEY
                      in .env upgrades to higher rate limits if set.

  4. Cross-tier     — DexScreener live data for static watchlist tokens outside the
                      bot's current cap tier. Surfaces established tokens that aren't
                      in the active scan but are trading right now.

All sources run concurrently. Results are merged, deduplicated, and filtered:
  - Bonding Curve mints prepended — highest priority (pre-trend signal)
  - SOL / USDC / stablecoin base tokens excluded
  - pump.fun tokens allowed — RugCheck + confidence gates handle quality filtering
  - Tokens already in the active static watchlist excluded
  - Hard filters (liquidity, volume, age) applied in main.py as before

Setup:
  BondingCurve: automatic — uses existing HELIUS_API_KEY from .env
  GeckoTerminal: nothing — works out of the box.
  Birdeye: always active (public tier). Add BIRDEYE_API_KEY=<key> to .env for higher limits.
"""
import asyncio
import os
import random
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv

def _normalize_gecko_pool(mint: str, attrs: dict) -> dict:
    """Convert GeckoTerminal pool attributes to our market_data format."""
    vol  = attrs.get("volume_usd") or {}
    txns = attrs.get("transactions") or {}
    pct  = attrs.get("price_change_percentage") or {}
    m5t  = txns.get("m5") or {}
    h1t  = txns.get("h1") or {}
    h24t = txns.get("h24") or {}

    created = attrs.get("pool_created_at", "")
    try:
        age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 3600 if created else 9999.0
    except Exception:
        age_hours = 9999.0

    name = attrs.get("name", "")
    sym  = name.split("/")[0].strip() if "/" in name else ""

    return {
        "mint":              mint,
        "symbol":            sym,
        "price_usd":         float(attrs.get("base_token_price_usd") or 0),
        "liquidity_usd":     float(attrs.get("reserve_in_usd") or 0),
        "volume_5m":         float(vol.get("m5") or 0),
        "volume_1h":         float(vol.get("h1") or 0),
        "volume_6h":         float(vol.get("h6") or 0),
        "volume_24h":        float(vol.get("h24") or 0),
        "price_change_5m":   float(pct.get("m5") or 0),
        "price_change_1h":   float(pct.get("h1") or 0),
        "price_change_6h":   float(pct.get("h6") or 0),
        "price_change_24h":  float(pct.get("h24") or 0),
        "txns_5m_buys":      int(m5t.get("buys") or 0),
        "txns_5m_sells":     int(m5t.get("sells") or 0),
        "txns_1h_buys":      int(h1t.get("buys") or 0),
        "txns_1h_sells":     int(h1t.get("sells") or 0),
        "txns_24h_buys":     int(h24t.get("buys") or 0),
        "txns_24h_sells":    int(h24t.get("sells") or 0),
        "fdv":               float(attrs.get("fdv_usd") or 0),
        "market_cap":        float(attrs.get("market_cap_usd") or 0),
        "pair_age_hours":    round(max(age_hours, 0.0), 1),
        "pair_address":      attrs.get("address", ""),
        "dex":               "gecko",
        "has_socials":       False,
        "has_website":       False,
        "paid_boost_active": False,
    }

load_dotenv()

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")
BIRDEYE_ENABLED = bool(BIRDEYE_API_KEY)  # free-tier key required — register at birdeye.so/api

GECKO_BASE   = "https://api.geckoterminal.com/api/v2"
BIRDEYE_BASE = "https://public-api.birdeye.so"

# S91: GeckoTerminal free tier is ~30 calls/min and rejects BURSTS. One discovery cycle
# fires 12 page requests at once (trending 8 + new_pools 2 + deep 2) via a single gather →
# the 12-wide burst tripped the limiter → the whole batch 429'd → silently returned empty
# ("0 tokens" ~50% of cycles). Two guards: (1) a global semaphore meters the burst into a
# stream that stays under the per-second limit; (2) 429-aware retry/backoff (honours
# Retry-After) so a transient throttle retries instead of becoming a lost cycle.
_GECKO_SEM         = asyncio.Semaphore(4)   # max concurrent GeckoTerminal requests fleet-cycle-wide
_GECKO_MAX_RETRIES = 3
_GECKO_BACKOFF_CAP = 5.0                     # seconds

# S91: max share of the candidate cap that bonding-curve hot mints may occupy.
# BC hot-count routinely exceeds max_tokens (145-173/cycle); when BC was prepended
# unbounded ahead of the GeckoTerminal sources, it filled the entire cap and sliced
# every deep/trending SELLABLE pool off the end → admission starved, trades dried up.
# Reserving the rest for the sellable tokens restores throughput.
_BC_SLOT_FRACTION = 0.5

# Mints we never want to trade against — always excluded from discovery
_SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH (Wormhole)
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
}


# ── GeckoTerminal ─────────────────────────────────────────────────────────────

def _extract_gecko_data(data: list) -> tuple:
    """Pull base-token mints + normalized pool data from a GeckoTerminal pool list."""
    mints, pool_data = [], {}
    for pool in data:
        rels    = pool.get("relationships", {})
        base_id = rels.get("base_token", {}).get("data", {}).get("id", "")
        if not base_id.startswith("solana_"):
            continue
        mint = base_id[len("solana_"):]
        if not mint or mint in _SKIP_MINTS:
            continue
        mints.append(mint)
        pool_data[mint] = _normalize_gecko_pool(mint, pool.get("attributes") or {})
    return mints, pool_data


async def _gecko_page(client: httpx.AsyncClient, page: int, endpoint: str = "trending_pools") -> tuple:
    """One page of GeckoTerminal pools. Returns (mints, pool_data).

    S91: throttled (global semaphore) + 429-aware retry/backoff. The request itself runs
    inside the semaphore; the back-off sleep happens OUTSIDE it so a waiting attempt never
    holds a slot. GET is idempotent → safe to retry.
    """
    backoff = 0.8
    for attempt in range(_GECKO_MAX_RETRIES):
        try:
            async with _GECKO_SEM:
                r = await client.get(
                    f"{GECKO_BASE}/networks/solana/{endpoint}",
                    params={"page": page},
                    headers={"Accept": "application/json;version=20230302"},
                    timeout=12,
                )
            if r.status_code == 200:
                return _extract_gecko_data(r.json().get("data", []))
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra else backoff
                except (TypeError, ValueError):
                    wait = backoff
                wait = min(wait, _GECKO_BACKOFF_CAP) + random.uniform(0, 0.4)
                if attempt < _GECKO_MAX_RETRIES - 1:
                    print(f"[GeckoTerminal] {endpoint} p{page} 429 — retry in {wait:.1f}s "
                          f"({attempt + 1}/{_GECKO_MAX_RETRIES})", flush=True)
                    await asyncio.sleep(wait)
                    backoff *= 2
                    continue
                print(f"[GeckoTerminal] {endpoint} p{page} 429 — gave up after {_GECKO_MAX_RETRIES}", flush=True)
                return [], {}
            print(f"[GeckoTerminal] {endpoint} p{page} HTTP {r.status_code}", flush=True)
            return [], {}
        except Exception as e:
            print(f"[GeckoTerminal] {endpoint} page {page} error: {e}", flush=True)
            return [], {}
    return [], {}


def _merge_gecko(results: list) -> tuple:
    """Merge (mints, pool_data) tuples from multiple gecko pages, deduplicating."""
    seen, mints, pool_data = set(), [], {}
    for page_mints, page_data in results:
        for mint in page_mints:
            if mint not in seen:
                seen.add(mint)
                mints.append(mint)
        for mint, d in page_data.items():
            if mint not in pool_data:
                pool_data[mint] = d
    return mints, pool_data


async def _fetch_geckoterminal(client: httpx.AsyncClient, pages: int = 3) -> tuple:
    """Trending Solana pools — already gaining traction. Returns (mints, pool_data)."""
    results = await asyncio.gather(*[_gecko_page(client, p) for p in range(1, pages + 1)])
    mints, pool_data = _merge_gecko(results)
    print(f"[GeckoTerminal] {len(mints)} trending tokens ({pages} pages)", flush=True)
    return mints, pool_data


async def _fetch_gecko_new_pools(client: httpx.AsyncClient, pages: int = 2) -> tuple:
    """
    GeckoTerminal newly created Solana pools — tokens 1-24h old that are just
    starting to attract buyers. These are the hidden gems before they trend.
    Returns (mints, pool_data).
    """
    results = await asyncio.gather(*[_gecko_page(client, p, "new_pools") for p in range(1, pages + 1)])
    mints, pool_data = _merge_gecko(results)
    print(f"[GeckoTerminal] {len(mints)} new-pool tokens ({pages} pages)", flush=True)
    return mints, pool_data


async def _fetch_gecko_hot(client: httpx.AsyncClient) -> tuple:
    """
    GeckoTerminal deep trending pages 4+5 — broader sweep. Returns (mints, pool_data).
    """
    results = await asyncio.gather(
        _gecko_page(client, 4),
        _gecko_page(client, 5),
    )
    mints, pool_data = _merge_gecko(results)
    print(f"[GeckoTerminal] {len(mints)} deep-trending tokens (pages 4-5)", flush=True)
    return mints, pool_data


# ── Birdeye ───────────────────────────────────────────────────────────────────

async def _fetch_birdeye_trending(client: httpx.AsyncClient) -> list:
    """
    Fetch trending tokens from Birdeye's rank-sorted endpoint (public tier, no key required).
    BIRDEYE_API_KEY in .env upgrades to higher rate limits if present.
    """
    _headers = {"accept": "application/json", "x-chain": "solana"}
    if BIRDEYE_API_KEY:
        _headers["X-API-KEY"] = BIRDEYE_API_KEY
    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/token_trending",
            params={"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": 50},
            headers=_headers,
            timeout=12,
        )
        if r.status_code in (401, 403):
            print(f"[Birdeye] trending auth error {r.status_code} — endpoint may require a key", flush=True)
            return []
        if r.status_code != 200:
            print(f"[Birdeye] trending HTTP {r.status_code}", flush=True)
            return []

        body = r.json()
        # Response can be {"data": {"items": [...]}} or {"data": {"tokens": [...]}}
        data   = body.get("data") or {}
        tokens = data.get("items") or data.get("tokens") or []
        if not tokens and isinstance(data, list):
            tokens = data  # some versions return the list directly

        mints = [
            t.get("address", "")
            for t in tokens
            if t.get("address")
            and t["address"] not in _SKIP_MINTS
        ]
        print(f"[Birdeye] {len(mints)} trending tokens", flush=True)
        return mints
    except Exception as e:
        print(f"[Birdeye] trending error: {e}", flush=True)
        return []


async def _fetch_birdeye_volume(client: httpx.AsyncClient) -> list:
    """
    Secondary Birdeye source: top tokens by 24h volume change with liquidity floor.
    Catches tokens that are trending by momentum but may not rank yet (public tier, no key required).
    """
    _headers = {"accept": "application/json", "x-chain": "solana"}
    if BIRDEYE_API_KEY:
        _headers["X-API-KEY"] = BIRDEYE_API_KEY
    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/tokenlist",
            params={
                "sort_by":       "v24hChangePercent",
                "sort_type":     "desc",
                "offset":        0,
                "limit":         50,
                "min_liquidity": 100_000,
            },
            headers=_headers,
            timeout=12,
        )
        if r.status_code != 200:
            return []

        body   = r.json()
        data   = body.get("data") or {}
        tokens = data.get("tokens") or data.get("items") or []
        if not tokens and isinstance(data, list):
            tokens = data

        mints = [
            t.get("address", "")
            for t in tokens
            if t.get("address")
            and t["address"] not in _SKIP_MINTS
        ]
        print(f"[Birdeye] {len(mints)} high-momentum tokens (v24h sort)", flush=True)
        return mints
    except Exception as e:
        print(f"[Birdeye] volume-sort error: {e}", flush=True)
        return []


# ── Cross-tier (DexScreener static list) ─────────────────────────────────────

async def _fetch_crosstier(client: httpx.AsyncClient, exclude: list) -> list:
    """
    Scan the full static watchlist for tokens outside the current cap tier.
    Surfaces established tokens that are actively trading right now.
    """
    import dexscreener
    from config import WATCHLIST, MIN_DISCOVERY_LIQUIDITY

    exclude_set = set(exclude or [])
    candidates  = [m for m in WATCHLIST if m not in exclude_set]
    if not candidates:
        return []

    market_data = await dexscreener.get_watchlist_data(client, candidates)
    active = [
        mint for mint, d in market_data.items()
        if d.get("liquidity_usd", 0) >= MIN_DISCOVERY_LIQUIDITY
    ]
    print(
        f"[Cross-tier] {len(candidates)} candidates → {len(active)} active "
        f"(liq≥${MIN_DISCOVERY_LIQUIDITY/1000:.0f}k)",
        flush=True,
    )
    return active


# ── Aggregator ────────────────────────────────────────────────────────────────

async def discover_trending_tokens(
    client:              httpx.AsyncClient,
    exclude:             list = None,
    max_tokens:          int  = 52,
    pages_trending:      int  = 3,
    pages_new_pools:     int  = 2,
    include_deep:        bool = True,
    bonding_curve_mints: list = None,
) -> tuple:
    """
    Run discovery sources in parallel and return a deduplicated mint list.

    Source depth scales with mode — wider net for higher-velocity modes:
      STOIC:  pages_trending=2, pages_new_pools=0, include_deep=False
      WILD:   pages_trending=3, pages_new_pools=1, include_deep=False
      INSANE: pages_trending=3, pages_new_pools=2, include_deep=True

    Priority (earlier = higher priority on dedup):
      0. Bonding Curve            — real-time whale buys on pump.fun (pre-trend)
      1. GeckoTerminal new_pools  — gems 1-24h old, before they trend
      2. GeckoTerminal trending   — already gaining traction
      3. GeckoTerminal deep       — pages 4-5, broader sweep (INSANE only)
      4. Birdeye trending         — proprietary rank (needs key)
      5. Birdeye high-momentum    — 24h volume change sort (needs key)
      6. Cross-tier               — watchlist tokens outside current cap tier
    """
    exclude_set = set(exclude or []) | _SKIP_MINTS

    async def _noop_gecko_fn() -> tuple:
        return [], {}

    new_pools_task = _fetch_gecko_new_pools(client, pages=pages_new_pools) if pages_new_pools > 0 else _noop_gecko_fn()
    trending_task  = _fetch_geckoterminal(client, pages=pages_trending)
    hot_task       = _fetch_gecko_hot(client) if include_deep else _noop_gecko_fn()
    birdeye_rank   = _fetch_birdeye_trending(client)
    birdeye_vol    = _fetch_birdeye_volume(client)
    crosstier_task = _fetch_crosstier(client, exclude)

    (new_pools, np_data), (trending, trend_data), (hot, hot_data), b_rank, b_vol, crosstier = await asyncio.gather(
        new_pools_task, trending_task, hot_task,
        birdeye_rank, birdeye_vol, crosstier_task,
    )

    # Bonding curve mints — real-time whale buys (pre-trend, the INSANE gem/sniper edge).
    # Filter out excluded mints first so they don't occupy slots.
    bc_mints = [m for m in (bonding_curve_mints or []) if m and m not in exclude_set]
    if bc_mints:
        print(f"[BondingCurve] {len(bc_mints)} hot mint(s) injected into discovery pool", flush=True)

    # S91: RESERVE candidate slots for the gecko-priced sellable pools. BC was
    # prepended unbounded and the merged list truncated to max_tokens; with BC
    # routinely >max_tokens, every deep/trending sellable pool got sliced off the
    # end and never reached the scorer. Cap BC's share (_BC_SLOT_FRACTION) so the
    # sellable tokens always get in; BC keeps a strong bounded share + back-fills
    # any slots the other sources leave free (so capacity is never wasted).
    def _dedup(mints, seen):
        out = []
        for m in mints:
            if m and m not in seen and m not in exclude_set:
                seen.add(m)
                out.append(m)
        return out

    seen: set = set()
    gecko_list = _dedup(new_pools + trending + hot, seen)   # sellable depth + pricing — the edge
    bc_list    = _dedup(bc_mints, seen)                     # pre-trend whale signals
    other_list = _dedup(b_rank + b_vol + crosstier, seen)   # mint-only (DexScreener prices these)

    bc_slots = int(max_tokens * _BC_SLOT_FRACTION)
    bc_take  = bc_list[:bc_slots]                           # bounded BC share (placed first → keeps its priority)
    rest     = gecko_list + other_list                      # sellable tokens claim the remainder first
    non_bc_budget = max_tokens - len(bc_take)
    result = bc_take + rest[:non_bc_budget]
    if len(result) < max_tokens:                            # back-fill unused capacity from either side
        leftover = bc_list[bc_slots:] + rest[non_bc_budget:]
        result += leftover[:max_tokens - len(result)]
    result = result[:max_tokens]

    # Merge GeckoTerminal pool data (earlier source wins on dedup — new_pools > trending > hot).
    # Birdeye and cross-tier are mint-only sources; DexScreener covers those tokens.
    gecko_pool_data: dict = {}
    for src_data in (np_data, trend_data, hot_data):
        for mint, d in src_data.items():
            if mint not in gecko_pool_data:
                gecko_pool_data[mint] = d
    result_pool_data = {m: gecko_pool_data[m] for m in result if m in gecko_pool_data}

    sources = f"BC={len(bc_mints)} Trending={len(trending)}"
    if pages_new_pools > 0:
        sources += f" NewPools={len(new_pools)}"
    if include_deep:
        sources += f" Deep={len(hot)}"
    if BIRDEYE_ENABLED:
        sources += f" Birdeye={len(set(b_rank+b_vol))}"
    sources += f" CrossTier={len(crosstier)}"

    print(
        f"         [Discovery] {sources} → {len(result)} unique candidates "
        f"({len(result_pool_data)} with gecko pricing)",
        flush=True,
    )
    return result, result_pool_data
