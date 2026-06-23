"""
LunarCrush social intelligence for Solana tokens.
Uses the LunarCrush AI MCP server (lunarcrush.ai/sse).

Free tier: 100 req/day, 4/min — but ALL data tools require a paid subscription.
Without a subscription every tool call returns -1.0 (score ignored in viral calc).
When a subscription is active, this module:
  1. Fetches top-500 Solana-ecosystem tokens by galaxy_score every 15 min (1 call)
  2. Fetches top-500 pump.fun tokens by galaxy_score every 15 min (1 call)
  3. Builds a symbol → score cache for instant lookups
  4. On cache miss: calls topic($SYMBOL) once per mint per session (1 call)

galaxy_score (0–100): organic reach, sentiment, cross-platform quality
social_score (0–100): raw aggregated volume + engagement
Combined score = galaxy_score×0.60 + social_score×0.40 → normalised 0–1.0

Setup:
  1. Go to https://lunarcrush.com/pricing → subscribe (Starter ~$29/mo)
  2. Your existing API key is already in .env as LUNARCRUSH_API_KEY
  3. Restart the bots — social scoring activates automatically

The `// VIRALITY` dashboard slider controls how the score influences trades
even without LunarCrush active — it still uses the on-chain viral score from
dexscreener.py (volume, buy pressure, pair age, FDV).
"""
import asyncio
import json
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("LUNARCRUSH_API_KEY", "").strip()
ENABLED = bool(API_KEY)

MCP_BASE   = "https://lunarcrush.ai"
_TTL       = 900   # 15-minute cache
_LOCK = None  # type: asyncio.Lock

_cache:    dict  = {}   # symbol.lower() → float (0–1)
_cache_ts: float = 0.0
_subscribed = None   # None=unknown, False=no sub, True=active


def _get_lock() -> asyncio.Lock:
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


async def _mcp_session(client: httpx.AsyncClient):
    """Open an MCP SSE session. Returns (session_id, post_url) or (None, None)."""
    try:
        async with client.stream("GET", f"{MCP_BASE}/sse?key={API_KEY}") as sse:
            async for line in sse.aiter_lines():
                if "sessionId=" in line:
                    data = line.split("data:", 1)[-1].strip()
                    session_id = data.split("sessionId=")[1].strip()
                    post_url = f"{MCP_BASE}/sse/message?key={API_KEY}&sessionId={session_id}"
                    return session_id, post_url
    except Exception as e:
        print(f"[LunarCrush] SSE connect error: {e}", flush=True)
    return None, None


async def _mcp_call(tool: str, arguments: dict):
    """
    Execute one MCP tool/call, returning the parsed result dict.
    Opens a fresh SSE session per call (simple and stateless).
    """
    timeout = httpx.Timeout(connect=8, read=20, write=8, pool=8)
    results: dict = {}
    got_result = asyncio.Event()

    async def _reader(session_id: str):
        try:
            async with httpx.AsyncClient(timeout=timeout) as rc:
                async with rc.stream(
                    "GET", f"{MCP_BASE}/sse?key={API_KEY}&sessionId={session_id}"
                ) as sse:
                    async for line in sse.aiter_lines():
                        if line.startswith("data:") and "{" in line:
                            try:
                                d = json.loads(line[5:].strip())
                                if d.get("id") in (1, 99):
                                    results[d["id"]] = d
                                    if 99 in results:
                                        got_result.set()
                                        return
                            except Exception:
                                pass
        except Exception:
            got_result.set()   # unblock sender even on read error

    async def _sender(post_url: str):
        await asyncio.sleep(0.4)
        try:
            async with httpx.AsyncClient(timeout=8) as pc:
                await pc.post(post_url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "vault-bot", "version": "1.0"}},
                })
                await pc.post(post_url, json={
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                })
                await pc.post(post_url, json={
                    "jsonrpc": "2.0", "id": 99,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                })
        except Exception as e:
            print(f"[LunarCrush] POST error: {e}", flush=True)
            got_result.set()

    # Step 1: get session ID (fresh connection)
    session_id = None
    try:
        async with httpx.AsyncClient(timeout=8) as sc:
            async with sc.stream("GET", f"{MCP_BASE}/sse?key={API_KEY}") as sse:
                async for line in sse.aiter_lines():
                    if "sessionId=" in line:
                        data = line.split("data:", 1)[-1].strip()
                        session_id = data.split("sessionId=")[1].strip()
                        break
    except Exception as e:
        print(f"[LunarCrush] Session error: {e}", flush=True)
        return None

    if not session_id:
        return None

    post_url = f"{MCP_BASE}/sse/message?key={API_KEY}&sessionId={session_id}"

    # Step 2: run reader + sender concurrently
    reader_task = asyncio.ensure_future(_reader(session_id))
    sender_task = asyncio.ensure_future(_sender(post_url))
    try:
        await asyncio.wait_for(got_result.wait(), timeout=18)
    except asyncio.TimeoutError:
        print(f"[LunarCrush] Timeout waiting for {tool} response", flush=True)

    reader_task.cancel()
    sender_task.cancel()

    resp = results.get(99)
    if not resp:
        return None

    # Check for subscription error
    content = resp.get("result", {}).get("content", [])
    for item in content:
        text = item.get("text", "")
        if "Subscription required" in text or "subscription" in text.lower():
            return {"_subscription_required": True}
        try:
            return json.loads(text)
        except Exception:
            pass
    return None


