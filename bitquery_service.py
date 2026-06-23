"""
bitquery_service.py — Bitquery v2 GraphQL real-time candidate source (canary-gated, fail-soft).

The $0 un-starve + freshness + flow upgrade proven in research/sol_strategy_2026 (SOL-STRATEGY-2026):
the free v2 Solana GraphQL feed surfaces ~2× GeckoTerminal's mint coverage in a ~2s window at ~0s lag,
WITH per-mint real-time buy/sell flow GeckoTerminal's count-aggregates can't give. This feeds the
candidate stream the fresh, actively-bought runner mints the moment they start trading.

DISCIPLINE (mirrors dex_discovery.py — the S119/S121 pattern):
  - DEFAULT-OFF: bitquery_on() is the single gate. No bots/botN/bitquery.json ⇒ every fn no-ops.
  - FAIL-SOFT: any HTTP/parse error returns ([], {}) — it can NEVER raise into the discovery loop.
  - Emits the EXACT candidate dict shape discovery._normalize_gecko_pool produces (dex="bitquery"),
    so it folds into gecko_pool_data and DexScreener enriches liquidity/momentum downstream
    (a too-fresh mint with no liquidity correctly scores 0 — safe).
  - Self-throttled (one query / ≥10s, semaphore) so the free tier is never hammered.
  - NOT ev_sizing, NOT a policy/admission change — purely a candidate FEED. THE ONE OPERATOR RULE +
    S110 freeze + S121 allowlist all stay in force (the allowlist still decides what trades).

Auth: the operator's key is an X-API-KEY (v1/v2 key, not an ory_at_ CoreCast token) → v2 GraphQL
via the X-API-KEY header. Key read from env BITQUERY_API_KEY / BITQUERY_TOKEN, or ~/solana-trader/.env.
"""
from __future__ import annotations
import os, json, time, asyncio
from pathlib import Path

try:
    import httpx
except Exception:
    httpx = None

