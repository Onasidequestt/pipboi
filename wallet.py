import json
import base64
import random
import time
from typing import Optional
import httpx

# Jito block engine sendBundle uses base58-encoded transactions.
# No external dependency — standard Bitcoin/Solana alphabet implementation.
_B58_ALPHABET = b'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

def _b58encode(data: bytes) -> str:
    leading = len(data) - len(data.lstrip(b'\x00'))
    num = int.from_bytes(data, 'big')
    result = []
    while num:
        num, rem = divmod(num, 58)
        result.append(_B58_ALPHABET[rem:rem+1])
    result.extend([_B58_ALPHABET[0:1]] * leading)
    return b''.join(reversed(result)).decode('ascii')
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction, Transaction
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.message import Message
from solders.system_program import transfer, TransferParams
from config import HELIUS_RPC_URL, KEYPAIR_PATH, FALLBACK_RPC_URL

# Jito block engine endpoints — jito.labs.io migrated to jito.wtf (May 2026)
# Ordered by observed latency: mainnet/slc ~345ms, ny ~580ms, EU/tokyo ~830ms
JITO_BUNDLE_URLS = [
    "https://slc.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles",
]

# Verified live via getTipAccounts 2026-05-31
JITO_TIP_ACCOUNTS = [
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
]

# Jito tip tiers — lamports paid to the block engine validator.
# Scaled by network congestion so bundles land during pump events without
# over-paying on quiet cycles. Measured via getRecentPrioritizationFees (µL/CU).
JITO_TIP_QUIET   = 10_000   # < 10k µL/CU  — normal market
JITO_TIP_NORMAL  = 25_000   # 10k-100k      — moderate activity
JITO_TIP_HOT     = 50_000   # 100k-1M       — busy market / trending token
JITO_TIP_PUMP    = 100_000  # > 1M µL/CU    — BOME-style pump, extreme contention

# 30-second cache so TWAP tranches share one pressure measurement instead of
# making three redundant RPC calls within the same buy execution.
_tip_cache: tuple[float, int] = (0.0, JITO_TIP_QUIET)  # (monotonic_ts, lamports)

# ③ S95 Jito circuit-breaker: when Jito systematically fails (every bundle rejected — e.g. the
# 'cannot lock vote accounts' outage), route jito_only BUYS to RPC so the mover entry LANDS
# instead of aborting. Trips after N consecutive all-endpoint failures; re-probes after cooldown.
_jito_breaker = {"fails": 0, "open_until": 0.0}  # consecutive failures · monotonic skip-until

# S105 (fee accounting): the Jito tip is paid in a SEPARATE bundle tx, so get_tx_sol_delta(swap_sig)
# — which reads only the swap tx — never sees it. proceeds-pnl therefore captures base+priority+
# slippage but NOT the tip (~10k-200k lamports/leg). Stash the tip-per-accepted-bundle here keyed by
# the swap sig so main.py can pop it and log a true `fee_sol` on the open/close rows (pure
# instrumentation — pnl_sol is left unchanged). RPC-broadcast swaps pay no Jito tip → absent key →
# pop returns 0.0 (correct). Bounded so an unread sig can't leak memory.
_tip_paid_by_sig: dict[str, int] = {}
_TIP_MAP_CAP = 4000

