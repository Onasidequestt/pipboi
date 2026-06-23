#!/usr/bin/env python3
"""execwire — offline tests for the Jupiter Ultra buy adapter. No network, no real keypair.

Covers: default-OFF inert (no canary → ultra_exec_on False), canary on/slippage read, /order param
shaping + RTSE (no slippageBps when None), /execute success/failed/non-JSON parsing, and ultra_buy
order→execute happy path + every failure mode returning (None, {}) so the caller falls back.
Run: python3 test_ultra_exec.py
"""
import asyncio, json, base64, tempfile, os
from pathlib import Path

import jupiter_ultra as ju

PASS = 0; FAIL = 0
def ok(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {name}")
    else:    FAIL += 1; print(f"  ✗ {name}")


# ── fakes ──────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status_code=200, payload=None, text="", raise_json=False):
        self.status_code = status_code; self._payload = payload; self.text = text
        self._raise_json = raise_json
    def json(self):
        if self._raise_json: raise ValueError("non-json")
        return self._payload

class FakeClient:
    """Records the last GET/POST and returns scripted responses."""
    def __init__(self, order_resp=None, exec_resp=None):
        self.order_resp = order_resp; self.exec_resp = exec_resp
        self.last_get = None; self.last_post = None
    async def get(self, url, params=None, timeout=None):
        self.last_get = (url, params); return self.order_resp
    async def post(self, url, json=None, timeout=None):
        self.last_post = (url, json); return self.exec_resp

class FakePub:
    def __str__(self): return "FakeTakerPubkey1111111111111111111111111111"
class FakeKeypair:
    def pubkey(self): return FakePub()

# stub wallet.sign_transaction (avoid solders / real signing)
import wallet
class _FakeSigned:
    def __bytes__(self): return b"signed-tx-bytes"
wallet.sign_transaction = lambda kp, txb64: _FakeSigned()


def _set_canary(tmp, content):
    ju._ULTRA_CANARY = Path(tmp) / "ultra_exec.json"
    ju._cfg_cache = (0.0, {})   # bust cache
    if content is None:
        try: ju._ULTRA_CANARY.unlink()
        except FileNotFoundError: pass
    else:
        ju._ULTRA_CANARY.write_text(json.dumps(content))


async def main():
    tmp = tempfile.mkdtemp()

    # 1) default-OFF: no canary file → inert
    _set_canary(tmp, None)
    ok("no canary → ultra_exec_on() False", ju.ultra_exec_on() is False)
    _set_canary(tmp, {"enabled": False})
    ok("enabled:false → OFF", ju.ultra_exec_on() is False)
    _set_canary(tmp, {"enabled": True})
    ok("enabled:true → ON", ju.ultra_exec_on() is True)
    _set_canary(tmp, {"enabled": True, "slippage_bps": 300})
    ok("slippage_bps read from canary", ju._ultra_cfg().get("slippage_bps") == 300)
    ok("corrupt canary → {} (OFF)", (_set_canary(tmp, None), (Path(tmp)/'ultra_exec.json').write_text("{bad"), ju.__setattr__('_cfg_cache',(0.0,{})), ju.ultra_exec_on())[-1] is False)

    # 2) /order param shaping
    order_dict = {"transaction": "b64tx", "requestId": "req-1", "outAmount": "1000", "slippageBps": 50}
    c = FakeClient(order_resp=_Resp(200, order_dict))
    o = await ju.ultra_order(c, "INMINT", "OUTMINT", 12345, "TAKER", slippage_bps=None)
    ok("order returns dict on 200", o == order_dict)
    ok("order GET hit /order url", c.last_get[0].endswith("/ultra/v1/order"))
    ok("RTSE: no slippageBps param when None", "slippageBps" not in c.last_get[1])
    ok("order amount stringified", c.last_get[1]["amount"] == "12345")
    c2 = FakeClient(order_resp=_Resp(200, order_dict))
    await ju.ultra_order(c2, "I", "O", 9, "T", slippage_bps=300)
    ok("slippageBps param present when set", c2.last_get[1].get("slippageBps") == "300")
    # order failure modes
    ok("order non-200 → None", await ju.ultra_order(FakeClient(order_resp=_Resp(429, None, "rate")), "I","O",1,"T") is None)
    ok("order missing requestId → None",
       await ju.ultra_order(FakeClient(order_resp=_Resp(200, {"transaction": "x"})), "I","O",1,"T") is None)

    # 3) /execute parsing
    sig, resp = await ju.ultra_execute(FakeClient(exec_resp=_Resp(200, {"status":"Success","signature":"SIG123","slot":42})),
                                       order_dict, FakeKeypair())
    ok("execute Success → sig", sig == "SIG123")
    sig, _ = await ju.ultra_execute(FakeClient(exec_resp=_Resp(200, {"status":"Failed","error":"slippage"})),
                                    order_dict, FakeKeypair())
    ok("execute Failed → None", sig is None)
    sig, _ = await ju.ultra_execute(FakeClient(exec_resp=_Resp(200, None, raise_json=True)),
                                    order_dict, FakeKeypair())
    ok("execute non-JSON → None", sig is None)
    sig, _ = await ju.ultra_execute(FakeClient(), {"transaction":"x"}, FakeKeypair())  # bad order
    ok("execute bad-order → None", sig is None)
    # execute POST payload shape
    c3 = FakeClient(exec_resp=_Resp(200, {"status":"Success","signature":"S"}))
    await ju.ultra_execute(c3, order_dict, FakeKeypair())
    _, body = c3.last_post
    ok("execute POST carries requestId", body.get("requestId") == "req-1")
    ok("execute POST signedTransaction is b64", base64.b64decode(body["signedTransaction"]) == b"signed-tx-bytes")

    # 4) ultra_buy end-to-end
    cbuy = FakeClient(order_resp=_Resp(200, order_dict),
                      exec_resp=_Resp(200, {"status":"Success","signature":"BUYSIG","outputAmountResult":"990","slot":7}))
    sig, meta = await ju.ultra_buy(cbuy, FakeKeypair(), "I", "O", 5000)
    ok("ultra_buy happy → sig", sig == "BUYSIG")
    ok("ultra_buy meta via=ultra", meta.get("via") == "ultra")
    ok("ultra_buy meta out_result", meta.get("out_result") == "990")
    # buy: order fails → (None,{})
    sig, meta = await ju.ultra_buy(FakeClient(order_resp=_Resp(500, None, "err")), FakeKeypair(), "I","O",1)
    ok("ultra_buy order-fail → (None,{})", sig is None and meta == {})
    # buy: order ok but execute fails → (None,{})
    sig, meta = await ju.ultra_buy(FakeClient(order_resp=_Resp(200, order_dict),
                                              exec_resp=_Resp(200, {"status":"Failed"})), FakeKeypair(), "I","O",1)
    ok("ultra_buy execute-fail → (None,{})", sig is None and meta == {})

    print(f"\n  {PASS} passed, {FAIL} failed")
    return FAIL


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
