"""
pump.fun bonding curve monitor — real-time whale signal detector.

Subscribes to pump.fun program logs via Helius WebSocket and parses
TradeEvent structs directly from the base64 instruction data in each log
notification. Zero extra API calls — events arrive in <100ms.

When a token receives a whale buy (≥ 0.5 SOL single entry) OR sustained
buy pressure (≥ 1.0 SOL total + ≥ 3 buys in a 5-minute window), it is
marked "hot" and surfaced to discovery.py as the highest-priority source.

This fires BEFORE the token appears on any trending list — the bots see
the signal the moment a whale wallet enters, not after DexScreener batches it.

Integration:
  main.py       — calls bonding_curve.start() at startup
  discovery.py  — calls bonding_curve.get_hot_mints() as source 7
"""
import asyncio
import base64
import hashlib
import json
import struct
import time
from collections import deque
from typing import Optional

from solders.pubkey import Pubkey

# ── Constants ──────────────────────────────────────────────────────────────────

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# Public Solana RPC handles logsSubscribe for high-volume programs like pump.fun.
# Helius free tier returns 429 on this subscription regardless of key.
_WS_URL          = "wss://api.mainnet-beta.solana.com"

# Anchor event discriminator: first 8 bytes of sha256("event:TradeEvent")
# Computed at import time — no hard-coded magic bytes.
_TRADE_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]

# Signal thresholds
WHALE_BUY_SOL   = 0.5    # single buy ≥ 0.5 SOL  →  notable entry
HOT_TOTAL_SOL   = 1.0    # OR: ≥ 1.0 SOL total in window
HOT_BUY_COUNT   = 3      # AND: ≥ 3 buys in window
HOT_WINDOW_SECS = 300    # 5-minute rolling window

# ── State ─────────────────────────────────────────────────────────────────────

# Per-mint rolling buy data
_activity: dict = {}        # mint → {"buys": deque[(ts, sol)], "largest": float, "buyers": set}

# Hot mint queue — read by get_hot_mints()
# maxlen=2000: with 5k+ signals/session the old 200 caused evicted mints to be silently
# dropped from _hot_mints while staying in _hot_set, blocking re-signals permanently.
_hot_mints: deque = deque(maxlen=2000)
_hot_set:   set   = set()
# Per-mint last-signal timestamp — allows re-signaling after a cooldown.
_hot_signal_ts: dict = {}   # mint → float (unix ts of last HOT signal)
HOT_RESIGNAL_COOLDOWN = 1800  # 30 min — allow whale activity re-signal after cooldown

# Public status flag for the dashboard / logs
connected = False

_stats = {"events": 0, "buys": 0, "whales": 0, "hot": 0, "errors": 0}

# Per-wallet whale tracker — session totals (resets on restart)
_whale_tracker: dict = {}   # wallet → {"total_sol": float, "buy_count": int, "largest": float, "mints": set}


# ── TradeEvent parser ──────────────────────────────────────────────────────────

def _parse_trade(data: bytes) -> Optional[dict]:
    """
    Parse a pump.fun TradeEvent from raw Anchor event bytes.

    Layout (after 8-byte discriminator):
      Offset  8-39  : mint          Pubkey (32 bytes)
      Offset 40-47  : sol_amount    u64 lamports
      Offset 48-55  : token_amount  u64
      Offset 56     : is_buy        bool
      Offset 57-88  : user          Pubkey (32 bytes)
      Offset 89-96  : timestamp     i64      (optional — not needed)
      Offset 97+    : reserves      u64×N    (optional — not needed)

    Returns None if discriminator doesn't match or data is too short.
    """
    if len(data) < 57 or data[:8] != _TRADE_DISC:
        return None
    try:
        mint      = str(Pubkey.from_bytes(data[8:40]))
        sol_amt   = struct.unpack_from("<Q", data, 40)[0] / 1e9  # lamports → SOL
        is_buy    = bool(data[56])
        user      = str(Pubkey.from_bytes(data[57:89])) if len(data) >= 89 else "unknown"
        return {"mint": mint, "sol_amount": sol_amt, "is_buy": is_buy, "user": user}
    except Exception:
        return None


