"""
DexScreener API client — real liquidity, volume, and price change data.
No API key required.
"""
import asyncio
import time
from typing import Optional
import httpx

BASE    = "https://api.dexscreener.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Per-mint result cache — avoids re-fetching stable prices every 30s cycle.
# TTL: 60s (2 cycles). Dramatically cuts DexScreener request volume.
_dex_cache: dict = {}   # mint -> (data_dict, timestamp)
_DEX_CACHE_TTL  = 60    # seconds

def _get_cached(mints: list) -> tuple:
    """Split mints into (cached_dict, fresh_list) by TTL."""
    now, cached, fresh = time.time(), {}, []
    for m in mints:
        entry = _dex_cache.get(m)
        if entry and now - entry[1] < _DEX_CACHE_TTL:
            cached[m] = entry[0]
        else:
            fresh.append(m)
    return cached, fresh

def _store_cache(data: dict) -> None:
    now = time.time()
    for mint, d in data.items():
        _dex_cache[mint] = (d, now)


def _normalize_pair(pair: dict, mint: str) -> dict:
    vol   = pair.get("volume") or {}
    txns  = pair.get("txns")   or {}
    info  = pair.get("info")   or {}
    boosts = pair.get("boosts") or {}

    # Pair age in hours from pairCreatedAt (ms timestamp)
    created_ms = pair.get("pairCreatedAt") or 0
    age_hours  = round((time.time() * 1000 - created_ms) / 3_600_000, 1) if created_ms else 9999.0

    return {
        "mint":              mint,
        "symbol":            (pair.get("baseToken") or {}).get("symbol", ""),
        "price_usd":         float(pair.get("priceUsd") or 0),
        "liquidity_usd":     float((pair.get("liquidity") or {}).get("usd") or 0),
        # ── Volume across timeframes ──────────────────────────────────────
        "volume_5m":         float(vol.get("m5") or 0),
        "volume_1h":         float(vol.get("h1") or 0),
        "volume_6h":         float(vol.get("h6") or 0),
        "volume_24h":        float(vol.get("h24") or 0),
        # ── Price change across timeframes ────────────────────────────────
        "price_change_5m":   float((pair.get("priceChange") or {}).get("m5") or 0),
        "price_change_1h":   float((pair.get("priceChange") or {}).get("h1") or 0),
        "price_change_6h":   float((pair.get("priceChange") or {}).get("h6") or 0),
        "price_change_24h":  float((pair.get("priceChange") or {}).get("h24") or 0),
        # ── Transactions (5m and 1h) ──────────────────────────────────────
        "txns_5m_buys":      int((txns.get("m5") or {}).get("buys") or 0),
        "txns_5m_sells":     int((txns.get("m5") or {}).get("sells") or 0),
        "txns_1h_buys":      int((txns.get("h1") or {}).get("buys") or 0),
        "txns_1h_sells":     int((txns.get("h1") or {}).get("sells") or 0),
        "txns_24h_buys":     int((txns.get("h24") or {}).get("buys") or 0),
        "txns_24h_sells":    int((txns.get("h24") or {}).get("sells") or 0),
        # ── Market structure ─────────────────────────────────────────────
        "fdv":               float(pair.get("fdv") or 0),
        "market_cap":        float(pair.get("marketCap") or 0),
        "pair_age_hours":    age_hours,
        "pair_address":      pair.get("pairAddress", ""),
        "dex":               pair.get("dexId", ""),
        # ── Legitimacy signals ────────────────────────────────────────────
        "has_socials":       len(info.get("socials") or []) > 0,
        "has_website":       len(info.get("websites") or []) > 0,
        "paid_boost_active": int(boosts.get("active") or 0) > 0,
    }


