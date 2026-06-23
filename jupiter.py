import asyncio
import random
from typing import Optional
import httpx
from config import JUPITER_QUOTE_URL, JUPITER_PRICE_URL, MAX_SLIPPAGE_BPS, STOIC_SLIPPAGE_BPS, USDC_MINT

# Dual-sample quote constants — used by measure_price_impact()
_PROBE_FRACTION       = 0.10   # probe at 10% of target size
_MIN_PROBE_LAMPORTS   = 1_000_000   # floor: 0.001 SOL minimum probe

# S89: 429-aware retry/backoff for the execution path. S87's 6–8× throughput across 3 bots
# (each buy = dual-sample quote + fresh re-quote + swap-build) saturated the Jupiter free-tier
# rate limit → ~60% of swaps failed on 429 (Failed to get quote / build swap). These raw calls
# had NO retry, so a transient 429 = a missed trade. Retry honours Retry-After, else exponential
# backoff (capped) with jitter. Pure upside: only fires on failure, leaves the happy path untouched.
_JUP_MAX_RETRIES   = 3
_JUP_BASE_BACKOFF  = 0.4   # seconds; doubles per attempt
_JUP_MAX_BACKOFF   = 3.0   # cap so a sell isn't delayed too long (the exit loop also re-tries)


async def _jup_request_with_retry(make_request, label: str):
    """Run an async request factory with 429/transient retry. `make_request` returns a fresh
    awaitable httpx.Response each call (so we can re-issue). Raises the last error if all fail."""
    last_exc: Optional[Exception] = None
    for attempt in range(_JUP_MAX_RETRIES):
        try:
            r = await make_request()
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            last_exc = e
            code = e.response.status_code if e.response is not None else 0
            if code == 429 and attempt < _JUP_MAX_RETRIES - 1:
                ra = e.response.headers.get("Retry-After") if e.response is not None else None
                try:
                    delay = float(ra) if ra else _JUP_BASE_BACKOFF * (2 ** attempt)
                except (TypeError, ValueError):
                    delay = _JUP_BASE_BACKOFF * (2 ** attempt)
                await asyncio.sleep(min(delay, _JUP_MAX_BACKOFF) + random.uniform(0, 0.2))
                continue
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt < _JUP_MAX_RETRIES - 1:
                await asyncio.sleep(_JUP_BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 0.2))
                continue
            raise
    if last_exc:
        raise last_exc

# S75: last error from build_swap_transaction (reason surfaced to the buy path
# for no-route-vs-transient diagnosis). None when the last build succeeded.
last_build_error: Optional[str] = None


async def get_prices(client: httpx.AsyncClient, mints: list[str]) -> dict[str, float]:
    """Fetch USD prices for a list of mints from Jupiter Price API v2."""
    ids = ",".join(mints)
    try:
        r = await client.get(
            f"{JUPITER_PRICE_URL}",
            params={"ids": ids},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        return {mint: float(info["price"]) for mint, info in data.items() if info}
    except Exception:
        return {}


async def get_quote(
    client: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = MAX_SLIPPAGE_BPS,
) -> Optional[dict]:
    """Get a swap quote from Jupiter."""
    try:
        r = await _jup_request_with_retry(
            lambda: client.get(
                f"{JUPITER_QUOTE_URL}/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": amount_lamports,
                    "slippageBps": slippage_bps,
                    "onlyDirectRoutes": False,
                },
                timeout=10,
            ),
            "quote",
        )
        return r.json()
    except Exception as e:
        print(f"[Jupiter] Quote error: {e}")
        return None


async def measure_price_impact(
    client: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    target_lamports: int,
    slippage_bps: int = MAX_SLIPPAGE_BPS,
) -> tuple[float, Optional[dict]]:
    """
    Dual-sample quote: compare output-per-lamport at probe size vs target size to
    measure the real-time price impact of the full order on the pool.

    Returns (impact_pct, target_quote):
      impact_pct   — % degradation from probe rate to target rate (0.0 on error/fallback)
      target_quote — full-size Jupiter quote ready to use if impact is acceptable

    Method:
      probe_rate  = probe_outAmount  / probe_inAmount   (output per lamport at 10% size)
      target_rate = target_outAmount / target_inAmount  (output per lamport at full size)
      impact_pct  = (probe_rate - target_rate) / probe_rate × 100

    Falls back to Jupiter's built-in priceImpactPct when the probe quote fails, and
    to 0.0 (atomic swap permitted) when neither is available.
    """
    probe_lamports = max(int(target_lamports * _PROBE_FRACTION), _MIN_PROBE_LAMPORTS)

    if probe_lamports >= target_lamports:
        # Position too small to probe meaningfully — single call, trust Jupiter's own figure
        target_quote = await get_quote(client, input_mint, output_mint, target_lamports, slippage_bps)
        if target_quote is None:
            return 0.0, None
        return float(target_quote.get("priceImpactPct", 0.0)), target_quote

    # Probe and target are independent — fetch both in parallel
    target_quote, base_quote = await asyncio.gather(
        get_quote(client, input_mint, output_mint, target_lamports, slippage_bps),
        get_quote(client, input_mint, output_mint, probe_lamports, slippage_bps),
    )
    if target_quote is None:
        return 0.0, None
    if base_quote is None:
        # Probe failed — fall back to Jupiter's priceImpactPct
        return float(target_quote.get("priceImpactPct", 0.0)), target_quote

    try:
        base_in    = int(base_quote["inAmount"])
        base_out   = int(base_quote["outAmount"])
        target_in  = int(target_quote["inAmount"])
        target_out = int(target_quote["outAmount"])

        if base_in <= 0 or target_in <= 0 or base_out <= 0:
            return float(target_quote.get("priceImpactPct", 0.0)), target_quote

        base_rate   = base_out   / base_in    # tokens per lamport at probe size
        target_rate = target_out / target_in  # tokens per lamport at target size

        # Positive impact_pct = target gets worse output per lamport = price moved against us
        impact_pct = (base_rate - target_rate) / base_rate * 100
        return round(max(0.0, impact_pct), 3), target_quote
    except Exception as e:
        print(f"[Jupiter] Impact calc error: {e}")
        return float(target_quote.get("priceImpactPct", 0.0)), target_quote


async def build_swap_transaction(
    client: httpx.AsyncClient,
    quote: dict,
    user_pubkey: str,
) -> Optional[str]:
    """Build a serialized swap transaction from a Jupiter quote."""
    global last_build_error
    try:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
            "dynamicSlippage": True,   # let Jupiter calculate optimal slippage
        }
        r = await _jup_request_with_retry(
            lambda: client.post(
                f"{JUPITER_QUOTE_URL}/swap",
                json=payload,
                timeout=15,
            ),
            "swap",
        )
        last_build_error = None
        return r.json().get("swapTransaction")
    except Exception as e:
        # S75: capture the reason so the buy path can tell a dead-route token
        # (no liquidity → correctly skip) from a transient HTTP/timeout error.
        # On HTTP errors, the response body carries Jupiter's actual errorCode.
        _detail = str(e)
        try:
            if hasattr(e, "response") and e.response is not None:
                _detail = f"{e} :: {e.response.text[:200]}"
        except Exception:
            pass
        last_build_error = _detail[:240]
        print(f"[Jupiter] Swap build error: {_detail}")
        return None