# ── Activity tracking ──────────────────────────────────────────────────────────

_last_cleanup = 0.0

def _record_buy(mint: str, sol: float, user: str) -> None:
    """Record a buy event and fire a hot signal if thresholds are crossed."""
    global _last_cleanup
    now = time.time()

    # Periodic cleanup: prune _activity entries that have had no buys in the last
    # 10 minutes. Prevents the dict from growing to tens-of-thousands over long sessions.
    # Also prune _hot_signal_ts for mints well past re-signal cooldown.
    if now - _last_cleanup > 600:  # every 10 minutes
        cutoff = now - HOT_WINDOW_SECS * 2  # 10 min stale
        stale_mints = [m for m, r in _activity.items() if not r["buys"] or r["buys"][-1][0] < cutoff]
        for m in stale_mints:
            del _activity[m]
        ts_cutoff = now - HOT_RESIGNAL_COOLDOWN * 2
        stale_ts = [m for m, ts in _hot_signal_ts.items() if ts < ts_cutoff]
        for m in stale_ts:
            del _hot_signal_ts[m]
        _last_cleanup = now
        if stale_mints:
            print(f"[BondingCurve] Pruned {len(stale_mints)} stale activity entries, {len(_activity)} remain", flush=True)

    if mint not in _activity:
        _activity[mint] = {"buys": deque(), "largest": 0.0, "buyers": set()}

    rec = _activity[mint]
    rec["buys"].append((now, sol))
    rec["buyers"].add(user)
    if sol > rec["largest"]:
        rec["largest"] = sol

    # Prune events outside the rolling window
    cutoff = now - HOT_WINDOW_SECS
    while rec["buys"] and rec["buys"][0][0] < cutoff:
        rec["buys"].popleft()

    _stats["buys"] += 1
    if sol >= WHALE_BUY_SOL:
        _stats["whales"] += 1
        # Track per-wallet totals for the dashboard leaderboard
        if user not in _whale_tracker:
            _whale_tracker[user] = {"total_sol": 0.0, "buy_count": 0, "largest": 0.0, "mints": set()}
        wt = _whale_tracker[user]
        wt["total_sol"] += sol
        wt["buy_count"] += 1
        if sol > wt["largest"]:
            wt["largest"] = sol
        wt["mints"].add(mint)

    # Re-signal allowed after cooldown — prevents silently dropping new whale activity
    # on mints that went hot earlier but have since cooled. Without this, once a mint
    # enters _hot_set it can never re-signal even after HOT_RESIGNAL_COOLDOWN seconds.
    if mint in _hot_set:
        last_ts = _hot_signal_ts.get(mint, 0)
        if time.time() - last_ts < HOT_RESIGNAL_COOLDOWN:
            return  # still in cooldown — no duplicate
        # Cooldown expired — allow re-signal and clear old state
        _hot_set.discard(mint)

    total_sol = sum(s for _, s in rec["buys"])
    buy_count = len(rec["buys"])
    # Use window-only whale check — rec["largest"] is all-time and can be stale.
    # A whale from 3h ago should NOT trigger a hot signal on 3 tiny new buys.
    has_whale = any(s >= WHALE_BUY_SOL for _, s in rec["buys"])

    if (has_whale or total_sol >= HOT_TOTAL_SOL) and buy_count >= HOT_BUY_COUNT:
        _hot_mints.append(mint)
        _hot_set.add(mint)
        _hot_signal_ts[mint] = time.time()
        _stats["hot"] += 1
        print(
            f"[BondingCurve] ◆ HOT {mint[:8]}... "
            f"largest=◎{rec['largest']:.3f}  total=◎{total_sol:.2f}  "
            f"buys={buy_count}  buyers={len(rec['buyers'])}",
            flush=True,
        )


# ── WebSocket log processor ────────────────────────────────────────────────────