def _record_tip(sig: str, tip_lamports: int) -> None:
    if not sig:
        return
    if len(_tip_paid_by_sig) >= _TIP_MAP_CAP:
        for k in list(_tip_paid_by_sig)[: _TIP_MAP_CAP // 10]:  # drop oldest ~10% (insertion-ordered)
            _tip_paid_by_sig.pop(k, None)
    _tip_paid_by_sig[sig] = int(tip_lamports)

def pop_tip_sol(sig: Optional[str]) -> float:
    """Pop the Jito tip (in SOL) paid on the bundle for `sig`; 0.0 if none (RPC path / unknown)."""
    if not sig:
        return 0.0
    return _tip_paid_by_sig.pop(sig, 0) / 1e9


def load_keypair() -> Keypair:
    from bot_config import DATA_DIR
    import os
    # Use per-bot keypair when present (bots 2+); fall back to config path for bot 1
    bot_kp = DATA_DIR / "keypair.json"
    path = str(bot_kp) if bot_kp.exists() else os.path.expanduser(KEYPAIR_PATH)
    with open(path) as f:
        return Keypair.from_bytes(bytes(json.load(f)))


def sign_transaction(keypair: Keypair, swap_tx_b64: str) -> VersionedTransaction:
    raw = base64.b64decode(swap_tx_b64)
    tx = VersionedTransaction.from_bytes(raw)
    return VersionedTransaction(tx.message, [keypair])


async def _fetch_pressure_microlamports(client: httpx.AsyncClient) -> int:
    """75th-percentile of recent network priority fees (microlamports per compute unit).
    Used as the mempool pressure signal for dynamic Jito tip sizing.
    Falls back to 1,000 on any error so tip selection degrades to QUIET tier.
    """
    try:
        r = await client.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getRecentPrioritizationFees",
            "params": [],
        }, timeout=5)
        fees = [
            f["prioritizationFee"]
            for f in r.json().get("result", [])
            if f.get("prioritizationFee", 0) > 0
        ]
        if not fees:
            return 1_000
        fees.sort()
        return max(1_000, fees[int(len(fees) * 0.75)])
    except Exception as e:
        print(f"[Wallet] Pressure fetch error: {e}", flush=True)
        return 1_000


async def get_dynamic_tip_lamports(client: httpx.AsyncClient) -> int:
    """Return a Jito tip scaled to current network congestion.

    Thresholds and tip amounts are read from config_manager so they can be
    tuned via thresholds_override.json without restarting the bot.
    Module-level constants (JITO_TIP_*) serve as fallbacks when config_manager
    is unavailable or base.json is missing.

    Result is cached (default 30 s) so TWAP tranches share one measurement.
    """
    from config_manager import cfg

    global _tip_cache
    now = time.monotonic()
    cache_ttl = cfg("jito.tip_cache_seconds", 30.0)
    if now - _tip_cache[0] < cache_ttl:
        return _tip_cache[1]

    pressure = await _fetch_pressure_microlamports(client)

    p_qn = cfg("jito.pressure_quiet_normal", 10_000)
    p_nh = cfg("jito.pressure_normal_hot",  100_000)
    p_hp = cfg("jito.pressure_hot_pump",  1_000_000)

    if pressure < p_qn:
        tip  = cfg("jito.tip_quiet_lamports", JITO_TIP_QUIET)
        tier = "QUIET"
    elif pressure < p_nh:
        tip  = cfg("jito.tip_normal_lamports", JITO_TIP_NORMAL)
        tier = "NORMAL"
    elif pressure < p_hp:
        tip  = cfg("jito.tip_hot_lamports", JITO_TIP_HOT)
        tier = "HOT"
    else:
        tip  = cfg("jito.tip_pump_lamports", JITO_TIP_PUMP)
        tier = "PUMP"

    _tip_cache = (now, tip)
    print(f"[Jito] Pressure {pressure:,} µL/CU → tip tier {tier} ({tip:,} lamports)", flush=True)
    return tip


async def _get_blockhash_str(client: httpx.AsyncClient) -> Optional[str]:
    for url in [HELIUS_RPC_URL, FALLBACK_RPC_URL]:
        try:
            r = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "confirmed"}],
            }, timeout=10)
            data = r.json()
            if data.get("error", {}).get("code") == -32429 and url == HELIUS_RPC_URL:
                print("[Wallet] Helius rate-limited for blockhash — using public RPC", flush=True)
                continue
            return data["result"]["value"]["blockhash"]
        except Exception as e:
            if url == HELIUS_RPC_URL:
                continue
            print(f"[Wallet] Blockhash fetch error: {e}", flush=True)
    return None


async def _build_tip_tx(
    client: httpx.AsyncClient,
    keypair: Keypair,
    tip_lamports: int,
) -> Optional[Transaction]:
    """Build a SOL transfer to a random Jito tip account (legacy tx, same as payout.py)."""
    blockhash_str = await _get_blockhash_str(client)
    if not blockhash_str:
        return None
    try:
        tip_ix = transfer(TransferParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS)),
            lamports=tip_lamports,
        ))
        blockhash = Hash.from_string(blockhash_str)
        msg = Message.new_with_blockhash([tip_ix], keypair.pubkey(), blockhash)
        return Transaction([keypair], msg, blockhash)
    except Exception as e:
        print(f"[Jito] Tip tx build error: {e}", flush=True)
        return None