def compute_viral_score(data: dict, social_score: float = -1.0) -> float:
    """
    Multi-factor viral quality score (0.0 – 1.0).
    Designed for tokens from token-profiles/latest/v1 — typically 1–24h old.

    Factors:
      Volume strength    (0–0.25): absolute 1h trading volume — is anyone actually here?
      Buy pressure 1h   (0–0.25): crowd buying over an hour, not one whale in 5m
      Pair age           (0–0.20): 1–6h = hot new launch; rewards freshness not age
      FDV gem range      (0–0.18): $300k–$15M = room to run; 0 FDV gets benefit of doubt
      Price holding up   (0–0.07): 1h price positive or rising = buyers supporting price
      Social presence    (0–0.07): Twitter/Telegram + website = real project
      No paid boost      (0.02):   organic listing, not a promoted dump

    Thresholds:
      < 0.35 → drop from pool (too many red flags)
      ≥ 0.50 → surface as potential gem
      ≥ 0.65 → high conviction → +50% sizing bonus
    """
    score = 0.0

    v_h1  = data.get("volume_1h", 0)
    v_h6  = data.get("volume_6h", 0)
    b_1h  = data.get("txns_1h_buys", 0)
    s_1h  = data.get("txns_1h_sells", 0)
    age   = data.get("pair_age_hours", 9999)
    fdv   = data.get("fdv", 0)
    p_1h  = data.get("price_change_1h", 0)

    # ── Volume strength ───────────────────────────────────────────────────
    # Absolute 1h volume is the clearest signal of real market interest.
    # New tokens won't have 6h history, so don't punish them for it.
    if v_h1 >= 100_000:   score += 0.25
    elif v_h1 >= 50_000:  score += 0.20
    elif v_h1 >= 20_000:  score += 0.14
    elif v_h1 >= 5_000:   score += 0.08
    elif v_h1 >= 1_000:   score += 0.03
    # If 6h history exists, bonus points for acceleration above the 6h pace
    if v_h6 > 0 and (v_h1 * 6) > v_h6 * 1.5:
        score += 0.04   # this hour is running 1.5x hotter than 6h average

    # ── 1h buy pressure ───────────────────────────────────────────────────
    # 5m buys can be one wallet; 1h buys = real crowd showing up.
    # Minimum 5 transactions (not 10) — new tokens won't have huge tx counts yet.
    total_1h = b_1h + s_1h
    if total_1h >= 5:
        score += (b_1h / total_1h) * 0.25  # 80% buy rate = 0.20 points

    # ── Pair age — reward freshness, not staleness ────────────────────────
    # discovery endpoint returns new tokens. 1–6h is the golden launch window:
    # long enough to prove the pair isn't a 5-minute rug, early enough to matter.
    if 1 <= age <= 6:          score += 0.20   # hot new launch
    elif 6 < age <= 24:        score += 0.14   # still same-day fresh
    elif 24 < age <= 72:       score += 0.07   # 1–3 days old
    elif 72 < age <= 168:      score += 0.03   # up to a week
    elif age > 168:            score += 0.01   # established — rare in discovery
    # age < 1h is hard-filtered upstream — never reaches here

    # ── FDV: the gem range ────────────────────────────────────────────────
    # $300k–$3M: micro gem — high risk, highest upside
    # $3M–$15M:  small gem — real market with room to run
    # $15M–$50M: mid-small — decent upside
    # Unknown (0): benefit of the doubt — new tokens often lack FDV data
    if 300_000 <= fdv <= 3_000_000:       score += 0.18
    elif 3_000_000 < fdv <= 15_000_000:   score += 0.13
    elif 15_000_000 < fdv <= 50_000_000:  score += 0.06
    elif fdv == 0:                         score += 0.05  # unknown — don't punish

    # ── Price holding up over 1h ──────────────────────────────────────────
    # Buyers are sustaining the price, not just spiking in a candle.
    if p_1h >= 10:    score += 0.07
    elif p_1h >= 3:   score += 0.05
    elif p_1h >= 0:   score += 0.02
    # Negative 1h: no points — but not a disqualifier on its own

    # ── Social legitimacy ─────────────────────────────────────────────────
    if data.get("has_socials"):  score += 0.04
    if data.get("has_website"):  score += 0.03

    # ── Organic signal ────────────────────────────────────────────────────
    if not data.get("paid_boost_active"):
        score += 0.02

    # ── Twitter/X social authenticity (optional) ──────────────────────────
    # social_score >= 0 means we have real data from the Twitter API.
    # Authentic organic buzz adds up to +0.15. Bot campaign (low score) subtracts up to -0.10.
    # -1.0 means Twitter is not configured — ignored entirely.
    if social_score >= 0:
        if social_score >= 0.70:
            score += 0.15   # strong organic discussion
        elif social_score >= 0.50:
            score += 0.08   # decent authentic buzz
        elif social_score >= 0.30:
            score += 0.02   # some signal, mixed quality
        elif social_score < 0.20:
            score -= 0.10   # looks like a bot campaign — reduce conviction

    return round(min(1.0, max(0.0, score)), 3)