def _process(msg: dict) -> None:
    """Extract and parse TradeEvents from a Helius log notification."""
    try:
        logs = (
            msg.get("params", {})
               .get("result", {})
               .get("value", {})
               .get("logs", [])
        )
        for log in logs:
            if not log.startswith("Program data: "):
                continue
            try:
                raw = base64.b64decode(log[14:])
            except Exception:
                continue
            _stats["events"] += 1
            ev = _parse_trade(raw)
            if ev and ev["is_buy"] and ev["sol_amount"] >= 0.01:
                _record_buy(ev["mint"], ev["sol_amount"], ev["user"])
    except Exception:
        _stats["errors"] += 1


# ── WebSocket listener ────────────────────────────────────────────────────────

async def _listen() -> None:
    """Connect to Helius and stream pump.fun program logs. Auto-reconnects."""
    global connected
    try:
        import websockets
    except ImportError:
        print(
            "[BondingCurve] 'websockets' package not installed — "
            "run: pip install websockets",
            flush=True,
        )
        return

    _backoff = 5
    while True:
        try:
            async with websockets.connect(
                _WS_URL,
                ping_interval=20,
                ping_timeout=10,
                max_size=2**20,
            ) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id":      1,
                    "method":  "logsSubscribe",
                    "params":  [
                        {"mentions": [PUMP_FUN_PROGRAM]},
                        {"commitment": "confirmed"},
                    ],
                }))
                connected = True
                _backoff = 5  # reset on successful connect
                print(
                    "[BondingCurve] Connected — watching pump.fun live "
                    f"(whale≥◎{WHALE_BUY_SOL}  hot≥◎{HOT_TOTAL_SOL}/{HOT_BUY_COUNT}buys)",
                    flush=True,
                )
                async for raw_msg in ws:
                    _process(json.loads(raw_msg))
        except Exception as e:
            connected = False
            print(
                f"[BondingCurve] Disconnected ({type(e).__name__}: {e}) "
                f"— reconnecting in {_backoff}s",
                flush=True,
            )
            await asyncio.sleep(_backoff)
            _backoff = min(_backoff * 2, 120)  # cap at 2 minutes


# ── Public API ────────────────────────────────────────────────────────────────

def get_hot_mints(max_age_seconds: int = 300) -> list:
    """
    Return mints with strong recent buy pressure.
    Called by discovery.py every DISCOVERY_REFRESH_CYCLES.
    """
    now = time.time()
    result = []
    for mint in _hot_mints:
        rec = _activity.get(mint)
        if rec and rec["buys"] and rec["buys"][-1][0] > now - max_age_seconds:
            result.append(mint)
    return result


def get_buy_activity(mint: str) -> Optional[dict]:
    """
    Return aggregated buy stats for a specific mint.
    Used by the strategy engine to boost confidence on BC-sourced tokens.
    """
    rec = _activity.get(mint)
    if not rec:
        return None
    now    = time.time()
    cutoff = now - HOT_WINDOW_SECS
    recent = [(ts, s) for ts, s in rec["buys"] if ts > cutoff]
    return {
        "total_sol_5m":  round(sum(s for _, s in recent), 4),
        "buy_count_5m":  len(recent),
        "largest_buy":   round(rec["largest"], 4),
        "unique_buyers": len(rec["buyers"]),
    }


def get_top_whales(n: int = 3) -> list:
    """
    Return the top N whale wallets ranked by total SOL spent this session.
    Each entry: {wallet, total_sol, buy_count, largest_buy, mint_count}.
    """
    ranked = sorted(
        [
            {
                "wallet":     k,
                "total_sol":  round(v["total_sol"], 4),
                "buy_count":  v["buy_count"],
                "largest_buy": round(v["largest"], 4),
                "mint_count": len(v["mints"]),
            }
            for k, v in _whale_tracker.items()
        ],
        key=lambda x: x["total_sol"],
        reverse=True,
    )
    return ranked[:n]


