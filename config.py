import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

_bot_id = int(os.getenv("BOT_ID", "1"))
_per_bot_key = os.getenv(f"HELIUS_API_KEY_{_bot_id}", "")
HELIUS_API_KEY  = _per_bot_key or os.getenv("HELIUS_API_KEY", "")
# RPC node: set RPC_URL in .env to route through a custom node; otherwise use Helius.
_RPC_OVERRIDE   = os.getenv("RPC_URL", "").strip()
HELIUS_RPC_URL  = _RPC_OVERRIDE or f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API_URL  = "https://api.helius.xyz"
FALLBACK_RPC_URL = "https://api.mainnet-beta.solana.com"  # used when Helius rate-limits
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")

# S89: api.jup.ag is Jupiter's KEYED/paid host — hitting it keyless lands in the most-throttled
# shared bucket (the ~60% swap-429 failure rate). lite-api.jup.ag is the documented FREE tier
# (no key, higher/cleaner limits for keyless use). Switch host to fix sustained capacity.
# To move to a paid plan later: set these back to api.jup.ag + add an `x-api-key` header in jupiter.py.
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1"
JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v2"

KEYPAIR_PATH = os.path.expanduser(os.getenv("KEYPAIR_PATH", "~/.config/solana/id.json"))
TOTAL_CAPITAL_USD = float(os.getenv("TOTAL_CAPITAL_USD", "100.0"))

MAX_POSITION_PCT = 0.75       # USD backstop in risk.py — aligned with hard cap so SOL-side sizing is the real constraint
MIN_POSITION_USD = 0.10       # legacy USD fallback
MAX_DAILY_LOSS_PCT = 0.10     # halt if down 10% on the day
MIN_LIQUIDITY_USD = 50_000    # skip tokens below this
MAX_SLIPPAGE_BPS = 500        # 5% slippage — was 3% but Custom:6001 errors frequent on fast-moving meme tokens
STOIC_SLIPPAGE_BPS = 100      # 1% slippage — Stoic mode (tighter, higher-liquidity entries only)

# ── SOL-native capital management ────────────────────────────────────────────
# Bot sizes every trade from actual wallet SOL balance, not a fixed env var.
# Sending more SOL to the wallet automatically raises all position sizes.
RESERVE_SOL          = 0.05   # always keep this much SOL liquid for gas fees
MAX_DEPLOYED_SOL_PCT = 1.00   # universal ceiling — max 100% of wallet in open trades (all modes)
MIN_POSITION_SOL     = 0.003  # floor — Jupiter won't route below this reliably

# ── Per-trade position sizing — size tier (dashboard toggle) ─────────────────
# The size tier sets the MAXIMUM a single trade can use as % of wallet balance.
# Actual size = confidence × tier_max × sol_balance, capped by remaining headroom.
# Three tiers selectable via dashboard; default medium.
SIZE_TIER_PCTS = {
    "small":  0.50,   # 50% MAX — conservative, max 50% of wallet per trade
    "medium": 0.75,   # 75% MAX — balanced, max 75% of wallet per trade
    "large":  1.00,   # 100% MAX — aggressive, max 100% of wallet per trade (matches hard cap)
}

# ── Confidence curve for position sizing ──────────────────────────────────────
# The tier ceiling is the MAXIMUM, not the target. It is only reached at conf=1.0.
# At the mode's minimum confidence threshold, only this fraction of the ceiling is used.
# Remaps [conf_min → 1.0] to [SIZE_BASE_FRACTION → 1.0] of the tier ceiling.
# Example at 75% tier (large), ◎1.2 wallet, INSANE (conf_min=0.50):
#   conf=0.50 → 10% of 75% → ◎0.09   (bare minimum — small exploratory trade)
#   conf=0.70 → 46% of 75% → ◎0.41   (decent track record)
#   conf=0.90 → 82% of 75% → ◎0.74   (high conviction)
#   conf=1.00 → 100% of 75% → ◎0.90  (exceptional — full ceiling)
# S105 (fee-bleed fix): raised 0.10 -> 0.20. Diagnosis (3h dig, 2026-06-09) found the fleet's
# real bleed is UNBOOKED tip/priority fees (~0.0005 SOL/round-trip, invisible to pnl_sol) at
# INSANE-mode frequency: a fresh conf~0.6 token gets _conf_sq~0.04 -> _size_frac~0.095 -> a
# ~0.04 SOL trade where the fee is ~1.3%. Doubling the base floor ~doubles fresh-token size ->
# ~halves the fee-as-%-of-position. Pairs with strict_gate 65->70 (fewer, higher-conviction
# trades). C2-only knob: the EV-sizing override (dormant) supersedes it when armed -> does NOT
# touch ev_sizing.json (ONE OPERATOR RULE intact). The per-trade tier ceiling (small=50% wallet)
# still caps each position. Revert: SIZE_BASE_FRACTION 0.20->0.10 + restart.
SIZE_BASE_FRACTION = 0.20

