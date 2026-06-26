"""
DATBOI STRATEGY ENGINE
--------------------------
Core mission: Accumulate SOL. Reach 2.0. Repay 1.0 to the wallet that funded this.
This bot exists to settle a debt. Every trade, every cycle, every gain serves that purpose.
The funding wallet extended trust. The bot's only job is to honour it.

Three operating modes:

WILD   -- Learning mode. Frequent trades, fast cycles, moderate gates.
          Trains the memory system. Burns gas. Discovers what works.
          TP: +4% | SL: -2.5% | Hold: 2h | 3 trades/cycle | 10–20% per trade

STOIC  -- Discipline mode. Waits for undeniable signals. Quality over quantity.
          Requires momentum, volume spike, buy pressure, and proven confidence.
          TP: +8% | SL: -4% | Hold: 8h  | 1 trade/cycle | 5–10% per trade

INSANE -- Sprint mode. Maximum capital velocity. No patience.
          Catches pumps and exits fast. For when the debt must be settled.
          TP: +3% | SL: -2% | Hold: 45m | All qualifying signals/cycle | 20–40% per trade

Position sizing (all modes):
  SOL-native: size_frac(confidence) × tier_max_pct × sol_balance, capped by headroom.
  Confidence is remapped from [mode_min, 1.0] → [10%, 100%] of the tier ceiling.
  50% absolute ceiling on total deployed capital across all modes.

Exit rules are checked every cycle. No overrides. No bag holding.
Positions persisted to positions.json -- restarts don't lose open trades.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import memory
import regime_policy as _regime_policy   # TAXONOMY: exit-family resolver (fail-open; no-op without the canary)
try:
    from bot_config import BOT_ID as _TAXO_BOT_ID   # TAXONOMY: which bot's canary governs the family resolver
except Exception:                                    # fail-open: unknown bot → canary reads False → legacy flags rule
    _TAXO_BOT_ID = 0
from config import (
    WATCHLIST,
    SMART_TRAIL_MULTIPLIER, SMART_TRAIL_HISTORY_LEN,
    SMART_TRAIL_MIN_BUFFER_PCT, SMART_TRAIL_MAX_BUFFER_PCT,
    DYNAMIC_TP1_VOL_WINDOW, DYNAMIC_TP1_VOL_THRESHOLD, DYNAMIC_TP1_EARLY_PCT,
    DISCOVERY_OVERBOUGHT_1H_PCT, DISCOVERY_CONF_CAP, DISCOVERY_CONF_MIN_TRADES,
)

from bot_config import DATA_DIR
POSITIONS_PATH      = DATA_DIR / "positions.json"
MODE_PATH           = DATA_DIR / "trading_mode.json"
MARKETCAP_PATH      = DATA_DIR / "marketcap_mode.json"
SIZE_MODE_PATH      = DATA_DIR / "size_mode.json"
VIRAL_WEIGHT_PATH   = DATA_DIR / "viral_weight.json"

# ── S72: Size-aware breakeven ratchet (ships DORMANT) ───────────────────────────
# Extends the BE✓ "can't-lose" protection (today only fires AFTER a TP1 partial) to
# BIGGER positions: the larger a bet's share of the wallet, the earlier + tighter its
# stop-loss ratchets toward breakeven — WITHOUT needing the partial first. This is the
# safety rail the SIZE lever (PATH TO PRESTIGE) needs: a sized-up bet's downside is
# bounded the moment it's meaningfully green, so size can grow without betting the wallet.
# It keys off wallet_frac (size as a share of wallet at open), so when EV-sizing later
# makes a 12% bet, the ratchet protects it automatically — independent of how the size
# was decided (C² conf OR ev_lo).
#
# Grounded in the brain's own data, not pulled from thin air:
#   • arm levels (+5/+6%) ≈ the median winner (+5.6%, S70) → by the time a bet is up
#     this much, a pullback to the floor is a genuine reversal, not entry noise;
#   • the medium-band floor (−2%) sits inside S67's "99% of winners survive −4%, 100%
#     survive −8%" → it protects capital without churning winners out on normal vol.
#
# ⚠ HONEST: a breakeven stop is NOT free EV — a token that spikes, dips below entry,
# then runs would be cut. So it's (a) gated/canary for live A/B, (b) applied only to
# BIGGER bets where capital protection outweighs the give-up, and (c) cushioned (−2%)
# for the medium band. Small bets keep the existing TP1→BE path untouched.
#
# Per-bot hot-reload canary (mirrors strict_gate / ev_sizing / sidecar_liqvel): enable
# on ONE bot via bots/botN/be_ratchet.json {"enabled": true} — 30s hot-reload, no
# restart, reversible (rm the file). Module master False = byte-for-byte current sizing.
_BE_RATCHET_ENABLED = False                    # module master (fleet-wide) — default OFF
_BE_RATCHET_PATH    = DATA_DIR / "be_ratchet.json"
_be_ratchet_cache   = (0.0, False)             # (read_ts, enabled) — 30s cache off the hot path
# (wallet_frac threshold, arm-at pnl %, breakeven floor %) — checked LARGEST first;
# first band whose threshold the position clears wins. Below the smallest threshold →
# no ratchet (tiny bets keep TP1→BE only).
_BE_RATCHET_BANDS = (
    (0.10, 5.0,  0.0),   # ≥10% of wallet: arm at +5% → TRUE breakeven (can't lose)
    (0.05, 6.0, -2.0),   # 5–10% of wallet: arm at +6% → −2% cushioned floor
)

def _be_ratchet_on() -> bool:
    """True when THIS bot should size-scale the breakeven floor on bigger positions."""
    global _be_ratchet_cache
    if _BE_RATCHET_ENABLED:
        return True
    if time.time() - _be_ratchet_cache[0] < 30.0:
        return _be_ratchet_cache[1]
    on = False
    try:
        on = bool(json.loads(_BE_RATCHET_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _be_ratchet_cache = (time.time(), on)
    return on


# ── S87 / B-step-2: catastrophic single-cycle LP-drain exit ──────────────────
# The 2-strike LP-drain guard (≥15% ×2 consecutive cycles) structurally CANNOT catch
# a one-cycle full rug — an 81%-in-a-single-cycle LP pull (observed S87: a $6.75M pool
# → $1.24M, then ridden to −100% / daily-loss halt) skips straight past it to a −100%
# stop. This is a 1-STRIKE catastrophic exit: a ≥50% pool collapse in a SINGLE cycle →
# sell NOW while a route still exists. Guarded against the S60 false-empty-read glitch
# by requiring the POST-drop pool to still be credibly sellable (≥$30k) — a near-zero
# read is ambiguous (RPC glitch vs full rug) and is left to the 2-strike / ghost
# machinery rather than 1-strike-panic-selling on it. Per-bot hot-reload canary,
# default OFF, reversible (rm the file). A/B: enable on ONE bot first.
_CATASTROPHIC_DRAIN_ENABLED = False                 # module master (fleet-wide) — default OFF
_CATASTROPHIC_DRAIN_PATH    = DATA_DIR / "catastrophic_drain.json"
_catastrophic_drain_cache   = (0.0, False)          # (read_ts, enabled) — 30s cache off the hot path
_CATASTROPHIC_DRAIN_PCT     = -0.50                 # ≥50% pool drop in ONE cycle = rug in progress
_CATASTROPHIC_MIN_LIQ       = 30_000.0              # post-drop pool must still be sellable (else defer to 2-strike)

def _catastrophic_drain_on() -> bool:
    """True when THIS bot should 1-strike-exit a ≥50% single-cycle LP collapse."""
    global _catastrophic_drain_cache
    if _CATASTROPHIC_DRAIN_ENABLED:
        return True
    if time.time() - _catastrophic_drain_cache[0] < 30.0:
        return _catastrophic_drain_cache[1]
    on = False
    try:
        on = bool(json.loads(_CATASTROPHIC_DRAIN_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _catastrophic_drain_cache = (time.time(), on)
    return on

# ── vol_accel exit A/B (paper-lab quest finding #5) ──────────────────────────
# The 10-h paper quest found a FIXED take-profit DESTROYS the volume-acceleration
# edge by clipping its fat right tail (tp15 → ev_lo −0.6 vs hold +3.95); the winning
# exit = a real stop-loss + a trail that RIDES. This canary remaps a bot's exits to
# that policy: SL −10%, trail ACTIVATES at +10% and rides via the existing smart-trail
# (no fixed-TP bank), TP1 early-partial disabled (don't bank the tail early). Reuses the
# battle-tested smart-trail rather than a hand-rolled retain-%. Per-bot hot-reload,
# default OFF. A/B: enable on Bot1 only, vs Bot2/3. The protective guards
# (catastrophic / LP-drain / dead-pool / max-hold) sit ahead of this and are UNAFFECTED.
_VOL_ACCEL_EXIT_ENABLED = False                     # module master (fleet-wide) — default OFF
_VOL_ACCEL_EXIT_PATH    = DATA_DIR / "vol_accel_exit.json"
_vol_accel_exit_cache   = (0.0, False)              # (read_ts, enabled) — 30s cache off the hot path
_VAE_TRAIL_ACT_PCT      = 10.0                      # trail activates here (RIDE, not a bank)
_VAE_STOP_LOSS_PCT      = -10.0                     # quest SL: cut the left tail at −10%
# S88-debug: the quest edge is SMALL-POOL vol-acceleration breakouts (finding #2: <$50k liq is
# where the fat right tail lives — vol_accel_validate confirmed the LIVE big-liq sniper book is
# −EV and is NOT the cohort). Scope the exit remap to that cohort by entry liquidity so the
# unvalidated wide-stop / no-bank policy is NOT applied to bot1's big-liq plays (the bleeder).
# A position with no recorded entry_liq falls through to the normal exit logic (safe default).
_VAE_MAX_ENTRY_LIQ      = 50_000.0                  # only remap exits for small-pool entries (<$50k)

# S92: price-glitch guard. A single corrupted exit-price read (a 4855× quote produced bot1's
# +485,538% "TP bank" phantom — trades.db row 2701, pnl_sol +341.7 / $23,140 that never hit the
# wallet) must not fire a take-profit or pollute the trail. A REAL pump persists across reads; a
# glitch reverts. Require an absurd UPWARD spike (≥ this multiple of the last-good read) to confirm
# on TWO consecutive cycles before acting. Downward reads are NOT guarded — a genuine one-cycle rug
# (the S87 −81% pull) must still reach the catastrophic-drain / stop-loss exits.
_PRICE_GLITCH_MULT      = 5.0                        # >5× the last-good price in one read = suspect feed
# S100: the S92 two-strike "confirm" was DEFEATED by a PERSISTENTLY-glitched feed. A handful of
# near-zero-liq pump.fun tokens (repeat offender Cm6fNnMk) have a DexScreener price that returns the
# same ~5000× value every cycle → two consecutive glitched reads "confirm" the spike → the guard
# banks a phantom TP anyway (fleet-wide, 3 fresh phantoms in one day: +495805% / +245506% / +483801%,
# ◎+388 of pure fiction on ◎0.10 of real positions). FIX: an ABSOLUTE hard ceiling — a read above
# this multiple is ALWAYS a feed glitch (no real token does +1900% in a single 30-60s cycle) and is
# CLAMPED to the last-good price NO MATTER the strike count. Clamping (vs the old `continue`-skip)
# also closes a stuck-hold hole: real exit guards (SL / dead-pool / max-hold) still evaluate on the
# sane price, so a permanently-glitched position still closes — booking a REAL small pnl, never the
# fabricated one. The 5×–20× band keeps the S92 two-strike confirm so a genuine fast pump still rides.
_PRICE_GLITCH_HARD_MULT = 20.0                       # ≥20× (+1900%) in one read = always a glitch, never trust

def _vol_accel_exit_on() -> bool:
    """True when THIS bot should use the quest's no-fixed-TP / SL+trail exit policy."""
    global _vol_accel_exit_cache
    if _VOL_ACCEL_EXIT_ENABLED:
        return True
    if time.time() - _vol_accel_exit_cache[0] < 30.0:
        return _vol_accel_exit_cache[1]
    on = False
    try:
        on = bool(json.loads(_VOL_ACCEL_EXIT_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _vol_accel_exit_cache = (time.time(), on)
    return on

# S90 — MOMENTUM-GEM RIDE (per-bot canary, A/B). The Jotchua-class capture: a multi-leg viral
# momentum gem on a SELLABLE pool ($50k–$250k) gets clipped by the default gem TP1 (+5%) while
# the token legs +40% / +1000% more (Bot1's BcHE…pump fill: TP1'd +5%, token ran another +40%+).
# Extend the vol_accel_exit RIDE (no fixed-TP / SL+trail) to that cohort — but gate on the ENTRY
# MOMENTUM SIGNATURE, never liquidity alone, so it NEVER touches the −EV big-liq sniper book (the
# bleeder, the mistake S88-debug fixed). Independent canary → clean A/B. The protective guards
# (catastrophic / LP-drain / dead-pool / max-hold) and the BE-ratchet sit AHEAD and are UNAFFECTED.
_MOMENTUM_GEM_RIDE_PATH  = DATA_DIR / "momentum_gem_ride.json"
_momentum_gem_ride_cache = (0.0, False)
_MG_MOM_MIN = 3.0                    # entered ON a real move (Jotchua=6.6); flat/quick entries excluded
_MG_LIQ_LO  = 50_000.0               # above the small-pool tier
_MG_LIQ_HI  = 250_000.0              # sellable mover, not a blue-chip
_MG_TIERS   = ("gem", "momentum")    # the runner plays only — never sniper/quick

def _momentum_gem_ride_on() -> bool:
    """True when THIS bot should extend the RIDE exit to momentum-gem entries (S90 A/B)."""
    global _momentum_gem_ride_cache
    if time.time() - _momentum_gem_ride_cache[0] < 30.0:
        return _momentum_gem_ride_cache[1]
    on = False
    try:
        on = bool(json.loads(_MOMENTUM_GEM_RIDE_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _momentum_gem_ride_cache = (time.time(), on)
    return on


# ── S95 (catch-more-movers): relay-BOOST canary — deepen/extend the cross-bot relay ──
# The relay (Bot1/INSANE primes a gem → Bot2/WILD piles in at a lower conf bar) is the fleet's
# mover-propagation engine. This canary lets a bot DEEPEN the conf discount (catch MORE of Bot1's
# primes) and optionally EXTEND the relay-receive to STOIC. Default OFF → today's behavior
# (WILD only, −0.10). Per-bot, 30s hot-reload. ⚠ extend_to_stoic: Bot3 ENTERS primed movers but
# classify_play hardcodes STOIC→"bank", so those take BANK exits (not the ride); Bot3 is the gate control.
_RELAY_BOOST_PATH  = DATA_DIR / "relay_boost.json"
_relay_boost_cache = (0.0, None)
def _relay_boost() -> tuple:
    """(enabled, discount, extend_to_stoic). Default (False, 0.10, False) = unchanged behavior."""
    global _relay_boost_cache
    if _relay_boost_cache[1] is not None and time.time() - _relay_boost_cache[0] < 30.0:
        return _relay_boost_cache[1]
    out = (False, 0.10, False)
    try:
        d = json.loads(_RELAY_BOOST_PATH.read_text())
        if d.get("enabled"):
            out = (True, float(d.get("discount", 0.15)), bool(d.get("extend_to_stoic", False)))
    except Exception:
        pass
    _relay_boost_cache = (time.time(), out)
    return out

# S94b — DEEP-POOL RIDE (per-bot canary, A/B). `prove_edge.py` showed the deep_pool/brain_rule
# cohort (the deploy-gate edge) is +EV under a RIDE exit (KEPT regimes shadow +0.74% ≈ live +1.71%,
# sniper +4.0%) but −EV under a fixed-TP bank (−2.75%) — yet deep_pool entries (always ≥$50k) are
# EXCLUDED from the vol_accel ride (scoped <$50k by _VAE_MAX_ENTRY_LIQ) so today they bank at the
# fixed TP. Scoped by the ENTRY PLAY (insane_tier ∈ {deep_pool, brain_rule}) — NOT a liq band — so
# it targets exactly the proven gate cohort and can NEVER touch the gem/quick/sniper bleeder book.
# Reuses the same RIDE params (_VAE_TRAIL_ACT_PCT / _VAE_STOP_LOSS_PCT) + smart-trail. The protective
# guards (catastrophic / LP-drain / dead-pool / max-hold) + the BE-ratchet sit AHEAD and are
# UNAFFECTED. Own canary → independent A/B. Default OFF; enable on Bot1 only.
_DEEP_POOL_RIDE_PATH  = DATA_DIR / "deep_pool_ride.json"
_deep_pool_ride_cache = (0.0, False)
_DP_RIDE_PLAYS = ("deep_pool", "brain_rule")   # the gate-advancing edge plays only
# EVOLVE12 (11h paper exit-search, 2026-06-09): on the deep_pool/brain_rule cohort the production
# ride trail activates too late (+10%) → it banks `fwd` instead of capturing the move. Tightening
# the trail ACTIVATION is the dominant realizable-EV lever (decomposed +1.87pp of a +3.18pp total;
# bootstrap CI on the gain excludes 0). Apply the tighter activation to the DEEP cohort ONLY —
# NOT the gem/small ride (_mg/_small keep _VAE_TRAIL_ACT_PCT; opposite cohort, opposite optimum).
# SL is deliberately KEPT WIDE (the shared _VAE_STOP_LOSS_PCT −10): the paper search liked −6, but
# S104's 530-real-close timing study widened deep-pool stops because tight stops cut eventual
# winners (p10 dip −4.5% / p05 −7.4%) — so the contested SL tighten is NOT taken. Moderate 5%
# (not the grid-boundary 3%) for a fleet-wide no-A/B roll to limit live whipsaw. Code change → restart.
_DP_RIDE_TRAIL_ACT_PCT = 5.0

def _deep_pool_ride_on() -> bool:
    """True when THIS bot should use the RIDE exit for deep_pool/brain_rule entries (S94b A/B)."""
    global _deep_pool_ride_cache
    if time.time() - _deep_pool_ride_cache[0] < 30.0:
        return _deep_pool_ride_cache[1]
    on = False
    try:
        on = bool(json.loads(_DEEP_POOL_RIDE_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _deep_pool_ride_cache = (time.time(), on)
    return on

# S107 — MOMENTUM-OVERRIDE RIDE. The running-momentum runner lane (observer.py/main.py) admits a
# low-liq Jotchua-shape cohort the scorer gates out; it is +EV ONLY under a trail exit (every
# fixed-TP/hold policy is −EV on this data, research/evolve_s107). Give the tagged position the
# RIDE policy (no fixed-TP bank, SL+smart-trail, reusing _VAE params) keyed on the position's
# momentum_override flag (set at open_position) — same canary FILE as the admission lane, so ONE
# bots/botN/momentum_override.json toggles admission + size-down + this exit together. Protective
# guards (catastrophic / LP-drain / dead-pool / max-hold) + the BE-ratchet sit AHEAD, UNAFFECTED.
_MOM_OVERRIDE_RIDE_PATH  = DATA_DIR / "momentum_override.json"
_mom_override_ride_cache = (0.0, False)

def _momentum_override_ride_on() -> bool:
    """True when THIS bot should RIDE (no fixed-TP) the momentum-override runner cohort (S107 A/B)."""
    global _mom_override_ride_cache
    if time.time() - _mom_override_ride_cache[0] < 30.0:
        return _mom_override_ride_cache[1]
    on = False
    try:
        on = bool(json.loads(_MOM_OVERRIDE_RIDE_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _mom_override_ride_cache = (time.time(), on)
    return on

# ── S96: HOUSE-MONEY DE-RISK ("paid off" exit) ───────────────────────────────────
# When a position does INCREDIBLY well (≥ trigger, default +100% = 2×), sell just
# enough to recover the INITIAL cost basis (× a conviction-scaled cushion) and let
# the remainder ride UNCAPPED. The trade is then GREEN even if the rest rugs to 0 —
# the operator's manual Jotchua play ($50→$120, sold $70, rode $50 free), systematized.
# Composes ON TOP of the ride canaries: it banks cost-basis ONCE at the big multiple;
# the smart-trail rides the remainder. Pure DE-RISK — it only ever REDUCES exposure on
# a proven winner, so it is safe on every play/tier (can't help the −EV bleeder; only
# locks in a win). NOT an entry-sizing change (no ev_sizing.json) → THE ONE OPERATOR
# RULE is untouched.
#
# CONVICTION-AWARE SIZING (the "+EV" half of the goal): how much RIDES vs BANKS scales
# with confidence × regime-EV. High confidence in a +EV regime (euphoria/aggressive/
# sniper) → bank EXACTLY cost basis, ride the MAX free tail. Low confidence or a −EV
# regime (normal/dead) → bank cost basis + a profit cushion, ride less. So the size of
# the free-riding tail is sized to how +EV the setup is.
_HOUSE_MONEY_PATH       = DATA_DIR / "house_money.json"
_house_money_cache      = (0.0, None)
_HM_TRIGGER_PCT_DEFAULT = 100.0    # +100% (2×) before de-risking — a genuine runner
_HM_TRIGGER_FLOOR       = 40.0     # never de-risk below this gain, whatever the config says
_HM_CUSHION_MAX         = 0.6      # lowest-conviction read pulls out up to 1.6× cost basis
_HM_MIN_SELL            = 0.20     # always BANK ≥20% of a huge winner
_HM_MAX_SELL            = 0.85     # always keep ≥15% riding (the free tail)
# Regime → EV weight (the brain's 5-band: euph/aggr/sniper are the +EV regimes, normal/
# dead are −EV — S85). Caps how much conviction a high-confidence read can earn, so the
# de-risk knows "is this regime even worth riding?".
_HM_REGIME_EV = {"euphoria": 1.0, "aggressive": 0.85, "sniper": 0.70,
                 "normal": 0.35, "dead": 0.15}
_HM_REGIME_EV_DEFAULT = 0.5        # unknown/missing regime → neutral

def _house_money_cfg():
    """Per-bot canary {enabled, trigger_pct?, cushion_max?}. Returns the dict when ON,
    else None (default OFF → unchanged behaviour). 30s hot-reload."""
    global _house_money_cache
    if time.time() - _house_money_cache[0] < 30.0:
        return _house_money_cache[1]
    cfg = None
    try:
        d = json.loads(_HOUSE_MONEY_PATH.read_text())
        if d.get("enabled"):
            cfg = d
    except Exception:
        cfg = None
    _house_money_cache = (time.time(), cfg)
    return cfg

def _house_money_plan(pnl_pct, confidence, regime, cushion_max=_HM_CUSHION_MAX):
    """Pure map → (sell_fraction, conviction, recover_mult) for a house-money de-risk.

    sell_fraction = the fraction of the CURRENT position to sell so the banked value
    equals recover_mult × the initial cost basis, clamped to [_HM_MIN_SELL, _HM_MAX_SELL]
    so we always bank a meaningful slice AND always keep a free tail riding.

    conviction = normalized confidence × regime-EV weight (∈ [0,1]). recover_mult shrinks
    toward 1.0 (bank exactly cost basis, ride max) as conviction → 1, and grows toward
    1+cushion (bank a profit cushion, ride less) as conviction → 0. This is the +EV sizing:
    a high-conviction +EV runner keeps the most riding; a low-conviction/−EV pop is banked
    harder. recover_mult ≥ 1.0 ⇒ the banked SOL ≥ the initial stake ⇒ the trade is GREEN
    even if the remainder goes to zero."""
    g = pnl_pct / 100.0
    if g <= 0:
        return 0.0, 0.0, 1.0
    conf      = 0.6 if confidence is None else confidence
    conf_norm = min(1.0, max(0.0, (conf - 0.4) / 0.5))      # 0.4→0, 0.9→1 (the live conf band)
    reg_w     = _HM_REGIME_EV.get(regime, _HM_REGIME_EV_DEFAULT)
    conviction   = conf_norm * reg_w
    recover_mult = 1.0 + (1.0 - conviction) * cushion_max
    sell_frac    = recover_mult / (1.0 + g)                 # value of this slice == recover_mult × basis
    sell_frac    = min(_HM_MAX_SELL, max(_HM_MIN_SELL, sell_frac))
    return round(sell_frac, 4), round(conviction, 4), round(recover_mult, 4)

# ── S102: RUNNER FREE-RIDE — make the house-money bank REACHABLE, then ride the free tail ──
# The Jotchua dig (bot1 entry $0.001256 → +338% to date) exposed the gap: the house-money +100%
# bank is a DEAD trigger because the smart-trail caps at 8% below peak, so a legging meme (which
# retraces 20–40% BETWEEN legs) is clipped at the FIRST inter-leg dip — bot1 peaked +28.2% and was
# trailed out at +21.7%, never within reach of +100%. The de-risk mechanism the operator described
# ("bank at 2×, ride the rest free") can therefore never fire on the exact tokens it was built for.
# This widens the trail for the runner RIDE cohort (the Jotchua signature: gem/momentum, mom≥3,
# $50k–250k — the SAME gate as _momentum_gem_ride), in two stages tied to house-money state:
#   • PRE-BANK  (basis still at risk): widen to _RUNNER_PREBANK_BUFFER_PCT so the runner survives a
#     normal inter-leg pullback and can ratchet its peak toward the +100% bank. The −10% ride SL
#     (_VAE_STOP_LOSS_PCT) is the absolute floor, and the trail floor (≥ +10% once past TP) still
#     locks a minimum win — so the downside is bounded, only the give-back room is widened.
#   • POST-BANK (house_money_taken → cost basis recovered, the trade is FREE): widen to
#     _RUNNER_FREE_BUFFER_PCT and ride the free remainder through the next leg. Nothing left to
#     protect but upside, so this is where the +338% tail is captured.
# Scoped + canary'd (bot1-only A/B, mirrors how S90's ride and S95's roll-out were shipped) so the
# wider give-back never touches the −EV sniper book. Pure EXIT policy — no ev_sizing.json, no entry
# change → THE ONE OPERATOR RULE is untouched.
_RUNNER_FREERIDE_PATH      = DATA_DIR / "runner_freeride.json"
_runner_freeride_cache     = (0.0, False)
_RUNNER_FREERIDE_ENABLED   = False     # master OFF — per-bot canary opts in
_RUNNER_PREBANK_BUFFER_PCT = 18.0      # pre-bank trail cap: survive an inter-leg dip toward the bank
_RUNNER_FREE_BUFFER_PCT    = 45.0      # post-bank (basis recovered = free): ride the next leg wide

def _runner_freeride_on() -> bool:
    """Per-bot canary bots/botN/runner_freeride.json {enabled:true}. Default OFF (master flag +
    no file → unchanged behaviour). 30s hot-reload off the exit hot path."""
    global _runner_freeride_cache
    if time.time() - _runner_freeride_cache[0] < 30.0:
        return _runner_freeride_cache[1]
    on = _RUNNER_FREERIDE_ENABLED
    try:
        on = bool(json.loads(_RUNNER_FREERIDE_PATH.read_text()).get("enabled", _RUNNER_FREERIDE_ENABLED))
    except Exception:
        on = _RUNNER_FREERIDE_ENABLED
    _runner_freeride_cache = (time.time(), on)
    return on

def _runner_trail_buffer(pos: dict, in_cohort: bool) -> float:
    """S102: the FIXED smart-trail give-back for a runner free-ride position, by house-money state.
    `in_cohort` must be the Jotchua momentum-gem ride signature (the caller's `_mg`) — NOT the
    generic `ride` flag, so the wider give-back never touches the big-liq sniper book. Returns
    None for any non-cohort / canary-off position (→ the vol-scaled 8%-capped buffer is unchanged).
      • pre-bank  → _RUNNER_PREBANK_BUFFER_PCT (18%): survive an inter-leg dip toward the +100% bank
      • post-bank → _RUNNER_FREE_BUFFER_PCT   (45%): ride the FREE remainder through the next leg"""
    if not in_cohort or not _runner_freeride_on():
        return None
    return _RUNNER_FREE_BUFFER_PCT if pos.get("house_money_taken") else _RUNNER_PREBANK_BUFFER_PCT

def _be_ratchet_floor(wallet_frac, pnl_pct):
    """Pure map: the SL floor (%) a position of this wallet fraction should ratchet to,
    once it has reached its band's arm level. Returns None = no ratchet (size below the
    smallest band, missing wallet_frac, or not yet at the arm profit). Never raises."""
    try:
        if wallet_frac is None:
            return None
        for thr, arm_pct, floor_pct in _BE_RATCHET_BANDS:
            if wallet_frac >= thr:
                return floor_pct if pnl_pct >= arm_pct else None
        return None
    except Exception:
        return None

# ── Mode configurations ───────────────────────────────────────────────────────
# Modes define the bot's TRIGGER FINGER — how easily it pulls the trigger on a trade.
# Each mode's confidence threshold, momentum gate, and per-cycle trade limit work
# together to produce a clear frequency spectrum:
#   STOIC:  ~1–3  trades/day  — sniper, extreme conviction only
#   WILD:   ~10–20 trades/day — opportunist, moderate gates, 3 max/cycle
#   INSANE: ~30–60 trades/day — sprayer, low gates, all qualifying/cycle (≈3–4× WILD)
MODES = {
    "stoic": {
        # Sniper. Redesigned from 3.0% → 1.5% momentum: BONK/JUP/WIF rarely do 3% in 5m
        # consistently, so Bot 3 had zero trades across all sessions. Now fires on real
        # momentum (1.5%) with confirmed buy pressure and a volume surge — still disciplined.
        # TP raised to 10% (blue chips sustain larger moves than meme coins).
        # Hold reduced from 8h → 6h (reduces dead bag-holding on stale positions).
        # Two concurrent slots: take 2 quality shots per cycle instead of 1.
        "MOMENTUM_MIN":       1.5,   # was 3.0 — blue chips can't do 3% in 5m reliably
        "BUY_SELL_RATIO_MIN": 1.3,   # was 1.5 — still requires clear buy dominance
        "VOLUME_SPIKE_MULT":  1.5,   # was 2.0 — confirmed surge, not requiring extreme spike
        "CONFIDENCE_MIN":     0.60,  # S64: 0.60→0.62; S89: 0.62→0.60 — 0.62 sat ABOVE the
                                     # no-history default (0.60), so STOIC could never enter a
                                     # token it hadn't already proven >62% → bot3 fully locked
                                     # out (10h idle). 0.60 still the pickiest mode (vs 0.50/0.40)
                                     # but lets it bootstrap fresh tokens at the default.
        "TAKE_PROFIT_PCT":    10.0,  # was 8.0 — blue chips sustain 10%+ on real moves
        "STOP_LOSS_PCT":     -3.5,   # was -4.0 — tighter, blue chips more predictable
        "MAX_HOLD_HOURS":     6.0,   # was 8.0 — cut dead bag-holding time
        "MAX_CONCURRENT":     2,     # was 1 — two quality shots per cycle
    },
    "wild": {
        # Opportunist. TP raised 4%→6% (4% barely clears round-trip slippage on Solana memes).
        # Hold raised 2h→3h: most mid-cap momentum plays need 2-3h to develop fully.
        # Slightly tighter entry quality (0.5%→0.7% momentum, 0.70→0.80 b/s ratio).
        # SL slightly widened (-2.5→-2.8) to avoid being stopped out by spread noise.
        "MOMENTUM_MIN":       0.7,   # was 0.5 — slightly higher quality bar
        "BUY_SELL_RATIO_MIN": 0.80,  # was 0.70 — slightly more buy pressure needed
        "VOLUME_SPIKE_MULT":  0.0,   # disabled — volume_5m floor ($200) still applies
        "CONFIDENCE_MIN":     0.50,  # was 0.55 — lowered 0.05 to reduce hesitation on ≥60% WR tokens
        "TAKE_PROFIT_PCT":    6.0,   # was 4.0 — 4% barely cleared slippage fees
        "STOP_LOSS_PCT":     -2.8,   # was -2.5 — slight widening avoids spread noise
        "MAX_HOLD_HOURS":     3.0,   # was 2.0 — most moves need 2-3h to fully develop
        "MAX_CONCURRENT":     4,     # was 3 — slightly more capacity for learning pace
    },
    "insane": {
        # Tiered gem hunter. Entry gates stay loose; TP/SL/hold adapt per signal conviction.
        # Three tiers (see INSANE_TIER_PARAMS below) are assigned at signal time and stored
        # on the position dict. These mode-level constants are the FALLBACK for stacks and
        # any position opened without tier params (backward compat).
        "MOMENTUM_MIN":       0.3,   # 0.3% in 5m — anything with real buying
        "BUY_SELL_RATIO_MIN": 0.50,  # roughly balanced — don't buy into clear selling
        "VOLUME_SPIKE_MULT":  0.0,   # disabled — volume_5m floor ($200) still applies
        "CONFIDENCE_MIN":     0.40,  # was 0.45 — lowered 0.05 to reduce hesitation on ≥60% WR tokens
        "TAKE_PROFIT_PCT":    3.0,   # fallback for stacks / backward compat (tier overrides this)
        "STOP_LOSS_PCT":     -2.0,   # fallback for stacks / backward compat
        "MAX_HOLD_HOURS":     0.75,  # fallback for stacks / backward compat
        "MAX_CONCURRENT":     10,    # effectively capital-capped; 10 prevents pathological stacking
    },
}

# ── INSANE mode — three execution tiers ──────────────────────────────────────
# Assigned per-signal by classify_insane_tier() and stored on the position dict.
# check_exits() reads per-position TP/SL/hold, falling back to MODES constants.
#
#  QUICK    low confidence, unknown token  → tiny bet, -1.5% stop, 20min
#  GEM      medium conf OR gem-path age    → moderate bet, -4% stop, 3h hold
#  HIGHCONV proven track record + signals  → large bet, -5% stop, 4h hold
#
# size_cap_pct  — wallet fraction ceiling applied AFTER the confidence formula
# disc_cap_pct  — discovery-token cap for tokens with < 3 trades in memory
INSANE_TIER_PARAMS = {
    "quick": {
        "take_profit_pct": 5.0,    # raised from 4.5%: clear slippage with margin
        "stop_loss_pct":  -3.0,    # keep: -1.5% triggered on normal spread noise; -3% is a real reversal
        "max_hold_hours":  0.75,   # was 0.5h: 45min gives more runway for the TP to develop
        "disc_cap_pct":    0.08,   # 8% discovery cap — small bet on unknown tokens
        "size_cap_pct":    0.10,   # was 0.12 — keep QUICK bets small: genuinely low-conviction
        "tp1_enabled":     False,  # no partial exits: position is already small & tight
    },
    "gem": {
        "take_profit_pct": 20.0,   # S64: 15→20 — let the trail begin later so a real gem rides further
        "stop_loss_pct":  -6.0,    # S104: -4→-6 — eventual winners dip to p10 -4.5%; a -4% stop cut ~11% of
                                   # winners and gem stops were the #1 realized bleed (-0.124◎/10 stops).
                                   # Widening to -6% recovers winners (replay favors deeper stop + tight trail).
        "max_hold_hours":  3.0,    # keep: 3h captures the bulk of a meme pump lifecycle
        "disc_cap_pct":    0.18,   # S64: 15→18 — INSANE is the gem hunter; bet harder on young momentum
        "size_cap_pct":    0.60,   # S64: 40→60 — aggressive concentration on conviction (Gem Hunter)
        "tp1_enabled":     True,   # PARTIAL EXIT: lock in floor profit at TP1
        "tp1_pct":         8.0,    # take partial at +8% (lock some, ride the rest)
        "tp1_fraction":    0.30,   # S64: sell 30% at TP1 → keep 70% riding (was 35%)
    },
    "highconv": {
        "take_profit_pct": 25.0,   # keep: proven token + whale signal = room for big run
        "stop_loss_pct":  -5.0,    # keep: wider stop for the high-conviction conviction play
        "max_hold_hours":  5.0,    # was 4.0h: extra patience for the full move to develop
        "disc_cap_pct":    0.30,   # 30% — proven token, trust the track record
        "size_cap_pct":    None,   # no extra cap — dashboard size tier is the real ceiling
        "tp1_enabled":     True,   # PARTIAL EXIT: lock floor profit before chasing 25%
        "tp1_pct":         10.0,   # take partial at +10%
        "tp1_fraction":    0.30,   # sell 30% at TP1 → move SL to breakeven on remaining 70%
    },
    "probe": {
        # Active Probing: accumulation entry in dead markets (vel=0, liq growing).
        # Bypasses the velocity gate — entered because liquidity is building, not momentum.
        # Position is intentionally tiny (0.5% wallet hard cap) — this is a scout bet.
        # If volume arrives and the trade moves to +8%, the Smart Trail extends the gain.
        # If nothing develops in 2h, the time-limit exit closes it with minimal loss.
        "take_profit_pct": 8.0,    # modest target: if liq growth leads to a pump, +8% is achievable
        "stop_loss_pct":  -3.0,    # tight: accumulation thesis breaks fast when it fails
        "max_hold_hours":  2.0,    # 2h window — if volume hasn't arrived, thesis is wrong
        "disc_cap_pct":    0.005,  # 0.5% discovery cap — true scout sizing
        "size_cap_pct":    0.005,  # 0.5% wallet hard cap (overrides confidence formula)
        "tp1_enabled":     False,  # no partial exit — position is already tiny
    },
    "force_fire": {
        # Force-FIRE: high-gradient pump.fun bonding curve — earliest possible entry.
        # Token has no DexScreener data yet; signal comes from on-chain account reads.
        # 1% hard cap because this is the highest-risk path (pre-market, no history).
        # 30-min max hold: pump.fun curves answer quickly — up big or exit via time limit.
        # Smart Trail activates at +100% so a genuine pump can be held past the target.
        "take_profit_pct": 100.0,  # 2× target; trail extends if pump continues
        "stop_loss_pct":   -20.0,  # wide SL: BC tokens are extremely volatile pre-graduation
        "max_hold_hours":    0.5,  # 30 min — if not moving, thesis is wrong
        "disc_cap_pct":     0.01,  # 1% discovery cap (no trade history on BC tokens)
        "size_cap_pct":     0.01,  # 1% wallet ceiling — intentionally small
        "tp1_enabled":      False, # tiny position, no partial needed
    },
    "mean_reversion": {
        # Mean-Reversion: counter-trend entry on exhausted-seller washout setups.
        # Triggers on a -5% to -10% 5m dip with volume spiking — not a slow bleed.
        # When sellers exhaust themselves into rising volume, the bounce risk/reward is good.
        # TP lower than GEM (bounce, not a new rally); SL wider than probe (already fallen).
        "take_profit_pct": 6.0,    # target the recovery: -7% dip → +6% bounce is a 13% round-trip
        "stop_loss_pct":  -4.0,    # wider: price has already fallen, give it room vs entry
        "max_hold_hours":  1.5,    # 90min: if not bouncing by then, sellers won and it's a bleed
        "disc_cap_pct":    0.02,   # 2% discovery cap — real thesis, not a scout
        "size_cap_pct":    0.02,   # 2% wallet hard cap — meaningful position
        "tp1_enabled":     False,  # quick in, quick out — no partial needed on 1.5h hold
    },
    "deep_pool": {
        # S67: the brain-validated edge, made tradeable as an entry path.
        # Admits LIQUID movers that match strategy_brain's `deep_pool_quality` rule
        # (m5≥1% & liq/mcap≥0.10 & buy/sell≥1) even when the regime score-threshold
        # rejects them — so the fleet can trade the proven edge in ANY market, not
        # just a hot one. Forward-obs (n=111, EV +7.3%, EV-lo +3.86%, WR 57%, H1/H2
        # both +EV) + exitability check (n=493: 100% of winners survive a -8% stop,
        # 0/13 big movers dipped below -8% before running) justify every parameter.
        "take_profit_pct": 20.0,   # KEEP: preserves the tail — 21% of grind / 32% of strict winners reach +20%
        "stop_loss_pct":  -8.0,    # KEEP: n=493 — 100% of winning liquid tokens survived -8% (vs 99% at -4%)
        "max_hold_hours":  1.0,    # edge is measured at a 30m forward horizon; 1h matures it, no bag-holding
        "disc_cap_pct":    0.10,   # 10% discovery cap — quality signal but may lack trade history
        "size_cap_pct":    0.15,   # 15% canary cap — real edge, new live path; raise as it proves
        "tp1_enabled":     True,   # lock the grind, ride the tail
        # S70 — GRIND RESHAPE. This is a frequent-small-win edge, NOT a moonshot: on 133
        # non-draining deep_pool obs the median WINNER is +5.6% and only 21% reach +20%, so
        # the old +10%@30% partial banked too little, too late — the 70% riding to +20%
        # mostly drifts back to the ~+5.6% endpoint (the paper→live leak). Bank a bigger
        # chunk at +7% (reached by 46% of grind / 66% of strict winners, clearly above the
        # ~1-2% cost wall), then ride the rest risk-free (SL→breakeven after the partial) to
        # the +20% tail. Captures the grind without capping the strict cohort's runner.
        "tp1_pct":         7.0,    # partial at +7% — the level winners actually reach
        "tp1_fraction":    0.40,   # sell 40% at TP1 → 60% rides to TP/trail
    },
    "brain_rule": {
        # S67: default exit profile for the generalized brain-rule registry — any token
        # admitted by a live brain candidate (deep_pool_strict / momentum_breakout / …).
        # Mirrors deep_pool (same liquid-mover, frequent-small-win, modest-tail profile);
        # kept as its own play so it can be tuned per-rule later without touching deep_pool.
        "take_profit_pct": 20.0,
        "stop_loss_pct":  -8.0,
        "max_hold_hours":  1.0,
        "disc_cap_pct":    0.10,
        "size_cap_pct":    0.15,   # 15% canary cap — raise as the registry proves out
        "tp1_enabled":     True,
        "tp1_pct":         7.0,    # S70 grind reshape — mirrors deep_pool (see its note)
        "tp1_fraction":    0.40,   # bank 40% at +7%, ride 60% risk-free to the tail
    },
}

# ── S64: "ride vs bank" exit policy per INSANE play ──────────────────────────
# ride=True  → on TP hit, hand off to Smart-Trail and let the winner run (the
#              ceiling mechanism — one gem can ride to multi-X and win the race).
# ride=False → on TP hit, close immediately and BANK the gain (discipline).
# INSANE is the Gem Hunter: its conviction + pump plays ride; its scalps bank.
_INSANE_RIDE = {
    "quick":          False,  # tiny scalp — take the +5% and move on
    "gem":            True,   # ride the gem
    "highconv":       True,   # ride the conviction play hardest
    "probe":          True,   # if accumulation turns into a pump, ride it
    "force_fire":     True,   # bonding-curve moonshot — ride
    "mean_reversion": False,  # bounce trade — bank the snap-back
    "deep_pool":      True,   # S67: ride to capture the validated forward move + modest tail
    "brain_rule":     True,   # S67: registry entries ride (same profile as deep_pool)
}
for _pk, _pv in INSANE_TIER_PARAMS.items():
    _pv.setdefault("ride", _INSANE_RIDE.get(_pk, True))

# ── S104: stop-loss WIDTH FLOOR for the goldilocks optimizer ──────────────────
# goldilocks grid-searches the EV-optimal stop on REALIZED trades, but it cannot see
# the winners a too-tight stop already killed (they were stopped out before they ran),
# so it converges on stops that are too tight (it currently pins gem -2.5%, quick -1.5%).
# The timing analysis (forward_obs path: eventual winners dip to p10 -4.5% before running)
# says that is the premature-cut bleed. Freeze the code-default SL as a FLOOR: the optimizer
# may WIDEN a stop but may NEVER TIGHTEN it past the analyzed default (stops are negative,
# so the floor is the more-negative bound). Revert: delete this dict + the clamp in apply_overrides.
_S104_SL_FLOOR = {name: p["stop_loss_pct"] for name, p in INSANE_TIER_PARAMS.items()
                  if p.get("stop_loss_pct") is not None}


# ── S64: WILD plays — "The Opportunist" (middle of the risk spectrum) ─────────
# Moderate size, partial-then-ride. Two real archetypes that already occur:
#   momentum — standard trending breakout entry
#   relay    — following a prime broadcast by the INSANE bot (memory.relay)
WILD_PLAYS = {
    "momentum": {
        "take_profit_pct": 10.0,
        "stop_loss_pct":  -5.0,    # S104: -3→-5 — a -3% stop prematurely cut ~15% of eventual winners
                                   # (winners dip to p10 -4.5%); momentum stops bled -0.024◎ realized.
        "max_hold_hours":  3.0,
        "disc_cap_pct":    0.10,
        "size_cap_pct":    0.35,   # medium concentration
        "ride":            True,   # let a runner run, but partial first
        "tp1_enabled":     True,
        "tp1_pct":         6.0,
        "tp1_fraction":    0.35,
    },
    "relay": {
        # Following an INSANE conviction prime — lean in a little harder.
        "take_profit_pct": 12.0,
        "stop_loss_pct":  -5.0,    # S104: -3→-5 — relay stops bled -0.018◎; widen so winners that dip
                                   # to -4.5% (p10) before running aren't cut early. Relay is a top green play.
        "max_hold_hours":  3.0,
        "disc_cap_pct":    0.10,
        "size_cap_pct":    0.40,
        "ride":            True,
        "tp1_enabled":     True,
        "tp1_pct":         6.0,
        "tp1_fraction":    0.30,
    },
}

# ── S64: STOIC plays — "The Vault Keeper" (lowest variance, banks early) ──────
# One disciplined archetype: a clean deep-liquidity momentum entry, banked at a
# modest TP with a tight stop. ride=False → never trails, never chases the moon.
# Small size, slow cadence — the safe, low-variance tortoise.
STOIC_PLAYS = {
    "bank": {
        "take_profit_pct": 8.0,
        "stop_loss_pct":  -5.0,    # S104: -3→-5 — bank stops bled -0.016◎ (7 stops, 0% win); winners
                                   # dip to p10 -4.5% first. STOIC banks early so a -5% stop still preserves capital.
        "max_hold_hours":  4.0,
        "disc_cap_pct":    0.08,
        "size_cap_pct":    0.15,   # small, measured — never concentrate
        "ride":            False,  # BANK at TP — no trailing, no FOMO
        "tp1_enabled":     False,
    },
}

# S67: deep_pool is a token-driven play (not a mode personality) used by the INSANE
# and WILD entry paths. Register it in WILD too so open_position() derives its
# ride/TP1 params; otherwise WILD deep_pool positions silently lose the +10% partial.
WILD_PLAYS["deep_pool"]  = INSANE_TIER_PARAMS["deep_pool"]
WILD_PLAYS["brain_rule"] = INSANE_TIER_PARAMS["brain_rule"]

# Mode-agnostic play registry — main.py / open_position look up params by (mode, play).
MODE_PLAYS = {
    "insane": INSANE_TIER_PARAMS,
    "wild":   WILD_PLAYS,
    "stoic":  STOIC_PLAYS,
}

# S68 — stall-aware time limit. A riding play that reaches its max_hold while still
# pinned to its high (current ≥ peak × this) is mid-breakout: keep holding rather than
# cut it on the clock. It only extends up to the 2× hard ceiling (always enforced), and
# the moment it fades off the high it hands off to Smart Trail / closes as before. This
# lets a genuine runner "see itself through" without bag-holding faders or overriding the
# stop-loss / rug-guard / hard-ceiling backstops.
_STILL_WORKING_NEAR_PEAK = 0.99   # within 1% of the position's peak = still working


class MeanReversionStrategy:
    """
    Qualifies counter-trend washout setups — tokens in a sharp 5m dip with rising volume.

    The signal: sellers are dumping heavily (price -5% to -10% in 5m) into INCREASING volume.
    Rising volume during a flush means buyers are absorbing the supply.  When sellers run out
    of inventory against those bids, price snaps back.  This is the 'exhausted seller' pattern.

    Guards that distinguish a washout from a continued crash:
      - 1h context not catastrophic (MAX_1H_LOSS = -20%) — a dip, not a rug
      - At least one buyer in the 5m window — absorption is happening
      - Volume must spike vs recent baseline (VOL_ACCEL_MIN = 1.5×) — flush, not slow bleed
      - Minimum pool depth (LIQ_MIN = $50k) — must be able to fill and exit cleanly

    Does NOT apply to static watchlist tokens (blue chips dipping -7% in 5m are usually
    macro-driven; the bounce thesis requires the token to have its own support level).
    Only active in INSANE mode — checked by caller.
    """
    DIP_MIN       = -10.0   # % — below this it's a crash/rug, not a washout
    DIP_MAX       =  -5.0   # % — above this is noise, not a significant dip
    VOL_ACCEL_MIN =   1.5   # current vol must be >= 1.5× recent baseline
    VOL_HIST_MIN  =     3   # minimum data points needed for vol history
    LIQ_MIN       = 50_000  # $50k minimum pool depth
    MAX_1H_LOSS   = -20.0   # 1h change can't be worse than -20%
    MIN_BUYS      =     1   # at least 1 buy txn — some absorption must be present

    def qualify(
        self,
        mint: str,
        mdata: dict,
        vol_history: list,
    ) -> tuple:
        """
        Returns (True, None, vol_accel) for a valid washout setup,
        or (False, skip_reason, 0.0) otherwise.

        vol_history — rolling list of recent vol5m values for this token (from Observer).
        Caller is responsible for memory-based checks (ban, confidence) — this method
        handles only market-data signals.
        """
        momentum_5m = mdata.get("price_change_5m", 0)
        p1h         = mdata.get("price_change_1h", 0)
        liq         = mdata.get("liquidity_usd", 0)
        vol5m       = mdata.get("volume_5m", 0)
        buys        = mdata.get("txns_5m_buys", 0)
        price       = mdata.get("price_usd", 0)

        if not price:
            return False, "no price", 0.0
        if not (self.DIP_MIN <= momentum_5m <= self.DIP_MAX):
            return False, f"mom {momentum_5m:.1f}% outside dip range [{self.DIP_MIN},{self.DIP_MAX}]", 0.0
        if p1h < self.MAX_1H_LOSS:
            return False, f"1h {p1h:.1f}% crash — token dying", 0.0
        if liq < self.LIQ_MIN:
            return False, f"liq ${liq/1000:.0f}k < ${self.LIQ_MIN/1000:.0f}k floor", 0.0
        if buys < self.MIN_BUYS:
            return False, "no buyers — pure dump, no absorption", 0.0
        if len(vol_history) < self.VOL_HIST_MIN:
            return False, "insufficient vol history for baseline", 0.0

        hist_avg  = sum(vol_history[:-1]) / max(len(vol_history[:-1]), 1)
        vol_accel = vol5m / hist_avg if hist_avg > 0 else 0.0
        if vol_accel < self.VOL_ACCEL_MIN:
            return False, f"vol×{vol_accel:.1f} < {self.VOL_ACCEL_MIN}× required for washout", 0.0

        return True, None, round(vol_accel, 2)


def classify_insane_tier(
    confidence: float,
    gem_path: bool,
    buy_sell_ratio: float,
    trades_in_memory: int,
    vol_acceleration: float = 1.0,
) -> str:
    """Classify an INSANE mode signal into its execution tier.

    HIGH CONVICTION: proven token (≥5 trades) with conf≥0.85 and strong buy pressure.
    GEM:             medium confidence OR young gem-path token OR first-time discovery token
                     (conf≥0.55 covers the default 0.60 no-history score).
                     Also: volume acceleration ≥2.0 with conf≥0.50 routes to GEM — accelerating
                     volume on a fresh token is a strong pre-pump signal even without history.
    QUICK:           genuinely low confidence — tokens that have already lost multiple times.
    """
    if confidence >= 0.85 and buy_sell_ratio >= 2.0 and trades_in_memory >= 5:
        return "highconv"
    if confidence >= 0.55 or gem_path or (vol_acceleration >= 2.0 and confidence >= 0.50):
        return "gem"
    return "quick"


def classify_play(
    mode: str,
    confidence: float,
    gem_path: bool,
    buy_sell_ratio: float,
    trades_in_memory: int,
    vol_acceleration: float = 1.0,
    relay_primed: bool = False,
) -> str:
    """Mode-agnostic play classifier for the STANDARD entry path.

    Returns a play key valid for `mode` (a key of MODE_PLAYS[mode]). The special
    INSANE paths (probe / mean_reversion / force_fire) are assigned directly at
    their detection sites in main.py, not here.

      STOIC  → always "bank"   (one disciplined archetype)
      WILD   → "relay" if following an INSANE prime, else "momentum"
      INSANE → quick / gem / highconv via classify_insane_tier()
    """
    if mode == "stoic":
        return "bank"
    if mode == "wild":
        return "relay" if relay_primed else "momentum"
    return classify_insane_tier(
        confidence, gem_path, buy_sell_ratio, trades_in_memory, vol_acceleration
    )


def get_mode() -> str:
    if MODE_PATH.exists():
        try:
            return json.loads(MODE_PATH.read_text()).get("mode", "stoic")
        except Exception:
            pass
    return "stoic"

def set_mode(mode: str) -> None:
    MODE_PATH.write_text(json.dumps({"mode": mode}))

def get_marketcap() -> str:
    if MARKETCAP_PATH.exists():
        try:
            return json.loads(MARKETCAP_PATH.read_text()).get("marketcap", "high")
        except Exception:
            pass
    return "high"

def set_marketcap(cap: str) -> None:
    MARKETCAP_PATH.write_text(json.dumps({"marketcap": cap}))

def get_size_tier() -> str:
    if SIZE_MODE_PATH.exists():
        try:
            return json.loads(SIZE_MODE_PATH.read_text()).get("size_tier", "medium")
        except Exception:
            pass
    return "medium"

def set_size_tier(tier: str) -> None:
    SIZE_MODE_PATH.write_text(json.dumps({"size_tier": tier}))

def get_viral_weight() -> str:
    """Return current virality weight: 'off' | 'normal' | 'boost'. Default: 'normal'."""
    if VIRAL_WEIGHT_PATH.exists():
        try:
            return json.loads(VIRAL_WEIGHT_PATH.read_text()).get("viral_weight", "normal")
        except Exception:
            pass
    return "normal"

def set_viral_weight(weight: str) -> None:
    VIRAL_WEIGHT_PATH.write_text(json.dumps({"viral_weight": weight}))


def apply_overrides(overrides: dict) -> None:
    """Apply thresholds_override.json values to live strategy dicts in-place.
    Mutates MODES and INSANE_TIER_PARAMS — changes take effect on the next evaluate() call.
    Called at startup (apply any existing file) and every 20 closes (goldilocks auto-run).
    """
    if not overrides:
        return

    tiers = overrides.get("tiers", {})
    changed = []

    for tier_name, tier_cfg in tiers.items():
        tp = tier_cfg.get("take_profit_pct")
        sl = tier_cfg.get("stop_loss_pct")

        # INSANE execution tiers (quick / gem / highconv)
        if tier_name in INSANE_TIER_PARAMS:
            p = INSANE_TIER_PARAMS[tier_name]
            if tp is not None and abs(tp - p.get("take_profit_pct", tp)) > 0.1:
                p["take_profit_pct"] = tp
                changed.append(f"{tier_name}.TP={tp:+.1f}%")
            if sl is not None:
                # S104 width floor: never let the optimizer TIGHTEN a stop past the analyzed
                # default (stops are negative → min() keeps the wider/more-negative bound).
                _floor = _S104_SL_FLOOR.get(tier_name)
                if _floor is not None:
                    sl = min(sl, _floor)
                if abs(sl - p.get("stop_loss_pct", sl)) > 0.1:
                    p["stop_loss_pct"] = sl
                    changed.append(f"{tier_name}.SL={sl:+.1f}%")

        # Mode-level params (wild / stoic — these are fallbacks + actual WILD/STOIC TP/SL)
        if tier_name in MODES:
            m = MODES[tier_name]
            if tp is not None and abs(tp - m.get("TAKE_PROFIT_PCT", tp)) > 0.1:
                m["TAKE_PROFIT_PCT"] = tp
                changed.append(f"{tier_name}.MODE_TP={tp:+.1f}%")
            if sl is not None and abs(sl - m.get("STOP_LOSS_PCT", sl)) > 0.1:
                m["STOP_LOSS_PCT"] = sl
                changed.append(f"{tier_name}.MODE_SL={sl:+.1f}%")

    # Momentum floors — insane uses the shared key; wild/stoic use mode-specific keys.
    # Allows goldilocks (or manual edits) to tune each mode independently.
    mom_floor = overrides.get("momentum_floor_pct")
    if mom_floor is not None:
        cur = MODES["insane"].get("MOMENTUM_MIN", 0.3)
        if abs(mom_floor - cur) > 0.05:
            MODES["insane"]["MOMENTUM_MIN"] = mom_floor
            changed.append(f"insane.MOMENTUM_MIN={mom_floor:.1f}%")

    wild_floor = overrides.get("wild_momentum_floor_pct")
    if wild_floor is not None:
        cur = MODES["wild"].get("MOMENTUM_MIN", 0.7)
        if abs(wild_floor - cur) > 0.05:
            MODES["wild"]["MOMENTUM_MIN"] = wild_floor
            changed.append(f"wild.MOMENTUM_MIN={wild_floor:.1f}%")

    stoic_floor = overrides.get("stoic_momentum_floor_pct")
    if stoic_floor is not None:
        cur = MODES["stoic"].get("MOMENTUM_MIN", 1.5)
        if abs(stoic_floor - cur) > 0.05:
            MODES["stoic"]["MOMENTUM_MIN"] = stoic_floor
            changed.append(f"stoic.MOMENTUM_MIN={stoic_floor:.1f}%")

    # Vol gate — mutates config.GEM_MIN_VOLUME_5M so main.py's live reference picks it up
    # without a restart. The old --patch-config regex approach is replaced by this.
    gem_vol5m = overrides.get("vol_gates", {}).get("gem_min_volume_5m")
    if gem_vol5m is not None:
        import config as _cfg
        if _cfg.GEM_MIN_VOLUME_5M != gem_vol5m:
            _cfg.GEM_MIN_VOLUME_5M = gem_vol5m
            changed.append(f"GEM_MIN_VOLUME_5M=${gem_vol5m:,}")

    if changed:
        print(f"[Strategy] Overrides applied ({overrides.get('trade_count', '?')} trades): "
              + "  ".join(changed))


def _trail_buffer(peak_price: float, price_history: list, fixed_buffer_pct: float = None) -> tuple:
    """Returns (buffer_pct, trigger_price) for the Smart-Trail exit.

    buffer_pct  — % below peak where the trail will fire
    trigger_price — absolute price level of that trigger
    Uses the coefficient of variation (std_dev / mean) of recent prices
    so the buffer scales with local volatility, not token price magnitude.

    S102: `fixed_buffer_pct` (the runner free-ride cohort) REPLACES the vol-scaled buffer
    with a wide fixed give-back. The vol-scaled buffer collapses to ~3.5% in a calm 5-min
    window (it clipped the real bot1 Jotchua at +21.7% off a +28.2% peak) — far too tight
    for a legging meme that retraces 20-40% between legs. The wider of (fixed, vol-scaled)
    is used so an unusually choppy runner still gets at least its local-vol room; floored
    at SMART_TRAIL_MIN_BUFFER_PCT.
    """
    if len(price_history) >= 2:
        mean = sum(price_history) / len(price_history)
        variance = sum((p - mean) ** 2 for p in price_history) / (len(price_history) - 1)
        std_dev = variance ** 0.5
        vol_pct = (std_dev / mean * 100) if mean > 0 else SMART_TRAIL_MIN_BUFFER_PCT
    else:
        vol_pct = SMART_TRAIL_MIN_BUFFER_PCT

    if fixed_buffer_pct is not None:
        buffer_pct = max(SMART_TRAIL_MIN_BUFFER_PCT, fixed_buffer_pct, SMART_TRAIL_MULTIPLIER * vol_pct)
    else:
        buffer_pct = max(
            SMART_TRAIL_MIN_BUFFER_PCT,
            min(SMART_TRAIL_MAX_BUFFER_PCT, SMART_TRAIL_MULTIPLIER * vol_pct),
        )
    return buffer_pct, peak_price * (1 - buffer_pct / 100)


def _recent_range_pct(price_history: list, window: int = DYNAMIC_TP1_VOL_WINDOW) -> float:
    """High-low % range over the last `window` price samples.
    With 30s cycles, the default window=3 covers ~90s — a "1m volatility" proxy.
    Returns 0.0 when there are fewer than 2 samples (position just opened).
    """
    samples = price_history[-window:]
    if len(samples) < 2:
        return 0.0
    lo, hi = min(samples), max(samples)
    return (hi - lo) / lo * 100 if lo > 0 else 0.0


class StoicStrategy:
    def __init__(self) -> None:
        self._volume_history: dict = {m: [] for m in WATCHLIST}
        self.positions: dict = self._load_positions()

        if self.positions:
            from bot_config import BOT_ID
            print(f"[Bot #{BOT_ID}] Restored {len(self.positions)} open position(s) from disk: "
                  + ", ".join(self.positions.keys()))

    # ── Persistence ───────────────────────────────────────────────────────────

    # Base58 alphabet — Solana addresses are base58-encoded 32-byte pubkeys.
    # Characters 0, I, O, l are excluded. Any address failing this is fake/test data.
    _BASE58_CHARS = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

    @staticmethod
    def _is_valid_mint(mint: str) -> bool:
        return (
            isinstance(mint, str)
            and 32 <= len(mint) <= 44
            and all(c in StoicStrategy._BASE58_CHARS for c in mint)
        )

    def _load_positions(self) -> dict:
        if POSITIONS_PATH.exists():
            try:
                raw = json.loads(POSITIONS_PATH.read_text())
                valid = {k: v for k, v in raw.items() if self._is_valid_mint(k)}
                if len(valid) < len(raw):
                    import os as _os
                    bad = set(raw) - set(valid)
                    print(f"[POSITIONS] Dropped {len(bad)} invalid mint(s) on load: {bad}", flush=True)
                    _tmp = POSITIONS_PATH.with_suffix(".tmp")
                    _tmp.write_text(json.dumps(valid, indent=2))
                    _os.replace(_tmp, POSITIONS_PATH)
                return valid
            except Exception:
                return {}
        return {}

    def _save_positions(self) -> None:
        import os as _os
        valid = {k: v for k, v in self.positions.items() if self._is_valid_mint(k)}
        if len(valid) < len(self.positions):
            bad = set(self.positions) - set(valid)
            print(f"[POSITIONS] Purged {len(bad)} invalid mint(s) from memory: {bad}", flush=True)
            self.positions = valid
        # Atomic write: if the process is killed mid-write the original file is preserved.
        _tmp = POSITIONS_PATH.with_suffix(".tmp")
        _tmp.write_text(json.dumps(self.positions, indent=2))
        _os.replace(_tmp, POSITIONS_PATH)

    # ── Volume tracking ───────────────────────────────────────────────────────

    def update_volume(self, mint: str, volume_5m: float) -> None:
        hist = self._volume_history.setdefault(mint, [])
        hist.append(volume_5m)
        if len(hist) > 20:
            hist.pop(0)

    # ── Signal evaluation ─────────────────────────────────────────────────────

    def evaluate(self, mint: str, data: dict, cycle: int) -> tuple:
        """Returns (signal_dict, None) on success, or (None, skip_reason_str) on skip.

        Pre-condition: caller has already run ValidationProfile.score() and confirmed
        score ≥ 80. This method handles only safety guards and memory-based checks that
        cannot be expressed as market-data scores (confidence, loss patterns, trend guards).
        """
        cfg            = MODES[get_mode()]
        CONFIDENCE_MIN = cfg["CONFIDENCE_MIN"]

        name = {
            "So11111111111111111111111111111111111111112": "SOL",
            "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
            "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
        }.get(mint, mint[:6])

        if memory.is_banned(mint, cycle):
            print(f"  [{name}] SKIP: banned", flush=True)
            return None, "banned"

        price = data.get("price_usd", 0)
        if not price:
            print(f"  [{name}] SKIP: no price", flush=True)
            return None, "no price"

        momentum_5m     = data.get("price_change_5m", 0)
        price_change_1h = data.get("price_change_1h", 0)
        liquidity       = data.get("liquidity_usd", 0)
        volume_5m       = data.get("volume_5m", 0)
        buys            = data.get("txns_5m_buys", 0)
        sells           = data.get("txns_5m_sells", 0)

        self.update_volume(mint, volume_5m)

        # Data integrity — DexScreener occasionally returns glitched momentum values.
        if momentum_5m > 200.0:
            print(f"  [{name}] SKIP: momentum {momentum_5m:.0f}% — data glitch", flush=True)
            return None, f"data glitch mom {momentum_5m:.0f}%"

        # Trend safety guards — directional checks that market-data scoring can't capture.
        if price_change_1h < -3.0 and momentum_5m < 3.0:
            print(f"  [{name}] SKIP: 1h downtrend {price_change_1h:.1f}% with weak 5m signal", flush=True)
            return None, f"1h downtrend {price_change_1h:.1f}%"

        # S126: the runner lane trades ALREADY-RUNNING momentum — vetoing 1h>12% excludes its
        # validated cohort by construction (79% of qualifying runners killed by THIS guard; the
        # vetoed cohort carries the edge, per-mint +43.1%/ev_lo +36.5 vs survivors −1.65 —
        # research/s126, re-receipted in S128 with ~6,800 live vetoes). Exempt momentum_override-
        # tagged signals from THIS guard ONLY — the b/s>6 pump guard and the >200% glitch guard
        # above stay fully in force. No lane canary → the flag is never stamped → byte-identical.
        _mo_lane = bool(data.get("momentum_override"))
        if mint not in WATCHLIST and price_change_1h > DISCOVERY_OVERBOUGHT_1H_PCT and not _mo_lane:
            print(
                f"  [{name}] SKIP: 1h overbought {price_change_1h:.1f}% > {DISCOVERY_OVERBOUGHT_1H_PCT:.0f}% "
                f"(discovery — move likely mature)",
                flush=True,
            )
            return None, f"1h overbought {price_change_1h:.1f}%"

        if mint not in WATCHLIST and price_change_1h > 6.0 and momentum_5m < 1.5:
            print(
                f"  [{name}] SKIP: weak continuation {momentum_5m:.2f}% on +{price_change_1h:.1f}% 1h run",
                flush=True,
            )
            return None, f"weak cont {momentum_5m:.2f}% on +{price_change_1h:.1f}% 1h"

        # Pump manipulation guard — b/s > 6 in a 5m window is coordinated pumping, not demand.
        if buys == 0 and sells == 0:
            print(f"  [{name}] SKIP: no transaction data", flush=True)
            return None, "no txn data"
        if sells > 0 and (buys / sells) > 6.0:
            ratio = buys / sells
            print(f"  [{name}] SKIP: buy/sell ratio {ratio:.2f} > 6.0 (likely pump)", flush=True)
            return None, f"b/s {ratio:.2f} > 6.0 pump"

        conf = memory.confidence_score(mint)

        # Confidence cap for discovery tokens with limited history.
        # A single win gives conf=1.00 which causes max-sized re-entries on overbought tokens.
        # Cap at DISCOVERY_CONF_CAP until the token has DISCOVERY_CONF_MIN_TRADES trades.
        # Root cause: 9VY2rDbt went 1W/0L → conf=1.00 → re-entered at double the size.
        if mint not in WATCHLIST:
            _stats = memory.get_stats(mint)
            _n = _stats.get("trades", 0) if _stats else 0
            if _n < DISCOVERY_CONF_MIN_TRADES:
                conf = min(conf, DISCOVERY_CONF_CAP)

        # Cross-bot sentiment relay: Bot 1 (INSANE) may have primed this token.
        # WILD reads an active prime and lowers the confidence threshold (default 0.10, floored
        # at 0.30). S95 relay-BOOST canary: deepen the discount and/or extend the receive to STOIC.
        _relay_primed = False
        _rb_on, _rb_disc, _rb_stoic = _relay_boost()
        if get_mode() == "wild" or (_rb_on and _rb_stoic and get_mode() == "stoic"):
            _prime = memory.get_relay_prime(mint)
            if _prime:
                _relay_primed  = True
                _original_min  = CONFIDENCE_MIN
                CONFIDENCE_MIN = max(0.30, CONFIDENCE_MIN - (_rb_disc if _rb_on else 0.10))
                print(
                    f"  [{name}] RELAY ▶ Bot#{_prime['primed_by']}"
                    f" ({_prime.get('tier','?')}) score={_prime.get('score',0):.0f}"
                    f" — conf min {_original_min:.2f}→{CONFIDENCE_MIN:.2f}",
                    flush=True,
                )

        if conf < CONFIDENCE_MIN:
            print(f"  [{name}] SKIP: confidence {conf:.2f} < {CONFIDENCE_MIN}", flush=True)
            return None, f"conf {conf:.2f} < {CONFIDENCE_MIN}"

        # Loss momentum guard: if this token has ≥3 losses AND we're entering at the same
        # momentum level it typically loses at, require 25% more confidence than the mode floor.
        # Prevents repeated entries into the same known-losing pattern for a specific token.
        _avg_loss_mom = memory.get_avg_loss_momentum(mint)
        if _avg_loss_mom > 0 and momentum_5m <= _avg_loss_mom * 1.2:
            _loss_guard_min = CONFIDENCE_MIN * 1.25
            if conf < _loss_guard_min:
                print(
                    f"  [{name}] SKIP: loss pattern (avg_loss_mom={_avg_loss_mom:.2f}% "
                    f"cur_mom={momentum_5m:.2f}%) — conf {conf:.2f} < {_loss_guard_min:.2f}",
                    flush=True,
                )
                return None, f"loss pattern mom {momentum_5m:.2f}% conf {conf:.2f} < {_loss_guard_min:.2f}"

        # Volume acceleration: how does current 5m volume compare to recent history?
        # Vol building (>1.5×) = momentum is real and strengthening.
        # Vol fading (<0.6×) = the move is decelerating — weaker signal quality.
        # Used in signal_quality ranking and tier classification; doesn't block entry on its own.
        _vol_hist = self._volume_history.get(mint, [])
        if len(_vol_hist) >= 3:
            _hist_avg = sum(_vol_hist[:-1]) / max(len(_vol_hist[:-1]), 1)
            vol_acceleration = volume_5m / _hist_avg if _hist_avg > 0 else 1.0
        else:
            vol_acceleration = 1.0

        stats          = memory.get_stats(mint)
        win_rate       = (stats["wins"] / stats["trades"]) if stats and stats.get("trades") else 0.0
        buy_sell_ratio = (buys / sells) if sells > 0 else (2.0 if buys > 0 else 1.0)
        _accel_tag = f" vol×{vol_acceleration:.1f}" if vol_acceleration != 1.0 else ""
        print(
            f"  [{name}] SIGNAL: momentum={momentum_5m:.2f}% 1h={price_change_1h:.1f}% "
            f"liq=${liquidity/1000:.0f}k b/s={buy_sell_ratio:.2f} conf={conf:.2f} wr={win_rate:.0%}{_accel_tag}",
            flush=True,
        )

        # size_sol / size_usd are NOT set here — caller (main.py) computes them
        # from actual wallet SOL balance so position sizing adapts to new deposits.
        return {
            "mint":             mint,
            "price":            price,
            "momentum_5m":      momentum_5m,
            "price_change_1h":  price_change_1h,
            "liquidity_usd":    liquidity,
            "volume_5m":        volume_5m,
            "confidence":       conf,
            "buy_sell_ratio":   round(buy_sell_ratio, 3),
            "vol_acceleration": round(vol_acceleration, 2),
            "relay_primed":     _relay_primed,   # S64: WILD followed an INSANE prime → "relay" play
            "rationale": (
                f"{momentum_5m:.1f}% mom | 1h={price_change_1h:.1f}% | liq ${liquidity/1000:.0f}k | "
                f"b/s={buy_sell_ratio:.2f} | conf {conf:.2f} | wr {win_rate:.0%} "
                f"({stats['trades'] if stats else 0} trades)"
            ),
        }, None

    # ── Position management ───────────────────────────────────────────────────

    def open_position(
        self,
        mint: str,
        entry_price: float,
        size_sol: float,
        size_usd: float,
        momentum: float,
        volume_5m: float = 0.0,
        take_profit_pct: float = None,
        stop_loss_pct: float = None,
        max_hold_hours: float = None,
        tier_label: str = None,
        mode: str = None,
        regime: str = None,
        wallet_frac: float = None,
        size_cap_pct: float = None,
        confidence: float = None,
        normal_slice: bool = False,
        dust_shadow: bool = False,
        momentum_override: bool = False,
    ) -> None:
        if not self._is_valid_mint(mint):
            print(f"[POSITIONS] Rejected open_position for invalid mint: {mint!r}", flush=True)
            return
        pos = {
            "entry_price":          entry_price,
            "size_sol":             size_sol,
            "size_usd":             size_usd,
            "momentum_at_entry":    momentum,
            "volume_5m_at_entry":   volume_5m,
            "opened_at":            datetime.now(timezone.utc).isoformat(),
            "peak_price":           entry_price,
            "trail_active":         False,
            "price_history":        [entry_price],
            "stack_count":          0,
            # Partial-exit tracking (GEM/HIGHCONV tiers only)
            "tp1_taken":            False,   # True after TP1 partial exit fires
            "remaining_fraction":   1.0,     # fraction of original position still open
        }
        if take_profit_pct is not None:
            pos["take_profit_pct"] = take_profit_pct
        if stop_loss_pct is not None:
            pos["stop_loss_pct"] = stop_loss_pct
        if max_hold_hours is not None:
            pos["max_hold_hours"] = max_hold_hours
        if tier_label is not None:
            pos["insane_tier"] = tier_label   # the "play" key (kept name for back-compat)
        # S64: universal trade-type tagging — mode + regime stored for "what works when" intel.
        if mode is not None:
            pos["mode"] = mode
        if regime is not None:
            pos["regime"] = regime
        # S96: entry confidence — drives the conviction-aware house-money de-risk sizing.
        if confidence is not None:
            pos["confidence"] = round(confidence, 4)
        # S98-reconcile: tag the tight `normal` deep_pool slice so its close row carries the flag
        # → prestige_tracker + ride_ab COUNT it despite regime=normal (the gate it was built to feed).
        if normal_slice:
            pos["normal_slice"] = True
        # S99: tag a dust-shadow entry so its close row carries the flag → the live arming gate
        # (prestige_tracker) EXCLUDES it and the size-normalized shadow gate (dust_gate.py) reads it.
        if dust_shadow:
            pos["dust_shadow"] = True
        # S107: tag the running-momentum runner entry → its close row carries the flag AND
        # check_exits gives it the RIDE (no fixed-TP) exit when the canary is on.
        if momentum_override:
            pos["momentum_override"] = True
        # S72: size as a share of wallet at open — drives the size-aware breakeven ratchet.
        if wallet_frac is not None:
            pos["wallet_frac"] = round(wallet_frac, 4)
        # S72: the play's wallet cap — the size-up evaluator caps adds against it.
        if size_cap_pct is not None:
            pos["size_cap_pct"] = size_cap_pct
        # S64: derive ride policy + TP1 params from the play table so check_exits()
        # works for ALL modes (was INSANE-only via INSANE_TIER_PARAMS).
        _play = MODE_PLAYS.get(mode, {}).get(tier_label, {}) if (mode and tier_label) else {}
        pos["ride"] = _play.get("ride", True)
        if _play.get("tp1_enabled"):
            pos["tp1_enabled"]  = True
            pos["tp1_pct"]      = _play.get("tp1_pct", 0.0)
            pos["tp1_fraction"] = _play.get("tp1_fraction", 0.35)
        self.positions[mint] = pos
        self._save_positions()

    def add_to_position(self, mint: str, add_price: float, add_size_sol: float, add_size_usd: float) -> None:
        """Stack onto an existing position. Weighted-average entry price, cumulative sizes."""
        if mint not in self.positions:
            return
        pos     = self.positions[mint]
        old_sol = pos["size_sol"]
        new_sol = old_sol + add_size_sol
        pos["entry_price"] = (pos["entry_price"] * old_sol + add_price * add_size_sol) / new_sol
        pos["size_sol"]    = round(new_sol, 6)
        pos["size_usd"]    = round(pos["size_usd"] + add_size_usd, 4)
        pos["stack_count"] = pos.get("stack_count", 0) + 1
        # S72: the bet just got bigger → scale its wallet share so the breakeven ratchet
        # re-bands it correctly. The %-based BE floor (sl_floor_pct) auto-carries against the
        # new blended entry, so protection stays continuous across the add (no unguarded window).
        if pos.get("wallet_frac") and old_sol > 0:
            pos["wallet_frac"] = round(pos["wallet_frac"] * new_sol / old_sol, 4)
        self._save_positions()

    def check_exits(self, current_prices: dict, cycle: int) -> list:
        cfg   = MODES[get_mode()]
        exits = []
        now   = datetime.now(timezone.utc)
        dirty = False
        # S96: read the house-money de-risk canary once per cycle (30s hot-reload cache).
        _hm_cfg = _house_money_cfg()
        if _hm_cfg is not None:
            _hm_trigger = max(_HM_TRIGGER_FLOOR, float(_hm_cfg.get("trigger_pct", _HM_TRIGGER_PCT_DEFAULT)))
            _hm_cushion = float(_hm_cfg.get("cushion_max", _HM_CUSHION_MAX))

        for mint, pos in list(self.positions.items()):
            # Per-position TP/SL/hold (set by INSANE tier classifier at open time).
            # Falls back to mode-level constants for STOIC/WILD and for stacks.
            take_profit = pos.get("take_profit_pct", cfg["TAKE_PROFIT_PCT"])
            stop_loss   = pos.get("stop_loss_pct",   cfg["STOP_LOSS_PCT"])
            max_hold    = pos.get("max_hold_hours",  cfg["MAX_HOLD_HOURS"])
            # Quest finding #5 (canary, per-bot A/B): no fixed-TP bank → ride the tail.
            # Remap to: trail activates at +10% (RIDE via smart-trail, not a bank), SL −10%,
            # TP1 early-partial off. effective_stop_loss = max(stop_loss, sl_floor) below, so
            # the BE-ratchet can still only TIGHTEN it. Protective guards are unaffected.
            # S88-debug: scoped to the validated SMALL-POOL cohort (entry_liq < _VAE_MAX_ENTRY_LIQ)
            # so the unproven wide-stop/no-bank policy never touches bot1's big-liq book (the −EV
            # bleeder). Positions with no recorded entry_liq keep the normal exit logic.
            _vae_entry_liq = pos.get("entry_liq", 0.0) or 0.0
            _small = _vol_accel_exit_on() and 0.0 < _vae_entry_liq < _VAE_MAX_ENTRY_LIQ
            # S90 momentum-gem tier: extend the RIDE to a Jotchua-class entry — a runner play that
            # entered ON momentum, on a sellable $50k–$250k pool. Gated on the ENTRY SIGNATURE
            # (tier ∈ {gem,momentum} + momentum_at_entry ≥ 3 + entry_liq band) so it can never hit
            # the sniper book. Own canary → A/Bs independently of vol_accel_exit.
            _mg = (_momentum_gem_ride_on()
                   and pos.get("insane_tier") in _MG_TIERS
                   and (pos.get("momentum_at_entry", 0) or 0) >= _MG_MOM_MIN
                   and _MG_LIQ_LO <= _vae_entry_liq < _MG_LIQ_HI)
            # S94b: extend the RIDE to the deep_pool/brain_rule cohort (the deploy-gate edge) —
            # scoped by PLAY, not liq, so it hits only the proven cohort, never the big-liq bleeder.
            _dp = (_deep_pool_ride_on()
                   and pos.get("insane_tier") in _DP_RIDE_PLAYS)
            # S107: the momentum-override runner cohort rides (no fixed-TP bank, SL+trail) —
            # it's +EV ONLY under a trail, −EV under a fixed TP. Keyed on the position flag
            # (set at open) so it can never touch any other book; same canary as its admission.
            _mo = (_momentum_override_ride_on() and bool(pos.get("momentum_override")))
            # TAXONOMY: when the v2 policy canary is ON for this bot, the named exit FAMILY
            # from the table (play_v2 × POOL DEPTH — the EVOLVE-12 conditioning) supersedes
            # the per-canary flag reads above. The mapping is byte-equivalent to the legacy
            # flag outcomes (tested, test_taxonomy.py): RIDE_TIGHT ≡ the _dp branch ·
            # RIDE_WIDE ≡ the _small/_mg/_mo branch · BANK/SCALP_OUT ≡ no remap (the
            # position's own tier params). Fail-open: family None → legacy flags rule.
            # house_money + catastrophic_drain are global overlays, untouched either way.
            _fam = _regime_policy.exit_family(pos, _TAXO_BOT_ID)
            if _fam is not None:
                _dp = _fam == "RIDE_TIGHT"
                _mo = _fam == "RIDE_WIDE"
                _small = _mg = False
            if _small or _mg or _dp or _mo:
                # EVOLVE12: deep-pool cohort activates the trail EARLIER (+5%) to capture the move;
                # gem/small ride keep the wider +10% activation (different cohort). SL stays −10 for all.
                take_profit        = _DP_RIDE_TRAIL_ACT_PCT if _dp else _VAE_TRAIL_ACT_PCT
                stop_loss          = _VAE_STOP_LOSS_PCT
                pos["ride"]        = True
                pos["tp1_enabled"] = False
            current_price = current_prices.get(mint)
            if not current_price:
                continue

            entry     = pos["entry_price"]
            pnl_pct   = ((current_price - entry) / entry) * 100
            age_hours = (now - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600

            # Back-compat: positions opened before Smart-Trail existed have no history fields
            if "price_history" not in pos:
                pos["price_history"] = [entry]
                pos["peak_price"]    = entry
                pos["trail_active"]  = False
                dirty = True

            # ── S92/S100: price-glitch guard (phantom-TP killer) ────────────────────
            # Reject an absurd UPWARD spike before it can bank a phantom take-profit or
            # poison peak/trail. A real pump persists across reads; a glitch reverts.
            #   • ≥_PRICE_GLITCH_HARD_MULT (20×, +1900%): ALWAYS a feed glitch — clamp to
            #     last-good regardless of strike count (S100: the old two-strike confirm let
            #     a PERSISTENTLY-glitched feed "confirm" itself and bank a ~5000× phantom).
            #   • _PRICE_GLITCH_MULT (5×) ≤ x < hard: a suspect spike — clamp for one cycle,
            #     then trust on the 2nd consecutive read (a genuine fast pump rides).
            # CLAMPING (vs the old `continue`-skip) keeps the real exit guards (SL / dead-pool /
            # max-hold) evaluating on a SANE price, so a permanently-glitched position still
            # closes — booking a REAL small pnl, never the fabricated one. Downward reads are
            # NOT guarded — a one-cycle rug (S87 −81%) must reach the catastrophic-drain/stop.
            _last_good      = pos["price_history"][-1]
            _glitch_clamped = False
            if _last_good > 0 and current_price > _last_good * _PRICE_GLITCH_MULT:
                _is_hard = current_price > _last_good * _PRICE_GLITCH_HARD_MULT
                pos["px_glitch_strikes"] = pos.get("px_glitch_strikes", 0) + 1
                if _is_hard or pos["px_glitch_strikes"] < 2:
                    print(f"  [PX-GLITCH] {mint[:6]}... read {current_price:.6g} = "
                          f"{current_price / _last_good:.0f}× last-good {_last_good:.6g} "
                          f"(pnl {pnl_pct:+.0f}%) — {'HARD glitch' if _is_hard else 'suspect spike'}, "
                          f"clamping to last-good", flush=True)
                    current_price   = _last_good
                    pnl_pct         = ((current_price - entry) / entry) * 100
                    _glitch_clamped = True
                    dirty           = True
            else:
                pos["px_glitch_strikes"] = 0

            # Maintain rolling price history and peak — skip when we clamped a glitch read
            # (do NOT let a fabricated price enter history or ratchet the peak/trail).
            if not _glitch_clamped:
                hist = pos["price_history"]
                hist.append(current_price)
                if len(hist) > SMART_TRAIL_HISTORY_LEN:
                    hist.pop(0)
                if current_price > pos["peak_price"]:
                    pos["peak_price"] = current_price
                dirty = True

            # ── LP-drain rug guard ───────────────────────────────────────────────
            # A pool draining fast is a rug in progress — exit before the price craters
            # (the −75%-in-30min tail risk). Reuses the per-position liq_history that main.py
            # maintains each cycle (parallel to vol_5m_history). TWO-STRIKE: the drain must
            # show on two consecutive cycles before firing, so a single false/empty liquidity
            # read (the S60 degraded-RPC failure mode) can't panic-sell a healthy position.
            _LIQ_DRAIN_PCT     = -0.15   # ≥15% pool drop in one cycle = draining
            _LIQ_DRAIN_STRIKES = 2       # consecutive draining cycles required to fire
            _liq_drain  = False
            _liq_vliq   = 0.0
            _lh = pos.get("liq_history", [])
            if len(_lh) >= 2 and _lh[-2] > 0:
                _liq_vliq = (_lh[-1] - _lh[-2]) / _lh[-2]
                if _liq_vliq <= _LIQ_DRAIN_PCT:
                    pos["liq_drain_strikes"] = pos.get("liq_drain_strikes", 0) + 1
                else:
                    pos["liq_drain_strikes"] = 0
                if pos["liq_drain_strikes"] >= _LIQ_DRAIN_STRIKES:
                    _liq_drain = True
                dirty = True
            elif pos.get("liq_drain_strikes"):
                pos["liq_drain_strikes"] = 0
                dirty = True

            # ── B-step-2: catastrophic single-cycle LP-drain (1-strike) ──────────
            # A ≥50% pool collapse in ONE cycle is a rug the 2-strike guard above can't
            # catch in time. Only fires when the post-drop pool is still credibly sellable
            # (≥$30k) so a false-empty RPC read (→ near-zero) can't 1-strike-panic-sell.
            _catastrophic_drain = (
                _catastrophic_drain_on()
                and len(_lh) >= 2 and _lh[-2] > 0
                and _liq_vliq <= _CATASTROPHIC_DRAIN_PCT
                and _lh[-1] >= _CATASTROPHIC_MIN_LIQ
            )

            # ── S74: dead-pool / volume-collapse guard ───────────────────────────
            # A pool whose 5m volume has collapsed to ~nothing is dying: its price feed
            # freezes at the last trade, so the position looks "still at its high" to the
            # trail logic and rides to the 2× hard ceiling, accruing ghost (unsellable)
            # risk every cycle. This is the FizdC artifact (S74): +58% "peak" pinned at
            # 0.001591 while vol_5m collapsed 61,128 → 1.37. Exit NOW while a route may
            # still exist. TWO-STRIKE on an absolute floor (parallel to the LP-drain guard)
            # so a single glitchy/empty volume read can't panic-sell a healthy position.
            #
            # S80: vol-dead ALONE was clipping quiet-but-HEALTHY deep pools at ~3–5min hold
            # (the +14% paper edge is a 30-min horizon — see S78/S79 data: every "Dead pool"
            # close was near-flat on a tiny hold). The real ghost signature is vol-dead AND a
            # FROZEN price feed (the quote pinned at the last trade). A quiet-but-alive pool
            # still ticks cycle-to-cycle, so it now gets its full 30-min window; only a pinned
            # feed (FizdC-type freeze) exits immediately. Both conditions strike-gated.
            _VOL_DEAD_USD     = 500.0   # 5m volume below this on a deep_pool entry = no trading
            _VOL_DEAD_STRIKES = 2       # consecutive dead reads required to fire
            _vh = pos.get("vol_5m_history", [])
            _vol_dead = (len(_vh) >= _VOL_DEAD_STRIKES
                         and all(0 <= v < _VOL_DEAD_USD for v in _vh[-_VOL_DEAD_STRIKES:]))
            # Frozen feed = the last _VOL_DEAD_STRIKES price reads are bit-identical (a dying
            # pool's quote pins at the last trade; a healthy quiet pool still moves a hair).
            _price_frozen = (len(hist) >= _VOL_DEAD_STRIKES
                             and len(set(hist[-_VOL_DEAD_STRIKES:])) == 1)
            _dead_pool = _vol_dead and _price_frozen

            reason = None

            # ── S72: Size-aware breakeven ratchet ─────────────────────────────────
            # Bigger bets ratchet their stop toward breakeven earlier/tighter than the
            # TP1→BE path below — protecting sized-up capital the moment it's meaningfully
            # green, without waiting for the partial. Monotonic (only ever tightens the
            # floor), so it coexists with TP1 (which later sets 0.0). No-op when the canary
            # is off, for small bets, or for old positions lacking wallet_frac.
            if _be_ratchet_on():
                _br_floor = _be_ratchet_floor(pos.get("wallet_frac"), pnl_pct)
                if _br_floor is not None:
                    _prev_floor = pos.get("sl_floor_pct")
                    if _prev_floor is None or _br_floor > _prev_floor:
                        pos["sl_floor_pct"]     = _br_floor
                        pos["be_ratchet_armed"] = True
                        dirty = True
                        _wf  = (pos.get("wallet_frac") or 0.0) * 100
                        _cl  = "breakeven — can't lose" if _br_floor >= 0.0 else f"floor {_br_floor:.0f}%"
                        print(
                            f"  [BE-RATCHET] {mint[:6]}... wf={_wf:.0f}% +{pnl_pct:.1f}% "
                            f"→ SL → {_cl}",
                            flush=True,
                        )

            # After TP1 partial exit fires, SL is raised to breakeven (0%).
            # sl_floor_pct = 0.0 means we never close below entry once partial profit is taken.
            effective_stop_loss = max(stop_loss, pos.get("sl_floor_pct", stop_loss))

            # 1a. B-step-2: catastrophic single-cycle LP collapse — a one-cycle ≥50% LP pull
            #     is a rug in progress the 2-strike guard can't catch in time. Exit NOW (ahead
            #     of even the stop-loss) while the post-drop pool (≥$30k) still has a sell route,
            #     turning a −100% ride into a partial loss.
            if _catastrophic_drain:
                reason = (f"Catastrophic LP drain {_liq_vliq*100:.0f}%/cyc 1-strike"
                          f" (liq ${_lh[-2]/1000:.0f}k→${_lh[-1]/1000:.0f}k, pnl {pnl_pct:+.1f}%)")

            # 1. Stop-loss — always enforced (uses breakeven floor after TP1)
            elif pnl_pct <= effective_stop_loss:
                be_tag = " [BE floor]" if pos.get("sl_floor_pct") is not None and effective_stop_loss > stop_loss else ""
                reason = f"Stop loss {pnl_pct:.1f}%{be_tag}"

            # 1b. LP-drain rug guard — pool draining on consecutive cycles, exit NOW while a
            #     route still exists, ahead of the time/trail logic (a rug won't wait for them).
            elif _liq_drain:
                reason = (f"LP drain {_liq_vliq*100:.0f}%/cyc ×{pos.get('liq_drain_strikes', 0)}"
                          f" (liq ${_lh[-1]/1000:.0f}k, pnl {pnl_pct:+.1f}%)")

            # 1c. S74/S80: dead-pool / volume-collapse — exit a dying pool before its price feed
            #     freezes and it rides to the 2× ceiling as a ghost. Overrides trail_active by
            #     sitting ahead of the time/trail logic (a frozen price never trips the trail).
            #     S80: now requires vol-dead AND a frozen feed, so quiet-but-healthy pools keep
            #     their 30-min window (the fix for the S78/S79 exit-side edge leak).
            elif _dead_pool:
                reason = (f"Dead pool — 5m vol ${_vh[-1]:,.0f} <${_VOL_DEAD_USD:.0f} +frozen px ×{_VOL_DEAD_STRIKES}"
                          f" (pnl {pnl_pct:+.1f}%) — exit before it ghosts")

            # 2. Hard absolute ceiling — 2× hold time as a backstop even while trailing
            elif age_hours >= max_hold * 2:
                reason = f"Max hold {age_hours:.1f}h"

            # 3. Normal time limit — STALL-AWARE (S68).
            #    S64 set the rule: "ride" plays extend via trail, "bank" plays (STOIC)
            #    close at the limit. S68 inserts one decision in front of that: a riding
            #    play that hits its time limit while STILL pinned to its high is mid-move,
            #    so keep holding it under the normal TP1/TP machinery (the 2× hard ceiling
            #    above remains the absolute backstop). Only once it has FADED off its high
            #    does it fall back to the Smart-Trail hand-off; flat/losing closes on time.
            elif age_hours >= max_hold and not pos["trail_active"]:
                _ride      = pos.get("ride", True)
                _near_peak = current_price >= pos["peak_price"] * _STILL_WORKING_NEAR_PEAK
                if _ride and pnl_pct > 1.0 and _near_peak:
                    # STILL WORKING — extend. Do nothing here so the TP1/full-TP logic
                    # below stays live; each cycle re-checks "near peak", so it naturally
                    # keeps riding while it makes highs and stops the instant it stalls.
                    if not pos.get("hold_ext_logged"):
                        pos["hold_ext_logged"] = True
                        dirty = True
                        peak_pct = (pos["peak_price"] - entry) / entry * 100
                        print(
                            f"  [HOLD-EXT] {mint[:6]}... past {max_hold:.1f}h at +{pnl_pct:.1f}% "
                            f"but still at its high (peak +{peak_pct:.1f}%) — extending toward "
                            f"the {max_hold*2:.1f}h ceiling while it climbs",
                            flush=True,
                        )
                elif _ride and pnl_pct > 1.0:
                    # STALLED but green — hand off to Smart Trail (S64 behavior).
                    pos["trail_active"] = True
                    # Floor near current profit (not TP) so we don't immediately exit just
                    # because pnl < take_profit. Never below the breakeven floor from TP1.
                    _be_floor = pos.get("sl_floor_pct", 0.0)
                    pos["trail_floor_pct"] = max(max(1.0, pnl_pct - 2.0), _be_floor)
                    dirty = True
                    peak_pct = (pos["peak_price"] - entry) / entry * 100
                    print(
                        f"  [TRAIL] {mint[:6]}... time limit at +{pnl_pct:.1f}% (faded off peak "
                        f"+{peak_pct:.1f}%) → trailing, floor +{pos['trail_floor_pct']:.1f}%",
                        flush=True,
                    )
                else:
                    reason = f"Time limit {age_hours:.1f}h"

            if not reason:
                # ─── S96: house-money de-risk ("paid off") — once, on a big winner ──────
                # Bank ~the initial cost basis (conviction-scaled) so the trade is GREEN
                # even if the remainder rugs; the remainder keeps riding UNCAPPED. Sits
                # AHEAD of TP1 and the trail, and fires even while trailing — so it composes
                # with the ride canaries (bank cost-basis here, the trail rides the rest next
                # cycle). One-shot per position via house_money_taken. `continue` like TP1.
                if (_hm_cfg is not None
                        and not pos.get("house_money_taken", False)
                        and pnl_pct >= _hm_trigger):
                    _hm_f, _hm_conv, _hm_rmult = _house_money_plan(
                        pnl_pct, pos.get("confidence"), pos.get("regime"), _hm_cushion)
                    pos["house_money_taken"]  = True
                    pos["remaining_fraction"] = round(
                        pos.get("remaining_fraction", 1.0) * (1.0 - _hm_f), 4)
                    dirty = True
                    print(
                        f"  [HOUSE-MONEY] {mint[:6]}... +{pnl_pct:.0f}% → bank {_hm_f*100:.0f}% "
                        f"(recover {_hm_rmult:.2f}× basis | conviction {_hm_conv:.2f} "
                        f"| {pos.get('regime','?')}/conf {pos.get('confidence','?')}) "
                        f"→ riding {(1-_hm_f)*100:.0f}% FREE",
                        flush=True,
                    )
                    exits.append({
                        "mint":        mint,
                        "exit_type":   "partial",
                        "fraction":    _hm_f,
                        "reason":      f"House-money +{pnl_pct:.0f}% (recover {_hm_rmult:.2f}× basis, conv {_hm_conv:.2f})",
                        "pnl_pct":     pnl_pct,
                        "house_money": True,
                        "position":    dict(pos),
                    })
                    continue  # banked this cycle; the trail rides the rest from next cycle

                # ─── TP1 partial exit — fires before full trail activation ─────────────
                # Only fires when pnl is in the zone between tp1_pct and full take_profit.
                # Sells a fraction (e.g., 35%) and moves SL to breakeven for the remainder.
                # Skipped if trail is already active (trail already managing the position).
                # S64: read TP1 params from the position (set per-play at open for ALL modes);
                # fall back to the mode's play table, then INSANE_TIER_PARAMS for positions
                # opened before this field existed (back-compat across a restart).
                _tier     = pos.get("insane_tier")
                _play_cfg = MODE_PLAYS.get(pos.get("mode"), INSANE_TIER_PARAMS).get(_tier, {}) if _tier else {}
                _tp1_on   = pos.get("tp1_enabled",  _play_cfg.get("tp1_enabled", False))
                _tp1_pct  = pos.get("tp1_pct",      _play_cfg.get("tp1_pct", 0.0))
                _tp1_frac = pos.get("tp1_fraction", _play_cfg.get("tp1_fraction", 0.35))

                # Dynamic TP1: if the token's recent price range signals high velocity,
                # take the partial exit earlier to capture gains before the pullback.
                _dyn_tp1           = False
                _vol               = 0.0
                _effective_tp1_pct = _tp1_pct
                if _tp1_on and _tp1_pct and not pos.get("tp1_taken", False):
                    _vol = _recent_range_pct(hist)
                    if _vol >= DYNAMIC_TP1_VOL_THRESHOLD and _tp1_pct > DYNAMIC_TP1_EARLY_PCT:
                        _effective_tp1_pct = DYNAMIC_TP1_EARLY_PCT
                        _dyn_tp1 = True

                if (_tp1_on and _effective_tp1_pct
                        and not pos.get("tp1_taken", False)
                        and not pos["trail_active"]
                        and _effective_tp1_pct <= pnl_pct < take_profit):
                    pos["tp1_taken"]          = True
                    pos["remaining_fraction"] = round(1.0 - _tp1_frac, 3)
                    pos["sl_floor_pct"]       = 0.0  # SL raised to breakeven for remainder
                    pos["dyn_tp1_fired"]      = _dyn_tp1
                    pos["tp1_pct_fired"]      = round(_effective_tp1_pct, 1)
                    dirty = True
                    _dyn_tag = f" [DYN-TP1 vol={_vol:.1f}%]" if _dyn_tp1 else ""
                    print(
                        f"  [TP1] {mint[:6]}... +{pnl_pct:.1f}% → "
                        f"partial {_tp1_frac*100:.0f}% | SL → breakeven | "
                        f"{pos['remaining_fraction']*100:.0f}% position remains{_dyn_tag}",
                        flush=True,
                    )
                    exits.append({
                        "mint":      mint,
                        "exit_type": "partial",
                        "fraction":  _tp1_frac,
                        "reason":    f"TP1 +{pnl_pct:.1f}%{_dyn_tag.strip()}",
                        "pnl_pct":   pnl_pct,
                        "position":  dict(pos),
                    })
                    continue  # don't process trail/full-tp this cycle

                # ─── Trail and full-TP logic ──────────────────────────────────────────
                if pos["trail_active"]:
                    # Trail is live — exit only when price drops through the volatility buffer.
                    # trail_floor_pct is set when the trail activates:
                    #   TP-triggered: floor = take_profit  (never give back the core profit)
                    #   Time-limit:   floor = pnl - 2%     (protect current profit, not the full TP)
                    # S102: runner free-ride uses a wide FIXED give-back (Jotchua cohort only) so a
                    # legging meme survives inter-leg pullbacks to the house-money bank, then rides
                    # the FREE remainder wide once basis is recovered. None → vol-scaled 8%-cap buffer.
                    # TAXONOMY: the RUNNER play (momentum_override, family RIDE_WIDE) joins the
                    # free-ride cohort when the v2 canary is ON — the live receipt (rowid 5081:
                    # trail clipped +31% of a +176% runner on a collapsed 1.1% vol-buffer) is the
                    # exact S102 pattern, and the validated runner edge was modeled on a WIDE
                    # trail (retain ~0.75 of peak), not a 1% buffer. Canary off → byte-identical.
                    _wide_cohort = _mg or (bool(pos.get("momentum_override"))
                                           and _regime_policy.exits_on(_TAXO_BOT_ID))
                    buffer_pct, raw_trigger = _trail_buffer(
                        pos["peak_price"], hist, fixed_buffer_pct=_runner_trail_buffer(pos, _wide_cohort))
                    _floor = pos.get("trail_floor_pct", take_profit)
                    tp_price = entry * (1 + _floor / 100)
                    trigger_price = max(raw_trigger, tp_price)
                    if current_price < trigger_price:
                        peak_pct = (pos["peak_price"] - entry) / entry * 100
                        reason = (
                            f"Smart trail +{pnl_pct:.1f}% "
                            f"(peak +{peak_pct:.1f}%, {buffer_pct:.1f}% buffer)"
                        )
                    else:
                        peak_pct = (pos["peak_price"] - entry) / entry * 100
                        trigger_pct = (trigger_price - entry) / entry * 100
                        print(
                            f"  [TRAIL] {mint[:6]}... "
                            f"pnl={pnl_pct:+.1f}% peak={peak_pct:+.1f}% "
                            f"trigger≥{trigger_pct:+.1f}% (buf {buffer_pct:.1f}%)",
                            flush=True,
                        )
                elif pnl_pct >= take_profit:
                    # S64: ride vs bank — the structural difference between the modes.
                    if pos.get("ride", True):
                        # RIDE (INSANE gem/highconv/force_fire, WILD) — hand off to Smart
                        # Trail and let the winner run. Floor at TP so core profit is safe.
                        pos["trail_active"]    = True
                        pos["trail_floor_pct"] = take_profit
                        dirty = True
                        peak_pct = (pos["peak_price"] - entry) / entry * 100
                        print(
                            f"  [TRAIL] {mint[:6]}... "
                            f"TP +{take_profit:.0f}% hit → Smart Trail ACTIVE (RIDE) "
                            f"| peak so far +{peak_pct:.1f}%",
                            flush=True,
                        )
                    else:
                        # BANK (STOIC) — take the profit at TP, no trailing. Discipline.
                        reason = f"TP bank +{pnl_pct:.1f}%"

                # ─── Flatline exit ─────────────────────────────────────────────
                # Release capital slots occupied by dead tokens.
                #
                # Two-condition trigger (both must hold simultaneously):
                #   1. Vol condition: avg of last 3 vol_5m readings (≈15m proxy) has
                #      fallen below 50% of the volume recorded at entry.
                #   2. Price condition: price has not drifted ±1% from the flatline
                #      anchor for two consecutive 30-minute windows (60 min total).
                #
                # Mechanics: when vol first drops < 50% of entry, the current price
                # is stored as `flatline_ref` and a timer starts.  Every cycle the
                # price drift is checked against that anchor.  If drift escapes the
                # ±1% band the anchor resets (timer restarts).  Exit fires only when
                # the timer has run continuously for 60 min.
                #
                # Guards:
                #   - position must be ≥ 1h old (new positions need time to develop)
                #   - trail must not be active (trail already managing the exit)
                #   - entry vol must be recorded (old pre-feature positions are skipped)
                _FL_VOL_RATIO  = 0.50   # vol threshold: < 50% of entry
                _FL_PRICE_BAND = 1.0    # ±1% price band around anchor
                _FL_HOLD_MIN   = 60.0   # timer threshold: 60 min = two 30-min windows
                if not pos.get("trail_active") and age_hours >= 1.0:
                    _entry_vol  = pos.get("volume_5m_at_entry", 0)
                    _vh         = pos.get("vol_5m_history", [])
                    _recent_avg = 0.0
                    _vol_dead   = False
                    if _entry_vol > 0 and len(_vh) >= 3:
                        _recent_avg = sum(_vh[-3:]) / 3
                        _vol_dead   = _recent_avg < _entry_vol * _FL_VOL_RATIO

                    if _vol_dead:
                        _fl_ref   = pos.get("flatline_ref", 0.0)
                        _fl_since = pos.get("flatline_since")

                        if not _fl_ref:
                            # First vol-dead cycle — anchor price and start timer.
                            pos["flatline_ref"]   = current_price
                            pos["flatline_since"] = now.isoformat()
                            dirty = True
                            print(
                                f"  [FLATLINE] {mint[:6]}... timer started"
                                f" (vol {_recent_avg:.0f} < 50% of entry {_entry_vol:.0f}"
                                f", pnl {pnl_pct:+.1f}%)",
                                flush=True,
                            )
                        else:
                            _drift_pct = abs(current_price - _fl_ref) / _fl_ref * 100
                            if _drift_pct >= _FL_PRICE_BAND:
                                # Price escaped the band — re-anchor and restart timer.
                                pos["flatline_ref"]   = current_price
                                pos["flatline_since"] = now.isoformat()
                                dirty = True
                            elif _fl_since:
                                try:
                                    _fl_age_m = (
                                        now - datetime.fromisoformat(_fl_since)
                                    ).total_seconds() / 60
                                    if _fl_age_m >= _FL_HOLD_MIN:
                                        reason = (
                                            f"Flatline {_fl_age_m:.0f}m"
                                            f" (vol {_recent_avg:.0f} < 50% of {_entry_vol:.0f}"
                                            f", ±{_drift_pct:.2f}% drift)"
                                        )
                                except Exception:
                                    pass
                    elif pos.get("flatline_since"):
                        # Vol recovered — discard the flatline state.
                        pos.pop("flatline_since", None)
                        pos.pop("flatline_ref",   None)
                        dirty = True

            if reason:
                exits.append({
                    "mint": mint, "reason": reason,
                    "pnl_pct": pnl_pct, "position": pos,
                })

        if dirty:
            self._save_positions()

        return exits

    def partial_close_position(self, mint: str, fraction: float) -> None:
        """Reduce position size by fraction after a partial (TP1) exit. Position stays open.
        Does NOT record to memory — the final close_position() call records the outcome.
        """
        pos = self.positions.get(mint)
        if not pos:
            return
        remaining = 1.0 - fraction
        pos["size_sol"] = round(pos["size_sol"] * remaining, 6)
        pos["size_usd"] = round(pos["size_usd"] * remaining, 4)
        self._save_positions()

    def close_position(self, mint: str, pnl_pct: float, cycle: int) -> None:
        pos = self.positions.pop(mint, None)
        self._save_positions()
        if not pos:
            return

        pnl_usd = pos["size_usd"] * (pnl_pct / 100)
        memory.record_trade(
            mint=mint,
            pnl_usd=pnl_usd,
            momentum_at_entry=pos["momentum_at_entry"],
            volume_spike=True,
            volume_5m_at_entry=pos.get("volume_5m_at_entry", 0.0),
            pnl_pct=pnl_pct,   # S89: size-independent break-even classification
        )

        stats = memory.get_stats(mint)
        consec = stats.get("consecutive_losses", 0) if stats else 0
        if consec >= 2:
            memory.ban_token(mint, consec)