async def get_token_data(client: httpx.AsyncClient, mint: str) -> Optional[dict]:
    """Fetch the best (highest liquidity) Solana pair for a single token."""
    try:
        r = await client.get(f"{BASE}/latest/dex/tokens/{mint}", timeout=10, headers=HEADERS)
        r.raise_for_status()
        all_pairs = r.json().get("pairs") or []
        pairs = [p for p in all_pairs if p.get("chainId") == "solana"]
        if not pairs:
            return None
        pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        return _normalize_pair(pair, mint)
    except Exception as e:
        print(f"[DexScreener] Error for {mint[:8]}: {e}")
        return None


async def _get_batch(client: httpx.AsyncClient, mints: list) -> dict:
    """Fetch up to 30 tokens in one API call. Returns dict keyed by mint."""
    if not mints:
        return {}
    try:
        chunk = ",".join(mints[:30])
        r = await client.get(
            f"{BASE}/latest/dex/tokens/{chunk}", timeout=15, headers=HEADERS
        )
        r.raise_for_status()
        # Group by base token mint, keep highest-liquidity Solana pair per token
        best: dict = {}
        for pair in (r.json().get("pairs") or []):
            if pair.get("chainId") != "solana":
                continue
            mint = pair.get("baseToken", {}).get("address", "")
            if not mint:
                continue
            liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            if liq > float((best.get(mint) or {}).get("liquidity_usd", 0)):
                best[mint] = _normalize_pair(pair, mint)
        return best
    except Exception as e:
        print(f"[DexScreener] Batch error ({len(mints)} tokens): {e}")
        return {}


async def get_watchlist_data(client: httpx.AsyncClient, mints: list) -> dict:
    """
    Fetch market data for all given tokens using batch calls (30 per request).
    Returns dict keyed by mint address. Cached results (< 60s old) are reused
    to avoid hammering DexScreener with 90-token fetches every 30s cycle.
    """
    if not mints:
        return {}
    cached, fresh = _get_cached(mints)
    if not fresh:
        return cached
    chunks = [fresh[i:i + 30] for i in range(0, len(fresh), 30)]
    results = await asyncio.gather(*[_get_batch(client, chunk) for chunk in chunks])
    fresh_data: dict = {}
    for r in results:
        fresh_data.update(r)
    _store_cache(fresh_data)
    print(f"         [DexCache] {len(cached)} cached + {len(fresh_data)} fresh ({len(fresh)-len(fresh_data)} missed)", flush=True)
    return {**cached, **fresh_data}



async def get_sol_price(client: httpx.AsyncClient) -> float:
    """Fetch current SOL/USD price via the Raydium SOL/USDC pair."""
    try:
        SOL_USDC = "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"
        r = await client.get(
            f"{BASE}/latest/dex/pairs/solana/{SOL_USDC}", timeout=10, headers=HEADERS
        )
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        if pairs:
            return float(pairs[0].get("priceUsd") or 150.0)
    except Exception:
        pass
    return 150.0