async def simulate_transaction(client: httpx.AsyncClient, signed_tx: VersionedTransaction) -> bool:
    """Simulate before broadcast. Returns False if the node reports an error."""
    try:
        serialized = base64.b64encode(bytes(signed_tx)).decode("utf-8")
        r = await client.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "simulateTransaction",
            "params": [serialized, {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
            }],
        }, timeout=15)
        r.raise_for_status()
        err = r.json().get("result", {}).get("value", {}).get("err")
        if err:
            print(f"[Wallet] Simulation rejected: {err}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"[Wallet] Simulate error (allowing): {e}", flush=True)
        return True  # don't block trades on simulate API failure


async def _broadcast(client: httpx.AsyncClient, signed_tx: VersionedTransaction) -> Optional[str]:
    """Standard RPC broadcast — tries Helius then public RPC on rate-limit."""
    serialized = base64.b64encode(bytes(signed_tx)).decode("utf-8")
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [serialized, {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}],
    }
    for url in [HELIUS_RPC_URL, FALLBACK_RPC_URL]:
        try:
            r = await client.post(url, json=payload, timeout=30)
            r.raise_for_status()
            result = r.json()
            err = result.get("error", {})
            if err.get("code") == -32429 and url == HELIUS_RPC_URL:
                print("[RPC] Helius rate-limited for broadcast — using public RPC", flush=True)
                continue
            if "error" in result:
                print(f"[RPC] Error: {result['error']}")
                return None
            return result.get("result")
        except Exception as e:
            if url == HELIUS_RPC_URL:
                print(f"[Wallet] Helius broadcast error, trying public RPC: {e}", flush=True)
                continue
            print(f"[Wallet] Send error: {e}")
    return None