# ── Position stacking (INSANE mode only) ─────────────────────────────────────
# When confidence in an open position is very high, the bot can add to it rather
# than skipping the signal. Stacks use half the normal position size so capital
# builds gradually. Max 2 stacks = 3 entries total per token.
STACK_CONFIDENCE_MIN  = 0.75   # proven win rate required before stacking
MAX_STACK_COUNT       = 2      # max additional entries on top of the initial
STACK_SIZE_MULTIPLIER = 0.5    # each stack is 50% of the computed fresh-entry size

# ── Discovery token safeguards ────────────────────────────────────────────────
# Prevents oversized re-entries on tokens that are already overbought or
# have only one trade in their history (conf=1.00 from a single win).
DISCOVERY_OVERBOUGHT_1H_PCT  = 12.0  # 1h gain above this = move is mature, skip (was 25 — 12+ on a meme token is late-stage)
DISCOVERY_CONF_CAP           = 0.70  # confidence ceiling for discovery tokens with < N trades
DISCOVERY_CONF_MIN_TRADES    = 3     # trades needed before confidence cap is lifted

POLL_INTERVAL_SECONDS = 30

# ── Smart-Trail (Volatility-Adjusted Trailing Take-Profit) ──────────────────
# Activates only after the fixed TP target is hit. Instead of exiting at TP,
# the bot holds and trails with a volatility buffer below the rolling peak.
# Exits when price drops > (MULTIPLIER × local std dev below peak).
SMART_TRAIL_MULTIPLIER     = 2.0   # N × local price std dev for trail buffer
SMART_TRAIL_HISTORY_LEN    = 10    # price observations kept (~5 min at 30s cycles)
SMART_TRAIL_MIN_BUFFER_PCT = 0.5   # floor: trail never tighter than 0.5% below peak
SMART_TRAIL_MAX_BUFFER_PCT = 8.0   # ceiling: trail never wider than 8% below peak

# ── Dynamic TP1 ───────────────────────────────────────────────────────────────
# High-velocity tokens tend to reverse before reaching the standard TP1 gate.
# If the recent high-low range exceeds the threshold, TP1 fires earlier.
DYNAMIC_TP1_VOL_WINDOW    = 3    # price samples in the volatility window (~90s at 30s cycles)
DYNAMIC_TP1_VOL_THRESHOLD = 3.0  # high-low range % that signals a high-velocity token
DYNAMIC_TP1_EARLY_PCT     = 5.0  # early TP1 level used instead of tier default (e.g. 7% → 5%)

# ── Dynamic token discovery ───────────────────────────────────────────────────
# In INSANE mode, trending tokens from DexScreener are added each cycle.
# Discovery refreshes every N cycles; discovered tokens need min liquidity to qualify.
DISCOVERY_REFRESH_CYCLES  = 3      # re-fetch every 3 cycles (~90s) — all modes
MIN_DISCOVERY_LIQUIDITY   = 100_000 # standard path: $100k+ liquidity
MIN_DISCOVERY_VOLUME_5M   = 2_500   # and $2.5k+ 5m volume (session 52: lowered from 5k to catch earlier-stage tokens)
MIN_DISCOVERY_VOLUME_24H  = 50_000  # and $50k+ 24h volume

