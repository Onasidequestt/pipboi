#!/usr/bin/env python3
"""Tests for bitquery_service.py (the Bitquery v2 GraphQL candidate feed). No network: the
canary-off path + record-shape contract + parsing are exercised with stubs."""
import asyncio, json, os, tempfile, time
import bitquery_service as B

_pass = _fail = 0
def ok(c, m):
    global _pass, _fail
    if c: _pass += 1
    else: _fail += 1; print("  FAIL:", m)

# 1) default-OFF: no canary file → inert (byte-identical to before; never raises)
B._CANARY_CACHE.update(ts=-1e9, val={})
ok(B.bitquery_on(999) is False, "OFF without bots/bot999/bitquery.json")

async def _off_returns_empty():
    class _C:  # a client that must never be called when OFF
        async def post(self, *a, **k): raise AssertionError("must not query when OFF")
    m, p = await B.fetch_candidates(_C())
    ok(m == [] and p == {}, "OFF fetch_candidates → ([],{})")
asyncio.run(_off_returns_empty())

# 2) record shape carries every key downstream scoring reads (no KeyError in the observer)
rec = B._empty_record("MintABC", "SYM", 0.00123, 7, 2)
need = {"mint","symbol","price_usd","liquidity_usd","volume_5m","volume_1h","volume_6h","volume_24h",
        "price_change_5m","price_change_1h","price_change_6h","price_change_24h",
        "txns_5m_buys","txns_5m_sells","txns_1h_buys","txns_1h_sells","txns_24h_buys","txns_24h_sells",
        "fdv","market_cap","pair_age_hours","pair_address","dex"}
ok(need <= set(rec), "record missing keys: %s" % (need - set(rec)))
ok(rec["dex"] == "bitquery" and rec["liquidity_usd"] == 0.0, "dex tag + liq=0 (DexScreener enriches)")
ok(rec["txns_5m_buys"] == 7 and rec["txns_5m_sells"] == 2, "buy/sell flow preserved")

# 3) canary ON reading (temp bots dir) → bitquery_on True only with {"enabled":true} AND a key
with tempfile.TemporaryDirectory() as d:
    os.makedirs(os.path.join(d, "bots", "bot1"))
    open(os.path.join(d, "bots", "bot1", "bitquery.json"), "w").write('{"enabled":true}')
    orig_root, orig_key = B._ROOT, B._key
    import pathlib
    B._ROOT = pathlib.Path(d)
    B._key = lambda: "test-key"                       # force a key present
    B._CANARY_CACHE.update(ts=-1e9, val={})
    ok(B.bitquery_on(1) is True, "canary ON + key → bitquery_on True")
    B._key = lambda: ""                               # no key → OFF regardless of canary
    B._CANARY_CACHE.update(ts=-1e9, val={})
    ok(B.bitquery_on(1) is False, "no key → OFF even with canary enabled")
    B._ROOT, B._key = orig_root, orig_key

# 4) trade aggregation: a buy (memecoin on Buy side) and a sell parse to the right mint+flow
async def _agg():
    MEME = "MeMeMint1111111111111111111111111111111111"
    rows = [
        {"Trade": {"Buy": {"PriceInUSD": 0.01, "Currency": {"MintAddress": MEME, "Symbol": "MM"}},
                   "Sell": {"Currency": {"MintAddress": B.WSOL, "Symbol": "WSOL"}}}},
        {"Trade": {"Buy": {"Currency": {"MintAddress": B.WSOL, "Symbol": "WSOL"}},
                   "Sell": {"PriceInUSD": 0.01, "Currency": {"MintAddress": MEME, "Symbol": "MM"}}}},
    ]
    class _Resp:
        status_code = 200
        def json(self): return {"data": {"Solana": {"DEXTrades": rows}}}
    class _C:
        async def post(self, *a, **k): return _Resp()
    B._ROOT = __import__("pathlib").Path(os.path.dirname(os.path.abspath(__file__)))
    B._key = lambda: "test-key"
    B._CANARY_CACHE.update(ts=time.monotonic(), val={"enabled": True})
    B._RESULT_CACHE.update(ts=-1e9, mints=[], pool={})
    m, p = await B.fetch_candidates(_C())
    ok(MEME in p, "memecoin mint surfaced from buy+sell")
    ok(B.WSOL not in p and B._SYS not in p, "WSOL/system filtered out")
    ok(p[MEME]["txns_5m_buys"] == 1 and p[MEME]["txns_5m_sells"] == 1, "1 buy + 1 sell aggregated")
asyncio.run(_agg())

print("\n  %d passed, %d failed" % (_pass, _fail))
print("  ALL PASS" if _fail == 0 else "  *** FAILURES ***")
raise SystemExit(1 if _fail else 0)