async def get_tx_sol_delta(
    client: httpx.AsyncClient,
    signature: str,
    pubkey: str,
) -> Optional[float]:
    """Ground-truth wallet SOL change for a CONFIRMED tx, in SOL (signed). [S105]

    Returns (postBalance − preBalance) for `pubkey` from the confirmed transaction's
    meta — the REAL lamports the wallet gained (sell, +) or spent (buy, −) net of the
    base fee + priority/Jito tip + actual on-chain slippage. This is the wallet truth
    the Jupiter quote `outAmount` cannot give: the quote is a pre-trade ESTIMATE that
    omits fees, tips, and real fill. Booking proceeds pnl off this reconciles the ledger
    to the wallet by construction (the price/quote-based pnl read flat while the wallet
    bled friction on dead-pool churn — S105 diagnosis). Returns None on any RPC/parse
    failure → caller falls back to the quote estimate (never raises, never blocks a sell).
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
    }
    for _url in [HELIUS_RPC_URL, FALLBACK_RPC_URL]:
        try:
            r = await client.post(_url, json=payload, timeout=15)
            d = r.json()
            if d.get("error"):
                if d.get("error", {}).get("code") == -32429 and _url == HELIUS_RPC_URL:
                    continue
                if _url == HELIUS_RPC_URL:
                    continue
                return None
            res = d.get("result") or {}
            meta = res.get("meta") or {}
            pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
            if not pre or not post or len(pre) != len(post):
                return None
            # Locate our account index (fee payer is normally index 0; match by pubkey to be safe).
            keys = (((res.get("transaction") or {}).get("message") or {}).get("accountKeys")) or []
            idx = 0
            for i, k in enumerate(keys):
                kp = k.get("pubkey") if isinstance(k, dict) else k
                if kp == pubkey:
                    idx = i
                    break
            if idx >= len(pre):
                idx = 0
            return (post[idx] - pre[idx]) / 1e9
        except Exception:
            if _url == HELIUS_RPC_URL:
                continue
            return None
    return None


async def send_transaction(
    client: httpx.AsyncClient,
    signed_tx: VersionedTransaction,
    keypair: Optional[Keypair] = None,
    skip_simulate: bool = False,
    jito_only: bool = False,
) -> Optional[str]:
    """Simulate (optional), then submit. Jito bundle if keypair provided; standard RPC fallback.

    skip_simulate=True skips the Helius dry-run for trusted, liquid tokens where simulation
    adds ~200ms latency with negligible safety benefit (skipPreflight is already True on broadcast).
    Only set this for static watchlist tokens — always simulate for discovered tokens.

    jito_only=True disables the Helius fallback for buys. If Jito fails, the function returns
    None rather than broadcasting via RPC. Use for INSANE/WILD buy entries where a late,
    slippage-heavy RPC broadcast costs more in expectation than a missed Jito bundle (~$0.001).
    """
    if not skip_simulate and not await simulate_transaction(client, signed_tx):
        return None

    if keypair is None:
        return await _broadcast(client, signed_tx)

    # ③ S95 breaker: skip Jito entirely during a sustained outage → land the buy via RPC.
    import time as _bt
    from config_manager import cfg
    if cfg("jito.breaker_enabled", True) and _bt.monotonic() < _jito_breaker["open_until"]:
        print("[Jito] breaker OPEN — routing to RPC (Jito systematically failing)", flush=True)
        return await _broadcast(client, signed_tx)

    # Build Jito bundle: [swap_tx, tip_tx]
    # Tip is sized dynamically from network pressure — pump events get higher tips
    # so the bundle competes for block inclusion when validators have their pick.
    _tip_lamports = await get_dynamic_tip_lamports(client)
    tip_tx = await _build_tip_tx(client, keypair, _tip_lamports)
    if not tip_tx:
        if jito_only:
            print("[Jito] Tip build failed — jito_only mode: aborting buy", flush=True)
            return None
        print("[Jito] Tip build failed — using RPC", flush=True)
        return await _broadcast(client, signed_tx)

    # Jito block engine expects base58-encoded transactions in sendBundle
    bundle = [
        _b58encode(bytes(signed_tx)),
        _b58encode(bytes(tip_tx)),
    ]
    for url in JITO_BUNDLE_URLS:
        try:
            r = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "sendBundle", "params": [bundle]},
                timeout=15,
            )
            result = r.json()
            if "error" not in result:
                print(f"[Jito] Bundle accepted | tip: {_tip_lamports:,} lamports", flush=True)
                _jito_breaker["fails"] = 0; _jito_breaker["open_until"] = 0.0
                _sig = str(signed_tx.signatures[0])
                _record_tip(_sig, _tip_lamports)   # S105: book the tip for main.py fee_sol accounting
                return _sig
            print(f"[Jito] {url.split('/')[2][:12]} rejected: {result['error']}", flush=True)
        except Exception as e:
            print(f"[Jito] {url.split('/')[2][:12]} error: {e}", flush=True)

    if cfg("jito.breaker_enabled", True):
        import time as _bt2
        _jito_breaker["fails"] += 1
        if _jito_breaker["fails"] >= cfg("jito.breaker_fail_threshold", 3):
            _jito_breaker["open_until"] = _bt2.monotonic() + cfg("jito.breaker_cooldown_s", 120.0)
            print(f"[Jito] circuit OPEN {cfg('jito.breaker_cooldown_s', 120.0):.0f}s after "
                  f"{_jito_breaker['fails']} fails — sustained outage, buys → RPC", flush=True)
            return await _broadcast(client, signed_tx)
    if jito_only:
        # PIPE12 (R-C2): the S95 breaker only trips on 3 CONSECUTIVE fails, so INTERMITTENT
        # Jito rejections ("cannot lock any vote accounts") each abort a mover buy and never
        # trip it (measured: exec_audit.py). With jito.per_attempt_fallback on, route THIS
        # failed jito_only buy to RPC immediately instead of aborting — recover the entry,
        # trading a little MEV exposure for fill-rate (the right call for the mover bots).
        # Default OFF → current "miss rather than slip" behaviour is unchanged until enabled.
        if cfg("jito.per_attempt_fallback", False):
            print("[Jito] All endpoints failed — per-attempt fallback: routing buy → RPC", flush=True)
            return await _broadcast(client, signed_tx)
        print("[Jito] All endpoints failed — jito_only mode: aborting buy", flush=True)
        return None
    print("[Jito] All endpoints failed — using RPC", flush=True)
    return await _broadcast(client, signed_tx)


async def confirm_transaction(
    client: httpx.AsyncClient,
    signature: str,
    signed_tx: Optional["VersionedTransaction"] = None,
    retries: int = 15,
    delay: float = 3.0,
) -> tuple[bool, str]:
    """Poll for confirmation, rebroadcasting via RPC ~every 1.5s so dropped Jito bundles land fast.

    Solana transactions are either fast (<15s) or dropped entirely — there is no slow path.
    Rebroadcasting the same signed tx periodically ensures validators see it without risk of
    double-execution (same signature = idempotent). See the S78 block below for why the early
    RPC rebroadcast (not the Jito bundle) is what reliably lands the tx.

    Max wait: ~retries × delay = 45s (shorter than blockhash expiry to avoid wasted polling).

    Returns (True, "") on success or (False, reason) where reason is "on-chain: <err>" or "timeout".
    """
    import asyncio
    import time as _mono
    # S78: early-RPC-rebroadcast + fast-poll confirmation. ROOT CAUSE of the ~11.5s tx latency
    # (median latency_ms 11552, a structural floor not network): buys/sells go out FIRST as a
    # Jito bundle (send_transaction → sendBundle), which returns a sig on bundle *acceptance* but
    # in a quiet/normal-tip market frequently NEVER LANDS. The tx then sits pending until the old
    # ~7s rebroadcast via standard RPC (_broadcast → sendTransaction, skipPreflight) lands it —
    # the log pattern was dead-consistent: "pending,pending,pending,↺Rebroadcast,confirmed". That
    # ~10s entry delay kept exits blind post-fill (fast paper peaks missed = the paper→live leak)
    # and serialized the loop. FIX: fire the RPC rebroadcast EARLY (~1.5s) instead of ~7s, and
    # poll fast to detect the post-rebroadcast confirm. Rebroadcast is the SAME signed tx (same
    # signature) → idempotent, zero double-execution risk even alongside the live Jito bundle.
    # Max wait preserved (~retries*delay ≈ 45s). Revert: .s78_backups/.
    _deadline = _mono.monotonic() + retries * delay
    _last_rebroadcast = _mono.monotonic()
    _poll_delay = 0.3
    await asyncio.sleep(0.3)            # S78: was 1.0
    attempt = -1
    while _mono.monotonic() < _deadline:
        attempt += 1
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignatureStatuses",
                "params": [[signature], {"searchTransactionHistory": True}],
            }
            # Try Helius first, fall back to public RPC on rate-limit
            data = {}
            for _url in [HELIUS_RPC_URL, FALLBACK_RPC_URL]:
                try:
                    _r = await client.post(_url, json=payload, timeout=15)
                    _d = _r.json()
                    if _d.get("error", {}).get("code") == -32429 and _url == HELIUS_RPC_URL:
                        continue
                    data = _d
                    break
                except Exception:
                    if _url == HELIUS_RPC_URL:
                        continue
            statuses = data.get("result", {}).get("value", [])
            if statuses and statuses[0]:
                status = statuses[0]
                if status.get("err"):
                    err = status["err"]
                    print(f"[Wallet] ❌ On-chain error: {err}", flush=True)
                    return False, f"on-chain: {err}"
                conf = status.get("confirmationStatus")
                print(f"[Wallet] Attempt {attempt+1}: {conf}", flush=True)
                if conf in ("processed", "confirmed", "finalized"):
                    return True, ""
            else:
                print(f"[Wallet] Attempt {attempt+1}: pending...", flush=True)
        except Exception as e:
            print(f"[Wallet] Confirm error: {e}", flush=True)

        # S78: rebroadcast via RPC every ~1.5s by WALL CLOCK (was every 3rd poll ≈ 7s). The Jito
        # bundle often fails to land on its own; the RPC resend is what actually lands the tx, so
        # firing it early cuts ~6s off entry latency. Same signature, idempotent, safe to resend.
        if signed_tx is not None and _mono.monotonic() - _last_rebroadcast >= 1.5:
            try:
                await _broadcast(client, signed_tx)
                print(f"[Wallet] ↺ Rebroadcast attempt {attempt+1}", flush=True)
                _last_rebroadcast = _mono.monotonic()
            except Exception:
                pass

        await asyncio.sleep(_poll_delay)            # S78: fast early polls, gentle backoff
        _poll_delay = min(_poll_delay * 1.5, 2.0)

    print(f"[Wallet] ⚠ Timeout after {attempt+1} polls — sig: {signature[:20]}...", flush=True)
    return False, "timeout"


async def check_token_balance(client: httpx.AsyncClient, owner: str, mint: str) -> int:
    """Return raw token balance units for a given mint in the owner's wallet."""
    from helius import rpc_post
    try:
        data = await rpc_post(client, {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        })
        accounts = data.get("result", {}).get("value", [])
        if accounts:
            return int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except Exception:
        pass
    return 0
