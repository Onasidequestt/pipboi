import asyncio
import time
from typing import Optional
import httpx
from config import HELIUS_RPC_URL, HELIUS_API_URL, HELIUS_API_KEY, MIN_LIQUIDITY_USD, FALLBACK_RPC_URL

# ── Latency-based RPC failover ────────────────────────────────────────────────
# Tracks exponential moving average of Helius round-trip times.
# When EMA exceeds _SLOW_MS, requests race both endpoints simultaneously and
# the first valid response wins — eliminating stall time on degraded Helius.
# EMA decays back toward fast on recovery; the fallback is never billed quota.
_ema_ms: float = 40.0     # start optimistic
_EMA_ALPHA: float = 0.25  # weight on each new sample (higher = faster adaptation)
_SLOW_MS: int = 200        # preemptive-race threshold


async def rpc_post(client: httpx.AsyncClient, payload: dict, timeout: int = 10) -> dict:
    """POST a JSON-RPC payload.

    Fast path  (EMA ≤ 200ms): try Helius with a 200ms hard deadline; on timeout
    or rate-limit immediately fire the public fallback.

    Slow path  (EMA > 200ms): race Helius and public fallback simultaneously;
    return whichever replies first, cancel the other.
    """
    global _ema_ms

    if _ema_ms <= _SLOW_MS:
        # ── Fast path: single request with latency deadline ───────────────────
        t0 = time.monotonic()
        try:
            r = await asyncio.wait_for(
                client.post(HELIUS_RPC_URL, json=payload, timeout=timeout),
                timeout=_SLOW_MS / 1000,
            )
            elapsed = (time.monotonic() - t0) * 1000
            _ema_ms = _ema_ms * (1 - _EMA_ALPHA) + elapsed * _EMA_ALPHA
            data = r.json()
            if data.get("error", {}).get("code") == -32429:
                print("[RPC] Helius rate-limited — using fallback", flush=True)
            else:
                return data
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - t0) * 1000
            _ema_ms = _ema_ms * (1 - _EMA_ALPHA) + elapsed * _EMA_ALPHA
            print(f"[RPC] Helius >{_SLOW_MS}ms (EMA {_ema_ms:.0f}ms) — switching to fallback",
                  flush=True)
        except Exception:
            pass

        try:
            # asyncio.wait_for gives a hard wall-clock cap on the fallback — httpx's own
            # timeout can be bypassed when the server drip-sends data just fast enough to
            # reset the per-read timer (stalled Solana public RPC behaviour observed in
            # production: connection ESTABLISHED for 10+ min with no RPC response).
            r = await asyncio.wait_for(
                client.post(FALLBACK_RPC_URL, json=payload, timeout=timeout),
                timeout=float(timeout),
            )
            return r.json()
        except Exception:
            return {}

    else:
        # ── Slow path: race both endpoints, first valid response wins ─────────
        async def _fetch(url: str) -> dict:
            r = await asyncio.wait_for(
                client.post(url, json=payload, timeout=timeout),
                timeout=float(timeout),
            )
            return r.json()

        tasks = {
            asyncio.ensure_future(_fetch(HELIUS_RPC_URL)):   "helius",
            asyncio.ensure_future(_fetch(FALLBACK_RPC_URL)): "fallback",
        }
        t0 = time.monotonic()
        result: dict = {}
        pending = set(tasks)

        while pending and not result:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    data = task.result()
                    if tasks[task] == "helius" and data.get("error", {}).get("code") == -32429:
                        continue  # rate-limited — let fallback win
                    result = data
                    break
                except Exception:
                    pass

        for t in pending:
            t.cancel()

        elapsed = (time.monotonic() - t0) * 1000
        _ema_ms = _ema_ms * (1 - _EMA_ALPHA) + elapsed * _EMA_ALPHA
        return result


async def get_token_price(client: httpx.AsyncClient, mint: str) -> Optional[float]:
    """Fetch USD price for a token via Helius RPC (getTokenSupply + market data)."""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAsset",
            "params": {"id": mint},
        }
        r = await client.post(HELIUS_RPC_URL, json=payload, timeout=10)
        r.raise_for_status()
        result = r.json().get("result", {})
        token_info = result.get("token_info", {})
        price_info = token_info.get("price_info", {})
        return price_info.get("price_per_token")
    except Exception:
        return None


async def get_token_prices_batch(client: httpx.AsyncClient, mints: list[str]) -> dict[str, float]:
    """Batch fetch prices for multiple mints."""
    prices: dict[str, float] = {}
    for mint in mints:
        price = await get_token_price(client, mint)
        if price is not None:
            prices[mint] = price
    return prices


async def get_liquidity(client: httpx.AsyncClient, mint: str) -> float:
    """
    Estimate pool liquidity for a token by querying Helius enhanced transaction
    history and token largest accounts as a proxy. Returns USD liquidity estimate.
    """
    try:
        url = f"{HELIUS_API_URL}/v0/token-metadata?api-key={HELIUS_API_KEY}"
        r = await client.post(url, json={"mintAccounts": [mint]}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data and isinstance(data, list):
            meta = data[0]
            # Use onChainMetadata supply as a rough proxy if no direct liquidity field
            supply = meta.get("onChainInfo", {}).get("supply", 0)
            # Return a placeholder — replace with Birdeye/DexScreener if needed
            return float(supply) if supply else 0.0
    except Exception:
        pass
    return 0.0


async def is_liquid_enough(client: httpx.AsyncClient, mint: str) -> bool:
    liq = await get_liquidity(client, mint)
    return liq >= MIN_LIQUIDITY_USD


async def get_recent_transactions(
    client: httpx.AsyncClient, mint: str, limit: int = 20
) -> list[dict]:
    """Fetch recent transactions for a mint to estimate volume."""
    try:
        url = f"{HELIUS_API_URL}/v0/addresses/{mint}/transactions?api-key={HELIUS_API_KEY}&limit={limit}"
        r = await client.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []
