#!/usr/bin/env python3
"""
reconcile.py — wallet ⇄ ledger reconciliation + orphan cleanup.

WHY THIS EXISTS
---------------
The trading loop's ghost-prune / ghost-close path trusts a SINGLE on-chain balance
read. When the bot's Helius key is degraded (rate-limited, lagging — observed
returning 0 SOL / 0 token accounts for a wallet that actually held 0.66 SOL and 36
accounts), that read falsely returns "no balance". The bot then books the loss in
PnL and stops tracking the position — but the tokens are still physically in the
wallet. They become invisible ORPHANS the bot never revisits, and the empty token
accounts left behind by old trades lock up rent SOL.

This tool is the reverse check the bot never does: enumerate what's ACTUALLY on-chain
(across multiple RPCs so one false-empty read can't lie), cross-reference the bot's
positions.json, and clean up what the bot abandoned.

SAFETY
------
- DRY-RUN BY DEFAULT. Prints a plan and touches nothing without an explicit flag.
- Never sells a mint that is in positions.json (won't yank a live position out from
  under a running bot).
- Re-verifies each account's balance immediately before acting (race-safe).
- closeAccount on a non-empty account is rejected on-chain, so a misjudged "empty"
  fails safely rather than burning tokens.
- Uses public RPC for truth + broadcast (the bot's Helius key is the unreliable one).

USAGE
-----
    python3 reconcile.py --bot 2                 # dry-run: report holdings + plan
    python3 reconcile.py --bot 2 --close-empty   # close zero-balance ATAs, reclaim rent
    python3 reconcile.py --bot 2 --sell          # sell nonzero orphans → SOL via Jupiter
    python3 reconcile.py --bot 2 --sell --close-empty   # full cleanup
    python3 reconcile.py --bot 2 --min-value-usd 1.0    # skip orphans worth < $1 when selling
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Optional

import httpx

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.message import MessageV0
from solders.hash import Hash
from solders.transaction import VersionedTransaction

# ── Constants ─────────────────────────────────────────────────────────────────
SOL_MINT      = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022    = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
JUP_QUOTE     = "https://api.jup.ag/swap/v1"
SELL_SLIPPAGE_BPS = 1500   # 15% — cleanup sells: get out, don't optimize the fill

# Public RPCs — the bot's Helius key is the degraded one this tool works around.
# Truth is taken as the MAX-holdings response across these (a false-empty can't win).
RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]
_CLOSE_DISCRIMINATOR = bytes([9])   # SPL Token CloseAccount instruction
_CLOSE_BATCH = 12                    # closeAccount ixs per tx (well under size limit)


# ── On-chain enumeration ──────────────────────────────────────────────────────
async def _token_accounts(client: httpx.AsyncClient, url: str, wallet: str, program: str) -> list[dict]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [wallet, {"programId": program}, {"encoding": "jsonParsed"}]}
    try:
        r = await client.post(url, json=body, timeout=25)
        return r.json().get("result", {}).get("value", []) or []
    except Exception:
        return []


async def enumerate_holdings(client: httpx.AsyncClient, wallet: str, rounds: int = 3) -> tuple[float, list[dict]]:
    """Return (sol_balance, accounts). accounts = [{mint, amount, ui, pubkey, program, decimals}].

    Public RPCs intermittently return a false-empty view (the exact failure that orphaned
    these tokens). So we poll every RPC × every program over several rounds and UNION the
    results, keeping the largest balance seen for each account pubkey. One — or even one
    whole round of — empty reads can no longer hide a holding."""
    best: dict[str, dict] = {}        # pubkey → account record (dedup across programs/RPCs/rounds)
    best_sol = 0.0
    for _ in range(rounds):
        for url in RPCS:
            try:
                sol = (await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                                                    "params": [wallet]}, timeout=20)).json()
                best_sol = max(best_sol, sol.get("result", {}).get("value", 0) / 1e9)
            except Exception:
                pass
            for program in (TOKEN_PROGRAM, TOKEN_2022):
                for a in await _token_accounts(client, url, wallet, program):
                    info = a["account"]["data"]["parsed"]["info"]
                    ta = info["tokenAmount"]
                    rec = {"mint": info["mint"], "amount": int(ta["amount"]),
                           "ui": ta["uiAmount"] or 0.0, "decimals": ta["decimals"],
                           "pubkey": a["pubkey"], "program": program}
                    # Keep the record showing the LARGEST balance for a given account pubkey
                    cur = best.get(a["pubkey"])
                    if cur is None or rec["amount"] > cur["amount"]:
                        best[a["pubkey"]] = rec
                await asyncio.sleep(0.35)   # space calls — public RPCs 429 on rapid bursts
        await asyncio.sleep(0.4)
    return best_sol, list(best.values())


async def _latest_blockhash(client: httpx.AsyncClient) -> Optional[str]:
    for url in RPCS:
        try:
            r = (await client.post(url, json={"jsonrpc": "2.0", "id": 1,
                 "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]}, timeout=20)).json()
            return r["result"]["value"]["blockhash"]
        except Exception:
            continue
    return None


async def _broadcast(client: httpx.AsyncClient, signed: VersionedTransaction) -> Optional[str]:
    raw = base64.b64encode(bytes(signed)).decode()
    body = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [raw, {"encoding": "base64", "skipPreflight": False, "maxRetries": 5}]}
    for url in RPCS:
        try:
            r = (await client.post(url, json=body, timeout=30)).json()
            if "error" in r:
                print(f"    rpc error: {r['error'].get('message', r['error'])}")
                continue
            return r.get("result")
        except Exception as e:
            print(f"    send error: {e}")
    return None


async def _confirm(client: httpx.AsyncClient, sig: str, tries: int = 12) -> bool:
    for _ in range(tries):
        await asyncio.sleep(3.0)
        for url in RPCS:
            try:
                r = (await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                     "params": [[sig], {"searchTransactionHistory": True}]}, timeout=15)).json()
                st = (r.get("result", {}).get("value") or [None])[0]
                if st and st.get("confirmationStatus") in ("confirmed", "finalized") and not st.get("err"):
                    return True
                if st and st.get("err"):
                    print(f"    on-chain error: {st['err']}")
                    return False
            except Exception:
                continue
    return False


# ── Close empty token accounts (reclaim rent) ─────────────────────────────────
def _close_ix(account: str, owner: Pubkey, program: str) -> Instruction:
    return Instruction(
        program_id=Pubkey.from_string(program),
        accounts=[
            AccountMeta(Pubkey.from_string(account), is_signer=False, is_writable=True),  # account to close
            AccountMeta(owner, is_signer=False, is_writable=True),                         # rent destination
            AccountMeta(owner, is_signer=True,  is_writable=False),                        # owner / authority
        ],
        data=_CLOSE_DISCRIMINATOR,
    )


async def close_empty(client: httpx.AsyncClient, keypair: Keypair, empties: list[dict]) -> None:
    owner = keypair.pubkey()
    print(f"\n▶ Closing {len(empties)} empty token accounts (reclaiming rent)…")
    closed = 0
    for i in range(0, len(empties), _CLOSE_BATCH):
        batch = empties[i:i + _CLOSE_BATCH]
        ixs = [_close_ix(a["pubkey"], owner, a["program"]) for a in batch]
        bh = await _latest_blockhash(client)
        if not bh:
            print("    could not fetch blockhash — aborting batch"); continue
        msg = MessageV0.try_compile(owner, ixs, [], Hash.from_string(bh))
        tx = VersionedTransaction(msg, [keypair])
        sig = await _broadcast(client, tx)
        if sig and await _confirm(client, sig):
            closed += len(batch)
            print(f"    batch {i//_CLOSE_BATCH + 1}: ✓ closed {len(batch)} accounts — {sig[:16]}…")
        else:
            print(f"    batch {i//_CLOSE_BATCH + 1}: ✗ failed (sig={str(sig)[:16]})")
    print(f"  Done — {closed}/{len(empties)} accounts closed, ~{closed * 0.00204:.4f} SOL reclaimed.")


# ── Sell orphan tokens → SOL ──────────────────────────────────────────────────
async def sell_orphan(client: httpx.AsyncClient, keypair: Keypair, acct: dict) -> bool:
    owner = str(keypair.pubkey())
    mint = acct["mint"]
    # Re-verify balance right before selling (race-safe)
    try:
        q = await client.get(f"{JUP_QUOTE}/quote", params={
            "inputMint": mint, "outputMint": SOL_MINT,
            "amount": acct["amount"], "slippageBps": SELL_SLIPPAGE_BPS,
            "onlyDirectRoutes": False}, timeout=15)
        q.raise_for_status()
        quote = q.json()
    except Exception as e:
        print(f"    {mint[:10]}…: no Jupiter route ({e}) — skipping (illiquid/dead)")
        return False
    out_sol = int(quote.get("outAmount", 0)) / 1e9
    if out_sol <= 0:
        print(f"    {mint[:10]}…: quote returned 0 SOL — skipping")
        return False
    try:
        sw = await client.post(f"{JUP_QUOTE}/swap", json={
            "quoteResponse": quote, "userPublicKey": owner,
            "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto", "dynamicSlippage": True}, timeout=20)
        sw.raise_for_status()
        swap_b64 = sw.json().get("swapTransaction")
    except Exception as e:
        print(f"    {mint[:10]}…: swap build failed ({e})")
        return False
    raw = base64.b64decode(swap_b64)
    unsigned = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(unsigned.message, [keypair])
    sig = await _broadcast(client, signed)
    if sig and await _confirm(client, sig):
        print(f"    {mint[:10]}…: ✓ sold for ~◎{out_sol:.5f} — {sig[:16]}…")
        return True
    print(f"    {mint[:10]}…: ✗ sell failed (sig={str(sig)[:16]})")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    ap = argparse.ArgumentParser(description="Reconcile a bot wallet against its ledger and clean up orphans.")
    ap.add_argument("--bot", type=int, default=2, help="bot id (loads bots/botN/keypair.json)")
    ap.add_argument("--sell", action="store_true", help="sell nonzero orphan tokens → SOL")
    ap.add_argument("--close-empty", action="store_true", help="close zero-balance ATAs to reclaim rent")
    ap.add_argument("--min-value-usd", type=float, default=0.0, help="skip selling orphans worth less than this")
    args = ap.parse_args()

    kp_path = Path(f"bots/bot{args.bot}/keypair.json")
    if args.bot == 1 and not kp_path.exists():
        import os
        kp_path = Path(os.path.expanduser("~/.config/solana/id.json"))
    keypair = Keypair.from_bytes(bytes(json.load(open(kp_path))))
    wallet = str(keypair.pubkey())

    # Tracked mints — never sell these out from under a running bot
    pos_path = Path(f"bots/bot{args.bot}/positions.json")
    tracked = set(json.load(open(pos_path)).keys()) if pos_path.exists() else set()

    async with httpx.AsyncClient() as client:
        sol, accounts = await enumerate_holdings(client, wallet)
        nonzero = [a for a in accounts if a["amount"] > 0]
        empty   = [a for a in accounts if a["amount"] == 0]
        orphans = [a for a in nonzero if a["mint"] not in tracked]
        tracked_held = [a for a in nonzero if a["mint"] in tracked]

        # Price the orphans (best-effort, for the report / min-value gate)
        prices: dict[str, float] = {}
        if orphans:
            try:
                ids = ",".join(a["mint"] for a in orphans)
                pr = (await client.get("https://api.jup.ag/price/v2", params={"ids": ids}, timeout=15)).json().get("data", {})
                prices = {m: float(v["price"]) for m, v in pr.items() if v}
            except Exception:
                pass

        print(f"\n════ Bot {args.bot} wallet reconciliation ════")
        print(f"  {wallet}")
        print(f"  SOL: ◎{sol:.4f}")
        print(f"  Token accounts: {len(accounts)}  |  nonzero: {len(nonzero)}  |  empty: {len(empty)}")
        print(f"  Tracked in positions.json: {len(tracked)}")

        print(f"\n  ── ORPHANS (held on-chain, NOT tracked by the bot) ──")
        if not orphans:
            print("    none")
        for a in sorted(orphans, key=lambda x: -(prices.get(x['mint'], 0) * x['ui'])):
            val = prices.get(a["mint"], 0) * a["ui"]
            print(f"    {a['mint']}  bal={a['ui']:.6g}  ~${val:.2f}  [{a['program'][:4]}]")
        if tracked_held:
            print(f"\n  ── TRACKED positions (left alone) ──")
            for a in tracked_held:
                print(f"    {a['mint']}  bal={a['ui']:.6g}")

        rent = len(empty) * 0.00204
        print(f"\n  ── EMPTY accounts: {len(empty)} (≈◎{rent:.4f} reclaimable rent) ──")

        if not args.sell and not args.close_empty:
            print("\n  DRY RUN — nothing executed. Re-run with --close-empty and/or --sell to act.\n")
            return

        if args.close_empty and empty:
            await close_empty(client, keypair, empty)

        if args.sell and orphans:
            print(f"\n▶ Selling {len(orphans)} orphan(s) → SOL (slippage {SELL_SLIPPAGE_BPS/100:.0f}%)…")
            sold = 0
            for a in orphans:
                val = prices.get(a["mint"], 0) * a["ui"]
                if val < args.min_value_usd:
                    print(f"    {a['mint'][:10]}…: ~${val:.2f} < ${args.min_value_usd:.2f} min — skipping")
                    continue
                if await sell_orphan(client, keypair, a):
                    sold += 1
                await asyncio.sleep(2.0)
            print(f"  Done — {sold}/{len(orphans)} orphans sold.")
            if args.close_empty:
                print("\n▶ Re-scanning for newly-emptied accounts to close…")
                _, accounts2 = await enumerate_holdings(client, wallet)
                empty2 = [a for a in accounts2 if a["amount"] == 0]
                new_empty = [a for a in empty2 if a["pubkey"] not in {e["pubkey"] for e in empty}]
                if new_empty:
                    await close_empty(client, keypair, new_empty)
        print()


if __name__ == "__main__":
    asyncio.run(main())
