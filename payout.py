"""
Payout module — SOL-native. Tiered profit sweeps back to the original funding wallet.

Bot now accumulates SOL. Payouts are sent in SOL.
Milestones are denominated in SOL value, not USD.

SECURITY:
  - Payout wallet locked ONCE from the first qualifying inbound transfer.
  - NEVER changes. Any attempt to update it is rejected and logged.
"""
import asyncio
import base64
import json
import struct
from pathlib import Path
from typing import Optional

import httpx
from solders.instruction import Instruction, AccountMeta
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import transfer as sol_transfer, TransferParams
from solders.transaction import Transaction
from solders.hash import Hash

import audit
from config import HELIUS_RPC_URL, HELIUS_API_URL, HELIUS_API_KEY, USDC_MINT, SOL_MINT, FALLBACK_RPC_URL
from helius import rpc_post

from bot_config import DATA_DIR, BOT_TIER
PAYOUT_WALLET_PATH     = DATA_DIR / "payout_wallet.json"
FUNDING_DETECT_MIN_SOL = 0.05   # detect first inbound SOL transfer >= 0.05 SOL as funding
FUNDING_DETECT_MIN_USD = 5.0    # also detect USDC funding >= $5

def _current_cycle(payout_count: int = 0) -> int:
    """Cycle number (1-indexed). Advances by 1 after each completed payout."""
    return payout_count + BOT_TIER


def get_milestone(payout_count: int = 0) -> dict:
    """Compute the active milestone for the current prestige cycle.

    Scaling formula — Cycle N:
      threshold_sol = 2N      (total portfolio must reach this)
      payout_sol    = N       (sent to funding wallet)
      keep_sol      = N       (stays in bot as seed for next cycle — grows with each prestige)

    Each completed prestige: cycle advances by 1 → threshold +◎2, payout +◎1, keep +◎1.
    The bot earns greater trust with each prestige, running with more capital each time.

    Bots 1-3 — BOT_TIER=1 — cycle 1: ◎2.0 threshold, ◎1.0 payout, ◎1.0 keep
    Bots 4-6 — BOT_TIER=2 — cycle 2: ◎4.0 threshold, ◎2.0 payout, ◎2.0 keep

    Example progression (bots 1-3):
      Cycle 1:  ◎2.0 → pay ◎1.0, keep ◎1.0
      Cycle 2:  ◎4.0 → pay ◎2.0, keep ◎2.0
      Cycle 3:  ◎6.0 → pay ◎3.0, keep ◎3.0
      Cycle 4:  ◎8.0 → pay ◎4.0, keep ◎4.0
    """
    cycle     = _current_cycle(payout_count)
    payout    = float(cycle)
    keep      = float(cycle)
    threshold = payout + keep            # = 2 × cycle
    return {
        "id":            f"cycle_{cycle}",
        "cycle":         cycle,
        "threshold_sol": threshold,
        "payout_sol":    payout,
        "keep_sol":      keep,
        "label":         f"◎{threshold:.0f} → send ◎{payout:.0f}, keep ◎{keep:.0f} (cycle {cycle})",
    }


def get_milestones() -> list:
    """Return a list containing the single active milestone (used by main.py)."""
    return [get_milestone(get_payout_count())]