async def _build_cache(http_client: httpx.AsyncClient) -> None:
    """Fetch Solana + pump.fun token social scores and rebuild the cache."""
    global _cache, _cache_ts, _subscribed

    new_cache: dict = {}
    for sector in ("solana-ecosystem", "pump-fun"):
        result = await _mcp_call("cryptocurrencies", {
            "sector": sector,
            "sort": "galaxy_score",
            "limit": 500,
        })
        if result is None:
            continue
        if result.get("_subscription_required"):
            _subscribed = False
            print(
                "[LunarCrush] No active subscription — social scoring disabled. "
                "Upgrade at lunarcrush.com/pricing to activate.",
                flush=True,
            )
            return
        _subscribed = True
        coins = result.get("data") or (result if isinstance(result, list) else [])
        for coin in coins:
            sym = (coin.get("symbol") or "").lower().strip()
            if not sym:
                continue
            galaxy = float(coin.get("galaxy_score") or 0) / 100.0
            social = float(coin.get("social_score") or 0) / 100.0
            new_cache[sym] = round(min(1.0, galaxy * 0.60 + social * 0.40), 3)

    if _subscribed:
        _cache    = new_cache
        _cache_ts = time.time()
        print(
            f"[LunarCrush] Cache built — {len(_cache)} Solana+pump.fun tokens",
            flush=True,
        )
    elif _subscribed is None:
        # Both sectors timed out / returned None — stamp the TTL anyway so we
        # don't hammer the API on every single token evaluation for the next 15m.
        _cache_ts = time.time()
        print("[LunarCrush] Cache build failed (timeout) — will retry in 15m", flush=True)


async def get_social_score(client: httpx.AsyncClient, symbol: str) -> float:
    """
    Return a normalised social authenticity score (0.0–1.0) for a token symbol.

      -1.0  → LunarCrush disabled, no subscription, or symbol not tracked
       0.0  → in index, zero social activity
       0.5  → moderate organic social buzz
       1.0  → top-tier social activity (galaxy_score ≈ 100)

    Bulk cache refreshes every 15 min (2 MCP calls for Solana + pump.fun sectors).
    Cache misses fall back to a per-symbol topic lookup.
    """
    if not ENABLED:
        return -1.0
    if not symbol:
        return -1.0
    if _subscribed is False:
        return -1.0   # already confirmed no subscription — skip API

    now = time.time()
    if now - _cache_ts > _TTL:
        async with _get_lock():
            if time.time() - _cache_ts > _TTL:   # double-check after lock
                await _build_cache(client)

    if _subscribed is False:
        return -1.0

    score = _cache.get(symbol.lower(), None)
    if score is not None:
        if score > 0:
            print(f"[LunarCrush] ${symbol}: {score:.3f} (cached)", flush=True)
        return score

    # Cache miss — token not in the Solana/pump.fun bulk cache.
    # We DO NOT do a live per-symbol topic lookup here: that opens an MCP SSE
    # session with an 18s timeout, and score_tokens() calls this sequentially for
    # every uncached discovery token (~120/cycle). With an active subscription that
    # is up to 120 × 18s ≈ 36 min/cycle — it freezes the whole bot before it can
    # even write status.  Negative-cache the miss and return neutral; validation.py
    # treats -1.0 as a neutral 18.75/25 social score, so unknown tokens aren't penalised.
    _cache[symbol.lower()] = -1.0
    return -1.0