# ── Per-mode discovery sizing ─────────────────────────────────────────────────
# Quality gates in evaluate() are the real filter — discovery just widens the net.
# More candidates = more chances to find tokens that pass high-conviction gates.
#   STOIC  gets trending pages only — new tokens fail its vol-spike/conf gates anyway
#   WILD   gets trending + some new_pools — moderate net, learning pace
#   INSANE gets everything — max velocity, max candidate pool
DISCOVERY_MAX_BY_MODE = {
    "stoic":  15,   # + 3 static HIGH  ≈  18 total  (target: 10–20)
    "wild":   65,   # + 7 static MID   ≈  72 total  (target: 50–80)
    "insane": 120,  # + 11 static LOW  ≈  131 total (session 52: raised from 85 to absorb 8 trending pages)
}
ACTIVE_WATCHLIST_CAP = {
    "stoic":  80,   # S79: was 20 — Bot3 (STOIC) is the race's deep_pool control but a 20-token
                    # universe gave it ~6.5x fewer shots at the rare deep_pool_strict_filling token
                    # than insane (130) → 0 signals / 0 trades all-time. 80 matches wild so it
                    # actually sees the edge candidates. Mode trigger thresholds (MODES) unchanged —
                    # still stoic-conservative on what it pulls the trigger on. Revert: -> 20.
    "wild":   80,
    "insane": 130,  # session 52: raised from 100 to match expanded discovery max
}
DISCOVERY_PAGES_BY_MODE = {
    "stoic":  {"trending": 2, "new_pools": 0, "deep": False},
    "wild":   {"trending": 3, "new_pools": 1, "deep": False},
    "insane": {"trending": 3, "new_pools": 2, "deep": False},  # S91: 8→3, deep True→False. The
    #          8-page trending fetch overlapped the deep=pages-4-5 fetch (pages 4-5 requested
    #          TWICE/cycle) and 12 gecko calls/cycle blew the free-tier ~30/min limit → ~50% empty
    #          cycles. trending 3 + new_pools 2 = 5 calls/cycle (10/min), under budget → 0 empty
    #          cycles, ~77 gecko-priced/cycle (over-fills the S91 BC-reserve, which needs ~60).
    #          deep (pages 4-5) dropped: it 429'd every cycle on the free tier for no benefit (the
    #          reserve is already full). Re-enable deep + raise trending IF a paid CoinGecko key is
    #          added (higher limit). (session 52 had raised trending 3→8, creating the redundancy.)
}
MAX_DISCOVERY_TOKENS = 85  # legacy alias used by discovery.py default

# ── Circuit breaker — consecutive-loss cool-off ───────────────────────────────
# Fires when N losses happen within the lookback window. Blocks new entries for
# the cooloff period, then resets automatically. Exits are never blocked.
CIRCUIT_BREAKER_LOSSES    = 3    # losses within window before cooloff
CIRCUIT_BREAKER_WINDOW_M  = 15   # lookback window in minutes (was 20 — tighter window = fewer false trips)
CIRCUIT_BREAKER_COOLOFF_M = 12   # entry freeze duration in minutes

# ── Conviction multiplier — elite signal size-up ──────────────────────────────
# When a signal clears the elite bar, size the initial entry larger than normal.
# Both multipliers are capped by remaining headroom — never exceed the 75% ceiling.
CONVICTION_BC_WHALE_MULT  = 1.40  # BC whale + conf≥0.75 + b/s≥2.0 → 40% larger
CONVICTION_ELITE_MULT     = 1.25  # top-20% momentum+b/s percentile → 25% larger

# ── Gem path — lower bars for tokens that look like early hidden gems ─────────
# A token 1-12h old with strong buy pressure doesn't need $100k liquidity yet.
# The safety net is: RugCheck still runs, plus the tighter signal gates below.
GEM_MIN_LIQUIDITY   = 50_000   # $50k is enough for real routing via Jupiter
GEM_MAX_AGE_HOURS   = 12       # only for tokens fresh enough to still have upside
GEM_MIN_BUY_RATIO   = 1.5      # buyers clearly dominating — not a random spike
GEM_MIN_VOLUME_5M   = 3_500    # $3.5k in 5m — raised from $2k: low-thrust tokens rarely reach GEM TP1 (+7%)
MOMENTUM_THRESHOLD = 0.03     # 3% price move in last interval triggers signal
VOLUME_SPIKE_MULTIPLIER = 2.0 # volume must be 2x recent average

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT  = "So11111111111111111111111111111111111111112"
BASE_MINT = SOL_MINT   # Bot accumulates SOL — all trades: SOL → token → SOL

# ── Goldilocks override (auto-generated by goldilocks.py --emit-override) ─────
# Loaded at startup and reloaded every 20 trades. stoic_strategy.apply_overrides()
# then mutates MODES and INSANE_TIER_PARAMS in-place so changes take effect immediately.
OVERRIDE_PATH = Path(__file__).parent / "thresholds_override.json"
_overrides: dict = {}