SPL_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if PAYOUT_WALLET_PATH.exists():
        try:
            return json.loads(PAYOUT_WALLET_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    PAYOUT_WALLET_PATH.write_text(json.dumps(state, indent=2))


def load_payout_wallet() -> Optional[str]:
    return _load_state().get("wallet")


def save_payout_wallet(address: str) -> None:
    state = _load_state()
    if state.get("locked") and state.get("wallet"):
        existing = state["wallet"]
        if address != existing:
            msg = (f"SECURITY ALERT: attempt to change payout wallet "
                   f"from {existing[:8]}... to {address[:8]}... — REJECTED")
            print(f"[Payout] ⚠ {msg}")
            audit._write({"event": "security_alert", "msg": msg})
        return
    state["wallet"]          = address
    state["locked"]          = True
    state["paid_milestones"] = []
    _save_state(state)
    print(f"[Payout] ✓ Funding wallet locked: {address}")
    audit._write({"event": "payout_wallet_set", "wallet": address})


def _mark_milestone_paid() -> None:
    """Advance the prestige counter after a successful payout."""
    state = _load_state()
    state["payout_count"] = state.get("payout_count", 0) + 1
    # Wipe legacy paid_milestones list if present — no longer used
    state.pop("paid_milestones", None)
    _save_state(state)


def _is_milestone_paid(milestone_id: str = "") -> bool:
    """Legacy compat shim — dynamic milestones never need this check."""
    return False


def get_payout_count() -> int:
    return _load_state().get("payout_count", 0)


def is_prestige_pending() -> bool:
    return bool(_load_state().get("prestige_pending", False))

def _set_prestige_pending() -> None:
    state = _load_state()
    state["prestige_pending"] = True
    _save_state(state)

def _clear_prestige_pending() -> None:
    state = _load_state()
    state.pop("prestige_pending", None)
    _save_state(state)


# ── Balances ──────────────────────────────────────────────────────────────────

def _parse_balance(data: dict):
    """Return SOL float from a getBalance response, or None if malformed/errored."""
    try:
        res = data.get("result")
        if isinstance(res, dict) and isinstance(res.get("value"), (int, float)):
            return res["value"] / 1e9
    except Exception:
        pass
    return None


async def get_sol_balance(client: httpx.AsyncClient, pubkey: str) -> float:
    """SOL balance, hardened against Helius 'false-empty' reads.

    A degraded Helius RPC can return a successful-looking value:0 for a funded
    wallet (observed in production, especially right after a restart). rpc_post's
    Helius→public fallback only fires on rate-limit/timeout, so that bad 0 slips
    through and the bot reports itself unfunded — which flips the dashboard to the
    'no funds / ACTIVATE' state. When a balance reads 0 (or malformed) we confirm
    it against the public RPC directly before trusting it; a genuinely empty wallet
    just pays one extra read.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey]}
    val = _parse_balance(await rpc_post(client, payload))
    if val is not None and val > 0.0:
        return val

    # Reads as 0 or malformed — verify against the public RPC before believing it.
    try:
        r = await asyncio.wait_for(client.post(FALLBACK_RPC_URL, json=payload, timeout=10), timeout=10.0)
        conf = _parse_balance(r.json())
        if conf is not None and conf > 0.0:
            print(f"[Balance] Helius false-empty caught — public RPC reports ◎{conf:.4f}", flush=True)
            return conf
        if conf is not None:
            return conf          # public confirms a genuine 0
    except Exception:
        pass
    return val if val is not None else 0.0


async def get_usdc_balance(client: httpx.AsyncClient, pubkey: str) -> float:
    data = await rpc_post(client, {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [pubkey, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
    })
    try:
        accounts = data.get("result", {}).get("value", [])
        if accounts:
            return float(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    except Exception:
        pass
    return 0.0


# ── Funding wallet detection (SOL or USDC) ────────────────────────────────────

async def detect_funding_wallet(client: httpx.AsyncClient, bot_pubkey: str) -> Optional[str]:
    """Detect the first wallet that sent SOL or USDC to fund the bot."""
    try:
        url = (f"{HELIUS_API_URL}/v0/addresses/{bot_pubkey}/transactions"
               f"?api-key={HELIUS_API_KEY}&limit=30")
        r = await client.get(url, timeout=10)
        r.raise_for_status()
        for tx in r.json():
            # Check SOL native transfers
            for native in tx.get("nativeTransfers", []):
                if (native.get("toUserAccount") == bot_pubkey and
                        native.get("amount", 0) / 1e9 >= FUNDING_DETECT_MIN_SOL):
                    sender = native.get("fromUserAccount")
                    if sender and sender != bot_pubkey:
                        return sender
            # Check USDC transfers
            for transfer in tx.get("tokenTransfers", []):
                if (transfer.get("mint") == USDC_MINT and
                        transfer.get("toUserAccount") == bot_pubkey and
                        float(transfer.get("tokenAmount", 0)) >= FUNDING_DETECT_MIN_USD):
                    return transfer.get("fromUserAccount")
    except Exception as e:
        print(f"[Payout] Detection error: {e}")
    return None


# ── SOL transfer (simple system program transfer) ─────────────────────────────

async def _get_blockhash(client: httpx.AsyncClient) -> Optional[Hash]:
    data = await rpc_post(client, {
        "jsonrpc": "2.0", "id": 1,
        "method": "getLatestBlockhash",
        "params": [{"commitment": "confirmed"}],
    })
    try:
        return Hash.from_string(data["result"]["value"]["blockhash"])
    except Exception as e:
        print(f"[Payout] Blockhash error: {e}")
        return None


async def send_sol(
    client: httpx.AsyncClient, keypair: Keypair,
    dest_address: str, amount_sol: float
) -> Optional[str]:
    """Send SOL via system program transfer. Much simpler than SPL token transfer."""
    try:
        lamports  = int(amount_sol * 1e9)
        dest      = Pubkey.from_string(dest_address)
        blockhash = await _get_blockhash(client)
        if not blockhash:
            return None

        ix  = sol_transfer(TransferParams(
            from_pubkey=keypair.pubkey(), to_pubkey=dest, lamports=lamports
        ))
        msg = Message.new_with_blockhash([ix], keypair.pubkey(), blockhash)
        tx  = Transaction([keypair], msg, blockhash)

        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [base64.b64encode(bytes(tx)).decode(), {
                "encoding": "base64",
                "skipPreflight": True,
                "maxRetries": 5,
            }],
        }
        result = await rpc_post(client, payload, timeout=30)
        if "error" in result:
            print(f"[Payout] SOL send error: {result['error']}")
            return None
        return result.get("result")
    except Exception as e:
        print(f"[Payout] SOL send error: {e}")
        return None


# ── USDC → SOL conversion (one-time startup) ──────────────────────────────────

async def convert_usdc_to_sol(
    client: httpx.AsyncClient, keypair: Keypair
) -> bool:
    """Swap all USDC in the wallet to SOL via Jupiter on startup."""
    from jupiter import get_quote, build_swap_transaction
    from wallet import sign_transaction, send_transaction, confirm_transaction

    usdc_bal = await get_usdc_balance(client, str(keypair.pubkey()))
    if usdc_bal < 0.50:
        return False

    print(f"[Startup] Converting ${usdc_bal:.2f} USDC → SOL via Jupiter...")
    usdc_units = int(usdc_bal * 1_000_000)

    quote = await get_quote(client, USDC_MINT, SOL_MINT, usdc_units)
    if not quote:
        print("[Startup] Jupiter quote failed — skipping conversion")
        return False

    sol_out = int(quote.get("outAmount", 0)) / 1e9
    print(f"[Startup] Quote: ${usdc_bal:.2f} USDC → {sol_out:.4f} SOL")

    swap_tx = await build_swap_transaction(client, quote, str(keypair.pubkey()))
    if not swap_tx:
        print("[Startup] Swap build failed — skipping conversion")
        return False

    signed = sign_transaction(keypair, swap_tx)
    sig    = await send_transaction(client, signed)
    if not sig:
        print("[Startup] Swap send failed")
        return False

    confirmed, _ = await confirm_transaction(client, sig)
    if confirmed:
        audit._write({"event": "usdc_converted", "usdc": usdc_bal, "sol_approx": sol_out, "sig": sig})
        print(f"[Startup] ✓ Converted to SOL | sig: {sig[:16]}...")
        return True

    print("[Startup] Conversion unconfirmed — proceeding anyway")
    return False


# ── Milestone checker (SOL-based) ─────────────────────────────────────────────

async def check_and_payout(
    client: httpx.AsyncClient, keypair: Keypair, payout_wallet: str,
    total_sol: float = 0.0,   # liquid + in_trades — milestone detection uses this
    reserve_sol: float = 0.1  # always keep this much SOL for gas
) -> bool:
    """Check milestones and send payout when both conditions are met:
    1. total_sol (liquid + deployed) crosses the milestone threshold → set prestige_pending
    2. liquid sol_balance >= payout_sol + reserve → actually send the transfer

    This decouples "prestige earned" from "payout executes" so trades that are open
    when the milestone is crossed don't block the celebration — they just delay the wire.
    """
    sol_balance = await get_sol_balance(client, str(keypair.pubkey()))
    check_sol   = total_sol if total_sol > 0 else sol_balance

    m          = get_milestone(get_payout_count())
    payout_sol = m["payout_sol"]
    keep_sol   = m["keep_sol"]
    threshold  = m["threshold_sol"]

    if check_sol < threshold:
        if is_prestige_pending():
            _clear_prestige_pending()
            print(f"[Payout] ↩ Prestige pending cleared — total ◎{check_sol:.4f} dropped below ◎{threshold:.1f}")
        return False

    # Milestone crossed — need enough liquid to cover payout + gas reserve
    needed = payout_sol + reserve_sol
    if sol_balance < needed:
        if not is_prestige_pending():
            _set_prestige_pending()
            print(
                f"[Payout] ⏳ PRESTIGE PENDING — total ◎{check_sol:.4f} ≥ ◎{threshold:.1f} | "
                f"liquid ◎{sol_balance:.4f} (need ◎{needed:.4f}) — waiting for positions to close"
            )
        else:
            print(f"[Payout] ⏳ Waiting... liquid ◎{sol_balance:.4f} / need ◎{needed:.4f}", flush=True)
        return False

    # Fire the payout
    print(
        f"[Payout] 🎯 Cycle {m['cycle']} — ◎{threshold:.1f} reached | "
        f"liquid ◎{sol_balance:.4f} → sending ◎{payout_sol:.1f}, keeping ◎{keep_sol:.1f}"
    )
    sig = await send_sol(client, keypair, payout_wallet, payout_sol)
    if sig:
        _mark_milestone_paid()
        _clear_prestige_pending()
        audit._write({
            "event":              "payout",
            "milestone":          m["id"],
            "label":              m["label"],
            "cycle":              m["cycle"],
            "amount_sol":         payout_sol,
            "keep_sol":           keep_sol,
            "balance_at_trigger": sol_balance,
            "total_at_trigger":   check_sol,
            "to":                 payout_wallet,
            "sig":                sig,
        })
        new_m = get_milestone(get_payout_count())
        print(
            f"[Payout] ✓ Sent ◎{payout_sol:.1f}, bot keeps ◎{keep_sol:.1f} | sig: {sig[:16]}... "
            f"| next: cycle {new_m['cycle']} → ◎{new_m['threshold_sol']:.1f} "
            f"(pay ◎{new_m['payout_sol']:.1f}, keep ◎{new_m['keep_sol']:.1f})"
        )
        return True
    else:
        print("[Payout] Transfer failed — will retry next cycle")
        return False
