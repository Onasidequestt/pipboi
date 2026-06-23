"""PIPE12 (R-C1) — Jupiter Ultra buy adapter, wired behind a default-OFF Bot1 canary [execwire].

Jupiter Ultra (/order + /execute) replaces the hand-rolled lite-api quote + Jito-bundle path:
RPC-less, gasless, the Beam landing engine (0–1 block / ~50–400ms, no Jito "cannot lock vote
accounts" class), and RTSE (Real-Time Slippage Estimator) instead of manual slippage. Fee 5–10 bps.

STATUS: wired into main.execute_buy, but INERT until the per-bot canary
`bots/botN/ultra_exec.json {"enabled": true}` exists — `ultra_exec_on()` returns False with no
file, so the buy path is byte-for-byte the lite-api path. A/B: enable Bot1 only, keep Bot2/3 on
lite-api, and measure landed-rate / realized-vs-quoted slippage / entry-miss Bot1 vs Bot2/3
(`research/pipe12/exec_audit.py`). On any Ultra order/execute failure the caller FALLS BACK to the
lite-api build/sign/send path → an Ultra hiccup never aborts an entry (recover the mover, log it).

Canary knobs (`bots/botN/ultra_exec.json`):
  {"enabled": true}                 → route buys through Ultra, RTSE auto-slippage (Ultra's headline)
  {"enabled": true, "slippage_bps": 300}  → cap slippage at 3% instead of letting RTSE decide

Docs: https://developers.jup.ag/docs/ultra
Revert: `rm bots/botN/ultra_exec.json` (hot, ≤30s — code goes inert) · restore the .bak.execwire.*
"""
import os, json, base64, time
from pathlib import Path

try:
    from bot_config import DATA_DIR
except Exception:
    DATA_DIR = Path(os.path.expanduser("~/solana-trader"))

# Ultra is keyless on the free tier (fee is taken in-swap); api.jup.ag hosts /ultra/v1.
_ULTRA_ORDER   = os.getenv("JUPITER_ULTRA_ORDER",   "https://api.jup.ag/ultra/v1/order")
_ULTRA_EXECUTE = os.getenv("JUPITER_ULTRA_EXECUTE", "https://api.jup.ag/ultra/v1/execute")
_ULTRA_CANARY  = DATA_DIR / "ultra_exec.json"
_cfg_cache     = (0.0, {})   # (monotonic_ts, parsed_dict)


def _ultra_cfg() -> dict:
    """Parsed canary dict (cached 30s). {} when the file is absent/unreadable → OFF."""
    global _cfg_cache
    if time.time() - _cfg_cache[0] < 30.0:
        return _cfg_cache[1]
    cfg = {}
    try:
        cfg = json.loads(_ULTRA_CANARY.read_text())
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}
    _cfg_cache = (time.time(), cfg)
    return cfg


def ultra_exec_on() -> bool:
    """True when THIS bot should route buys through Jupiter Ultra (canary). Default OFF."""
    return bool(_ultra_cfg().get("enabled"))


async def ultra_order(client, input_mint: str, output_mint: str, amount_lamports: int,
                      taker_pubkey: str, slippage_bps=None) -> "dict | None":
    """GET /ultra/v1/order — aggregates liquidity, RTSE slippage, returns an unsigned tx.
    Returns the order dict (carries `transaction` base64 + `requestId`) or None.
    slippage_bps None → let RTSE decide (Ultra's headline feature)."""
    params = {"inputMint": input_mint, "outputMint": output_mint,
              "amount": str(int(amount_lamports)), "taker": taker_pubkey}
    if slippage_bps is not None:
        params["slippageBps"] = str(int(slippage_bps))   # else RTSE decides
    try:
        r = await client.get(_ULTRA_ORDER, params=params, timeout=15)
        if r.status_code != 200:
            print(f"[Ultra] order {r.status_code}: {r.text[:120]}", flush=True)
            return None
        d = r.json()
        if not d or "transaction" not in d or "requestId" not in d:
            print(f"[Ultra] order missing transaction/requestId: {str(d)[:120]}", flush=True)
            return None
        return d
    except Exception as e:
        print(f"[Ultra] order error: {e}", flush=True)
        return None


async def ultra_execute(client, order: dict, keypair) -> "tuple[str | None, dict]":
    """POST /ultra/v1/execute — sign the order's tx and let Beam land it.
    Returns (signature, response_dict). signature is None on any failure (caller falls back).
    Beam returns status=Success only once the tx is landed → no separate confirm needed, though
    the caller still polls confirm_transaction(sig) for the on-chain status (idempotent)."""
    if not order or "transaction" not in order or "requestId" not in order:
        return None, {}
    try:
        import wallet  # lazy: avoids a solders import at module load (wallet does not import this)
        signed = wallet.sign_transaction(keypair, order["transaction"])
        signed_b64 = base64.b64encode(bytes(signed)).decode("ascii")
        r = await client.post(_ULTRA_EXECUTE,
                              json={"signedTransaction": signed_b64, "requestId": order["requestId"]},
                              timeout=30)
        try:
            d = r.json()
        except Exception:
            print(f"[Ultra] execute {r.status_code}: non-JSON {r.text[:120]}", flush=True)
            return None, {}
        sig = d.get("signature")
        ok = (d.get("status") == "Success") or (sig and not d.get("error") and d.get("status") != "Failed")
        if ok and sig:
            return sig, d
        print(f"[Ultra] execute not-success: status={d.get('status')} "
              f"code={d.get('code')} err={str(d.get('error'))[:100]}", flush=True)
        return None, d
    except Exception as e:
        print(f"[Ultra] execute error: {e}", flush=True)
        return None, {}


async def ultra_buy(client, keypair, input_mint: str, output_mint: str,
                    amount_lamports: int, slippage_bps=None) -> "tuple[str | None, dict]":
    """Convenience: order → sign → execute. Returns (signature, meta) or (None, {}) on failure.

    `meta` carries a few read-only fields for the A/B (no behaviour use): realized in/out amounts
    and the order's quoted slippage so exec_audit can compare executed-vs-quoted. slippage_bps None
    falls back to the canary's `slippage_bps` (if set), else RTSE."""
    if slippage_bps is None:
        slippage_bps = _ultra_cfg().get("slippage_bps")  # None → RTSE
    order = await ultra_order(client, input_mint, output_mint, amount_lamports,
                              str(keypair.pubkey()), slippage_bps=slippage_bps)
    if not order:
        return None, {}
    sig, resp = await ultra_execute(client, order, keypair)
    if not sig:
        return None, {}
    meta = {
        "via": "ultra",
        "quoted_out": order.get("outAmount"),
        "out_result": resp.get("outputAmountResult") or resp.get("totalOutputAmount"),
        "slippage_bps": order.get("slippageBps"),
        "slot": resp.get("slot"),
    }
    return sig, meta