def load_overrides() -> dict:
    """Read thresholds_override.json and cache in _overrides. Returns the dict."""
    global _overrides
    if OVERRIDE_PATH.exists():
        try:
            _overrides = json.loads(OVERRIDE_PATH.read_text())
        except Exception as e:
            print(f"[Config] Override load error: {e}")
    return _overrides


load_overrides()

# ── Marketcap tiers ──────────────────────────────────────────────────────────
# Tier selection is CUMULATIVE — lower tiers scan a wider universe:
#   HIGH → 3 tokens  (blue chips only — tight, trusted, fewer opportunities)
#   MID  → 7 tokens  (mid-caps + blue chips — balanced)
#   LOW  → 11 tokens (4 low-caps + 4 MID + 3 HIGH — widest net, most volatile)
#
# INSANE uses WATCHLISTS_EXACT (non-cumulative): LOW=4, MID=4, HIGH=3.
# (DATBIHGAH removed after 0W/2L; static tokens fully banned as of session 20 —
#  bot runs discovery-only until bans expire.)
#
# The raw lists define membership; WATCHLISTS_BY_CAP builds the cumulative views.
#
# Liquidity verified via DexScreener /latest/dex/tokens/{mint} 2026-05-31:
#   BOME $10.2M | MEW $8.5M | TRUMP $34.7M | PENGU $3.7M | POPCAT $3.3M
#   WIF $4.8M | RAY $3.9M | PYTH $304k | JUP $858k | BONK $779k
#   WEN $41k (below $50k MIN_LIQUIDITY_USD — will be skipped in evaluate())
WATCHLIST_LOW = [
    # WEN removed (session 57): $41k liq / ~$867 5m vol — became an unsellable ghost
    #   close (-◎0.22). Static bypass let it take a full GEM-size entry on a dead pool.
    # BOME removed (session 57): conf 0.25 after 5 consecutive losses — burned-out edge.
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr", # POPCAT — Solana cat meme ($3.3M liq)
    "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",  # PENGU  — Pudgy Penguins ($3.7M liq)
]
WATCHLIST_MID = [
    "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",  # MEW   — cat in a dogs world ($8.5M liq)
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3", # PYTH  — oracle protocol ($304k liq)
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", # RAY   — Raydium DEX ($3.9M liq)
    "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",  # TRUMP — Official Trump ($34.7M liq)
]
WATCHLIST_HIGH = [
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", # BONK — Solana blue-chip meme ($779k liq)
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",  # JUP  — Jupiter aggregator ($858k liq)
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", # WIF  — dogwifhat ($4.8M liq)
]
WATCHLISTS_BY_CAP = {
    "high": WATCHLIST_HIGH,                                       # 3 — blue chips only
    "mid":  WATCHLIST_MID + WATCHLIST_HIGH,                       # 7 — mid + blue chips
    "low":  WATCHLIST_LOW + WATCHLIST_MID + WATCHLIST_HIGH,       # 9 — full universe (WEN/BOME removed)
}

# Non-cumulative tiers — used by INSANE mode only.
# INSANE is designed for volatile low-caps (3% TP / 45min hold).
# Blue chips (JUP, BONK, WIF) and mid-caps (PYTH, RAY) can't hit that target
# consistently and create a structural negative edge in INSANE mode.
# WILD and STOIC keep cumulative tiers (they want depth + blue chip exposure).
WATCHLISTS_EXACT = {
    "high": WATCHLIST_HIGH,   # 3 — blue chips only
    "mid":  WATCHLIST_MID,    # 4 — mid-caps only (no blue chips)
    "low":  WATCHLIST_LOW,    # 2 — low-caps only (POPCAT, PENGU)
}

# Full universe — used as fallback and for sweep/safety checks
WATCHLIST = WATCHLIST_LOW + WATCHLIST_MID + WATCHLIST_HIGH

# ── Market Impact Predictor + TWAP execution ──────────────────────────────────
# Before broadcasting a buy, a dual-sample Jupiter quote measures real-time price
# impact (probe size vs target size). If the measured degradation exceeds
# TWAP_IMPACT_THRESHOLD, the atomic swap is replaced by a TWAP split:
# TWAP_TRANCHES equal sub-transactions executed over sequential Solana blocks,
# smoothing entry across the bonding curve on low-liquidity gems.
TWAP_IMPACT_THRESHOLD    = 2.0   # % — price impact above this triggers TWAP split
TWAP_TRANCHES            = 3     # sub-transactions per TWAP split
TWAP_INTER_TRANCHE_DELAY = 2.0   # seconds between tranches (~5 Solana blocks @ 400ms/block)