def status_line() -> str:
    """One-line status for startup/heartbeat logs."""
    return (
        f"{'🟢' if connected else '🔴'} BondingCurve | "
        f"events={_stats['events']} buys={_stats['buys']} "
        f"whales={_stats['whales']} hot={_stats['hot']}"
    )


def start() -> None:
    """
    Schedule the bonding curve listener as a background asyncio task.
    Must be called from within an active asyncio event loop (i.e. inside main()).
    """
    asyncio.ensure_future(_listen())
    print("[BondingCurve] Starting pump.fun bonding curve monitor...", flush=True)


# ── Gradient Scanner ──────────────────────────────────────────────────────────
# Queries on-chain bonding curve accounts for the top-N hot mints and surfaces
# high-gradient / early-stage setups as Force-FIRE signals.
#
# Signal construction (hybrid):
#   Gradient  = total_sol_5m / 5  (SOL/min from the 5-min WebSocket window)
#               Smoother than 30s RPC delta; immune to missed events.
#   Stage     = real_sol_reserves from on-chain account via getMultipleAccounts
#               Authoritative: the WS event stream can't tell us how far along
#               the curve a token already is.
#
# Thresholds:
#   gradient ≥ 0.5 SOL/min  — meaningful capital commitment, above HOT baseline
#   real_sol  < 30 SOL       — early stage (graduation ~85 SOL = only ~35% in)
#
# BondingCurve account Borsh layout (8-byte Anchor discriminator prefix):
#   +8   virtual_token_reserves  u64  (raw token units)
#   +16  virtual_sol_reserves    u64  (lamports) — used for price calculation
#   +24  real_token_reserves     u64
#   +32  real_sol_reserves       u64  (lamports) — actual SOL deposited
#   +40  token_total_supply      u64
#   +48  complete                bool — True = graduated to Raydium

_GRADIENT_MIN_SOL_PER_MIN = 0.5   # 0.5 SOL/min — genuine capital flow
_CURVE_SOL_MAX            = 30.0  # early stage: < 30 of ~85 SOL to graduation


def _get_curve_pda(mint_str: str) -> Optional[str]:
    """Derive the pump.fun BondingCurve PDA for a given token mint."""
    try:
        mint_key = Pubkey.from_string(mint_str)
        prog_key = Pubkey.from_string(PUMP_FUN_PROGRAM)
        pda, _   = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_key)],
            prog_key,
        )
        return str(pda)
    except Exception:
        return None


def _parse_curve_account(b64data: str) -> Optional[dict]:
    """
    Parse a pump.fun BondingCurve account from base64-encoded account data.
    Returns None if data is missing, too short, or malformed.
    """
    try:
        raw = base64.b64decode(b64data)
        if len(raw) < 49:
            return None
        # skip 8-byte Anchor discriminator
        virtual_tokens = struct.unpack_from("<Q", raw,  8)[0]
        virtual_sol    = struct.unpack_from("<Q", raw, 16)[0]
        real_sol_lamps = struct.unpack_from("<Q", raw, 32)[0]
        complete       = bool(raw[48])
        return {
            "virtual_tokens": virtual_tokens,          # raw token units
            "virtual_sol":    virtual_sol,             # lamports
            "real_sol":       real_sol_lamps / 1e9,    # SOL
            "complete":       complete,
        }
    except Exception:
        return None