_ROOT = Path(__file__).resolve().parent
_URL = "https://streaming.bitquery.io/graphql"
WSOL = "So11111111111111111111111111111111111111112"
_SYS = "11111111111111111111111111111111"
_STABLES = {WSOL, _SYS, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
# memecoin venues (pump_amm + raydium carry the fresh-runner flow)
_PROGRAMS = ["pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
             "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"]

_CANARY_CACHE = {"ts": -1e9, "val": {}}     # -1e9 sentinel: the S121/S127 monotonic cold-start lesson
_KEY_CACHE = {"ts": -1e9, "key": ""}
_RESULT_CACHE = {"ts": -1e9, "mints": [], "pool": {}}
_MIN_INTERVAL = 60.0                        # ≥60s between live queries — conserve the free points budget
                                            # (fail-soft to GeckoTerminal if/when the monthly budget exhausts)
_SEM = asyncio.Semaphore(1)


def _bot_id() -> int:
    try:
        return int(os.getenv("BOT_ID", "1"))
    except Exception:
        return 1


def _key() -> str:
    now = time.monotonic()
    if now - _KEY_CACHE["ts"] < 60 and _KEY_CACHE["key"]:
        return _KEY_CACHE["key"]
    k = (os.environ.get("BITQUERY_API_KEY") or os.environ.get("BITQUERY_TOKEN") or "").strip()
    if not k:
        try:
            envp = _ROOT / ".env"
            if envp.exists():
                for l in envp.read_text().splitlines():
                    for nm in ("BITQUERY_API_KEY=", "BITQUERY_TOKEN="):
                        if l.strip().startswith(nm):
                            k = l.split("=", 1)[1].strip().strip('"').strip("'"); break
                    if k:
                        break
        except Exception:
            k = ""
    _KEY_CACHE.update(ts=now, key=k)
    return k


def bitquery_on(bot_id: int | None = None) -> bool:
    """The single gate: a key present AND bots/botN/bitquery.json {"enabled":true}. 30s cache. Fail-OFF."""
    if httpx is None or not _key():
        return False
    now = time.monotonic()
    if now - _CANARY_CACHE["ts"] >= 30.0:
        val = {}
        try:
            fp = _ROOT / f"bots/bot{bot_id if bot_id is not None else _bot_id()}/bitquery.json"
            if fp.exists():
                val = json.loads(fp.read_text()) or {}
        except Exception:
            val = {}
        _CANARY_CACHE.update(ts=now, val=val)
    return bool(_CANARY_CACHE["val"].get("enabled"))


def _empty_record(mint: str, symbol: str, price_usd: float, buys: int, sells: int) -> dict:
    """The discovery._normalize_gecko_pool shape, dex='bitquery'. Liquidity/momentum 0 → DexScreener
    enriches downstream; a mint DexScreener can't price stays score-0 (correct, safe)."""
    return {
        "mint": mint, "symbol": symbol or "", "price_usd": price_usd or 0.0,
        "liquidity_usd": 0.0, "volume_5m": 0.0, "volume_1h": 0.0, "volume_6h": 0.0, "volume_24h": 0.0,
        "price_change_5m": 0.0, "price_change_1h": 0.0, "price_change_6h": 0.0, "price_change_24h": 0.0,
        "txns_5m_buys": int(buys), "txns_5m_sells": int(sells),
        "txns_1h_buys": int(buys), "txns_1h_sells": int(sells),
        "txns_24h_buys": int(buys), "txns_24h_sells": int(sells),
        "fdv": 0.0, "market_cap": 0.0, "pair_age_hours": 0.0, "pair_address": "",
        "dex": "bitquery", "has_socials": False, "has_website": False, "paid_boost_active": False,
    }


_TRADES_Q = """{ Solana { DEXTrades(limit: {count: %d}, orderBy: {descending: Block_Time},
  where: {Trade: {Dex: {ProgramAddress: {in: [%s]}}}}) {
  Trade {
    Buy  { PriceInUSD Currency { MintAddress Symbol } }
    Sell { PriceInUSD Currency { MintAddress Symbol } }
  } } } }"""


async def fetch_candidates(client, count: int = 300, max_n: int = 120) -> tuple:
    """Bitquery v2 GraphQL real-time trade flow → (mints, pool) in discovery's candidate shape.
    Ranked by buy-pressure (the runner signal). Fail-soft → ([], {}). 10s result cache."""
    if not bitquery_on():
        return [], {}
    now = time.monotonic()
    if now - _RESULT_CACHE["ts"] < _MIN_INTERVAL:
        return list(_RESULT_CACHE["mints"]), dict(_RESULT_CACHE["pool"])
    key = _key()
    if httpx is None or not key:
        return [], {}
    progs = ", ".join('"%s"' % p for p in _PROGRAMS)
    q = _TRADES_Q % (count, progs)
    try:
        async with _SEM:
            r = await client.post(_URL, json={"query": q},
                                  headers={"X-API-KEY": key, "Content-Type": "application/json"},
                                  timeout=20.0)
        if r.status_code != 200:
            return list(_RESULT_CACHE["mints"]), dict(_RESULT_CACHE["pool"])
        data = (r.json() or {}).get("data") or {}
        rows = ((data.get("Solana") or {}).get("DEXTrades")) or []
    except Exception:
        return list(_RESULT_CACHE["mints"]), dict(_RESULT_CACHE["pool"])

    agg = {}
    for row in rows:
        tr = row.get("Trade") or {}
        b = tr.get("Buy") or {}; s = tr.get("Sell") or {}
        bc = b.get("Currency") or {}; sc = s.get("Currency") or {}
        bm = bc.get("MintAddress"); sm = sc.get("MintAddress")
        # the memecoin side = whichever is not WSOL/stable/system
        if bm and bm not in _STABLES:
            mint, sym, px, is_buy = bm, bc.get("Symbol"), b.get("PriceInUSD"), True
        elif sm and sm not in _STABLES:
            mint, sym, px, is_buy = sm, sc.get("Symbol"), s.get("PriceInUSD"), False
        else:
            continue
        a = agg.get(mint)
        if a is None:
            a = agg[mint] = {"buys": 0, "sells": 0, "px": 0.0, "sym": sym or ""}
        a["buys" if is_buy else "sells"] += 1
        if px:
            a["px"] = px
        if sym and not a["sym"]:
            a["sym"] = sym

    ranked = sorted(agg.items(),
                    key=lambda kv: ((kv[1]["buys"] / kv[1]["sells"]) if kv[1]["sells"] else float(kv[1]["buys"] or 1),
                                    kv[1]["buys"]), reverse=True)[:max_n]
    pool = {m: _empty_record(m, a["sym"], a["px"], a["buys"], a["sells"]) for m, a in ranked}
    mints = [m for m, _ in ranked]
    _RESULT_CACHE.update(ts=now, mints=mints, pool=pool)
    return mints, pool


if __name__ == "__main__":
    print("httpx:", httpx is not None, "| key:", bool(_key()), "| enabled (canary):", bitquery_on())
    if httpx and _key():
        async def _demo():
            _CANARY_CACHE.update(ts=time.monotonic(), val={"enabled": True})  # bypass canary for the demo
            async with httpx.AsyncClient() as c:
                mints, pool = await fetch_candidates(c)
                print(f"fetched {len(mints)} live candidates (ranked by buy-pressure):")
                for m in mints[:8]:
                    r = pool[m]
                    print(f"  {(r['symbol'] or m[:6])[:14]:14} buys={r['txns_5m_buys']:>3} sells={r['txns_5m_sells']:>3} px={r['price_usd']}")
        asyncio.run(_demo())