async def _batch_get_curve_accounts(
    client,
    rpc_url: str,
    pda_addresses: list,
    mint_addresses: list,
) -> dict:
    """
    Fetch bonding curve accounts for a list of (mint, pda) pairs.
    Returns {mint: parsed_state_or_None}.
    Single getMultipleAccounts RPC call — cheap regardless of n.
    """
    if not pda_addresses:
        return {}
    try:
        # asyncio.wait_for is a hard wall-clock ceiling — httpx's per-read timeout can be
        # defeated by a server that drip-streams the response body just inside the window,
        # resetting the read timer indefinitely (observed in production as a stuck cycle
        # holding an ESTABLISHED Helius connection for minutes). This runs every cycle in
        # INSANE mode, so an unbounded hang here freezes the whole bot.
        resp = await asyncio.wait_for(
            client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id":      "bc_gradient",
                    "method":  "getMultipleAccounts",
                    "params":  [
                        pda_addresses,
                        {"encoding": "base64", "commitment": "confirmed"},
                    ],
                },
                timeout=3.0,
            ),
            timeout=5.0,
        )
        resp.raise_for_status()
        accounts = resp.json().get("result", {}).get("value", [])
        result: dict = {}
        for mint, acc in zip(mint_addresses, accounts):
            if acc and isinstance(acc.get("data"), list) and acc["data"]:
                result[mint] = _parse_curve_account(acc["data"][0])
            else:
                result[mint] = None
        return result
    except Exception as e:
        print(f"[BondingCurve] Gradient RPC error: {e}", flush=True)
        return {}


async def scan_gradient(
    client,
    rpc_url: str,
    sol_price: float,
    n: int = 5,
) -> list:
    """
    Identify high-gradient / early-stage pump.fun bonding curves.

    Algorithm:
      1. Rank the top N hot mints by 5m SOL flow from the WebSocket activity tracker.
      2. Fetch their bonding curve accounts via getMultipleAccounts.
      3. Keep mints where gradient >= 0.5 SOL/min AND real_sol < 30 SOL.

    Returns a list of dicts, each describing one Force-FIRE candidate:
      {mint, gradient_sol_per_min, real_sol, price_usd, bc_activity}

    price_usd is approximated from the bonding curve virtual reserves
    (pump.fun uses 6-decimal tokens):
      price_sol = virtual_sol_lamports / virtual_tokens_raw × 10^3
      price_usd = price_sol × sol_price
    """
    # Rank hot mints by 5m SOL flow — gradient proxy from the WS 5-min window
    candidates = []
    for mint in get_hot_mints(max_age_seconds=300):
        act = get_buy_activity(mint)
        if act and act.get("total_sol_5m", 0) > 0:
            candidates.append((mint, act))
    candidates.sort(key=lambda x: x[1]["total_sol_5m"], reverse=True)
    candidates = candidates[:n]

    if not candidates:
        return []

    # Build (mint, pda) pairs — skip mints where PDA derivation fails
    mints, pdas = [], []
    for mint, _ in candidates:
        pda = _get_curve_pda(mint)
        if pda:
            mints.append(mint)
            pdas.append(pda)

    curve_data = await _batch_get_curve_accounts(client, rpc_url, pdas, mints)

    results = []
    for mint, act in candidates:
        if mint not in curve_data:
            continue
        state = curve_data[mint]
        if not state or state.get("complete"):
            continue  # graduated to Raydium or unreadable

        gradient = act["total_sol_5m"] / 5   # SOL/min (5-min window → per-min rate)
        real_sol = state["real_sol"]

        if gradient < _GRADIENT_MIN_SOL_PER_MIN:
            continue
        if real_sol > _CURVE_SOL_MAX:
            continue

        # Approximate price from virtual AMM (pump.fun uses 6-decimal tokens)
        vt = state.get("virtual_tokens", 0)
        vs = state.get("virtual_sol", 0)
        # virtual_sol (lamports) / virtual_tokens (raw) × 1e3 = SOL per token unit
        price_sol = vs / vt / 1e3 if vt > 0 else 0.0
        price_usd = price_sol * sol_price

        results.append({
            "mint":                 mint,
            "gradient_sol_per_min": round(gradient, 3),
            "real_sol":             round(real_sol, 2),
            "price_usd":            price_usd,
            "bc_activity":          act,
        })

    if results:
        print(
            f"[BondingCurve] ◆ {len(results)} gradient signal(s): "
            + "  ".join(
                f"{r['mint'][:6]}.. ◎{r['gradient_sol_per_min']:.2f}/min @{r['real_sol']:.0f}◎"
                for r in results
            ),
            flush=True,
        )

    return results
