"""
Solana Trading Bot — The Stoic Strategy
Learns from every trade. Sizes by confidence. Never deviates from the rules.
"""
import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx

import audit
import bonding_curve
from helius import rpc_post
import dexscreener
import discovery
import jupiter
import jupiter_ultra   # execwire: Jupiter Ultra buy adapter (default-OFF Bot1 canary)
import memory
import payout
import safety
import rug_screen
import wash_veto
import admit_guard   # ADMITGUARD: evidence-gated entry brake (skip PROVEN −EV play×regime cells; gate-protected)
import regime_policy  # REGIMEPOLICY: staged play×depth policy surface (default-OFF, fail-open; research/regime_rethink)
import lunarcrush
import wallet
import debater
import config as _config_mod
import config_manager as _cfg_mgr
from observer import Observer
from auditor import Auditor
from config import (
    BASE_MINT, HELIUS_RPC_URL, FALLBACK_RPC_URL, POLL_INTERVAL_SECONDS, TOTAL_CAPITAL_USD,
    USDC_MINT, WATCHLIST, WATCHLIST_LOW, WATCHLISTS_BY_CAP, WATCHLISTS_EXACT,
    RESERVE_SOL, MAX_DEPLOYED_SOL_PCT, MIN_POSITION_SOL,
    MAX_SLIPPAGE_BPS, STOIC_SLIPPAGE_BPS,
    SIZE_TIER_PCTS, SIZE_BASE_FRACTION,
    DISCOVERY_REFRESH_CYCLES,
    DISCOVERY_MAX_BY_MODE, ACTIVE_WATCHLIST_CAP, DISCOVERY_PAGES_BY_MODE,
    GEM_MIN_LIQUIDITY, GEM_MAX_AGE_HOURS, GEM_MIN_BUY_RATIO,
    STACK_CONFIDENCE_MIN, MAX_STACK_COUNT, STACK_SIZE_MULTIPLIER,
    CIRCUIT_BREAKER_LOSSES, CIRCUIT_BREAKER_WINDOW_M, CIRCUIT_BREAKER_COOLOFF_M,
    CONVICTION_BC_WHALE_MULT, CONVICTION_ELITE_MULT,
    TWAP_IMPACT_THRESHOLD, TWAP_TRANCHES, TWAP_INTER_TRANCHE_DELAY,
)
from risk import RiskManager
from stoic_strategy import (
    StoicStrategy, MODES, INSANE_TIER_PARAMS, MODE_PLAYS, MeanReversionStrategy,
    get_mode as stoic_strategy_module_mode, get_marketcap, get_size_tier, set_size_tier,
    get_viral_weight, classify_insane_tier, classify_play, apply_overrides as strategy_apply_overrides,
)
from entry_queue import EntryQueue

from bot_config import DATA_DIR, BOT_ID
from datetime import datetime as _dt, timezone as _tz
_SESSION_START = _dt.now(_tz.utc).isoformat()  # failures before this restart are ignored
_B = f"Bot #{BOT_ID}"  # prefix tag for all log lines — identifies which bot in mixed terminal output

STATUS_PATH   = DATA_DIR / "status.json"
HISTORY_PATH  = DATA_DIR / "balance_history.jsonl"
EXIT_FLAG     = DATA_DIR / "exit_requested.json"
PAUSE_FLAG    = DATA_DIR / "pause_flag.json"
POSITIONS_PATH = DATA_DIR / "positions.json"

# Tx-failure tracking — keyed by mint, value = cycle number of last failure
# 1-cycle cooldown: skip any mint whose last failure was the previous cycle
# Session ban: skip any mint that has failed 2+ times this session
_tx_fail_last_cycle: dict = {}   # mint -> cycle when last tx failed
_tx_fail_count:      dict = {}   # mint -> total tx failures this session

# Circuit breaker — consecutive-loss cool-off
# Tracks recent closed-trade outcomes so repeated losses in a short window
# trigger an entry freeze. Exits are never blocked by this.
_recent_trade_results: deque = deque(maxlen=20)  # (datetime, was_win: bool)
_circuit_breaker_until: Optional[datetime] = None

# ── Fleet brain: rolling PnL% window for self-optimization ───────────────────
_recent_pnl_pcts: deque = deque(maxlen=20)   # pnl% of last 20 real closes (not ghost/partial)
_goldilocks_trade_count: int = 0             # closes since last goldilocks --emit-override run
_auto_size_baseline: Optional[str] = None    # tier active before auto step-down; None = not managed

# ── EV halt constants (mirrors risk.py _EV_HALT_* — kept here for log strings) ─
_EV_HALT_STREAK    = 10
_EV_HALT_THRESHOLD = -0.1

# ── Wire-to-live rule filter (DORMANT) ────────────────────────────────────────
# When strategy_brain has a candidate that fully clears the live-readiness bar
# (n≥100, EV≥+2%, WR≥55%, EV-lo>0, ≥24h span) AND this flag is True, every entry must
# also satisfy that rule's predicate (see live_rule.py). It can ONLY make entries more
# selective — never adds/upsizes a trade — so it cannot increase risk. Flag OFF =
# byte-for-byte current behavior; the import is guarded so a missing/broken module can
# never break the fleet. Flip to True + ./run.sh ONLY after `strategy_brain.py --evolve`
# prints ✅ READY FOR REAL SOL (and after enriching sig["liq_mc"] — see live_rule.py).
_LIVE_RULE_ENABLED = False
try:
    import live_rule as _live_rule_mod
except Exception as _e:        # pragma: no cover — never let this break startup
    _live_rule_mod = None
    print(f"[LIVE-RULE] module unavailable ({_e}) — filter disabled", flush=True)

# ── EV-weighted sizing (DORMANT — the §G.1 / PATH-TO-PRESTIGE step-2 lever) ─────────
# When enabled, a brain-VALIDATED entry (deep_pool / brain_rule) is sized by the admitting
# rule's MEASURED edge (ev_lo → fraction of the play's size_cap) instead of the conf²/C²
# mapping. Fresh discovery tokens default to conf ~0.6 → C² crushes their size to ~1.5% of
# wallet, so the brain's validated +5–12.8% edge is bet like a lottery ticket; this sizes it
# by the edge instead (deep_pool_filling ev_lo +6 → ~12% of wallet). ALL downstream guards
# stay (vol_scale, disc cap, per-play ceiling, headroom, MIN_POSITION_SOL) — it can only size
# WITHIN the existing risk envelope, never beyond. ⚠ Flip True + ./run.sh ONLY after live
# deep_pool proves +EV (go_live_check: ghost-rate ≤10% AND live net ≥0). Sizing up an unproven
# (currently −EV) edge just loses faster. Flag OFF = byte-for-byte current C² sizing.
_EV_WEIGHTED_SIZING_ENABLED = False
try:
    from ev_sizing import ev_size_fraction_of_cap as _ev_size_frac
    from ev_sizing import ev_strong_fraction_of_cap as _ev_strong_frac   # S75: strong+stable only
except Exception as _e:        # pragma: no cover — never let this break startup
    _ev_size_frac = None
    _ev_strong_frac = None
    print(f"[EV-SIZING] module unavailable ({_e}) — EV-weighted sizing disabled", flush=True)

# ── Native strict-score gate (A/B experiment, per-bot, default OFF) ─────────────
# When enabled for THIS bot via bots/botN/strict_gate.json {"enabled": true, "floor": 80},
# entries whose validation_score is below the floor are skipped — the bot then trades ONLY
# the high-score band ([80,95) was +7.3% EV / WR 68% on sellable obs; score<50 is −1.75% EV,
# the bulk of the bleed). Enforced at execute_buy (the single chokepoint) so it covers EVERY
# path including the score-bypassing probe / MR / force_fire entries (force_fire score=0 →
# correctly filtered). Reversible: delete/disable the file + ./run.sh. A/B: enable on ONE bot
# (Bot 1 = highest volume = fastest feedback), leave others current, let edge_report.py judge.
_STRICT_GATE_PATH  = DATA_DIR / "strict_gate.json"
_strict_gate_cache = (0.0, None)   # (read_ts, floor_or_None) — 30s cache off the hot path

def _strict_gate_floor():
    global _strict_gate_cache
    if time.time() - _strict_gate_cache[0] < 30.0:
        return _strict_gate_cache[1]
    floor = None
    try:
        _d = json.loads(_STRICT_GATE_PATH.read_text())
        if _d.get("enabled"):
            floor = float(_d.get("floor", 80.0))
    except Exception:
        floor = None
    _strict_gate_cache = (time.time(), floor)
    return floor


# ── EV-weighted sizing deploy flag — per-bot, hot-reload (the canary switch) ────────
# Deployment of the EV-sizing lever (PATH-TO-PRESTIGE step 2) is a per-bot file so it can
# be canaried on ONE bot, reversed instantly, and needs NO restart (30s hot-reload). Enable
# via bots/botN/ev_sizing.json {"enabled": true}. EV-sizing is active when the module master
# flag is True OR this per-bot file is enabled. Default (no file) = OFF = current C² sizing.
_EV_SIZING_PATH  = DATA_DIR / "ev_sizing.json"
_ev_sizing_cache = (0.0, False, 1.0)   # (read_ts, enabled, scale) — 30s cache off the hot path

def _ev_sizing_refresh():
    """Read bots/botN/ev_sizing.json once per 30s → (enabled, scale).
    `scale` (clamped 0..1, default 1.0) damps the EV target — this is the SIZE GENE
    knob for the prestige race: Bot1 full (no scale / 1.0), Bot2 moderate (e.g. 0.5),
    Bot3 control (no file → EV-sizing off). Never raises."""
    global _ev_sizing_cache
    if time.time() - _ev_sizing_cache[0] < 30.0:
        return _ev_sizing_cache
    enabled, scale = False, 1.0
    try:
        _cfg = json.loads(_EV_SIZING_PATH.read_text())
        enabled = bool(_cfg.get("enabled"))
        _s = _cfg.get("scale")
        if _s is not None:
            scale = max(0.0, min(1.0, float(_s)))
    except Exception:
        enabled, scale = False, 1.0
    _ev_sizing_cache = (time.time(), enabled, scale)
    return _ev_sizing_cache

def _ev_sizing_on():
    if _EV_WEIGHTED_SIZING_ENABLED:
        return True
    return _ev_sizing_refresh()[1]

def _ev_sizing_scale():
    """Size-gene damping for THIS bot's EV-sizing (1.0 = full, <1 = moderate)."""
    return _ev_sizing_refresh()[2]

# ── S72: brain-driven mid-trade size-up (pyramiding into BE-protected winners) ──────
# The offensive sibling of the BE ratchet: once a position is BE-protected (can't lose
# below ~breakeven) AND the brain sees fresh conviction arriving, ADD to it — capturing
# more SOL on a move that's already proven, with bounded downside (the locked tranche-1
# profit cushions the new tranche down to the blended breakeven). It's the "I should size
# up" event the operator asked for: evidence-gated, not greedy.
#
# Trigger = MODERATE (operator choice): a BE-armed winner still making new highs, in a
# non-draining pool, that shows ANY of — (a) liquidity FILLING (lqv>0, the brain's #1
# sub-edge), (b) a buy-pressure / volume surge, or (c) a fresh whale on the held mint.
# Add SIZE is driven by the rule's measured ev_lo (ev_sizing.py), damped, and capped by a
# hard total-position ceiling so it can never balloon.
#
# DORMANT + per-bot canary, gated behind the SAME proven-live posture as EV-sizing — it
# only ever adds to the SUBSET that already reached +profit AND is still accelerating, so
# it's safer than blind entry-sizing, but it is NOT free EV (it can churn), so it stays off
# until deep_pool proves +EV live. Enable on ONE bot via bots/botN/sizeup.json
# {"enabled": true} — 30s hot-reload, reversible (rm the file). Default = current behavior.
_SIZEUP_ENABLED        = False              # module master (fleet-wide) — default OFF
_SIZEUP_PATH           = DATA_DIR / "sizeup.json"
_sizeup_cache          = (0.0, False)       # (read_ts, enabled) — 30s cache off the hot path
_SIZEUP_MAX_ADDS       = 2        # max size-ups per position (mirrors MAX_STACK_COUNT)
_SIZEUP_NEAR_PEAK      = 0.985    # "still making highs" = current ≥ 98.5% of peak
_SIZEUP_FILL_THRESH    = 0.01     # liq_velocity > +1% over window = pool FILLING (brain #1 edge)
_SIZEUP_BS_SURGE       = 1.5      # buy/sell ratio ≥ this = demand surge
_SIZEUP_VACC_SURGE     = 1.5      # vol_5m vs its SMA ≥ this = volume surge
_SIZEUP_DEFAULT_FRAC   = 0.20     # add = this fraction of play cap when the rule has no ev_lo
_SIZEUP_DAMP           = 0.5      # adds are HALF the target — build into a winner, don't double blind
_SIZEUP_TOTAL_CAP_MULT = 1.5      # total position ≤ 1.5× the play's size_cap (anti-greed ceiling)

# ── S79: regime-aware size posture (replaces the old continuous vol_scale curve) ──────
# The SIZE block scales every bet by a per-regime multiplier so each of the 5 market
# conditions has a DISTINCT sizing stance — matching the 5-regime entry ladder (the old
# max(0.35,(agg/500k)^0.5) curve was anchored to the dead 3-regime world: EUPHORIA got no
# premium over AGGRESSIVE, and DEAD≈SNIPER both floored at 0.35). Multiplies C² conf sizing
# and is multiplied IN TURN by the (dormant) EV-sizing override, so EV/edge stays orthogonal.
#   EUPHORIA   0.85  ride the frenzy but blow-off tops reverse hard — don't max in
#   AGGRESSIVE 1.00  cleanest trend = full participation
#   NORMAL     0.70  balanced
#   SNIPER     0.45  quiet, lower conviction → smaller
#   DEAD       0.25  -EV bleed zone — the looser DEAD entry bar pairs with the smallest size
# Tune here; takes effect on restart. Unknown regime → NORMAL (0.70) as a safe default.
_REGIME_SIZE_MULT = {
    "euphoria":   0.85,
    "aggressive": 1.00,
    "normal":     0.70,
    "sniper":     0.45,
    "dead":       0.25,
}

# S98 (gate-unfreeze): extra size-DOWN for the UNPROVEN `normal` deep_pool slice (the tight
# filling+bs>=1.5 core admitted via bots/botN/normal_deep_pool.json). That slice exists only to
# feed the deploy gate's n on a thin window (gate_unfreeze.py: mean +8.3/+11.0 but ev_lo<0 on
# ~2d/n=17), so we accumulate the gate's closes at REDUCED ◎ exposure until the gate's own
# net>=0 / ghost<=10% / n>=15 bars prove it. Stacks ON TOP of _REGIME_SIZE_MULT["normal"]=0.70
# → ~0.35× wallet posture. Does NOT touch ev_sizing (ONE OPERATOR RULE intact). Revert: set 1.0.
_NORMAL_DP_SIZE_MULT = 0.5

# S107 momentum-override lane: the running-momentum runner cohort is admitted via observer's
# score-bypass lane (research/evolve_s107). It's an UNPROVEN, low-liq cohort with high price-EV
# but real ghost risk → size it DOWN as the risk control (the liq belt + rug_screen + drain-guard
# + trail exit are the others). C²-only knob; EV-sizing (dormant) supersedes when armed → does
# NOT touch ev_sizing.json (THE ONE OPERATOR RULE intact). Keyed on the signal flag. Revert: 1.0.
_MOMENTUM_OVERRIDE_SIZE_MULT = 0.5

# TAXONOMY: dual-tag migration — every close row carries additive {play_v2, regime_v2}
# alongside the old tags (historical rows are NEVER rewritten; readers migrate in a named
# follow-up). regime_v2 uses the exact agg-at-open stamped here at buy time; positions that
# survive a restart fall back to the lossy name shim. Pure bookkeeping, no behavior.
_TAXO_OPEN_AGG: dict = {}

def _taxo_tags(pos, mint):
    try:
        return regime_policy.tags(
            pos.get("insane_tier"),
            momentum_override=bool(pos.get("momentum_override")),
            normal_slice=bool(pos.get("normal_slice")),
            dust_shadow=bool(pos.get("dust_shadow")),
            legacy_regime=pos.get("regime"),
            agg_at_open=_TAXO_OPEN_AGG.get(mint),
        )
    except Exception:
        return {}

# BLEEDTRIM: the fleet's realized loss is a STOP-LOSS TAIL on four normal-regime books whose
# bodies are actually +EV but whose ~10-20% big-loser tail (−7%…−14% stop/gap-downs) more than
# wipes the body out (gem/momentum/relay/bank × normal each net <0; e.g. bot1 gem-normal: body
# +0.037 over 102 closes, tail −0.145 over 22 → net −0.108◎). Since each pocket is net-NEGATIVE,
# halving size strictly reduces the ◎ bleed (smaller +EV body still ≥0, tail loser costs half).
# Scoped to `normal` ONLY so the +EV relay×aggressive book is untouched; the gem×sniper rug slice
# is already skipped (observer _GEM_SNIPER_SKIP). NOT the deep_pool cohort → the S110 freeze /
# kill_criterion sample is undisturbed. C²-only knob → does NOT touch ev_sizing.json (THE ONE
# OPERATOR RULE intact; EV-sizing supersedes if it ever arms). Revert: set _BLEED_TRIM_SIZE_MULT=1.0.
_BLEED_TRIM_POCKETS   = frozenset({"gem", "momentum", "relay", "bank"})
_BLEED_TRIM_SIZE_MULT = 0.5

# ── S99 (dust shadow executor): fire the deep_pool predicate in the SKIPPED regimes at
# DUST size, purely to manufacture real on-chain closes for the evidence base. The deploy
# gate is starved by FIRE-RATE (deep_pool skips dead/normal, market is `normal` 75-85%;
# euphoria ~6% — volume the market won't give). A dust entry (~◎0.01) is a GENUINE fill
# with genuine friction + genuine ghosts, so read as return-% (pnl_sol/size_sol, S80) it
# carries the true exitability signal at near-zero capital — sizing the unproven edge DOWN
# to learn it, never up (THE ONE OPERATOR RULE holds). Dust closes feed the SEPARATE
# size-normalized shadow gate (dust_gate.py), NEVER the live arming gate (prestige_tracker
# excludes dust_shadow). Dust positions are EXEMPT from the concurrent-position cap (they
# must not starve real entries) and bounded by their own _DUST_MAX_CONCURRENT. Admission +
# canary live in observer.py (_dust_shadow_on); this file only sizes + tags + caps.
# ⚠ HONEST PROXY CAVEAT: dust pays outsized fee/impact drag vs a scaled entry, so its
# return-% is slightly CONSERVATIVE — safe-direction (a real +EV edge reads marginally
# worse, never better), which is exactly why the shadow gate, not the live gate, judges it.
_DUST_SHADOW_SOL     = 0.01    # the dust entry size (~$1.50); clamped to MIN_POSITION_SOL floor below
_DUST_MAX_CONCURRENT = 8       # cap on simultaneously-open dust positions (bounds wallet/RPC load)

# ── S79 (option C): signal-quality size nudge — TIGHT band, capped small until EV proves ──
# The bot mostly trades fresh tokens that all default to confidence 0.6, so C² can't tell a
# strong signal from a marginal one. This lets the 0–100 validation SCORE nudge size within
# a narrow ±15% band: a stronger signal bets modestly more, a barely-passing one modestly
# less — WITHOUT sizing up −EV momentum (the band is tight, all caps + regime mult still
# apply, and the real "size by edge" lever stays the gated EV-sizing override). Neutral (1.0)
# when a signal carries no score (force_fire BC tokens). Widen the band ONLY once EV-sizing
# is live and the edge is proven. Linear: score ≤REF_LO → LO×, ≥REF_HI → HI×, ~75 → 1.0×.
_SCORE_NUDGE_LO     = 0.85
_SCORE_NUDGE_HI     = 1.15
_SCORE_NUDGE_REF_LO = 60.0
_SCORE_NUDGE_REF_HI = 90.0

def _sizeup_on():
    """True when THIS bot should evaluate brain-driven mid-trade size-ups."""
    global _sizeup_cache
    if _SIZEUP_ENABLED:
        return True
    if time.time() - _sizeup_cache[0] < 30.0:
        return _sizeup_cache[1]
    on = False
    try:
        on = bool(json.loads(_SIZEUP_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _sizeup_cache = (time.time(), on)
    return on

# ── S98: pre-entry left-tail RUG SCREEN (gem / price-action path) ──────────────────
# Attack the fat negative tail at the source — ~95% of fleet drawdown (S94b) is 8
# catastrophic rugs/ghosts (8AB1Jsbd unsellable ≈−0.22◎, the $6.75M→LP-pull −0.085◎,
# BCdwQBAn honeypot −0.11◎). S87's catastrophic-drain exit and S92's price-glitch guard
# are REACTIVE; this is the PROACTIVE complement: screen the gem/INSANE plays that BYPASS
# the liquidity floor (observer.py:~807) for mint/freeze authority, LP-lock, top-holder
# concentration, and pool age BEFORE capital commits (rug_screen.screen_token).
#
# Per-bot canary bots/botN/rug_screen.json, 30s hot-reload. SCOPED to non-deep_pool/
# brain_rule entries by the caller (the gate cohort is already sellable + throughput-
# starved → never screened, so this can't re-trigger the S87/S89/S91 lockout). FAIL-CLOSED
# on gems (operator): an unscreenable fresh gem is the highest-risk case → skip it. Default
# (no file) = OFF = current behavior. A/B: enable Bot1 first vs Bot2/3. Revert: rm the file.
_RUG_SCREEN_PATH  = DATA_DIR / "rug_screen.json"
_rug_screen_cache = (0.0, {})   # (read_ts, cfg) — 30s cache off the hot path
_RUG_SCREEN_DEFAULTS = {
    "enabled":            False,   # master per-bot switch (no file → off)
    "fail_closed":        True,    # unresolved authority on a gem → skip (operator choice)
    "top_holder_max_pct": 25.0,    # single largest non-LP holder above this → skip
    "lp_locked_min_pct":  50.0,    # LP locked/burned below this → skip
    "min_pool_age_s":     0,       # 0 = pool-age gate off (cold-start already blocks <120s)
    "screen_timeout_s":   1.2,     # whole-screen wall-clock cap (authority RPC ~<200ms)
}

def _rug_screen_cfg() -> dict:
    """Read bots/botN/rug_screen.json once per 30s → merged cfg dict. Never raises."""
    global _rug_screen_cache
    if time.time() - _rug_screen_cache[0] < 30.0:
        return _rug_screen_cache[1]
    cfg = dict(_RUG_SCREEN_DEFAULTS)
    try:
        cfg.update(json.loads(_RUG_SCREEN_PATH.read_text()))
    except Exception:
        pass
    _rug_screen_cache = (time.time(), cfg)
    return cfg

# ── WASHVETO — admission FAKE-BUY veto (cure-hunt 2026-06-11; wash_veto.check). The count-based
# sidecar can be told "strong buy" (bs>=1.5) while real on-chain $ flow is leaving; those drew down
# deeper on 2.5k harvested obs (fwd_min -6.7% vs -4.7%) and mapped to ~40% of live loser mints
# (J4x1 -0.23, Cm6fNn -0.18…). One READ-ONLY GeckoTerminal tape GET, FAIL-OPEN, throttled. SCOPED to
# gem/price-action by the caller (deep_pool/brain_rule = the S110-frozen gate cohort, never vetoed).
# Per-bot canary bots/botN/wash_veto.json, 30s hot-reload. Default (no file)=OFF. Revert: rm the file.
_WASH_VETO_PATH  = DATA_DIR / "wash_veto.json"
_wash_veto_cache = (0.0, {})
_WASH_VETO_DEFAULTS = {
    "enabled":      False,   # master per-bot switch (no file → off)
    "min_count_bs": 1.5,     # only contradict a candidate the COUNT already calls a buy
    "min_trades":   12,      # need a tape this thick to trust the flow read (else allow)
    "timeout_s":    2.5,     # tape-fetch wall-clock cap (fail-OPEN on timeout)
}

def _wash_veto_cfg() -> dict:
    """Read bots/botN/wash_veto.json once per 30s → merged cfg dict. Never raises."""
    global _wash_veto_cache
    if time.time() - _wash_veto_cache[0] < 30.0:
        return _wash_veto_cache[1]
    cfg = dict(_WASH_VETO_DEFAULTS)
    try:
        cfg.update(json.loads(_WASH_VETO_PATH.read_text()))
    except Exception:
        pass
    _wash_veto_cache = (time.time(), cfg)
    return cfg

# ── PIPE12 (R-D2): proceeds-based PnL — record pnl_sol from the sell swap's REALIZED outAmount
# (SOL returned = ground truth) instead of price% (size_usd*(1+pnl_pct/100)). Price-derived pnl is
# the S92/S100 phantom root: a glitched feed fabricates a +495805% "win". Proceeds-based pnl makes
# that architecturally impossible and reconciles the ledger to the wallet by construction. The
# realized_sol_out is ALWAYS captured (informational, zero behaviour change); only when the canary
# is ON does the RECORDED pnl_sol switch to proceeds-based. Default (no file) = OFF = current pnl.
# Validate-first: research/pipe12/proceeds_audit.py. Enable per-bot: bots/botN/proceeds_pnl.json
# {"enabled": true} (30s hot-reload, reversible: rm it). pnl_pct still drives the EXIT DECISION.
_PROCEEDS_PNL_PATH  = DATA_DIR / "proceeds_pnl.json"
_proceeds_pnl_cache = (0.0, False)

def _proceeds_pnl_on() -> bool:
    """True when THIS bot should book pnl_sol from realized swap proceeds (PIPE12 R-D2 canary)."""
    global _proceeds_pnl_cache
    if time.time() - _proceeds_pnl_cache[0] < 30.0:
        return _proceeds_pnl_cache[1]
    try:
        on = bool(json.loads(_PROCEEDS_PNL_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _proceeds_pnl_cache = (time.time(), on)
    return on

# ── S105: DEAD-POOL RE-ENTRY QUARANTINE canary ─────────────────────────────────
# Stops the friction-churn leak: when a position exits "Dead pool — …exit before it ghosts"
# (the 83–88% flat-exit cohort), quarantine the mint from re-entry (memory.quarantine_dead_pool,
# honoured by the existing is_banned checks at every admission point), escalating on repeats.
# Pure capital-protection — can only SKIP a re-entry, never sizes/sends. Per-bot:
# bots/botN/dead_pool_quarantine.json {"enabled": true} (30s hot-reload, reversible: rm it).
_DEAD_POOL_QUARANTINE_PATH  = DATA_DIR / "dead_pool_quarantine.json"
_dead_pool_quarantine_cache = (0.0, False)

def _dead_pool_quarantine_on() -> bool:
    """True when THIS bot should quarantine a mint after a dead-pool exit (S105 canary)."""
    global _dead_pool_quarantine_cache
    if time.time() - _dead_pool_quarantine_cache[0] < 30.0:
        return _dead_pool_quarantine_cache[1]
    try:
        on = bool(json.loads(_DEAD_POOL_QUARANTINE_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _dead_pool_quarantine_cache = (time.time(), on)
    return on

# ── Session loss sniper gate ───────────────────────────────────────────────────
# When cumulative session PnL drops below -0.05 SOL, new entries must have
# confidence ≥ 0.85. The gate resets at bot restart (session-scoped).
_session_pnl_sol: float       = 0.0    # running session PnL in SOL
_sniper_gate_active: bool     = False  # True once session loss exceeds threshold
_SESSION_LOSS_GATE_SOL        = -0.05  # SOL loss that triggers the gate
_SESSION_SNIPER_CONF_MIN      = 0.85   # minimum confidence while gate is active

# ── Per-cycle globals for cost_to_prestige (updated once per cycle) ───────────
_cur_sol_balance:   float = 0.0
_cur_sol_in_trades: float = 0.0
_cur_milestone_sol: float = 2.0

# ── Market State: volatility regime + adaptive momentum floor ────────────────
_last_buy_time: Optional[datetime] = None  # UTC time of last confirmed buy
_market_state_active: bool = False          # True while vol-gated floor relaxation is live
_volatility_regime: str = "normal"          # S79 ladder: euphoria|aggressive|normal|sniper|dead

# ── Profit Lock — home-stretch capital protection ─────────────────────────────
_profit_lock_active: bool = False           # True when portfolio ≥ 75% of payout milestone

# ── Entry queue — separates discovery from execution ──────────────────────────
# Discovery sizes signals and pushes here; the execution consumer drains one at a
# time with a 5-second cooldown between trades to avoid Jupiter/Jito rate limits.
_entry_queue = EntryQueue()

# ── Three-tier architecture ────────────────────────────────────────────────────
# Tier 1 — Observer:    pre-qualifies tokens (discovery + market data + scoring)
# Tier 2 — Executioner: fast path (stoic.evaluate + sizing + execute_buy)
# Tier 3 — Auditor:     background DB writes + goldilocks (never blocks main loop)
_observer = Observer()
_auditor  = Auditor()


async def _run_goldilocks_and_reload() -> None:
    """Run goldilocks --emit-override in a background subprocess, then reload overrides.
    Non-blocking — the trading loop continues while goldilocks reads the DBs.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "goldilocks.py", "--emit-override",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            import config as _cfg
            fresh = _cfg.load_overrides()
            strategy_apply_overrides(fresh)
            print(f"         [Goldilocks] Override reloaded ✓", flush=True)
        else:
            print(f"         [Goldilocks] Run failed: {stderr.decode()[:120]}", flush=True)
    except asyncio.TimeoutError:
        print(f"         [Goldilocks] Timed out — will retry after next 20 closes", flush=True)
    except Exception as e:
        print(f"         [Goldilocks] Error: {e}", flush=True)


def write_status(
    pubkey: str,
    risk: RiskManager,
    market_data: dict,
    signals: list,
    sol_balance: float = 0.0,
    usdc_balance: float = 0.0,
    payout_milestones: Optional[list] = None,
    sol_price_usd: float = 150.0,
    stoic_positions: Optional[dict] = None,
    sol_in_trades: float = 0.0,
    available_sol: float = 0.0,
    thinking: Optional[dict] = None,
    prestige_pending: bool = False,
) -> None:
    # Enrich open_positions with entry price, open time, and position state
    enriched_positions = {}
    for mint, size_usd in risk.open_positions.items():
        sp = (stoic_positions or {}).get(mint, {})
        enriched_positions[mint] = {
            "size_usd":    size_usd,
            "size_sol":    sp.get("size_sol", round(size_usd / max(sol_price_usd, 1), 6)),
            "entry_price": sp.get("entry_price", 0),
            "opened_at":   sp.get("opened_at", ""),
            "trail_active": sp.get("trail_active", False),
            "stack_count":  sp.get("stack_count", 0),
            "peak_price":   sp.get("peak_price", 0),
            # Per-play params — set at open time, used by Think Log for per-position display
            "insane_tier":     sp.get("insane_tier"),
            "mode":            sp.get("mode"),       # S64: which personality opened this
            "regime":          sp.get("regime"),     # S64: market regime at entry
            "ride":            sp.get("ride", True), # S64: ride (let run) vs bank (exit at TP)
            "take_profit_pct": sp.get("take_profit_pct"),
            "stop_loss_pct":   sp.get("stop_loss_pct"),
            "max_hold_hours":  sp.get("max_hold_hours"),
            # TP1 partial-exit state — used by Think Log to show partial-close badge
            "tp1_taken":          sp.get("tp1_taken", False),
            "remaining_fraction": sp.get("remaining_fraction", 1.0),
            "sl_floor_pct":       sp.get("sl_floor_pct"),
            "dyn_tp1_fired":      sp.get("dyn_tp1_fired", False),
            "tp1_pct_fired":      sp.get("tp1_pct_fired"),
            # S72: size-aware breakeven ratchet + brain-driven size-up state (dashboard badges)
            "be_ratchet_armed":   sp.get("be_ratchet_armed", False),
            "wallet_frac":        sp.get("wallet_frac"),
            "size_cap_pct":       sp.get("size_cap_pct"),
            "sizeup_count":       sp.get("sizeup_count", 0),
        }

    mem = memory.summary()

    # Per-bot win/loss from this bot's own trades.db (not shared fleet memory)
    bot_wins = bot_losses = 0
    _db_path = STATUS_PATH.parent / "trades.db"
    if _db_path.exists():
        try:
            with sqlite3.connect(str(_db_path)) as _con:
                for (_d,) in _con.execute("SELECT data FROM trades WHERE event='close'"):
                    _r   = json.loads(_d)
                    _pnl = _r.get("pnl", 0) or 0.0
                    # S88-debug + S89: break-even is NEUTRAL, never a loss (the S87/S89 durable lesson —
                    # flat exits crashed WR/confidence into an 8h deadlock when counted as losses).
                    # S89 fix: judge "flat" by % MOVE (pnl_sol/size_sol), not $ — a flat exit is flat by
                    # PERCENT, not dollars (the $0.01 USD band was too tight for the dead-pool cohort).
                    # Mirrors memory._BREAKEVEN_PCT; USD fallback for legacy rows lacking size_sol.
                    _ps, _sz = _r.get("pnl_sol"), _r.get("size_sol")
                    if _ps is not None and _sz:
                        _flat = abs(_ps / _sz * 100.0) < memory._BREAKEVEN_PCT
                    else:
                        _flat = abs(_pnl) <= 0.01
                    if _flat:
                        pass                       # break-even — neutral
                    elif _pnl > 0:
                        bot_wins += 1
                    else:
                        bot_losses += 1
        except Exception:
            pass
    closed_trades = bot_wins + bot_losses

    STATUS_PATH.write_text(json.dumps({
        "wallet":        pubkey,
        "sol_balance":   round(sol_balance, 6),
        "usdc_balance":  round(usdc_balance, 2),
        "daily_pnl":     round(risk.daily_pnl, 4),
        "halted":        risk.is_halted(),
        "paused":          PAUSE_FLAG.exists(),
        "prestige_pending": prestige_pending,
        "open_positions": enriched_positions,
        "closed_trades": closed_trades,
        "bot_wins":      bot_wins,
        "bot_losses":    bot_losses,
        # Capital breakdown — what the dashboard needs
        "sol_in_trades": round(sol_in_trades, 6),
        "sol_reserved":  RESERVE_SOL,
        "available_sol": round(available_sol, 6),
        "market_data": {
            k: {
                "price_usd": d["price_usd"],
                "price_change_5m": d["price_change_5m"],
                "price_change_1h": d["price_change_1h"],
                "liquidity_usd": d["liquidity_usd"],
                "volume_5m": d["volume_5m"],
            }
            for k, d in market_data.items()
        },
        "last_signals": signals[:3],
        "last_update": datetime.utcnow().isoformat(),
        "mode":         stoic_strategy_module_mode(),
        "marketcap":    get_marketcap(),
        "size_tier":    get_size_tier(),
        "viral_weight": get_viral_weight(),
        "sol_price_usd": round(sol_price_usd, 2),
        "token_memory": mem,
        "payout_milestones": payout_milestones or [],
        "thinking": thinking or {},
    }))


MAX_HISTORY_LINES = 2880  # 24h at ~30s/cycle

def record_history(risk: RiskManager, cycle: int, sol_balance: float = 0.0, usdc_balance: float = 0.0) -> None:
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "cycle": cycle,
        "daily_pnl": round(risk.daily_pnl, 4),
        "sol_balance": round(sol_balance, 6),
        "usdc_balance": round(usdc_balance, 2),
        "positions": len(risk.open_positions),
    }
    new_line = json.dumps(entry) + "\n"
    if HISTORY_PATH.exists():
        lines = HISTORY_PATH.read_text().splitlines(keepends=True)
        if len(lines) >= MAX_HISTORY_LINES:
            lines = lines[-(MAX_HISTORY_LINES - 1):]
        lines.append(new_line)
        HISTORY_PATH.write_text("".join(lines))
    else:
        HISTORY_PATH.write_text(new_line)


def _apply_route_meta(stoic, mint: str, signal: dict) -> None:
    """S99: copy the entry-time route-depth metrics (captured by the honeypot reverse-quote)
    onto the freshly-opened position so they ride through to the close audit row. Lets
    dust_gate.py split the deep_pool cohort into would-have-been-sellable vs ghost. Best-effort,
    no-op if the position or the fields are absent — never affects execution."""
    pos = stoic.positions.get(mint)
    if pos is None:
        return
    for _rk in ("route_roundtrip_pct", "route_impact_pct", "route_n", "route_out_sol"):
        if signal.get(_rk) is not None:
            pos[_rk] = signal[_rk]


async def execute_buy(
    client: httpx.AsyncClient,
    keypair,
    signal: dict,
    risk: RiskManager,
    stoic: StoicStrategy,
    cycle: int,
    fail_counts: dict = None,
) -> None:
    mint     = signal["mint"]
    price    = signal["price"]
    size_sol = signal["size_sol"]
    size_usd = signal["size_usd"]

    # Defense-in-depth: block session-banned tokens even if the outer eval loop missed it.
    if fail_counts is not None and fail_counts.get(mint, 0) >= 2:
        return

    # Wire-to-live rule filter. Mirrors the ban-gate's defense-in-depth pattern.
    # active() returns None (→ no effect) unless a brain rule has truly cleared the
    # readiness bar (n≥100, EV≥+2%, WR≥55%, EV-lo>0, stable halves, ≥24h span).
    #   • flag ON  → entries failing the rule are SKIPPED (live filter).
    #   • flag OFF → SHADOW mode: log what WOULD be skipped but do NOT block, so the
    #                operator can observe real skip-rate + confirm the predicate fires
    #                on live sigs before going live. Silent until a rule is live-ready.
    if _live_rule_mod is not None:
        _lr = _live_rule_mod.active()
        if _lr is not None and not _lr.passes(signal):
            if _LIVE_RULE_ENABLED:
                _auditor.push("risk_reject", {"mint": mint, "reason": f"live-rule '{_lr.name}' not satisfied"})
                print(f"[LIVE-RULE] {mint[:8]}... skip — '{_lr.name}' not satisfied", flush=True)
                return
            print(f"[LIVE-RULE] {mint[:8]}... would-skip — '{_lr.name}' not satisfied (SHADOW, flag OFF)", flush=True)

    # Native strict-score gate (A/B, per-bot, default OFF — see _strict_gate_floor).
    # Skips any entry below the high-score floor, across ALL paths. Can only reduce entries.
    # S98-reconcile: the tight `normal` deep_pool slice is admitted on its filling/buy STRUCTURE,
    # not its score (gate_unfreeze.py: score≥65 is the -EV subset in normal; the +EV edge is
    # filling+buy≥1.5, score-agnostic). Exempt ONLY that slice from the score floor; the ×0.5
    # size-down (sizing block) is its risk control while it proves. Regular deep_pool stays gated.
    # S107: the momentum-override runner lane is admitted on its momentum SIGNATURE (m5≥10/bs≥1.2/
    # v5≥2000), not its score (the scorer gates it to ~0 because liq<$30k) — exempt it from the
    # score floor exactly like the normal_slice; the ×0.5 size + rug_screen + drain-guard + trail
    # exit are its risk controls. Regular entries stay gated.
    _sg = _strict_gate_floor()
    if (_sg is not None and not signal.get("normal_slice") and not signal.get("momentum_override")
            and (signal.get("validation_score", 0.0) or 0.0) < _sg):
        _auditor.push("risk_reject", {"mint": mint, "reason": f"strict-gate score {signal.get('validation_score', 0.0):.0f} < {_sg:.0f}"})
        print(f"[STRICT-GATE] {mint[:8]}... skip — score {signal.get('validation_score', 0.0):.0f} < {_sg:.0f}", flush=True)
        return

    # Cold-start lock: tokens < 120s old require b/s > 2.0.
    # Very fresh pairs surface wash-trade liquidity before real demand forms — the initial
    # buy pressure that gets them into the watchlist is often the whole move.
    # Static watchlist tokens default to pair_age 999h so this never fires for them.
    _pair_age_h = signal.get("pair_age_hours", 999.0)
    if _pair_age_h < (120 / 3600) and signal.get("buy_sell_ratio", 0.0) <= 2.0:
        _age_s = int(_pair_age_h * 3600)
        _auditor.push("risk_reject", {"mint": mint, "reason": f"cold-start: pair {_age_s}s old b/s={signal.get('buy_sell_ratio', 0):.2f}"})
        print(f"[ColdStart] {mint[:8]}... blocked — pair {_age_s}s old, b/s={signal.get('buy_sell_ratio', 0):.2f} ≤ 2.0", flush=True)
        return

    is_static = mint in set(WATCHLIST)
    sol_lamports = int(size_sol * 1e9)
    _mode = stoic_strategy_module_mode()
    # S74-bot3: deep_pool/brain_rule edge entries use the aggressive slippage even in stoic mode —
    # Bot3 trades the same volatile race-edge tokens as insane/wild, so 1% would just revert (Custom:6001).
    _edge_entry = signal.get("insane_tier") in ("deep_pool", "brain_rule")
    slippage = STOIC_SLIPPAGE_BPS if (_mode == "stoic" and not _edge_entry) else MAX_SLIPPAGE_BPS
    _jito_only = _mode in ("insane", "wild")  # Helius fallback skipped for high-velocity buys

    if is_static:
        # Static watchlist tokens are trusted — skip RugCheck (already bypassed in safety.py).
        # Dual-sample quote measures price impact at the same time as the safety short-circuit.
        impact_pct, quote = await jupiter.measure_price_impact(client, BASE_MINT, mint, sol_lamports, slippage)
        safe, safety_reason = True, "trusted watchlist token"
    else:
        # Discovered tokens: safety check and dual-sample quote run concurrently — independent.
        # RugCheck is capped at 450ms; a hanging API never blocks an available Jupiter quote.
        # Jupiter runs without a hard outer cap — it already handles its own errors.
        async def _safe_check() -> tuple[bool, str]:
            try:
                return await asyncio.wait_for(
                    safety.is_safe_token(client, mint), timeout=0.45
                )
            except asyncio.TimeoutError:
                return True, "rugcheck >450ms — allowing"

        # ── S98 rug screen — gem / price-action only (deep_pool/brain_rule = the gate cohort,
        # already sellable + throughput-starved → never screened). Runs CONCURRENTLY with the
        # existing safety + quote gather so it adds no serial latency on the hot path.
        _rug_cfg = _rug_screen_cfg()
        _screen_on = bool(_rug_cfg.get("enabled")) and not _edge_entry

        async def _rug_check() -> tuple[bool, str, bool, dict]:
            if not _screen_on:
                return True, "screen off", False, {}
            try:
                res = await asyncio.wait_for(
                    rug_screen.screen_token(
                        client, mint,
                        fail_closed=bool(_rug_cfg.get("fail_closed", True)),
                        top_holder_max_pct=float(_rug_cfg.get("top_holder_max_pct", 25.0)),
                        lp_locked_min_pct=float(_rug_cfg.get("lp_locked_min_pct", 50.0)),
                        min_pool_age_s=float(_rug_cfg.get("min_pool_age_s", 0) or 0),
                        pair_age_hours=signal.get("pair_age_hours", 999.0),
                    ),
                    timeout=float(_rug_cfg.get("screen_timeout_s", 1.2)),
                )
                return res.ok, res.reason, res.hard, res.detail
            except asyncio.TimeoutError:
                # A screen we couldn't complete on a gem = the unscreenable case. Fail-closed
                # skips it but does NOT session-ban (transient — re-eval when RPC recovers).
                if bool(_rug_cfg.get("fail_closed", True)):
                    return False, "rug-screen timeout (fail-closed)", False, {"timeout": True}
                return True, "rug-screen timeout — allowing", False, {"timeout": True}

        # ── WASHVETO — FAKE-BUY veto, gem/price-action only, concurrent + FAIL-OPEN (can only
        # ever SKIP a confident fake buy; any error/timeout ⇒ allow ⇒ cannot lock the bot out).
        _wash_cfg = _wash_veto_cfg()
        _wash_on  = bool(_wash_cfg.get("enabled")) and not _edge_entry

        async def _wash_check() -> tuple[bool, str, dict]:
            if not _wash_on:
                return True, "veto off", {}
            try:
                res = await wash_veto.check(
                    client,
                    signal.get("pair_address"),
                    float(signal.get("buy_sell_ratio", signal.get("bs", 0)) or 0),
                    min_count_bs=float(_wash_cfg.get("min_count_bs", 1.5)),
                    min_trades=int(_wash_cfg.get("min_trades", 12)),
                    timeout=float(_wash_cfg.get("timeout_s", 1.2)),
                )
                return res.ok, res.reason, res.detail
            except Exception as _e:   # fail-OPEN on anything
                return True, f"wash-veto error — allow ({_e})", {}

        (safe, safety_reason), (impact_pct, quote), (rug_ok, rug_reason, rug_hard, rug_detail), (wash_ok, wash_reason, wash_detail) = \
            await asyncio.gather(
                _safe_check(),
                jupiter.measure_price_impact(client, BASE_MINT, mint, sol_lamports, slippage),
                _rug_check(),
                _wash_check(),
            )

    if not safe:
        _auditor.push("risk_reject", {"mint": mint, "reason": f"safety: {safety_reason}"})
        print(f"[Safety] Rejected {mint[:8]}... — {safety_reason}", flush=True)
        # Session-ban: don't re-evaluate this token for the rest of the session
        if fail_counts is not None:
            fail_counts[mint] = 2
        return

    # ── S98 rug-screen verdict (gem path only; rug_ok defaults True when screen off/static) ──
    if not is_static:
        if rug_detail:
            # Audit even on PASS so the LP/holder distributions can be tuned later from live data.
            _auditor.push("rug_screen", {"event": "rug_screen", "mint": mint, "ok": rug_ok, "reason": rug_reason, **rug_detail})
        if not rug_ok:
            _auditor.push("risk_reject", {"mint": mint, "reason": f"rug-screen: {rug_reason}"})
            print(f"[RugScreen] {_B} BLOCKED {mint[:8]}... — {rug_reason}", flush=True)
            # Session-ban only non-healing rug signatures (authority enabled / rugged / LP unlocked
            # / over-concentrated). A fail-closed timeout or too-new pool may heal → just skip.
            if rug_hard and fail_counts is not None:
                fail_counts[mint] = 2
            return

    # ── WASHVETO verdict (gem path only; wash_ok defaults True when off/static/fail-open) ──
    if not is_static and not wash_ok:
        _auditor.push("wash_veto", {"event": "wash_veto", "mint": mint, "ok": False, "reason": wash_reason, **wash_detail})
        _auditor.push("risk_reject", {"mint": mint, "reason": f"wash-veto: {wash_reason}"})
        print(f"[WashVeto] {_B} SKIP {mint[:8]}... — {wash_reason}", flush=True)
        # NO session-ban: on-chain flow is transient — re-eval next cycle (a real buy may follow).
        return
    if not is_static and wash_detail:
        # Audit PASS too (when the tape was actually read) so the flow distribution is tunable.
        _auditor.push("wash_veto", {"event": "wash_veto", "mint": mint, "ok": True, "reason": wash_reason, **wash_detail})
    if not quote:
        _auditor.push("error", {"context": mint, "error": "Failed to get quote"})
        return

    # ── S74-honeypot: SELLABILITY pre-check ───────────────────────────────────────
    # BCdwQBAn (S74) lost ~10% of Bot2's wallet: the buy landed, but the token had NO sell
    # route — 240 tokens worth $0, unsellable (a honeypot). RugCheck didn't catch it. The
    # definitive functional test is "can we sell what we'd receive?": reverse-quote the buy's
    # outAmount tokens back to SOL. No route (or round-trip < 50%) ⇒ honeypot/illiquid trap ⇒
    # skip + session-ban BEFORE committing capital. Just a quote — nothing signed/sent, zero
    # double-submit risk. Sits ahead of the TWAP branch so atomic + tranche buys are both covered.
    # Static/watchlist tokens are trusted (skip). Revert: delete this S74-honeypot block.
    if not is_static:
        _tok_out  = int(quote.get("outAmount", 0))
        _sell_q   = await jupiter.get_quote(client, mint, BASE_MINT, _tok_out, slippage) if _tok_out > 0 else None
        _sell_out = int(_sell_q.get("outAmount", 0)) if _sell_q else 0
        _roundtrip = (_sell_out / sol_lamports) if sol_lamports > 0 else 0.0
        # S99 (route-depth feature): the honeypot check ALREADY quotes the reverse leg at our
        # intended size — capture its fill-based exitability metrics onto the signal (ZERO extra
        # Jupiter calls; respects the S91 rate-limit discipline). forward_obs is PRICE-based, so
        # the ghost (un-sellability) tail is only discovered at EXIT; this logs the FILL quality
        # at entry → dust_gate.py can split the cohort into would-have-been-sellable vs ghost and
        # mature n on a clean sample. route_roundtrip_pct = immediate buy→sell friction% (0 = perfect,
        # negative = exit costs you — the ghost predictor); route_impact_pct = sell-side price impact;
        # route_n = viable route legs. Carried into the position after open → onto the close row.
        signal["route_roundtrip_pct"] = round((_roundtrip - 1.0) * 100, 3)
        signal["route_impact_pct"]    = round(float((_sell_q or {}).get("priceImpactPct", 0.0) or 0.0) * 100, 4)
        signal["route_n"]             = len((_sell_q or {}).get("routePlan", []) or [])
        signal["route_out_sol"]       = round(_sell_out / 1e9, 6)
        # No SELL route while the BUY route just succeeded = the honeypot signature (buy-only token).
        _no_route = (_tok_out <= 0 or not _sell_q or _sell_out <= 0)
        if _no_route or _roundtrip < 0.50:
            _hp_reason = ("no sell route (honeypot)" if _no_route
                          else f"sell round-trip {_roundtrip*100:.0f}% < 50% (illiquid/honeypot trap)")
            _auditor.push("risk_reject", {"mint": mint, "reason": f"sellability: {_hp_reason}"})
            print(f"[Honeypot] {mint[:8]}... BLOCKED — {_hp_reason}", flush=True)
            # Session-ban only the DEFINITIVE no-route case (a honeypot won't heal); a low round-trip
            # can be transient volatility/thin liquidity, so just skip this cycle without banning.
            if _no_route and fail_counts is not None:
                fail_counts[mint] = 2
            return

    slippage_bps = quote.get("slippageBps", 0)
    ok, reason = risk.check_trade(mint, size_usd, signal["liquidity_usd"], slippage_bps,
                                  allow_stack=signal.get("is_stack", False))
    if not ok:
        _auditor.push("risk_reject", {"mint": mint, "reason": reason})
        return

    # ── Market Impact Predictor ────────────────────────────────────────────────
    # Dual-sample quote delta tells us how much our order size moves the pool price.
    # Above TWAP_IMPACT_THRESHOLD%, abort the atomic swap and split into tranches
    # so each leg's impact is ~1/N of the total — prevents whale-self-slaughter on
    # low-liquidity gems where a single large entry destroys its own entry price.
    should_twap, impact_reason = risk.evaluate_market_impact(
        impact_pct, size_sol, TWAP_IMPACT_THRESHOLD, TWAP_TRANCHES
    )
    if should_twap:
        _mip_tier = f"[{signal.get('insane_tier','').upper()}] " if signal.get("insane_tier") else ""
        print(f"[MIP] {_mip_tier}{mint[:8]}... {impact_reason}", flush=True)
        await execute_twap_buy(client, keypair, signal, risk, stoic, cycle, fail_counts=fail_counts)
        return

    # ── Pre-flight rate check ──────────────────────────────────────────────────
    # Time has elapsed since the original quote (RugCheck + risk checks ≈ up to 500ms).
    # On high-velocity tokens the pool can move enough to make the quoted rate stale.
    # Take a fresh single quote and compare output-per-lamport. If the rate has degraded
    # by more than 0.5%, abort — the signal no longer matches the entry price we evaluated.
    # On success, use the fresh quote for the swap so execution gets the current fill.
    _orig_rate = int(quote["outAmount"]) / int(quote["inAmount"]) if quote.get("outAmount") and quote.get("inAmount") else None
    if _orig_rate:
        _fresh_quote = await jupiter.get_quote(client, BASE_MINT, mint, sol_lamports, slippage)
        if _fresh_quote and _fresh_quote.get("outAmount") and _fresh_quote.get("inAmount"):
            _fresh_rate = int(_fresh_quote["outAmount"]) / int(_fresh_quote["inAmount"])
            _rate_drift_pct = (_orig_rate - _fresh_rate) / _orig_rate * 100
            if _rate_drift_pct > 0.5:
                _auditor.push("risk_reject", {"mint": mint, "reason": f"pre-flight: rate drifted {_rate_drift_pct:.2f}%"})
                print(f"[PreFlight] {mint[:8]}... aborted — rate drifted {_rate_drift_pct:.2f}% since quote", flush=True)
                return
            quote = _fresh_quote  # always execute against the freshest available price

    _qout = int(quote.get("outAmount", 0)) if quote else 0
    _qin  = int(quote.get("inAmount",  0)) if quote else 0

    t_broadcast = time.time()
    sig = None
    signed = None   # set by the lite-api path; left None on the Ultra path (no rebroadcast needed)

    # execwire (PIPE12 R-C1): when the Ultra canary is on for THIS bot, route the buy through
    # Jupiter Ultra (Beam landing + RTSE slippage, no Jito bundle dependency). Default OFF →
    # ultra_exec_on() is False with no canary file, the whole block is skipped, and the buy is the
    # unchanged lite-api path below. On ANY Ultra order/execute failure sig stays None and we fall
    # through to lite-api → an Ultra hiccup NEVER aborts an entry. Pure routing change: the quote,
    # sizing, pre-flight rate check, route-meta and all downstream accounting are untouched.
    if jupiter_ultra.ultra_exec_on():
        sig, _umeta = await jupiter_ultra.ultra_buy(client, keypair, BASE_MINT, mint, sol_lamports)
        if sig:
            print(f"[Ultra] {mint[:8]}... landed via Beam | slip_bps={_umeta.get('slippage_bps')} "
                  f"slot={_umeta.get('slot')} sig: {sig[:12]}...", flush=True)
        else:
            print(f"[Ultra] {mint[:8]}... order/execute failed — falling back to lite-api", flush=True)

    if sig is None:
        # ── lite-api path (the default, and the fallback when Ultra is off or failed) ──
        swap_tx = await jupiter.build_swap_transaction(client, quote, str(keypair.pubkey()))
        if not swap_tx:
            # S75: a build failure means NO tx was signed or sent → zero double-submit risk.
            # Previously this path returned WITHOUT counting the failure, so a dead-route token
            # (e.g. 3oL99t, no Jupiter route) burned a fresh qualified signal EVERY cycle forever.
            # Mirror the send-failure ban (below) so it gets session-banned after 2 attempts and
            # the leaked-signal cycles stop. The captured reason distinguishes no-route from transient.
            _berr = getattr(jupiter, "last_build_error", None)
            _auditor.push("error", {"context": mint, "error": "Failed to build swap tx", "detail": _berr})
            _tx_fail_last_cycle[mint] = cycle
            _tx_fail_count[mint] = _tx_fail_count.get(mint, 0) + 1
            if _tx_fail_count[mint] >= 2:
                print(f"[BUY]  {mint[:8]}... session-banned after {_tx_fail_count[mint]} build failures", flush=True)
            return

        signed = wallet.sign_transaction(keypair, swap_tx)
        # Static watchlist tokens skip simulate (~200ms Helius round-trip saved).
        # Discovered tokens always simulate — they're unvetted and worth the safety check.
        sig = await wallet.send_transaction(client, signed, keypair=keypair, skip_simulate=is_static, jito_only=_jito_only)
        if not sig:
            _auditor.push("error", {"context": mint, "error": "Failed to send transaction"})
            _tx_fail_last_cycle[mint] = cycle
            _tx_fail_count[mint] = _tx_fail_count.get(mint, 0) + 1
            if _tx_fail_count[mint] >= 2:
                print(f"[BUY]  {mint[:8]}... session-banned after {_tx_fail_count[mint]} send failures", flush=True)
            return

    confirmed, fail_reason = await wallet.confirm_transaction(client, sig, signed_tx=signed)
    latency_ms = int((time.time() - t_broadcast) * 1000)
    _auditor.push("tx_result", {"mint": mint, "sig": sig, "confirmed": confirmed, "latency_ms": latency_ms})
    if confirmed:
        _tx_fail_last_cycle.pop(mint, None)
        _tx_fail_count.pop(mint, None)
        global _last_buy_time
        _last_buy_time = datetime.utcnow()
        if signal.get("is_stack"):
            stoic.add_to_position(mint, price, size_sol, size_usd)
            risk.stack_position(mint, size_usd)
            _auditor.push("trade_open", {"mint": mint, "size_usd": size_usd, "price": price, "signature": sig, "size_sol": size_sol, "tier": signal.get("insane_tier"), "quote_out": _qout, "quote_in": _qin})
            pos = stoic.positions[mint]
            print(
                f"[STACK #{pos.get('stack_count',1)}] {mint[:8]}... +◎{size_sol:.4f} SOL (${size_usd:.2f}) @ ${price:.6f} | "
                f"avg_entry=${pos['entry_price']:.6f} | total=◎{pos['size_sol']:.4f} | "
                f"conf={signal['confidence']:.2f} | impact={impact_pct:.2f}% | latency={latency_ms}ms",
                flush=True,
            )
        else:
            risk.open_position(mint, size_usd)
            stoic.open_position(
                mint, price, size_sol, size_usd,
                signal["momentum_5m"], signal.get("volume_5m", 0.0),
                take_profit_pct=signal.get("take_profit_pct"),
                stop_loss_pct=signal.get("stop_loss_pct"),
                max_hold_hours=signal.get("max_hold_hours"),
                tier_label=signal.get("insane_tier"),
                mode=_mode, regime=_volatility_regime,
                wallet_frac=signal.get("wallet_frac"),
                size_cap_pct=signal.get("size_cap_pct"),
                confidence=signal.get("confidence"),
                normal_slice=signal.get("normal_slice", False),
                momentum_override=signal.get("momentum_override", False),  # S107: tag → ×0.5 size, trail exit, close-row flag
                dust_shadow=signal.get("dust_shadow", False),   # S99: tag the position → close row carries it (shadow gate reads, live gate excludes)
            )
            _apply_route_meta(stoic, mint, signal)   # S99: entry route-depth → position → close row
            _TAXO_OPEN_AGG[mint] = regime_policy.current_agg()   # TAXONOMY: exact regime_v2 tag at close
            _auditor.push("trade_open", {"mint": mint, "size_usd": size_usd, "price": price, "signature": sig, "size_sol": size_sol, "tier": signal.get("insane_tier"), "mode": _mode, "play": signal.get("insane_tier"), "regime": _volatility_regime, "quote_out": _qout, "quote_in": _qin, "route_roundtrip_pct": signal.get("route_roundtrip_pct"), "route_n": signal.get("route_n")})
            _tier_tag = f"[{signal['insane_tier'].upper()}] " if signal.get("insane_tier") else ""
            _tp_sl = (
                f"TP={signal['take_profit_pct']}% SL={signal['stop_loss_pct']}% hold={signal['max_hold_hours']}h | "
                if signal.get("take_profit_pct") else ""
            )
            print(
                f"[BUY] {_tier_tag}{mint[:8]}... ◎{size_sol:.4f} SOL (${size_usd:.2f}) @ ${price:.6f} | "
                f"conf={signal['confidence']:.2f} | {_tp_sl}impact={impact_pct:.2f}% | latency={latency_ms}ms | sig: {sig[:12]}...",
                flush=True,
            )
        # S105: capture the REAL on-chain buy cost (wallet SOL delta on the confirmed buy tx,
        # = SOL spent incl fee+tip+slippage) so the round-trip proceeds-pnl at close uses a true
        # cost basis (not the intended size_sol). Gated by the proceeds canary (only bots booking
        # proceeds need it → no extra RPC fleet-wide); accumulates across stacks; best-effort.
        if _proceeds_pnl_on() and mint in stoic.positions:
            try:
                _bd = await wallet.get_tx_sol_delta(client, sig, str(keypair.pubkey()))
                if _bd is not None and _bd < 0:
                    _p = stoic.positions[mint]
                    _p["actual_cost_sol"] = round(float(_p.get("actual_cost_sol", 0.0)) + (-_bd), 6)
            except Exception:
                pass
        # S105 (fee accounting): accumulate the Jito TIP paid on this buy bundle onto the position
        # (the one fee proceeds-pnl / actual_cost_sol both MISS — it's a separate bundle tx). Cheap
        # dict pop, always-on, accumulates across stacks. Logged as round-trip fee_sol at close;
        # pnl_sol is NOT altered → pure instrumentation, zero behaviour change to gate/break-even.
        if mint in stoic.positions:
            _p = stoic.positions[mint]
            _p["tip_sol"] = round(float(_p.get("tip_sol", 0.0)) + wallet.pop_tip_sol(sig), 9)
    else:
        _auditor.push("error", {"context": mint, "error": f"BUY {fail_reason}: {sig[:20]}..."})
        _tx_fail_last_cycle[mint] = cycle
        _tx_fail_count[mint] = _tx_fail_count.get(mint, 0) + 1
        if _tx_fail_count[mint] >= 2:
            print(f"[BUY]  {mint[:8]}... session-banned after {_tx_fail_count[mint]} failed txs", flush=True)


async def execute_twap_buy(
    client: httpx.AsyncClient,
    keypair,
    signal: dict,
    risk: RiskManager,
    stoic: StoicStrategy,
    cycle: int,
    fail_counts: dict = None,
) -> None:
    """
    Three-tranche TWAP execution. Called by execute_buy when the Market Impact Predictor
    detects that an atomic swap would move the pool price by more than TWAP_IMPACT_THRESHOLD%.

    Each tranche fetches a fresh Jupiter quote so subsequent legs adapt to price movement
    caused by the previous tranche. The risk slot is claimed upfront for non-stacks to
    prevent duplicate entries during the multi-cycle execution window (~6–120s).

    Position reconciliation at the end adjusts tracked sizes to actual confirmed SOL,
    handling partial fills (some tranches failed) cleanly.
    """
    mint      = signal["mint"]
    price     = signal["price"]
    size_sol  = signal["size_sol"]
    size_usd  = signal["size_usd"]
    sol_price = size_usd / size_sol if size_sol > 1e-9 else 150.0
    is_static  = mint in set(WATCHLIST)
    _mode      = stoic_strategy_module_mode()
    # S74-bot3: edge entries (deep_pool/brain_rule) use aggressive slippage even in stoic (TWAP path).
    _edge_entry = signal.get("insane_tier") in ("deep_pool", "brain_rule")
    slippage   = STOIC_SLIPPAGE_BPS if (_mode == "stoic" and not _edge_entry) else MAX_SLIPPAGE_BPS
    _jito_only = _mode in ("insane", "wild")

    tranche_sol = size_sol / TWAP_TRANCHES
    tranche_usd = size_usd / TWAP_TRANCHES

    # Claim the risk slot upfront for fresh positions so the 30s polling loop cannot
    # open a second entry in the same mint while TWAP tranches are confirming.
    # Stacks skip this — their base position already occupies the risk slot.
    if not signal.get("is_stack"):
        risk.open_position(mint, size_usd)

    confirmed_sol    = 0.0
    confirmed_usd    = 0.0
    position_opened  = False   # True after first tranche opens the stoic position

    print(
        f"[TWAP] {mint[:8]}... starting {TWAP_TRANCHES}-tranche split "
        f"◎{size_sol:.4f} total | ◎{tranche_sol:.4f}/tranche",
        flush=True,
    )

    for i in range(TWAP_TRANCHES):
        tranche_lamports = int(tranche_sol * 1e9)

        # Fresh quote per tranche — price moves between tranches as each leg executes
        quote = await jupiter.get_quote(client, BASE_MINT, mint, tranche_lamports, slippage)
        if not quote:
            print(f"[TWAP] {mint[:8]}... tranche {i+1}/{TWAP_TRANCHES} quote failed — aborting", flush=True)
            if fail_counts is not None:
                fail_counts[mint] = fail_counts.get(mint, 0) + 1
            break

        swap_tx = await jupiter.build_swap_transaction(client, quote, str(keypair.pubkey()))
        if not swap_tx:
            print(f"[TWAP] {mint[:8]}... tranche {i+1}/{TWAP_TRANCHES} build failed — aborting", flush=True)
            break

        signed        = wallet.sign_transaction(keypair, swap_tx)
        t_broadcast   = time.time()
        sig = await wallet.send_transaction(client, signed, keypair=keypair, skip_simulate=is_static, jito_only=_jito_only)
        if not sig:
            print(f"[TWAP] {mint[:8]}... tranche {i+1}/{TWAP_TRANCHES} send failed", flush=True)
            if fail_counts is not None:
                fail_counts[mint] = fail_counts.get(mint, 0) + 1
            break

        confirmed_flag, fail_reason = await wallet.confirm_transaction(client, sig, signed_tx=signed)
        latency_ms = int((time.time() - t_broadcast) * 1000)
        _auditor.push("tx_result", {"mint": mint, "sig": sig, "confirmed": confirmed_flag, "latency_ms": latency_ms})

        if not confirmed_flag:
            print(
                f"[TWAP] {mint[:8]}... tranche {i+1}/{TWAP_TRANCHES} failed ({fail_reason}) "
                f"| sig: {sig[:12]}...",
                flush=True,
            )
            if fail_counts is not None:
                fail_counts[mint] = fail_counts.get(mint, 0) + 1
            break

        confirmed_sol += tranche_sol
        confirmed_usd += tranche_usd
        print(
            f"[TWAP] {mint[:8]}... tranche {i+1}/{TWAP_TRANCHES} ◎{tranche_sol:.4f} confirmed "
            f"(◎{confirmed_sol:.4f} / ◎{size_sol:.4f}) | latency={latency_ms}ms | sig: {sig[:12]}...",
            flush=True,
        )

        # Update position tracking per confirmed tranche
        _tqout = int(quote.get("outAmount", 0)) if quote else 0
        _tqin  = int(quote.get("inAmount",  0)) if quote else 0
        if signal.get("is_stack"):
            stoic.add_to_position(mint, price, tranche_sol, tranche_usd)
            risk.stack_position(mint, tranche_usd)
            _auditor.push("trade_open", {"mint": mint, "size_usd": tranche_usd, "price": price, "signature": sig, "size_sol": tranche_sol, "tier": signal.get("insane_tier"), "quote_out": _tqout, "quote_in": _tqin})
        elif not position_opened:
            # First confirmed tranche — open the stoic position (TWAP open)
            stoic.open_position(
                mint, price, tranche_sol, tranche_usd,
                signal["momentum_5m"], signal.get("volume_5m", 0.0),
                take_profit_pct=signal.get("take_profit_pct"),
                stop_loss_pct=signal.get("stop_loss_pct"),
                max_hold_hours=signal.get("max_hold_hours"),
                tier_label=signal.get("insane_tier"),
                mode=_mode, regime=_volatility_regime,
                wallet_frac=signal.get("wallet_frac"),
                size_cap_pct=signal.get("size_cap_pct"),
                confidence=signal.get("confidence"),
                normal_slice=signal.get("normal_slice", False),
                momentum_override=signal.get("momentum_override", False),  # S107: tag → ×0.5 size, trail exit, close-row flag
                dust_shadow=signal.get("dust_shadow", False),   # S99: tag the position → close row carries it (shadow gate reads, live gate excludes)
            )
            _apply_route_meta(stoic, mint, signal)   # S99: entry route-depth → position → close row (route captured in execute_buy's honeypot reverse-quote, before TWAP dispatch)
            _TAXO_OPEN_AGG[mint] = regime_policy.current_agg()   # TAXONOMY: exact regime_v2 tag at close
            _auditor.push("trade_open", {"mint": mint, "size_usd": tranche_usd, "price": price, "signature": sig, "size_sol": tranche_sol, "tier": signal.get("insane_tier"), "mode": _mode, "play": signal.get("insane_tier"), "regime": _volatility_regime, "quote_out": _tqout, "quote_in": _tqin, "route_roundtrip_pct": signal.get("route_roundtrip_pct"), "route_n": signal.get("route_n")})
            position_opened = True
        else:
            # Subsequent tranches — add into the open position
            stoic.add_to_position(mint, price, tranche_sol, tranche_usd)
            _auditor.push("trade_open", {"mint": mint, "size_usd": tranche_usd, "price": price, "signature": sig, "size_sol": tranche_sol, "tier": signal.get("insane_tier"), "quote_out": _tqout, "quote_in": _tqin})

        # Inter-tranche delay — lets the pool price recover between legs
        if i < TWAP_TRANCHES - 1:
            await asyncio.sleep(TWAP_INTER_TRANCHE_DELAY)

    # ── Reconcile position tracking against actual confirmed fills ────────────
    if confirmed_sol < MIN_POSITION_SOL:
        # Zero tranches confirmed — remove the pre-claimed risk/stoic slot
        risk.open_positions.pop(mint, None)
        if mint in stoic.positions and not signal.get("is_stack"):
            stoic.positions.pop(mint, None)
            stoic._save_positions()
        print(f"[TWAP] {mint[:8]}... all tranches failed — position slot cleared", flush=True)
        return

    if confirmed_sol < size_sol - 1e-6:
        # Partial fill — shrink risk and stoic tracking to actual deployed capital
        actual_usd = round(confirmed_sol * sol_price, 4)
        if not signal.get("is_stack"):
            risk.open_positions[mint] = actual_usd
        if mint in stoic.positions:
            stoic.positions[mint]["size_sol"] = round(confirmed_sol, 6)
            stoic.positions[mint]["size_usd"] = actual_usd
            stoic._save_positions()

    n_done    = round(confirmed_sol / tranche_sol) if tranche_sol > 1e-9 else 0
    _tier_tag = f"[{signal['insane_tier'].upper()}] " if signal.get("insane_tier") else ""
    _tp_sl    = (
        f"TP={signal['take_profit_pct']}% SL={signal['stop_loss_pct']}% hold={signal['max_hold_hours']}h | "
        if signal.get("take_profit_pct") else ""
    )
    print(
        f"[TWAP] {_tier_tag}{mint[:8]}... ◎{confirmed_sol:.4f} SOL ({n_done}/{TWAP_TRANCHES} tranches) "
        f"@ ${price:.6f} | {_tp_sl}conf={signal['confidence']:.2f}",
        flush=True,
    )


def _parse_token_units(data: dict) -> int:
    """Pull the raw token amount out of a getTokenAccountsByOwner response (0 if none)."""
    try:
        accts = (data or {}).get("result", {}).get("value", [])
        if accts:
            return int(accts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except Exception:
        pass
    return 0


async def get_token_balance_units(client: httpx.AsyncClient, owner: str, mint: str) -> int:
    """Return the actual token balance in raw units (respects any decimal count).

    A zero from the primary (Helius) RPC is CONFIRMED against the public RPC before it is
    trusted. A degraded/rate-limited Helius returns a *successful-looking* empty result (no
    error code), which `rpc_post` cannot distinguish from a genuinely empty wallet — that
    false-empty read is exactly what booked the SPCX ghost close (S70): the tokens were
    on-chain the whole time and the orphan sweep recovered them 8h later. Same illusion S60
    fixed in the prune path and S69 in the SOL-balance read; this closes it on the sell path.
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    }
    try:
        units = _parse_token_units(await rpc_post(client, payload))
        if units > 0:
            return units
        # Primary read empty — confirm directly against the public RPC before trusting it.
        try:
            r = await asyncio.wait_for(
                client.post(FALLBACK_RPC_URL, json=payload, timeout=10), timeout=10.0)
            pub = _parse_token_units(r.json())
        except Exception:
            pub = 0
        if pub > 0:
            print(f"[Balance] Helius false-empty caught — public RPC reports {pub} units for {mint[:8]}...", flush=True)
        return pub
    except Exception as e:
        print(f"[Sell] Balance fetch error: {e}")
    return 0


async def execute_partial_sell(
    client: httpx.AsyncClient,
    keypair,
    mint: str,
    fraction: float,
    pnl_pct: float,
    reason: str,
    risk: RiskManager,
    stoic: StoicStrategy,
    cycle: int,
    sol_price: float = 80.0,
) -> None:
    """Sell a fraction of an open position (TP1 partial exit). Position stays open."""
    pos = stoic.positions.get(mint, {})
    size_usd = pos.get("size_usd", 0)
    if not size_usd:
        return

    token_units = await get_token_balance_units(client, str(keypair.pubkey()), mint)
    if token_units == 0:
        print(f"[TP1] {mint[:8]}... zero on-chain balance — skip partial exit", flush=True)
        return

    units_to_sell = int(token_units * fraction)
    if units_to_sell == 0:
        return

    quote = await jupiter.get_quote(client, mint, BASE_MINT, units_to_sell)
    if not quote:
        _auditor.push("error", {"context": mint, "error": "Partial sell quote failed"})
        return

    swap_tx = await jupiter.build_swap_transaction(client, quote, str(keypair.pubkey()))
    if not swap_tx:
        _auditor.push("error", {"context": mint, "error": "Partial sell swap build failed"})
        return

    signed = wallet.sign_transaction(keypair, swap_tx)
    t_broadcast = time.time()
    sig = await wallet.send_transaction(client, signed, keypair=keypair)
    if not sig:
        _auditor.push("error", {"context": mint, "error": "Partial sell send failed"})
        return

    confirmed, fail_reason = await wallet.confirm_transaction(client, sig, signed_tx=signed)
    latency_ms = int((time.time() - t_broadcast) * 1000)
    _auditor.push("tx_result", {"mint": mint, "sig": sig, "confirmed": confirmed, "latency_ms": latency_ms})

    if confirmed:
        partial_usd  = size_usd * fraction
        exit_usd     = partial_usd * (1 + pnl_pct / 100)
        pnl_usd      = exit_usd - partial_usd
        size_sol     = pos.get("size_sol", 0)
        approx_sol_price = size_usd / size_sol if size_sol > 0 else max(sol_price, 1)
        pnl_sol      = pnl_usd / approx_sol_price

        # PIPE12 (R-D2): proceeds-based pnl for the partial — realized SOL out (quote.outAmount)
        # minus the fraction's cost basis. Always captured; only books when the canary is on.
        _p_realized_sol = int(quote.get("outAmount", 0)) / 1e9 if quote else 0.0
        _p_pnl_proceeds = round(_p_realized_sol - size_sol * fraction, 6) if _p_realized_sol > 0 else None
        _p_pnl_price    = None
        if _proceeds_pnl_on() and _p_pnl_proceeds is not None:
            _p_pnl_price = round(pnl_sol, 6)
            pnl_sol = _p_pnl_proceeds

        risk.partial_close_position(mint, fraction, exit_usd)
        stoic.partial_close_position(mint, fraction)

        _recent_trade_results.append((datetime.now(timezone.utc), pnl_usd >= 0))
        _auditor.push("write", {
            "event":    "partial_close",
            "mint":     mint,
            "fraction": fraction,
            "pnl_usd":  round(pnl_usd, 4),
            "pnl_sol":  round(pnl_sol, 6),
            "pnl_sol_price":    _p_pnl_price,
            "realized_sol_out": round(_p_realized_sol, 6) if _p_realized_sol > 0 else None,
            "pnl_sol_proceeds": _p_pnl_proceeds,
            "sig":      sig,
        })
        print(
            f"[TP1] {mint[:8]}... {reason} | {fraction*100:.0f}% taken | "
            f"pnl=◎{pnl_sol:+.4f} (${pnl_usd:+.4f}) | "
            f"remaining {(1-fraction)*100:.0f}% with BE stop | "
            f"latency={latency_ms}ms | sig: {sig[:12]}...",
            flush=True,
        )
    else:
        # Reset tp1_taken so the check can retry next cycle
        if mint in stoic.positions:
            stoic.positions[mint]["tp1_taken"] = False
            # S96: also un-latch the house-money flag so a FAILED de-risk bank retries next
            # cycle (otherwise the once-per-position guard would skip it permanently).
            stoic.positions[mint].pop("house_money_taken", None)
            stoic.positions[mint].pop("sl_floor_pct", None)
            stoic.positions[mint].pop("remaining_fraction", None)
            stoic._save_positions()
        _auditor.push("error", {"context": mint, "error": f"PARTIAL_SELL {fail_reason}: {sig[:20]}..."})
        print(f"[TP1] {mint[:8]}... partial sell failed ({fail_reason}) — will retry", flush=True)


def _ghost_diag(pos: dict) -> dict:
    """S85-B: liquidity-trajectory fields for ghost diagnosis (logging only, NO behavior change).
    Populated each cycle by the exit loop's per-position liq tracker; read here at close time.
    entry_liq / min_liq in USD; max_cyc_drain = worst single-cycle pool change (negative = drain,
    e.g. -0.62 = a 62% one-cycle collapse → the single-cycle-rug signature)."""
    return {
        "entry_liq":     round(pos["entry_liq"]) if pos.get("entry_liq") else None,
        "min_liq":       round(pos["min_liq"])   if pos.get("min_liq")   else None,
        "max_cyc_drain": pos.get("max_cyc_drain"),
    }


async def execute_sell(
    client: httpx.AsyncClient,
    keypair,
    mint: str,
    pnl_pct: float,
    reason: str,
    risk: RiskManager,
    stoic: StoicStrategy,
    cycle: int,
    position: dict = None,
) -> None:
    global _goldilocks_trade_count, _session_pnl_sol
    # Use pre-fetched position dict from check_exits when available — avoids re-fetching
    # after stoic.positions may have been modified by other exits in the same cycle.
    pos = position if position is not None else stoic.positions.get(mint, {})
    size_usd = pos.get("size_usd", 0)
    if not size_usd:
        return

    # Use actual on-chain balance — correct for any token decimal count.
    # Double-confirm before abandoning: a single zero read can be an RPC hiccup.
    # Two independent zeroes (2s apart) = genuinely empty.
    import asyncio as _asyncio
    token_units = await get_token_balance_units(client, str(keypair.pubkey()), mint)
    if token_units == 0:
        print(f"[Sell] {mint[:8]}... balance=0 on first read — confirming in 2s", flush=True)
        await _asyncio.sleep(2.0)
        token_units = await get_token_balance_units(client, str(keypair.pubkey()), mint)
    if token_units == 0:
        # Both reads (each public-RPC-confirmed) say empty. Before writing off the position
        # as a total loss, guard against the two ways a *live* token reads empty:
        #   1) RPC indexing lag right after a buy — the token account isn't queryable yet.
        #   2) A transient double false-empty. The SPCX/S70 ghost hit both: zero-read 126s
        #      after a 12s-latency buy, tokens on-chain the whole time (sweep recovered them).
        _now = datetime.now(timezone.utc)
        _opened = pos.get("opened_at", "")
        _age = None
        if _opened:
            try:
                _age = (_now - datetime.fromisoformat(_opened)).total_seconds()
            except Exception:
                _age = None
        if _age is None:
            # No usable timestamp — backfill and grant the full guard window rather than
            # ghost-close a possibly-fresh fill (mirrors the sweep's backfill behaviour).
            pos["opened_at"] = _now.isoformat()
            if mint in stoic.positions:
                stoic.positions[mint]["opened_at"] = pos["opened_at"]
                try: stoic._save_positions()
                except Exception: pass
            print(f"[Sell] {mint[:8]}... zero balance, missing opened_at — backfilled + deferring (RPC-lag guard)", flush=True)
            return
        if _age < _SELL_GHOST_GUARD_SECS:
            print(f"[Sell] {mint[:8]}... zero balance but opened {_age:.0f}s ago — deferring, NOT ghost-closing (RPC-lag guard)", flush=True)
            return
        # Aged position still reads empty after public-RPC confirm — two-strike before write-off.
        _miss = _sell_zero_miss.get(mint, 0) + 1
        _sell_zero_miss[mint] = _miss
        if _miss < 2:
            print(f"[Sell] {mint[:8]}... confirmed-zero ({_miss}/2) — deferring one cycle before ghost-close", flush=True)
            return
        _sell_zero_miss.pop(mint, None)
        _auditor.push("error", {"context": mint, "error": "Zero token balance — nothing to sell (confirmed ×2 + public-RPC)"})
        size_sol = pos.get("size_sol", 0)
        # ── S74: ghost_close = remaining tokens UNSELLABLE (zero balance) → realized NOTHING.
        # Book the REMAINING position as a LOSS (mirrors ghost_prune's -size_sol), NEVER the
        # frozen-peak paper gain. The old `size_usd*(pnl_pct/100)` credited +peak% on the FULL
        # position — FizdC booked +◎0.0277 phantom on tokens it never sold, which made the
        # deploy gate's net≥0 PASS on a ghost (a ghost could falsely ARM the EV-sizing genes on
        # fake edge — the exact capital risk the gate exists to prevent). The tp1 partial, if any,
        # was already banked at tp1 time, so only the remaining position is lost here.
        # S102: size_usd/size_sol are ALREADY scaled to the remaining position by
        # partial_close_position, so the remaining IS the full loss — the old extra
        # "* remaining_fraction" double-reduced the booked ghost loss (orig×rem²).
        pnl_usd = -size_usd
        pnl_sol = -size_sol
        _ghost_pct = -100.0   # close_position books size_usd*(pct/100) = the (already-scaled) remaining loss
        _auditor.push("trade_close", {"mint": mint, "pnl": pnl_usd, "signature": "ghost_close", "pnl_sol": pnl_sol, "size_sol": pos.get("size_sol", 0), "tier": pos.get("insane_tier"), "mode": pos.get("mode"), "play": pos.get("insane_tier"), "regime": pos.get("regime"), "normal_slice": pos.get("normal_slice", False), "dust_shadow": pos.get("dust_shadow", False), "momentum_override": pos.get("momentum_override", False), "ghost": True, "exit_reason": "ghost_close — zero balance, token unsellable", **_ghost_diag(pos), **_taxo_tags(pos, mint)})
        stoic.close_position(mint, _ghost_pct, cycle)
        risk.close_position(mint, size_usd)
        return

    _sell_zero_miss.pop(mint, None)   # real balance found — clear any pending zero-strike
    quote = await jupiter.get_quote(client, mint, BASE_MINT, token_units)
    if not quote:
        _auditor.push("error", {"context": mint, "error": "Sell quote failed"})
        stoic.close_position(mint, pnl_pct, cycle)
        exit_usd = size_usd * (1 + pnl_pct / 100)
        risk.close_position(mint, exit_usd)
        return

    swap_tx = await jupiter.build_swap_transaction(client, quote, str(keypair.pubkey()))
    if not swap_tx:
        _auditor.push("error", {"context": mint, "error": "Sell swap build failed"})
        return

    signed = wallet.sign_transaction(keypair, swap_tx)
    t_broadcast = time.time()
    sig = await wallet.send_transaction(client, signed, keypair=keypair)
    if not sig:
        _auditor.push("error", {"context": mint, "error": "Sell send failed"})
        return

    confirmed, fail_reason = await wallet.confirm_transaction(client, sig, signed_tx=signed)
    latency_ms = int((time.time() - t_broadcast) * 1000)
    _auditor.push("tx_result", {"mint": mint, "sig": sig, "confirmed": confirmed, "latency_ms": latency_ms})
    if confirmed:
        exit_usd = size_usd * (1 + pnl_pct / 100)
        pnl_usd = risk.close_position(mint, exit_usd)
        stoic.close_position(mint, pnl_pct, cycle)
        size_sol = pos.get("size_sol", 0)
        approx_sol_price = size_usd / size_sol if size_sol > 0 else 80.0
        pnl_sol = pnl_usd / approx_sol_price
        _session_pnl_sol += pnl_sol
        _cost_prestige = max(0.0, _cur_milestone_sol - (_cur_sol_balance + _cur_sol_in_trades))
        # Slippage-integrity: compare Jupiter's sell quote to estimated actual fill.
        # Jupiter outAmount (lamports) = what we expected to receive for the tokens.
        # Estimated actual SOL = entry_size_sol + pnl_sol (derived from DexScreener price).
        # Drift > 5 % = exec_penalty 1.0; < 0 % (over-fill) = 0.0.
        _expected_sol = int(quote.get("outAmount", 0)) / 1e9 if quote else 0.0
        _actual_sol   = size_sol + pnl_sol
        _exec_penalty = 0.0
        if _expected_sol > 0 and _actual_sol < _expected_sol:
            _drift = (_expected_sol - _actual_sol) / _expected_sol
            _exec_penalty = round(min(1.0, max(0.0, _drift / 0.05)), 4)
        # PIPE12 (R-D2) + S105: proceeds-based pnl from REAL on-chain swap proceeds (ground truth).
        # The quote outAmount (_expected_sol) is a PRE-trade ESTIMATE that omits fee+tip+slippage —
        # booking off it (or off price%) read FLAT while the wallet bled friction on dead-pool churn.
        # S105: when the canary is on, fetch the CONFIRMED sell tx's wallet delta (real SOL received,
        # net of fee+tip+slippage) and use the REAL entry cost basis (actual_cost_sol, captured at
        # buy from the buy tx delta) → pnl reconciles to the wallet by construction. Falls back to the
        # quote estimate if the RPC read fails (None) so a sell is never blocked.
        _cost_basis_sol   = pos.get("actual_cost_sol") or size_sol
        _real_sol_out     = None
        if _proceeds_pnl_on():
            try:
                _real_sol_out = await wallet.get_tx_sol_delta(client, sig, str(keypair.pubkey()))
            except Exception:
                _real_sol_out = None
        if _real_sol_out is not None and _real_sol_out > 0:
            _realized_sol_out = round(_real_sol_out, 6)
            _pnl_sol_proceeds = round(_real_sol_out - _cost_basis_sol, 6)
        else:
            _realized_sol_out = round(_expected_sol, 6) if _expected_sol > 0 else None
            _pnl_sol_proceeds = round(_expected_sol - size_sol, 6) if _expected_sol > 0 else None
        if _proceeds_pnl_on() and _pnl_sol_proceeds is not None:
            _pnl_sol_price = round(pnl_sol, 6)
            pnl_sol = _pnl_sol_proceeds
            _session_pnl_sol += (_pnl_sol_proceeds - (pnl_usd / approx_sol_price))  # correct the session sum to proceeds
        else:
            _pnl_sol_price = None
        # S105 (fee accounting): round-trip Jito TIP = buy-leg tips accumulated on the position +
        # this sell-leg's tip. The one fee proceeds-pnl misses (separate bundle tx). Logged as a
        # standalone field; pnl_sol is NOT reduced by it → true-net-of-tip = pnl_sol − fee_sol is a
        # REPORT-side computation (fee_report.py), so the gate/break-even/win-loss are unchanged.
        _fee_sol = round(float(pos.get("tip_sol", 0.0)) + wallet.pop_tip_sol(sig), 9)
        _auditor.push("trade_close", {"mint": mint, "pnl": pnl_usd, "signature": sig, "pnl_sol": pnl_sol, "pnl_sol_price": _pnl_sol_price, "realized_sol_out": _realized_sol_out, "pnl_sol_proceeds": _pnl_sol_proceeds, "size_sol": size_sol, "fee_sol": _fee_sol, "tier": pos.get("insane_tier"), "mode": pos.get("mode"), "play": pos.get("insane_tier"), "regime": pos.get("regime"), "normal_slice": pos.get("normal_slice", False), "dust_shadow": pos.get("dust_shadow", False), "momentum_override": pos.get("momentum_override", False), "route_roundtrip_pct": pos.get("route_roundtrip_pct"), "route_impact_pct": pos.get("route_impact_pct"), "route_n": pos.get("route_n"), "cost_to_prestige": round(_cost_prestige, 4), "exec_penalty": _exec_penalty, "exit_reason": reason, "hold_min": round((datetime.now(timezone.utc) - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 60.0, 1) if pos.get("opened_at") else None, **_ghost_diag(pos), **_taxo_tags(pos, mint)})  # S77: record WHICH exit fired + hold time so the paper->live edge leak is diagnosable. S85-B: + liq-trajectory (entry/min/max_cyc_drain) for ghost diagnosis. S80: size_sol = capital deployed, so realized EV/trade (pnl_sol/size_sol) is computable — the race's only noise-free fitness metric. S105: fee_sol = round-trip Jito tip (the residual unbooked fee).
        # S105: dead-pool re-entry quarantine — stop the friction-churn leak. A dead-pool exit is a
        # flat (~0% price) close that books break-even, so the loss-streak ban never fires; quarantine
        # the mint so admission stops re-buying a dead/frozen pool round-trip after round-trip.
        if _dead_pool_quarantine_on() and isinstance(reason, str) and reason.startswith("Dead pool"):
            try: memory.quarantine_dead_pool(mint)
            except Exception as _e: print(f"[Memory] quarantine err {mint[:8]}: {_e}", flush=True)
        # Record outcome for circuit breaker and fleet self-optimization
        _recent_trade_results.append((datetime.now(timezone.utc), pnl_usd >= 0))
        _recent_pnl_pcts.append(pnl_pct)
        _goldilocks_trade_count += 1
        # EV halt: 10 all-loss full-closes with net SOL < -0.1 → auto-pause new entries
        risk.record_trade_result(pnl_sol)
        if risk.ev_pause_triggered() and not PAUSE_FLAG.exists():
            PAUSE_FLAG.write_text("{}")
            _net_sol = sum(risk._recent_trade_pnl_sol)
            _auditor.push("halt", {"reason": f"EV halt: {_EV_HALT_STREAK} all-loss closes, net ◎{_net_sol:.4f}"})
            print(
                f"[EV-HALT] {_EV_HALT_STREAK} consecutive losses, net ◎{_net_sol:.4f} < ◎{_EV_HALT_THRESHOLD} "
                f"— auto-pausing new entries. Unpause via dashboard when ready.",
                flush=True,
            )
        print(
            f"[SELL] {mint[:8]}... {reason} | pnl=◎{pnl_sol:+.4f} (${pnl_usd:+.4f}) | latency={latency_ms}ms | sig: {sig[:12]}...",
            flush=True,
        )
    else:
        # TX timed out or failed on-chain. For timeouts, check if tokens are actually gone —
        # the TX may have landed late. If balance is 0, close the position now to prevent
        # a double-count spike in the chart (sol_bal already has returned SOL but position
        # would still be in sol_in_trades for the next cycle).
        if fail_reason == "timeout":
            token_units_after = await get_token_balance_units(client, str(keypair.pubkey()), mint)
            if token_units_after == 0:
                exit_usd = size_usd * (1 + pnl_pct / 100)
                pnl_usd = risk.close_position(mint, exit_usd)
                stoic.close_position(mint, pnl_pct, cycle)
                size_sol = pos.get("size_sol", 0)
                approx_sol_price = size_usd / size_sol if size_sol > 0 else 80.0
                pnl_sol = pnl_usd / approx_sol_price
                _session_pnl_sol += pnl_sol
                _cost_prestige = max(0.0, _cur_milestone_sol - (_cur_sol_balance + _cur_sol_in_trades))
                _auditor.push("trade_close", {"mint": mint, "pnl": pnl_usd, "signature": sig, "pnl_sol": pnl_sol, "size_sol": size_sol, "tier": pos.get("insane_tier"), "mode": pos.get("mode"), "play": pos.get("insane_tier"), "regime": pos.get("regime"), "normal_slice": pos.get("normal_slice", False), "dust_shadow": pos.get("dust_shadow", False), "momentum_override": pos.get("momentum_override", False), "route_roundtrip_pct": pos.get("route_roundtrip_pct"), "route_impact_pct": pos.get("route_impact_pct"), "route_n": pos.get("route_n"), "cost_to_prestige": round(_cost_prestige, 4), **_taxo_tags(pos, mint)})
                _recent_pnl_pcts.append(pnl_pct)
                _goldilocks_trade_count += 1
                risk.record_trade_result(pnl_sol)
                if risk.ev_pause_triggered() and not PAUSE_FLAG.exists():
                    PAUSE_FLAG.write_text("{}")
                    _net_sol = sum(risk._recent_trade_pnl_sol)
                    _auditor.push("halt", {"reason": f"EV halt: {_EV_HALT_STREAK} all-loss closes, net ◎{_net_sol:.4f}"})
                    print(
                        f"[EV-HALT] {_EV_HALT_STREAK} consecutive losses, net ◎{_net_sol:.4f} < ◎{_EV_HALT_THRESHOLD} "
                        f"— auto-pausing new entries. Unpause via dashboard when ready.",
                        flush=True,
                    )
                print(
                    f"[SELL] {mint[:8]}... {reason} | late-confirm | pnl=◎{pnl_sol:+.4f} (${pnl_usd:+.4f}) | sig: {sig[:12]}...",
                    flush=True,
                )
                return
        _auditor.push("error", {"context": mint, "error": f"SELL {fail_reason}: {sig[:20]}..."})


# Consecutive "no on-chain balance" reads per mint. A position must read empty on TWO
# consecutive sweeps before it is pruned — one degraded/false-empty RPC read can no longer
# orphan a live position (the failure mode that abandoned the Bot 2 tokens).
_ghost_miss_counts: dict[str, int] = {}
_GHOST_MISS_PRUNE_THRESHOLD = 2

# Sell-path ghost guard (S70): don't book a phantom loss on a zero-balance read for a
# freshly-bought position — the token account may simply not be indexed yet (the SPCX ghost
# was zero-read 126s after a 12s-latency buy, then recovered on-chain by the sweep). Defer
# the close for newly-opened positions, and require two consecutive confirmed-zero reads
# (each already public-RPC-confirmed) before writing off an aged one.
_SELL_GHOST_GUARD_SECS = 180.0
_sell_zero_miss: dict[str, int] = {}


async def sweep_orphan_tokens(client: httpx.AsyncClient, keypair, stoic=None, risk=None) -> None:
    """
    On startup: reconcile tracked positions against actual on-chain token holdings.

    Forward (orphan sell): tokens in wallet not tracked in positions.json → sell back to SOL.
    Reverse (ghost prune): positions tracked in memory with no on-chain token balance → drop them.

    Ghost positions cause phantom SOL to appear in the balance chart (portfolio_val = sol_bal +
    sol_in_trades, so a tracked-but-never-bought position inflates sol_in_trades and creates a
    false spike). Pruning on startup keeps the chart truthful.
    """
    # Query BOTH token programs. pump.fun mints are Token-2022 — querying only the
    # classic Token program made every Token-2022 position read as "0 on-chain balance",
    # which (a) ghost-pruned valid pump.fun positions after the 120s guard and (b) hid them
    # from the forward-sell, leaving permanent un-sellable orphans in the wallet.
    _TOKEN_PROGRAMS = (
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # classic SPL
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
    )
    accounts = []
    _fetch_ok = False
    for _prog in _TOKEN_PROGRAMS:
        try:
            data = await rpc_post(client, {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [str(keypair.pubkey()), {"programId": _prog}, {"encoding": "jsonParsed"}],
            })
            accounts.extend(data.get("result", {}).get("value", []))
            _fetch_ok = True
        except Exception as e:
            print(f"[Sweep] Failed to fetch {_prog[:8]}.. accounts: {e}")
    if not _fetch_ok:
        return   # both programs failed to fetch — do not prune on a blind read

    # Build map of tokens the wallet actually holds: mint → (amount_raw, ui_amount).
    # Only non-dust balances (≥1 token unit) are tracked.
    wallet_tokens: dict[str, tuple[int, float]] = {}
    for acct in accounts:
        info      = acct["account"]["data"]["parsed"]["info"]
        mint      = info["mint"]
        amount    = int(info["tokenAmount"]["amount"])
        decimals  = int(info["tokenAmount"]["decimals"])
        ui_amount = amount / (10 ** decimals)
        # S90: track ANY nonzero on-chain balance, not `ui_amount >= 1`. The old whole-token
        # dust filter silently dropped fractional holdings of HIGH-unit-price tokens — e.g. a
        # $34/token Token-2022 mint (SPCX) where 0.107 SOL bought only 0.21 tokens (<1 unit) —
        # so the reverse ghost-prune read it as "0 on-chain", booked a phantom -0.107 SOL loss,
        # AND hid it from the forward-sell, stranding a real, sellable position as an orphan.
        # A genuinely failed buy delivers 0 units, so it still prunes correctly. Value-based
        # dust is handled by the forward-sell's no-quote skip below, not a unit-count threshold.
        if amount > 0:
            wallet_tokens[mint] = (amount, ui_amount)

    known_positions = set(json.loads(POSITIONS_PATH.read_text()).keys()) if POSITIONS_PATH.exists() and POSITIONS_PATH.read_text().strip() not in ("{}", "") else set()

    # ── Reverse check: prune ghost positions (tracked but no on-chain balance) ──
    # A ghost position means a buy was never executed (e.g. injected for testing,
    # or the buy tx failed silently before open_position was guarded). Keeping it
    # inflates sol_in_trades and corrupts the portfolio chart:
    #   portfolio_val = sol_bal + sol_in_trades
    # The wallet balance stays real but sol_in_trades counts phantom SOL, so the
    # chart shows a false spike equal to the ghost position's size_sol.
    if stoic is not None and risk is not None:
        # Safety bail: a wallet with open positions should hold ≥1 token account. If the
        # merged read came back with ZERO accounts while positions are tracked, treat it as
        # a degraded RPC read (the exact false-empty failure that orphaned Bot 2's tokens)
        # and skip pruning entirely this sweep rather than wipe every position at once.
        if not wallet_tokens and stoic.positions:
            print("[Sweep] 0 token accounts returned but positions are tracked — "
                  "treating as degraded read, skipping prune this sweep", flush=True)
            return

        # Skip positions opened in the last 120s — the token account may not be indexed
        # by the RPC yet immediately after a confirmed buy.  Session 52: raised from 90s
        # to 120s to accommodate fresher/newer tokens from the expanded discovery pool.
        _GHOST_PRUNE_GUARD_SECS = 120.0
        _now_sweep = datetime.now(timezone.utc)
        ghosts = []
        for _m in list(stoic.positions):
            if _m in wallet_tokens:
                _ghost_miss_counts.pop(_m, None)   # seen on-chain — reset miss streak
                continue
            _opened_str = stoic.positions[_m].get("opened_at", "")
            _age_s = None
            if _opened_str:
                try:
                    _age_s = (_now_sweep - datetime.fromisoformat(_opened_str)).total_seconds()
                except Exception:
                    _age_s = None
            if _age_s is None:
                # No usable timestamp — likely a just-opened buy whose opened_at wasn't
                # persisted yet. Backfill it and grant the full guard window rather than
                # prune a fresh fill, which is what causes buy→prune→re-buy double-buy
                # loops (the 4k3D ghost cluster). A genuine ghost is pruned on a later
                # sweep once this backfilled timestamp ages past the guard.
                stoic.positions[_m]["opened_at"] = _now_sweep.isoformat()
                stoic._save_positions()
                print(f"[Sweep] {_m[:8]}... missing opened_at — backfilled, guarding {_GHOST_PRUNE_GUARD_SECS:.0f}s", flush=True)
                continue
            if _age_s < _GHOST_PRUNE_GUARD_SECS:
                print(f"[Sweep] Skipping {_m[:8]}... (opened {_age_s:.0f}s ago, RPC lag guard)", flush=True)
                continue
            # Two-strike rule: require the position to read empty on TWO consecutive sweeps
            # before pruning. A single false-empty RPC read just increments the counter.
            _miss = _ghost_miss_counts.get(_m, 0) + 1
            _ghost_miss_counts[_m] = _miss
            if _miss < _GHOST_MISS_PRUNE_THRESHOLD:
                print(f"[Sweep] {_m[:8]}... reads empty ({_miss}/{_GHOST_MISS_PRUNE_THRESHOLD}) "
                      f"— confirming next sweep before prune", flush=True)
                continue
            ghosts.append(_m)
        if ghosts:
            for mint in ghosts:
                size_sol = stoic.positions[mint].get("size_sol", 0)
                print(
                    f"[Sweep] Ghost position pruned: {mint[:8]}... "
                    f"(◎{size_sol:.4f} tracked, 0 on-chain) — removing from memory",
                    flush=True,
                )
                stoic.positions.pop(mint, None)
                risk.open_positions.pop(mint, None)
                _ghost_miss_counts.pop(mint, None)
                _auditor.push("write", {"event": "ghost_prune", "mint": mint, "size_sol": size_sol})
                # Write a close event so the chart drop has a matching entry in the trade log.
                # pnl_sol = -size_sol because removing the phantom causes portfolio_val to drop
                # by exactly that amount. Not a real trade loss — a phantom balance correction.
                _auditor.push("trade_close", {"mint": mint, "pnl": 0.0, "signature": "ghost_prune", "pnl_sol": -size_sol, "ghost": True, "exit_reason": "ghost_prune — no on-chain balance"})
            stoic._save_positions()

    # ── Forward check: sell tokens in wallet not tracked in positions ────────────
    _SWEEP_EXCLUDE = {USDC_MINT, "So11111111111111111111111111111111111111112"}  # USDC + wSOL dust
    for mint, (amount_raw, ui_amount) in wallet_tokens.items():
        if mint in _SWEEP_EXCLUDE:
            continue
        if mint in known_positions:
            continue  # tracked position, skip

        print(f"[Sweep] Found orphan token {mint[:8]}... amount={ui_amount:.4f} — selling back to SOL", flush=True)
        await asyncio.sleep(1.5)  # avoid Jupiter rate limiting
        quote = await jupiter.get_quote(client, mint, BASE_MINT, amount_raw)
        if not quote:
            print(f"[Sweep] No quote for {mint[:8]}... will retry next cycle", flush=True)
            continue
        # S94b: the SOL this orphan sell will recover (Jupiter outAmount, lamports → SOL).
        recovered_sol = int(quote.get("outAmount", 0)) / 1e9
        await asyncio.sleep(1.0)
        swap_tx = await jupiter.build_swap_transaction(client, quote, str(keypair.pubkey()))
        if not swap_tx:
            print(f"[Sweep] Swap build failed for {mint[:8]}... will retry next cycle", flush=True)
            continue
        signed = wallet.sign_transaction(keypair, swap_tx)
        sig = await wallet.send_transaction(client, signed)
        if sig:
            confirmed, _ = await wallet.confirm_transaction(client, sig)
            status = "✓ confirmed" if confirmed else "sent"
            print(f"[Sweep] Sold {mint[:8]}... → SOL | {status} | recovered ◎{recovered_sol:.4f} | sig: {sig[:16]}...", flush=True)
            _auditor.push("write", {"event": "sweep", "mint": mint, "amount": ui_amount,
                                    "sol_recovered": round(recovered_sol, 6), "sig": sig})
            # S94b — LEDGER THE RECOVERY (fixes S92 BUG B). The orphan sweep recovers real SOL, but
            # the OLD code wrote only this audit row — no trade_close — so the ledger booked the
            # ghost LOSS (ghost_prune -size_sol above) and NEVER the matching recovery → the gate/EV
            # read drifted below the real wallet (bot2 went ◎0.80→1.03 on unbooked sweeps).
            # SAFE BY DESIGN — cannot fabricate a phantom GAIN (the inverse of S92 BUG A): the
            # recovery is booked ONLY to OFFSET a prior un-recovered ghost loss for this SAME mint,
            # capped at that loss. A pure orphan with no prior booked loss books nothing. The cap
            # also makes the double-sweep case (same orphan, two sigs) self-limiting.
            if confirmed and recovered_sol > 0:
                try:
                    _ghost_loss = _prior_recovery = 0.0
                    _dbp = STATUS_PATH.parent / "trades.db"
                    if _dbp.exists():
                        with closing(sqlite3.connect(str(_dbp))) as _c:
                            for (_d,) in _c.execute("SELECT data FROM trades WHERE event='close'"):
                                _r  = json.loads(_d)
                                if _r.get("mint") != mint:
                                    continue
                                _ps = _r.get("pnl_sol") or 0.0
                                _er = _r.get("exit_reason") or ""
                                if _er.startswith("sweep recovery"):
                                    _prior_recovery += _ps
                                elif _ps < 0 and (_r.get("ghost") or _er.startswith("ghost")):
                                    _ghost_loss += _ps
                    _unoffset = max(0.0, -(_ghost_loss + _prior_recovery))
                    _credit   = round(min(recovered_sol, _unoffset), 6)
                    if _credit > 0:
                        _auditor.push("trade_close", {
                            "mint": mint, "pnl": 0.0, "signature": "sweep_recovery",
                            "pnl_sol": _credit, "ghost": True,
                            "exit_reason": "sweep recovery — offsets prior ghost loss"})
                        print(f"[Sweep] Ledgered recovery {mint[:8]}...: +◎{_credit:.4f} "
                              f"(prior un-recovered ghost loss ◎{_unoffset:.4f})", flush=True)
                except Exception as _e:
                    print(f"[Sweep] recovery-ledger skipped for {mint[:8]}...: {_e}", flush=True)
        else:
            print(f"[Sweep] Sell failed for {mint[:8]}... will retry next cycle", flush=True)


async def main() -> None:
    print(f"[{_B}] ══════════════════════════════════════════════════════════")
    _m0 = payout.get_milestone(payout.get_payout_count())
    print(f"[{_B}] MISSION : Accumulate SOL. Reach ◎{_m0['threshold_sol']:.1f}. "
          f"Pay ◎{_m0['payout_sol']:.1f} back. Keep ◎{_m0['keep_sol']:.1f} as seed. (cycle {_m0['cycle']})")
    print(f"[{_B}] PURPOSE : This bot exists to compound. Every trade builds the stack.")
    print(f"[{_B}] ══════════════════════════════════════════════════════════")
    print(f"[{_B}] Starting — SOL accumulation mode | Watchlist: {len(WATCHLIST)} tokens")
    print(f"[{_B}] Base currency: SOL | All trades: SOL → token → SOL")

    keypair = wallet.load_keypair()
    print(f"[{_B}] Wallet: {keypair.pubkey()}")

    # Start pump.fun bonding curve monitor (runs as background asyncio task).
    # Watches for whale buys in real time — feeds pre-trend signals to discovery.
    bonding_curve.start()

    # Start Tier 3 Auditor background task — DB writes never block the main loop.
    asyncio.create_task(_auditor.consume_loop())

    risk  = RiskManager()
    stoic = StoicStrategy()

    # Apply any existing goldilocks override immediately so the bot starts with tuned params
    strategy_apply_overrides(_config_mod._overrides)

    # Restore risk manager positions from persisted state
    for mint, pos in stoic.positions.items():
        risk.open_position(mint, pos["size_usd"])

    payout_wallet = payout.load_payout_wallet()
    if payout_wallet:
        print(f"[{_B}] Payout wallet: {payout_wallet[:8]}...")
    else:
        print(f"[{_B}] Waiting to detect funding wallet from first inbound transfer...")

    # NOTE: expired bans stay expired. The consecutive_losses penalty in the confidence
    # formula already reduces position size on re-entry — no need to re-ban on restart.

    # USDC → SOL conversion and orphan sweep handled inside the main loop once network is ready

    cycle = 0
    sol_bal = 0.0  # fetched at end of each cycle; initialized to avoid NameError on first cycle
    _last_good_sol_bal = 0.0  # last non-zero balance — used to detect RPC glitches
    _last_good_portfolio = 0.0  # last reliable (sol_bal + sol_in_trades) for stale-read guard
    _post_settle_skip    = 0   # countdown: skip chart recording for N cycles after any trade
    # Discovery, market data, scoring → managed by _observer (Tier 1)
    _bc_hot:     list = []   # refreshed from _observer each cycle
    _bc_hot_set: set  = set()
    _momentum_history: deque = deque(maxlen=200)  # rolling window of seen signal momentum values
    _buysell_history:  deque = deque(maxlen=200)  # rolling window of seen signal buy/sell ratios
    # Hard per-request timeout on the shared client.
    # httpx's per-read timeout can be defeated when servers drip-stream response bodies
    # (each chunk arrives just inside the window, resetting the timer indefinitely).
    # A Timeout object with an explicit 'read' ceiling caps any single response at 20s.
    # Individual calls may pass a shorter timeout= override; this is the absolute ceiling.
    _client_timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=_client_timeout) as client:
        pubkey_str = str(keypair.pubkey())

        # ── Startup: balance + immediate payout check ─────────────────────────
        sol_bal = await payout.get_sol_balance(client, pubkey_str)
        print(f"[{_B}] Startup balance: ◎{sol_bal:.4f} SOL")
        if sol_bal > 0.0:
            _last_good_sol_bal = sol_bal   # seed the RPC-glitch guard from cycle 1

        # Detect funding wallet now if not already locked — don't wait for cycle 1
        if not payout_wallet:
            detected = await payout.detect_funding_wallet(client, pubkey_str)
            if detected:
                payout.save_payout_wallet(detected)
                payout_wallet = detected
                print(f"[{_B}] Funding wallet locked on startup: {payout_wallet[:8]}...")

        # If balance already clears the milestone, pay immediately — don't wait a cycle
        if payout_wallet:
            _startup_in_trades = sum(p.get("size_sol", 0) for p in stoic.positions.values())
            _startup_total = sol_bal + _startup_in_trades
            _startup_m = payout.get_milestone(payout.get_payout_count())
            if _startup_total >= _startup_m["threshold_sol"]:
                print(
                    f"[{_B}] *** Total ◎{_startup_total:.4f} already clears "
                    f"'{_startup_m['label']}' — triggering payout NOW ***"
                )
            await payout.check_and_payout(client, keypair, payout_wallet, total_sol=_startup_total)
        else:
            print(f"[{_B}] No funding wallet detected yet — payout armed once first inbound transfer arrives.")

        # ─────────────────────────────────────────────────────────────────────
        while True:
            cycle += 1
            now = datetime.utcnow().strftime("%H:%M:%S")

            if risk.is_halted():
                _auditor.push("halt", {"reason": f"Daily PnL: ${risk.daily_pnl:.2f}"})
                print(f"[{now}] HALTED — daily loss limit hit. Pausing 1h.")
                await asyncio.sleep(3600)
                risk.reset_daily()
                continue

            # --- Detect funding wallet ---
            if not payout_wallet:
                detected = await payout.detect_funding_wallet(client, pubkey_str)
                if detected:
                    payout.save_payout_wallet(detected)
                    payout_wallet = detected

            # --- Payout check ---
            # Pass total_sol (liquid + in_trades) so the milestone fires as soon as the bot
            # crosses ◎2.0 total, even if some capital is still deployed. The actual SOL
            # transfer waits until liquid is sufficient; new entries are paused in the meantime.
            # Positions store size_sol directly — no price conversion needed here.
            if payout_wallet:
                _payout_in_trades = sum(p.get("size_sol", 0) for p in stoic.positions.values())
                await payout.check_and_payout(
                    client, keypair, payout_wallet,
                    total_sol=sol_bal + _payout_in_trades,
                )

            # --- Heartbeat ---
            print(
                f"[{_B}][{now}] Cycle #{cycle} | PnL: ${risk.daily_pnl:+.4f} | "
                f"Positions: {len(stoic.positions)} | Learned: {len(memory.summary())} tokens",
                flush=True,
            )

            # ── TIER 1: OBSERVER — Phase 1: Discovery + Market Data ────────────
            # Fetches discovery, builds active watchlist, fetches DexScreener data.
            # Returns market_data, sol_price, and aggregate vol for regime detection.
            mode = stoic_strategy_module_mode()
            market_data, sol_price_live, _agg_vol_5m_obs = await _observer.scan_data(
                client,
                cycle=cycle,
                open_position_mints=list(stoic.positions.keys()),
            )
            _bc_hot     = _observer.bc_hot
            _bc_hot_set = _observer.bc_hot_set
            active_watchlist = _observer.active_watchlist

            if _observer.disc_ran:
                _disc_cfg = DISCOVERY_PAGES_BY_MODE.get(mode, DISCOVERY_PAGES_BY_MODE["insane"])
                print(
                    f"         [Observer] mode={mode} trending={_disc_cfg['trending']} "
                    f"new_pools={_disc_cfg['new_pools']} deep={_disc_cfg['deep']} "
                    f"pool={len(active_watchlist)}",
                    flush=True,
                )
            static_set = set(WATCHLIST)
            print(
                f"         Data: {len(market_data)} tokens "
                f"({sum(1 for m in market_data if m in static_set)} static + "
                f"{sum(1 for m in market_data if m not in static_set)} discovered) | "
                + " | ".join(
                    f"{m[:4]}={d['price_change_5m']:+.1f}%"
                    for m, d in list(market_data.items())[:4]
                ),
                flush=True,
            )

            # --- Emergency exit-all check ---
            current_prices = {m: d["price_usd"] for m, d in market_data.items()}

            # Fallback: open positions DexScreener didn't return get their last-known
            # price from price_history so time/SL exits still fire even on batch misses.
            for _mint, _pos in stoic.positions.items():
                if _mint not in current_prices and _pos.get("price_history"):
                    _last = _pos["price_history"][-1]
                    if _last > 0:
                        current_prices[_mint] = _last
                        print(f"         [Stale] {_mint[:8]}... last-known ${_last:.6f} (DexScreener miss)", flush=True)
            if EXIT_FLAG.exists():
                print(f"[{now}] ⚠ EXIT ALL requested via dashboard — closing all positions...", flush=True)
                EXIT_FLAG.unlink()
                for mint in list(stoic.positions.keys()):
                    price = current_prices.get(mint, 0)
                    entry = stoic.positions[mint]["entry_price"]
                    pnl_pct = ((price - entry) / entry * 100) if entry else 0
                    await execute_sell(client, keypair, mint, pnl_pct, "EXIT ALL", risk, stoic, cycle)
                _auditor.push("write", {"event": "exit_all", "positions_closed": len(stoic.positions)})
                print(f"[{now}] EXIT ALL complete.", flush=True)

            # --- Exit check (before entries) ---
            # Rolling vol_5m history per position — used by the TP1 parabola suppressor below.
            # Updated every cycle so the 10-sample SMA reflects the token's volume at exit time.
            _VOL5M_SMA_LEN = 10
            for _pos_mint, _pos in stoic.positions.items():
                _v = market_data.get(_pos_mint, {}).get("volume_5m", 0.0)
                if _v > 0:
                    _vh = _pos.setdefault("vol_5m_history", [])
                    _vh.append(_v)
                    if len(_vh) > _VOL5M_SMA_LEN:
                        _vh.pop(0)
                # LP-drain guard: parallel rolling liquidity history (consumed by check_exits).
                # Only append real (>0) reads — a 0/empty read is a DexScreener/RPC miss, not a
                # drain (S60 false-empty lesson), so skipping it avoids a phantom drain strike.
                _li = market_data.get(_pos_mint, {}).get("liquidity_usd", 0.0)
                if _li > 0:
                    _lh = _pos.setdefault("liq_history", [])
                    _lh.append(_li)
                    if len(_lh) > _VOL5M_SMA_LEN:
                        _lh.pop(0)
                    # ── S85-B: ghost-diagnosis instrumentation (logging only — NO behavior change) ──
                    # Track each position's liquidity trajectory so a future ghost_close is diagnosable:
                    # entry liq, running min, and the worst SINGLE-CYCLE drain %. This tells us whether a
                    # ghost was a one-cycle full rug (→ a 1-strike catastrophic-drain exit would help) or a
                    # gradual drain (the existing 2-strike LP-drain guard already covers that). See S85_FINDINGS.md (B).
                    _pos.setdefault("entry_liq", _li)
                    _prev_li = _pos.get("_last_liq")
                    if _prev_li and _prev_li > 0:
                        _cyc_drain = (_li - _prev_li) / _prev_li
                        if _cyc_drain < _pos.get("max_cyc_drain", 0.0):
                            _pos["max_cyc_drain"] = round(_cyc_drain, 4)
                    _pos["_last_liq"] = _li
                    _pos["min_liq"] = min(_pos.get("min_liq", _li), _li)

            exits = stoic.check_exits(current_prices, cycle)
            for ex in exits:
                if ex.get("exit_type") == "partial":
                    # TP1 parabola suppressor: if vol_5m at exit time is >200% of the 10-sample
                    # SMA, the token is mid-parabola — suppress the partial exit and let the full
                    # position trail to the TP ceiling. Requires ≥3 samples for a valid SMA.
                    _pm       = ex["mint"]
                    _vol_now  = market_data.get(_pm, {}).get("volume_5m", 0.0)
                    _pos_ref  = stoic.positions.get(_pm, {})
                    _vol_hist = _pos_ref.get("vol_5m_history", [])
                    _suppress = False
                    # S96: the house-money de-risk must ALWAYS bank — never suppress it. The
                    # operator's intent is to lock in the cost basis even mid-parabola ("sold
                    # $70, riding $50 free"); the de-risk already leaves the tail riding, so the
                    # parabola suppressor (which would also clobber remaining_fraction→1.0) only
                    # applies to ordinary TP1 partials.
                    if not ex.get("house_money") and _vol_now > 0 and len(_vol_hist) >= 3:
                        _sma = sum(_vol_hist) / len(_vol_hist)
                        if _sma > 0 and _vol_now > _sma * 2.0:
                            _suppress = True
                            _pos_ref["tp1_taken"]          = False
                            _pos_ref["remaining_fraction"] = 1.0
                            _pos_ref.pop("sl_floor_pct", None)
                            stoic._save_positions()
                            print(
                                f"         [TP1-SUPPRESS] {_pm[:8]}... "
                                f"vol_5m ${_vol_now:,.0f} > 200% SMA ${_sma:,.0f} "
                                f"— parabola active, holding full position",
                                flush=True,
                            )
                    if not _suppress:
                        # Normal TP1 partial exit — sell fraction, keep position with BE stop
                        await execute_partial_sell(
                            client, keypair,
                            ex["mint"], ex["fraction"], ex["pnl_pct"], ex["reason"],
                            risk, stoic, cycle, sol_price=sol_price_live,
                        )
                else:
                    await execute_sell(
                        client, keypair,
                        ex["mint"], ex["pnl_pct"], ex["reason"],
                        risk, stoic, cycle,
                        position=ex.get("position"),
                    )

            # --- Entry signals ---
            sol_price = sol_price_live

            # Revert rate guard: if >20% of last 10 txs failed confirmation, skip entries.
            # S74: min_samples 3→6. At 3 samples a SINGLE revert = 33% > 20% blocked ALL entries,
            # and in a quiet market (few new txs to dilute) the rate stayed elevated, throttling the
            # most-active bot off opportunities for a long window (Bot1: 14× high_revert_rate on
            # rate=0.33). Here the reverts were token-specific slippage (Custom:6001), not a
            # network/fee problem — exactly what the guard should NOT react to on a sample of one.
            # ≥6 samples → 1 revert = 16.7% (no trip); needs a real pattern (≥2 of 6 = 33%) to block.
            revert_rate = audit.get_revert_rate(session_start=_SESSION_START, min_samples=6)
            if revert_rate > 0.20:
                print(
                    f"         ⚠ Revert rate {revert_rate:.0%} — skipping entries (network/fee issue)",
                    flush=True,
                )
                _auditor.push("write", {"event": "high_revert_rate", "rate": round(revert_rate, 2)})

            # Update risk capital before sizing so check_trade uses current balance
            risk.update_capital(sol_bal, sol_price)

            # S74: cost basis of the REMAINING position only. After a partial exit
            # (TP1 sells e.g. 40%), those proceeds are already back in the wallet
            # (sol_bal). The REMAINING cost basis is the already-scaled size_sol
            # (stoic.partial_close_position multiplies size_sol by `remaining` on each
            # partial), so use size_sol directly. S102: the old extra "* remaining_fraction"
            # here scaled it a SECOND time (orig×rem² instead of orig×rem) → it under-counted
            # deployed capital after a partial, which made Total SOL dip on the winning bank
            # and then JUMP UP when the (often red) remainder closed and released the
            # under-count. The S74 "graph drops on a winning trade" fix over-corrected.
            sol_in_trades = sum(   # S102: size_sol is ALREADY scaled by partial_close_position — do NOT re-apply remaining_fraction (that double-reduced deployed capital after a partial)
                p.get("size_sol", p.get("size_usd", 0) / max(sol_price, 1))
                for p in stoic.positions.values()
            )

            # Step 1: evaluate all signals (no sizes yet — sizes depend on ceiling)
            raw_signals  = []
            _token_evals = []   # filled after observer.score_tokens() below
            _thinking = {
                "cycle":       cycle,
                "phase":       "signals",
                "revert_rate": round(revert_rate, 3),
                "guard_blocked": revert_rate > 0.20,
                # Legacy keys — kept for any tooling that reads them directly
                "discovery": {
                    "ran":         _observer.disc_ran,
                    "mode":        mode,
                    "sources":     _observer.sources,
                    "pool_size":   _observer.discovered_count,
                    "watchlist_size": len(active_watchlist),
                },
                "bonding_curve": {
                    "connected": bonding_curve.connected,
                    "hot_count": len(_bc_hot),
                    "stats":     dict(bonding_curve._stats),
                    "hot_mints": [
                        {"mint": m, **(bonding_curve.get_buy_activity(m) or {})}
                        for m in _bc_hot[:8]
                        if bonding_curve.get_buy_activity(m)
                    ],
                    "top_whales": bonding_curve.get_top_whales(3),
                },
                "tokens":  _token_evals,   # updated after score_tokens()
                "capital": {},
                "blocked": None,
                "fired":   [],
                "debater": [],
                "entry_queue": _entry_queue.snapshot(),
                # ── Three-tier architecture data ──────────────────────────────
                "tier1_observer":    {},   # filled after score_tokens()
                "tier2_executioner": {},   # filled after inner loop
                "tier3_auditor":     _auditor.snapshot(),
            }

            # ── Market State: S79 5-regime ladder + adaptive momentum floor ──────
            # Regime is chosen by agg_vol_5m with hysteresis on EVERY boundary:
            #   _REGIME_ENTER[r] = vol needed to CLIMB into r from below
            #   _REGIME_STAY[r]  = vol you must fall below to DROP out of r
            # The enter↔stay gap on each cell prevents boundary flapping.
            _REGIME_ORDER = ["euphoria", "aggressive", "normal", "sniper", "dead"]
            _REGIME_ENTER = {
                "euphoria":   600_000.0,
                "aggressive": 280_000.0,
                "normal":     110_000.0,
                "sniper":      48_000.0,
                "dead":             0.0,
            }
            _REGIME_STAY = {
                "euphoria":   480_000.0,
                "aggressive": 230_000.0,
                "normal":      90_000.0,
                "sniper":      40_000.0,
                "dead":             0.0,
            }
            _STARVATION_MINS     = 180
            _MKTSTATE_VOL_THRESH = 150_000.0   # was 500k — same as AGGRESSIVE entry, never fired in NORMAL band
            _MKTSTATE_MOM_FLOOR  = 2.0          # was 4.0 — rescue floor must be reachable in normal markets
            _agg_vol_5m = _agg_vol_5m_obs   # computed by Observer during scan_data
            global _last_buy_time, _market_state_active, _volatility_regime

            # base = highest regime whose climb-in floor is satisfied
            _base = "dead"
            for _r in _REGIME_ORDER:
                if _agg_vol_5m >= _REGIME_ENTER[_r]:
                    _base = _r
                    break
            _cur = _volatility_regime if _volatility_regime in _REGIME_ORDER else "normal"
            # If base sits BELOW the current regime, hold current until vol drops under its
            # stay floor (sticky band); otherwise climb/settle to base immediately.
            if (_REGIME_ORDER.index(_base) > _REGIME_ORDER.index(_cur)
                    and _agg_vol_5m >= _REGIME_STAY[_cur]):
                _new_regime = _cur
            else:
                _new_regime = _base

            if _new_regime != _volatility_regime:
                _label = {"euphoria": "★ EUPHORIA", "aggressive": "▲ HIGH VOLATILITY",
                          "normal": "◆ NEUTRAL", "sniper": "▼ LOW VOLATILITY",
                          "dead": "✖ DEAD / FROZEN"}
                _action = {r: f"→ {r.upper()} profile" for r in _REGIME_ORDER}
                print(
                    f"         [MarketState] {_label[_new_regime]} ${_agg_vol_5m/1_000:.0f}k "
                    f"{_action[_new_regime]} (was {_volatility_regime})",
                    flush=True,
                )
                _volatility_regime = _new_regime

            # ── Starvation rescue (INSANE + normal regime only) ──────────────────
            # When a regime is active, its ValidationProfile already sets the momentum
            # floor — this rescue is only meaningful in the neutral band.
            _saved_mom_floor: Optional[float] = None
            if mode == "insane" and _volatility_regime == "normal":
                _since_min = (
                    (datetime.utcnow() - _last_buy_time).total_seconds() / 60
                    if _last_buy_time is not None else float("inf")
                )
                _starving  = _since_min > _STARVATION_MINS
                _mkt_hot   = _agg_vol_5m >= _MKTSTATE_VOL_THRESH
                _cur_floor = MODES["insane"].get("MOMENTUM_MIN", 0.3)
                if _starving and _mkt_hot and _cur_floor > _MKTSTATE_MOM_FLOOR:
                    _saved_mom_floor = _cur_floor
                    MODES["insane"]["MOMENTUM_MIN"] = _MKTSTATE_MOM_FLOOR
                    if not _market_state_active:
                        print(
                            f"         [MarketState] ◆ agg_vol_5m ${_agg_vol_5m/1_000:.0f}k | "
                            f"starving {_since_min:.0f}m → floor {_cur_floor:.1f}%→{_MKTSTATE_MOM_FLOOR:.1f}% this cycle",
                            flush=True,
                        )
                        _market_state_active = True
                elif _market_state_active and not (_starving and _mkt_hot):
                    print(
                        f"         [MarketState] ○ deactivated (starvation={_starving} vol=${_agg_vol_5m/1_000:.0f}k)",
                        flush=True,
                    )
                    _market_state_active = False

            _thinking["market_state"] = {
                "regime":        _volatility_regime,
                "active":        _market_state_active,
                "agg_vol_5m":    round(_agg_vol_5m),
                "floor_lowered": _saved_mom_floor is not None,
            }

            # ── TIER 1: OBSERVER — Phase 2: Score tokens with fresh regime ────────
            # ValidationProfile scoring runs here with the just-updated regime.
            # Any starvation-rescue floor change is already applied via MODES mutation above.
            # S87 FIX: break-even exits (|pnl%| < _FLAT_PCT, e.g. the flat dead-pool guard)
            # are NEUTRAL — exclude them from both numerator and denominator. Counting flats
            # as losses dragged fleet WR to 0% and tripped the WR-safety gate into an 8h
            # no-trade deadlock. WR = wins / (wins+losses); all-flat → 1.0 (gate stays open).
            _FLAT_PCT = 0.01
            _wr_wins = sum(1 for p in _recent_pnl_pcts if p >  _FLAT_PCT)
            _wr_loss = sum(1 for p in _recent_pnl_pcts if p < -_FLAT_PCT)
            # S107 FIX: extend the S87 "all-flat → 1.0" guard to "too-few-decisive → 1.0".
            # In a flat market the recent window is ~all dead-pool scalps (flat, excluded) plus a
            # couple of small stop-losses → 0 wins / few losses → WR reads a catastrophic 0% off a
            # tiny, unrepresentative decisive sample. That tripped the observer WR-safety gate
            # (floor 0.40→0.50) and locked out the whole 0.40–0.49 confidence band → progressive
            # no-trade deadlock (S87/S89's cousin via the WR-gate path). Require a minimum decisive
            # sample before the WR gate is allowed to bind; below it, WR is NEUTRAL so the gate stays
            # open and the bots can probe + re-earn a real WR. Safe-direction (can only re-open
            # admission a thin-sample false-alarm was closing); no sizing touched.
            _MIN_DECISIVE_WR = 5
            _wr_decisive = _wr_wins + _wr_loss
            # ★ WR-STALENESS GUARD (deadlock fix): the WR deque is per-process + in-memory, so when the
            # WR-safety gate locks (decisive WR < 45% → conf floor 0.40→0.50, blocking the 0.40–0.49 band)
            # AND the bot has no open positions, NO new closes can ever refill the deque → WR is frozen
            # below 45% forever → permanent no-trade halt (observed: ~10.5h idle, all 3 bots). S107 only
            # neutralised the THIN-sample case; this handles the GENUINE-low-WR-then-frozen case. A WR built
            # from closes older than _WR_STALE_SEC is stale → treat as NEUTRAL so an idle/locked bot can
            # PROBE and re-earn a real WR. Safe-direction (only re-opens admission a stale lock was closing);
            # no sizing / no ev_sizing touched.
            _WR_STALE_SEC = 1800   # 30 min with no decisive close ⇒ WR is stale ⇒ neutral, let it probe
            _wr_stale = (not _recent_trade_results) or (
                (datetime.now(timezone.utc) - _recent_trade_results[-1][0]).total_seconds() > _WR_STALE_SEC)
            if _wr_stale:
                _fleet_wr = 1.0
            else:
                _fleet_wr = _wr_wins / _wr_decisive if _wr_decisive >= _MIN_DECISIVE_WR else 1.0
            await _observer.score_tokens(client, _volatility_regime, sol_bal=sol_bal, fleet_wr=_fleet_wr)

            # Restore starvation-rescue floor now that scoring is done
            if _saved_mom_floor is not None:
                MODES["insane"]["MOMENTUM_MIN"] = _saved_mom_floor

            # _token_evals from Observer feeds Tier 1 dashboard panel
            _token_evals = _observer.token_evals

            # ── TIER 2: EXECUTIONER — evaluate Observer's hot list ───────────────
            # Only tokens that passed ValidationProfile reach stoic.evaluate().
            # Stack / ban / tx-cooldown checks still run here (need live state).
            _exec_evals: list[dict] = []  # Executioner decisions (for Tier 2 panel)
            for _obs_tok in _observer.get_hot_list():
                mint  = _obs_tok.mint
                # S79: defensive — a scored hot-list token can occasionally be absent from
                # market_data (discovery/scan race, pruned entry). Hard-subscripting here
                # KeyError'd and crashed the whole loop; skip the token instead. (Latent bug
                # exposed once the recalibrated low-vol regimes started qualifying tokens.)
                mdata = market_data.get(mint)
                if mdata is None:
                    continue
                sym   = _obs_tok.symbol

                _is_stack = False
                if mint in stoic.positions:
                    pos  = stoic.positions[mint]
                    conf = memory.confidence_score(mint)
                    cur  = mdata.get("price_usd", 0)
                    pnl_pct = ((cur - pos["entry_price"]) / pos["entry_price"] * 100) if pos.get("entry_price") else 0
                    if (mode == "insane"
                            and not _sizeup_on()   # S72: size-up evaluator is authoritative when on
                            and conf >= STACK_CONFIDENCE_MIN
                            and pos.get("stack_count", 0) < MAX_STACK_COUNT
                            and not pos.get("trail_active", False)
                            and pnl_pct > -1.0):
                        _is_stack = True
                    else:
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "held",
                                            "reason": "position open", "conf": round(conf, 2)})
                        continue

                _tok_conf = round(memory.confidence_score(mint), 2)

                if _tx_fail_count.get(mint, 0) >= 2:
                    _exec_evals.append({"mint": mint, "sym": sym, "action": "ban",
                                        "reason": "session ban", "conf": _tok_conf})
                    continue
                if _tx_fail_last_cycle.get(mint, -99) >= cycle - 1:
                    _exec_evals.append({"mint": mint, "sym": sym, "action": "ban",
                                        "reason": "tx cooldown", "conf": _tok_conf})
                    continue

                # ── Active Probe path ─────────────────────────────────────────
                # Probe tokens passed Observer's liq-growth filter, not ValidationProfile.
                # They have vel=0 so stoic.evaluate() would reject them on momentum gates.
                # Build the signal manually with the PROBE tier params (0.5% wallet cap).
                if _obs_tok.probe and mode in ("insane", "wild"):
                    if memory.is_banned(mint, cycle):
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "ban",
                                            "reason": "banned", "conf": _tok_conf})
                        continue
                    _p1h   = mdata.get("price_change_1h", 0)
                    _price = mdata.get("price_usd", 0)
                    if _p1h < -3.0:
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "skip",
                                            "reason": f"probe: 1h downtrend {_p1h:.1f}%",
                                            "conf": _tok_conf})
                        continue
                    if not _price:
                        continue
                    if _tok_conf < MODES[mode]["CONFIDENCE_MIN"]:
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "skip",
                                            "reason": f"probe: conf {_tok_conf:.2f} < min",
                                            "conf": _tok_conf})
                        continue
                    _tp = INSANE_TIER_PARAMS["probe"]
                    _mc_p = mdata.get("market_cap", 0) or 0
                    _liq_mc_p = ((mdata.get("liquidity_usd", 0) or 0) / _mc_p) if _mc_p > 0 else 0.0
                    sig = {
                        "mint":             mint,
                        "price":            _price,
                        "momentum_5m":      mdata.get("price_change_5m", 0),
                        "price_change_1h":  _p1h,
                        "liquidity_usd":    mdata.get("liquidity_usd", 0),
                        "volume_5m":        mdata.get("volume_5m", 0),
                        "confidence":       _tok_conf,
                        "buy_sell_ratio":   round(_obs_tok.buy_sell, 3),
                        "vol_acceleration": 1.0,
                        "rationale": (
                            f"PROBE liq+{_obs_tok.liq_growth_pct:.3f}% | "
                            f"liq ${mdata.get('liquidity_usd',0)/1000:.0f}k | "
                            f"conf {_tok_conf:.2f}"
                        ),
                        "viral_score":      mdata.get("viral_score", 0.0),
                        "gem_path":         mdata.get("gem_path", False),
                        "is_stack":         False,
                        "pair_age_hours":   mdata.get("pair_age_hours", 999.0),
                        "validation_score": _obs_tok.score,
                        "liq_mc":           round(_liq_mc_p, 6),
                        "insane_tier":      "probe",
                        "take_profit_pct":  _tp["take_profit_pct"],
                        "stop_loss_pct":    _tp["stop_loss_pct"],
                        "max_hold_hours":   _tp["max_hold_hours"],
                        "disc_cap_pct":     _tp["disc_cap_pct"],
                        "size_cap_pct":     _tp["size_cap_pct"],
                    }
                    raw_signals.append(sig)
                    _exec_evals.append({
                        "mint":   mint, "sym": sym, "action": "signal",
                        "reason": None, "conf": _tok_conf,
                        "mom":    round(mdata.get("price_change_5m", 0), 2),
                        "bs":     round(_obs_tok.buy_sell, 2),
                        "tier":   "probe",
                        "score":  round(_obs_tok.score, 1),
                    })
                    print(
                        f"  [{sym[:6]}] PROBE: liq+{_obs_tok.liq_growth_pct:.3f}%"
                        f" liq=${mdata.get('liquidity_usd',0)/1000:.0f}k"
                        f" conf={_tok_conf:.2f}",
                        flush=True,
                    )
                    continue

                # ── Mean-Reversion path ───────────────────────────────────────
                # Washout tokens passed Observer's dip+vol-spike filter.
                # stoic.evaluate() would reject them (1h-downtrend guard fires on negative mom).
                # Build the signal manually; size is capped at 2% of wallet.
                if _obs_tok.mean_reversion and mode == "insane":
                    if memory.is_banned(mint, cycle):
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "ban",
                                            "reason": "banned", "conf": _tok_conf})
                        continue
                    _price = mdata.get("price_usd", 0)
                    if not _price:
                        continue
                    if _tok_conf < MODES[mode]["CONFIDENCE_MIN"]:
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "skip",
                                            "reason": f"mr: conf {_tok_conf:.2f} < min",
                                            "conf": _tok_conf})
                        continue
                    _tp  = INSANE_TIER_PARAMS["mean_reversion"]
                    _p1h = mdata.get("price_change_1h", 0)
                    _bs  = _obs_tok.buy_sell
                    _mc_m = mdata.get("market_cap", 0) or 0
                    _liq_mc_m = ((mdata.get("liquidity_usd", 0) or 0) / _mc_m) if _mc_m > 0 else 0.0
                    sig  = {
                        "mint":             mint,
                        "price":            _price,
                        "momentum_5m":      mdata.get("price_change_5m", 0),
                        "price_change_1h":  _p1h,
                        "liquidity_usd":    mdata.get("liquidity_usd", 0),
                        "volume_5m":        mdata.get("volume_5m", 0),
                        "confidence":       _tok_conf,
                        "buy_sell_ratio":   round(_bs, 3),
                        "vol_acceleration": _obs_tok.mr_vol_accel,
                        "rationale": (
                            f"MR: {mdata.get('price_change_5m',0):.1f}% dip"
                            f" vol×{_obs_tok.mr_vol_accel:.1f}"
                            f" | 1h={_p1h:.1f}%"
                            f" | liq ${mdata.get('liquidity_usd',0)/1000:.0f}k"
                            f" | conf {_tok_conf:.2f}"
                        ),
                        "viral_score":      mdata.get("viral_score", 0.0),
                        "gem_path":         mdata.get("gem_path", False),
                        "is_stack":         False,
                        "pair_age_hours":   mdata.get("pair_age_hours", 999.0),
                        "validation_score": _obs_tok.score,
                        "liq_mc":           round(_liq_mc_m, 6),
                        "insane_tier":      "mean_reversion",
                        "take_profit_pct":  _tp["take_profit_pct"],
                        "stop_loss_pct":    _tp["stop_loss_pct"],
                        "max_hold_hours":   _tp["max_hold_hours"],
                        "disc_cap_pct":     _tp["disc_cap_pct"],
                        "size_cap_pct":     _tp["size_cap_pct"],
                    }
                    raw_signals.append(sig)
                    # Relay: share MR washout signal with WILD bot — it hasn't seen this dip
                    memory.write_relay_prime(
                        mint, primed_by=BOT_ID,
                        score=_obs_tok.score,
                        momentum=mdata.get("price_change_5m", 0),
                        tier="mean_reversion",
                    )
                    _exec_evals.append({
                        "mint":   mint, "sym": sym, "action": "signal",
                        "reason": None, "conf": _tok_conf,
                        "mom":    round(mdata.get("price_change_5m", 0), 2),
                        "bs":     round(_bs, 2),
                        "tier":   "mean_reversion",
                        "score":  round(_obs_tok.score, 1),
                    })
                    print(
                        f"  [{sym[:6]}] MR:"
                        f" {mdata.get('price_change_5m',0):.1f}% dip"
                        f" vol×{_obs_tok.mr_vol_accel:.1f}"
                        f" liq=${mdata.get('liquidity_usd',0)/1000:.0f}k"
                        f" conf={_tok_conf:.2f}",
                        flush=True,
                    )
                    continue

                # ── Force-Fire path ───────────────────────────────────────────
                # High-gradient pump.fun bonding curve token — pre-market entry.
                # Token may not be indexed by DexScreener yet; price comes from
                # bonding curve virtual reserves (computed in bonding_curve.py).
                # Bypasses ValidationProfile and stoic.evaluate().
                # RugCheck still runs in execute_buy() — all non-static tokens
                # go through it regardless of how they entered the hot list.
                if _obs_tok.force_fire and mode == "insane":
                    if memory.is_banned(mint, cycle):
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "ban",
                                            "reason": "banned", "conf": _tok_conf})
                        continue
                    # Price: prefer DexScreener if already indexed; fall back to BC math
                    _price = mdata.get("price_usd") or 0.0
                    if not _price:
                        _gsi = next(
                            (g for g in _observer.gradient_signals if g["mint"] == mint),
                            {},
                        )
                        _price = _gsi.get("price_usd", 0.0)
                    if not _price:
                        continue  # can't size without a price reference
                    if _tok_conf < MODES[mode]["CONFIDENCE_MIN"]:
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "skip",
                                            "reason": f"ff: conf {_tok_conf:.2f} < min",
                                            "conf": _tok_conf})
                        continue
                    _gsi  = next(
                        (g for g in _observer.gradient_signals if g["mint"] == mint), {}
                    )
                    _tp   = INSANE_TIER_PARAMS["force_fire"]
                    _bca  = _gsi.get("bc_activity", {})
                    sig   = {
                        "mint":             mint,
                        "price":            _price,
                        "momentum_5m":      0.0,
                        "price_change_1h":  0.0,
                        "liquidity_usd":    0.0,
                        "volume_5m":        0.0,
                        "confidence":       _tok_conf,
                        "buy_sell_ratio":   2.0,
                        "vol_acceleration": 1.0,
                        "rationale": (
                            f"FF ◎{_obs_tok.gradient_sol_per_min:.2f}/min"
                            f" stage={_gsi.get('real_sol',0):.0f}◎"
                            f" buys={_bca.get('buy_count_5m',0)}"
                            f" whale=◎{_bca.get('largest_buy',0):.2f}"
                        ),
                        "viral_score":      0.0,
                        "gem_path":         False,
                        "is_stack":         False,
                        "pair_age_hours":   mdata.get("pair_age_hours", 0.1),
                        "validation_score": 0.0,
                        "liq_mc":           0.0,  # BC token pre-DexScreener: no liq/mcap → fail-selective (safe)
                        "insane_tier":      "force_fire",
                        "take_profit_pct":  _tp["take_profit_pct"],
                        "stop_loss_pct":    _tp["stop_loss_pct"],
                        "max_hold_hours":   _tp["max_hold_hours"],
                        "disc_cap_pct":     _tp["disc_cap_pct"],
                        "size_cap_pct":     _tp["size_cap_pct"],
                    }
                    raw_signals.append(sig)
                    _exec_evals.append({
                        "mint": mint, "sym": sym, "action": "signal",
                        "reason": None, "conf": _tok_conf,
                        "tier":     "force_fire",
                        "gradient": _obs_tok.gradient_sol_per_min,
                        "score":    0.0,
                    })
                    print(
                        f"  [{sym[:6]}] FORCE-FIRE"
                        f" ◎{_obs_tok.gradient_sol_per_min:.2f}/min"
                        f" stage={_gsi.get('real_sol',0):.0f}◎"
                        f" conf={_tok_conf:.2f}",
                        flush=True,
                    )
                    continue

                # ── Deep-Pool path (S67) ──────────────────────────────────────
                # Liquid movers that matched Observer's brain-validated deep_pool_quality
                # filter (m5≥1% & liq/mcap≥0.10 & b/s≥1) but scored below the regime
                # threshold. stoic.evaluate() may reject them on the regime gate; build
                # the signal manually with the deep_pool tier params (15% wallet cap, -8% SL,
                # ride to +20%). RugCheck + risk checks still run in execute_buy().
                if _obs_tok.deep_pool and mode in ("insane", "wild", "stoic"):  # S74-bot3: STOIC trades the deep_pool edge too
                    if memory.is_banned(mint, cycle):
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "ban",
                                            "reason": "banned", "conf": _tok_conf})
                        continue
                    _p1h   = mdata.get("price_change_1h", 0)
                    _price = mdata.get("price_usd", 0)
                    if _p1h < -3.0:
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "skip",
                                            "reason": f"deep_pool: 1h downtrend {_p1h:.1f}%",
                                            "conf": _tok_conf})
                        continue
                    if not _price:
                        continue
                    # S74-bot3: stoic's 0.62 conf floor exceeds typical deep_pool token conf (~0.60),
                    # so it would reject the SAME tokens insane/wild trade. For edge entries, stoic uses
                    # the wild-level floor (0.50) so Bot3 actually trades the race edge. (Edit revert:
                    # drop the `if mode == "stoic"` line below.)
                    _conf_floor_dp = MODES[mode]["CONFIDENCE_MIN"]
                    if mode == "stoic":
                        _conf_floor_dp = min(_conf_floor_dp, 0.50)
                    if _tok_conf < _conf_floor_dp:
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "skip",
                                            "reason": f"deep_pool: conf {_tok_conf:.2f} < min",
                                            "conf": _tok_conf})
                        continue
                    _tp = INSANE_TIER_PARAMS["deep_pool"]
                    sig = {
                        "mint":             mint,
                        "price":            _price,
                        "momentum_5m":      mdata.get("price_change_5m", 0),
                        "price_change_1h":  _p1h,
                        "liquidity_usd":    mdata.get("liquidity_usd", 0),
                        "volume_5m":        mdata.get("volume_5m", 0),
                        "confidence":       _tok_conf,
                        "buy_sell_ratio":   round(_obs_tok.buy_sell, 3),
                        "vol_acceleration": 1.0,
                        "rationale": (
                            f"DEEP-POOL m5={mdata.get('price_change_5m',0):.1f}% | "
                            f"liq/mc={_obs_tok.deep_pool_liq_mc:.2f} | "
                            f"liq ${mdata.get('liquidity_usd',0)/1000:.0f}k | "
                            f"conf {_tok_conf:.2f}"
                        ),
                        "viral_score":      mdata.get("viral_score", 0.0),
                        "gem_path":         mdata.get("gem_path", False),
                        "is_stack":         False,
                        "pair_age_hours":   mdata.get("pair_age_hours", 999.0),
                        "validation_score": _obs_tok.score,
                        "liq_mc":           round(_obs_tok.deep_pool_liq_mc, 6),
                        "insane_tier":      "deep_pool",
                        "normal_slice":     getattr(_obs_tok, "normal_slice", False),  # S98-reconcile: tight normal slice → exempt from score floor, count toward gate/ride-A/B
                        "dust_shadow":      getattr(_obs_tok, "dust_shadow", False),  # S99: dust lane (skipped-regime, dust-sized, shadow-gate only)
                        "deep_pool_strong_rule": _obs_tok.deep_pool_strong_rule,  # S75: EV-sizing target
                        "take_profit_pct":  _tp["take_profit_pct"],
                        "stop_loss_pct":    _tp["stop_loss_pct"],
                        "max_hold_hours":   _tp["max_hold_hours"],
                        "disc_cap_pct":     _tp["disc_cap_pct"],
                        "size_cap_pct":     _tp["size_cap_pct"],
                    }
                    raw_signals.append(sig)
                    _exec_evals.append({
                        "mint":   mint, "sym": sym, "action": "signal",
                        "reason": None, "conf": _tok_conf,
                        "mom":    round(mdata.get("price_change_5m", 0), 2),
                        "bs":     round(_obs_tok.buy_sell, 2),
                        "tier":   "deep_pool",
                        "score":  round(_obs_tok.score, 1),
                    })
                    print(
                        f"  [{sym[:6]}] DEEP-POOL: m5={mdata.get('price_change_5m',0):.1f}%"
                        f" liq/mc={_obs_tok.deep_pool_liq_mc:.2f}"
                        f" liq=${mdata.get('liquidity_usd',0)/1000:.0f}k"
                        f" conf={_tok_conf:.2f}",
                        flush=True,
                    )
                    continue

                # ── Brain-rule registry path (S67) ────────────────────────────
                # Liquid movers admitted by the generalized brain-rule registry (any
                # brain candidate currently clearing the robustness bar). Mirrors the
                # deep_pool path; uses the brain_rule exit profile and tags the trade
                # with which rule admitted it, so edge_report can see what's working.
                if _obs_tok.brain_rule and mode in ("insane", "wild", "stoic"):  # S74-bot3
                    if memory.is_banned(mint, cycle):
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "ban",
                                            "reason": "banned", "conf": _tok_conf})
                        continue
                    _p1h   = mdata.get("price_change_1h", 0)
                    _price = mdata.get("price_usd", 0)
                    if _p1h < -3.0:
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "skip",
                                            "reason": f"brain_rule: 1h downtrend {_p1h:.1f}%",
                                            "conf": _tok_conf})
                        continue
                    if not _price:
                        continue
                    # S74-bot3: same edge-floor relaxation as the deep_pool path above.
                    _conf_floor_br = MODES[mode]["CONFIDENCE_MIN"]
                    if mode == "stoic":
                        _conf_floor_br = min(_conf_floor_br, 0.50)
                    if _tok_conf < _conf_floor_br:
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "skip",
                                            "reason": f"brain_rule: conf {_tok_conf:.2f} < min",
                                            "conf": _tok_conf})
                        continue
                    _tp = INSANE_TIER_PARAMS["brain_rule"]
                    _mc_b = mdata.get("market_cap", 0) or 0
                    _liq_mc_b = ((mdata.get("liquidity_usd", 0) or 0) / _mc_b) if _mc_b > 0 else 0.0
                    _rname = _obs_tok.brain_rule_name or "brain_rule"
                    sig = {
                        "mint":             mint,
                        "price":            _price,
                        "momentum_5m":      mdata.get("price_change_5m", 0),
                        "price_change_1h":  _p1h,
                        "liquidity_usd":    mdata.get("liquidity_usd", 0),
                        "volume_5m":        mdata.get("volume_5m", 0),
                        "confidence":       _tok_conf,
                        "buy_sell_ratio":   round(_obs_tok.buy_sell, 3),
                        "vol_acceleration": 1.0,
                        "rationale": (
                            f"BRAIN-RULE [{_rname}] m5={mdata.get('price_change_5m',0):.1f}% | "
                            f"liq/mc={_liq_mc_b:.2f} | "
                            f"liq ${mdata.get('liquidity_usd',0)/1000:.0f}k | "
                            f"conf {_tok_conf:.2f}"
                        ),
                        "viral_score":      mdata.get("viral_score", 0.0),
                        "gem_path":         mdata.get("gem_path", False),
                        "is_stack":         False,
                        "pair_age_hours":   mdata.get("pair_age_hours", 999.0),
                        "validation_score": _obs_tok.score,
                        "liq_mc":           round(_liq_mc_b, 6),
                        "insane_tier":      "brain_rule",
                        "brain_rule_name":  _rname,
                        "take_profit_pct":  _tp["take_profit_pct"],
                        "stop_loss_pct":    _tp["stop_loss_pct"],
                        "max_hold_hours":   _tp["max_hold_hours"],
                        "disc_cap_pct":     _tp["disc_cap_pct"],
                        "size_cap_pct":     _tp["size_cap_pct"],
                    }
                    raw_signals.append(sig)
                    _exec_evals.append({
                        "mint":   mint, "sym": sym, "action": "signal",
                        "reason": None, "conf": _tok_conf,
                        "mom":    round(mdata.get("price_change_5m", 0), 2),
                        "bs":     round(_obs_tok.buy_sell, 2),
                        "tier":   "brain_rule",
                        "rule":   _rname,
                        "score":  round(_obs_tok.score, 1),
                    })
                    print(
                        f"  [{sym[:6]}] BRAIN-RULE [{_rname}]:"
                        f" m5={mdata.get('price_change_5m',0):.1f}%"
                        f" liq/mc={_liq_mc_b:.2f} conf={_tok_conf:.2f}",
                        flush=True,
                    )
                    continue

                sig, skip_reason = stoic.evaluate(mint, mdata, cycle)
                if sig:
                    sig["viral_score"]       = mdata.get("viral_score", 0.0)
                    sig["gem_path"]          = mdata.get("gem_path", False)
                    sig["is_stack"]          = _is_stack
                    sig["pair_age_hours"]    = mdata.get("pair_age_hours", 999.0)
                    sig["pair_address"]      = mdata.get("pair_address")  # WASHVETO: GeckoTerminal tape key
                    sig["validation_score"]  = mdata.get("validation_score", 0.0)
                    sig["momentum_override"] = mdata.get("momentum_override", False)  # S107 runner lane: ×0.5 size + trail exit + score-gate exempt
                    _mc_s = mdata.get("market_cap", 0) or 0
                    sig["liq_mc"] = ((mdata.get("liquidity_usd", 0) or 0) / _mc_s) if _mc_s > 0 else 0.0

                    # S64: classify into a per-mode "play" and attach its TP/SL/hold/size/ride
                    # params. Applies to ALL modes now (was INSANE-only) so STOIC/WILD trades are
                    # tagged + sized by play too — the backbone of the per-trade intel.
                    # Stacks inherit the base position's params — no reclassification needed.
                    if not _is_stack:
                        _tok_stats  = memory.get_stats(mint)
                        _n_trades   = _tok_stats.get("trades", 0) if _tok_stats else 0
                        _play       = classify_play(
                            mode,
                            sig["confidence"],
                            sig.get("gem_path", False),
                            sig.get("buy_sell_ratio", 1.0),
                            _n_trades,
                            sig.get("vol_acceleration", 1.0),
                            relay_primed=sig.get("relay_primed", False),
                        )
                        _tp         = MODE_PLAYS[mode][_play]
                        sig["mode"]            = mode
                        sig["play"]            = _play
                        sig["insane_tier"]     = _play   # back-compat field name (label + param key)
                        sig["take_profit_pct"] = _tp["take_profit_pct"]
                        sig["stop_loss_pct"]   = _tp["stop_loss_pct"]
                        sig["max_hold_hours"]  = _tp["max_hold_hours"]
                        sig["disc_cap_pct"]    = _tp["disc_cap_pct"]
                        sig["size_cap_pct"]    = _tp["size_cap_pct"]
                        sig["ride"]            = _tp.get("ride", True)
                        print(
                            f"  [{mint[:6]}] {mode.upper()}/{_play.upper()}: "
                            f"TP={_tp['take_profit_pct']}% SL={_tp['stop_loss_pct']}% "
                            f"hold={_tp['max_hold_hours']}h ride={_tp.get('ride', True)} "
                            f"{'[GEM PATH]' if sig.get('gem_path') else ''}",
                            flush=True,
                        )

                        # ── admit_guard (S85 tool, wired live ADMITGUARD): skip a PROVEN −EV
                        #    (play × regime) cell when this bot's canary is live. The structural
                        #    bleed is the price-action book (gem/relay/momentum/bank) running in
                        #    the `normal` regime (~75% of the tape) where it is measured −EV. This
                        #    is the entry-side brake the WR-gate (removed by S114 WRSTALE) used to
                        #    incidentally provide. Safe-direction: can ONLY skip a proven −EV side
                        #    play; deep_pool/brain_rule are gate-protected inside admit_guard and
                        #    are NEVER skipped → the deploy gate / S110 hold are untouched.
                        #    No-op without bots/botN/admit_guard.json {"enabled":true,"mode":"live"}.
                        #    ★ EXEMPT the score-gate-exempt special lanes (RUNNEREDGE momentum_override
                        #    runner cohort + S98 normal_slice): they are a SEPARATE, validated +EV
                        #    cohort that merely shares a play label — admit_guard's gem/momentum×normal
                        #    cut must not eat them (the SPECIAL_LANES exemption is now enforced INSIDE
                        #    should_skip via the lane= arg, which also powers S121 allowlist mode).
                        _ag_lane = ("momentum_override" if sig.get("momentum_override")
                                    else ("normal_slice" if sig.get("normal_slice") else None))
                        _ag_skip, _ag_v = admit_guard.should_skip(_play, _volatility_regime, bot=BOT_ID, lane=_ag_lane)
                        if _ag_v is not None and _ag_v.action == "SKIP":
                            print(
                                f"  [ADMIT-GUARD] {mint[:6]} {_play}×{_volatility_regime} "
                                f"{_ag_v.reason} (n={_ag_v.n}, ev/tr {_ag_v.ev:+.4f}◎) "
                                f"{'→ SKIP' if _ag_skip else '(shadow/off — not enforced)'}",
                                flush=True,
                            )
                        if _ag_skip:
                            _exec_evals.append({
                                "mint": mint, "sym": sym, "action": "skip",
                                "reason": f"admit_guard −EV {_play}×{_volatility_regime}",
                                "conf": _tok_conf,
                            })
                            continue

                        # ── REGIMEPOLICY (staged, default-OFF): the evidence-derived play×DEPTH
                        #    policy surface (research/regime_rethink — the 5-band volume regime is
                        #    the wrong axis; the token's own pool depth is the stable conditioner).
                        #    Sits AFTER admit_guard/the S121 allowlist so it composes: it can only
                        #    ADD a skip, never admit past the freeze. deep_pool/brain_rule/
                        #    normal_slice/dust_shadow are hardcoded-frozen inside the module (S110).
                        #    Fail-open on any error/missing data. No-op without
                        #    bots/botN/regime_policy.json {"enabled":true}.
                        # TAXONOMY v2: the resolver now decides on the 4×3 grid
                        # (RUNNER/SCALP/DEEP/DUST × RISK_ON/NEUTRAL/RISK_OFF); dust-tagged
                        # signals are shadow (never blocked here).
                        if regime_policy.admission_on(BOT_ID):
                            _rp_skip, _rp_why = regime_policy.admission_skip(
                                BOT_ID, _play, _ag_lane,
                                sig.get("liquidity_usd") or mdata.get("liquidity_usd"),
                                dust=sig.get("dust_shadow", False))
                            if _rp_skip:
                                print(f"  [REGIME-POLICY] {mint[:6]} {_rp_why} → SKIP", flush=True)
                                _exec_evals.append({
                                    "mint": mint, "sym": sym, "action": "skip",
                                    "reason": f"regime_policy {_rp_why}",
                                    "conf": _tok_conf,
                                })
                                continue

                    _debate = debater.evaluate(sig, mdata, mode)
                    _thinking["debater"].append({
                        "mint":      mint,
                        "sym":       sym,
                        "tier":      sig.get("insane_tier"),
                        "aggregate": _debate.aggregate,
                        "threshold": _debate.threshold,
                        "trend":     _debate.trend_score,
                        "risk":      _debate.risk_score,
                        "sentiment": _debate.sentiment_score,
                        "clean":     _debate.risk_clean,
                        "passed":    _debate.passed,
                        "veto":      _debate.veto_reason,
                        "notes":     _debate.notes,
                    })
                    if not _debate.passed:
                        _veto_pfx = f"[{sig.get('insane_tier','?').upper()}] " if sig.get("insane_tier") else ""
                        print(
                            f"  [DEBATER] VETO {_veto_pfx}{mint[:8]}... "
                            f"agg={_debate.aggregate:.3f}≤thr={_debate.threshold:.3f} "
                            f"T={_debate.trend_score:.2f} R={_debate.risk_score:.2f} "
                            f"C={_debate.sentiment_score:.2f}"
                            + (f" | {_debate.veto_reason}" if _debate.veto_reason else ""),
                            flush=True,
                        )
                        _exec_evals.append({"mint": mint, "sym": sym, "action": "debater_veto",
                                            "reason": _debate.veto_reason or "debater", "conf": _tok_conf})
                        continue

                    raw_signals.append(sig)
                    # Relay: when INSANE fires a conviction signal (GEM/HIGHCONV), prime it
                    # for WILD bot.  QUICK signals are too low-conviction to relay.
                    # Stacks are not primed — WILD following a stack would be noise.
                    if (mode == "insane"
                            and not _is_stack
                            and sig.get("insane_tier") in ("gem", "highconv")):
                        memory.write_relay_prime(
                            mint, primed_by=BOT_ID,
                            score=sig.get("validation_score", 0),
                            momentum=sig.get("momentum_5m", 0),
                            tier=sig.get("insane_tier", "?"),
                        )
                    _exec_evals.append({
                        "mint":  mint, "sym": sym,
                        "action": "stack" if _is_stack else "signal",
                        "reason": None,
                        "mom":   round(sig["momentum_5m"], 2),
                        "conf":  round(sig["confidence"], 2),
                        "viral": round(sig["viral_score"], 2),
                        "gem":   sig["gem_path"],
                        "bs":    round(sig.get("buy_sell_ratio", 0), 2),
                        "tier":  sig.get("insane_tier"),
                        "score": round(sig["validation_score"], 1),
                    })
                else:
                    _exec_evals.append({"mint": mint, "sym": sym, "action": "skip",
                                        "reason": skip_reason or "?", "conf": _tok_conf})

            # Tag BC hot mints in exec_evals
            for _ev in _exec_evals:
                if _ev.get("mint") in _bc_hot_set:
                    _ev["bc_hot"] = True

            # Feed session history with this cycle's signals for percentile tracking
            for _s in raw_signals:
                _momentum_history.append(_s["momentum_5m"])
                _buysell_history.append(_s.get("buy_sell_ratio", 1.0))

            # Compute 80th-percentile thresholds (top 20%) once we have enough history.
            # Signals where BOTH momentum AND buy/sell ratio are in the top 20% of recent
            # activity get priority tier 0; all others get tier 1.
            def _pct(seq, p):
                s = sorted(seq)
                k = (len(s) - 1) * p / 100.0
                lo, hi = int(k), min(int(k) + 1, len(s) - 1)
                return s[lo] + (s[hi] - s[lo]) * (k - lo)

            _have_history = len(_momentum_history) >= 20
            _mom_p80 = _pct(list(_momentum_history), 80) if _have_history else None
            _bs_p80  = _pct(list(_buysell_history),  80) if _have_history else None

            # Viral multiplier for signal ranking:
            #   off    → viral score has no weight (×0)
            #   normal → standard weight (×1, current behaviour)
            #   boost  → doubled weight (×2, social buzz strongly promoted)
            _vw_mult = {"off": 0.0, "normal": 1.0, "boost": 2.0}.get(get_viral_weight(), 1.0)

            def _signal_quality(s):
                # Volume acceleration multiplier — cap at 3× so one explosive candle
                # doesn't dominate the ranking; floor at 0.5 so fading vol still fires.
                _va = max(0.5, min(s.get("vol_acceleration", 1.0), 3.0))
                return (s["confidence"]
                        * s["momentum_5m"]
                        * min(s.get("buy_sell_ratio", 1.0), 2.0)
                        * _va
                        * (1.0 + s.get("viral_score", 0.0) * _vw_mult))

            def _rank_key(s):
                # Tier 0 = elite (both momentum and b/s in top 20% of recent history)
                # Tier 1 = everything else; within each tier sort by quality score desc
                elite = (_have_history
                         and s["momentum_5m"] >= _mom_p80
                         and s.get("buy_sell_ratio", 0.0) >= _bs_p80)
                return (0 if elite else 1, -_signal_quality(s))

            raw_signals.sort(key=_rank_key)

            if _have_history and any(_rank_key(s)[0] == 0 for s in raw_signals):
                n_elite = sum(1 for s in raw_signals if _rank_key(s)[0] == 0)
                print(f"         [Percentile] {n_elite} elite signal(s) promoted "
                      f"(mom≥{_mom_p80:.2f}% AND b/s≥{_bs_p80:.2f})", flush=True)

            _thinking["percentile"] = {
                "have_history": _have_history,
                "mom_p80": round(_mom_p80, 3) if _mom_p80 is not None else None,
                "bs_p80":  round(_bs_p80, 3) if _bs_p80 is not None else None,
            }

            # ── Circuit breaker — consecutive-loss cool-off ──────────────────────
            # Computed here (before the tier snapshots) so tier2_executioner captures
            # the live state rather than always seeing an empty dict.
            global _circuit_breaker_until
            _now_cb = datetime.now(timezone.utc)
            _window_start = _now_cb - timedelta(minutes=CIRCUIT_BREAKER_WINDOW_M)
            _recent_losses = sum(1 for _t, _w in _recent_trade_results if not _w and _t > _window_start)
            _cb_active = _circuit_breaker_until is not None and _now_cb < _circuit_breaker_until
            if _recent_losses >= CIRCUIT_BREAKER_LOSSES and not _cb_active:
                _circuit_breaker_until = _now_cb + timedelta(minutes=CIRCUIT_BREAKER_COOLOFF_M)
                _cb_active = True
                print(
                    f"         [CIRCUIT] {_recent_losses} losses in {CIRCUIT_BREAKER_WINDOW_M}m "
                    f"→ entry freeze for {CIRCUIT_BREAKER_COOLOFF_M}m "
                    f"(until {_circuit_breaker_until.strftime('%H:%M:%S')})",
                    flush=True,
                )
            elif _cb_active:
                _remaining_cb = (_circuit_breaker_until - _now_cb).seconds // 60
                print(f"         [CIRCUIT] Breaker active — {_remaining_cb}m remaining, no new entries", flush=True)

            _cb_remaining_m = int((_circuit_breaker_until - _now_cb).total_seconds() // 60) if (_cb_active and _circuit_breaker_until) else 0
            _thinking["circuit_breaker"] = {
                "active":        _cb_active,
                "remaining_m":   _cb_remaining_m,
                "losses":        _recent_losses,
                "threshold":     CIRCUIT_BREAKER_LOSSES,
                "window_m":      CIRCUIT_BREAKER_WINDOW_M,
                "cooloff_m":     CIRCUIT_BREAKER_COOLOFF_M,
            }

            # ── Update tier snapshot keys now that all data is available ─────────
            _obs_snap = _observer.snapshot()
            _thinking["tier1_observer"] = {
                **_obs_snap,
                "market_state": _thinking.get("market_state", {}),
            }
            _thinking["tokens"] = _obs_snap.get("token_evals", [])  # backward compat
            _thinking["tier2_executioner"] = {
                "exec_evals":    _exec_evals,
                "entry_queue":   _entry_queue.snapshot(),
                "circuit_breaker": _thinking.get("circuit_breaker", {}),
                "percentile":    _thinking["percentile"],
                "revert_rate":   round(revert_rate, 3),
                "guard_blocked": revert_rate > 0.20,
                "paused":        PAUSE_FLAG.exists(),
                "halted":        risk.is_halted(),
            }
            _thinking["tier3_auditor"] = _auditor.snapshot()

            # Step 2: compute deployment ceiling
            # Hard cap: 75% of wallet across all open positions (all modes).
            # Per-trade size is governed by the size tier toggle + confidence.
            dynamic_cap = MAX_DEPLOYED_SOL_PCT

            max_deployable = max(0.0, sol_bal - RESERVE_SOL) * dynamic_cap
            available_sol  = max(0.0, max_deployable - sol_in_trades)
            deployed_pct   = sol_in_trades / sol_bal * 100 if sol_bal else 0
            print(
                f"         Capital: ◎{sol_bal:.4f} total | ◎{sol_in_trades:.4f} deployed "
                f"({deployed_pct:.1f}%) | cap={dynamic_cap*100:.0f}% | "
                f"◎{available_sol:.4f} headroom",
                flush=True,
            )

            # Size tier must be read before _thinking["capital"] and the sizing loop
            _size_tier_max = SIZE_TIER_PCTS.get(get_size_tier(), 0.50)

            # ── Profit Lock: home-stretch capital protection ──────────────────
            # When the total portfolio (liquid + deployed) reaches 75% of the active
            # payout milestone, cap trade size at 25% MAX for the remainder of the cycle.
            # Override is in-memory only — the user's configured tier is preserved on disk
            # and auto-size / EV-downsize logic continues to run normally in the background.
            # The lock lifts automatically if the balance falls back below the threshold
            # (e.g. after a drawdown) so it never permanently strands the bot at small size.
            global _profit_lock_active
            _cur_milestone        = payout.get_milestone(payout.get_payout_count())
            _portfolio_total      = sol_bal + sol_in_trades
            _lock_threshold       = _cur_milestone["threshold_sol"] * 0.75
            _small_tier_max       = SIZE_TIER_PCTS.get("small", 0.25)
            if _portfolio_total >= _lock_threshold:
                if _size_tier_max > _small_tier_max:
                    _size_tier_max = _small_tier_max
                if not _profit_lock_active:
                    _profit_lock_active = True
                    print(
                        f"         [ProfitLock] ◆ ◎{_portfolio_total:.4f} ≥ ◎{_lock_threshold:.2f} "
                        f"(75% of ◎{_cur_milestone['threshold_sol']:.0f} milestone) "
                        f"→ size locked at 25% until payout",
                        flush=True,
                    )
            elif _profit_lock_active:
                _profit_lock_active = False
                print(
                    f"         [ProfitLock] ○ ◎{_portfolio_total:.4f} < ◎{_lock_threshold:.2f} "
                    f"— lock released",
                    flush=True,
                )

            # Populate capital section now that all values are computed
            _thinking["capital"] = {
                "sol_bal":          round(sol_bal, 4),
                "deployed":         round(sol_in_trades, 4),
                "deployed_pct":     round(deployed_pct, 1),
                "available":        round(available_sol, 4),
                "max_deployed_pct": int(dynamic_cap * 100),
                "size_tier":        get_size_tier(),
                "size_tier_pct":    int(_size_tier_max * 100),
                "profit_lock":      _profit_lock_active,
            }

            # Step 3: size and fire — INSANE and STOIC execute all qualifying signals in quality
            # order, re-sizing each from remaining headroom so the 50% ceiling is never breached.
            # Per-cycle trade limits reflect each mode's trigger finger:
            #   STOIC:  1   — one precise shot per cycle
            #   WILD:   3   — up to 3 per cycle, 2h hold
            #   INSANE: all qualifying — maximum velocity, capital ceiling is the real cap
            signals = []
            remaining = available_sol
            if mode == "stoic":
                max_signals = 1
            elif mode == "wild":
                max_signals = 3
            else:
                max_signals = len(raw_signals)

            # Concurrent position cap — prevents long-hold modes (WILD: 2h) from
            # stacking more open positions than INSANE (45m), which inverts the
            # expected aggression order.
            _max_concurrent = MODES[mode].get("MAX_CONCURRENT", 10)
            # S99: dust-shadow positions are EXEMPT from the real concurrent-position cap — a
            # ◎0.01 evidence probe must never block a real-capital entry. Count only real
            # positions against the cap; dust is bounded separately (_DUST_MAX_CONCURRENT, applied
            # at sizing). Default-OFF → no dust positions exist → byte-for-byte current behaviour.
            _real_open = sum(1 for _p in stoic.positions.values() if not _p.get("dust_shadow"))
            _open_slots = max(0, _max_concurrent - _real_open)
            max_signals = min(max_signals, _open_slots)
            if _open_slots == 0:
                print(f"         [CAP] {_real_open}/{_max_concurrent} positions open — skipping new entries", flush=True)

            _prestige_pending = payout.is_prestige_pending()
            if PAUSE_FLAG.exists():
                print(f"         ⏸ PAUSED — holding, no new entries", flush=True)
            if _prestige_pending:
                print(f"         ⏳ PRESTIGE PENDING — no new entries until payout clears", flush=True)

            # ── Fleet brain: goldilocks auto-trigger every 20 closes ─────────────
            # Runs goldilocks --emit-override in a background subprocess so it never
            # blocks the trading loop. On the NEXT cycle, config.load_overrides() picks
            # up the new file and strategy_apply_overrides() mutates the live dicts.
            global _goldilocks_trade_count, _auto_size_baseline
            if _goldilocks_trade_count >= 20:
                _goldilocks_trade_count = 0
                print(f"         [Goldilocks] 20 closes reached — running optimizer...", flush=True)
                asyncio.create_task(_run_goldilocks_and_reload())
                _auditor.push("goldilocks_ran", {"ts": time.time()})

            # Apply overrides from any file goldilocks wrote on a previous cycle.
            # _cfg_mgr.reload() re-merges base.json + thresholds_override.json so
            # cfg() calls in wallet.py and elsewhere pick up the latest values too.
            _cfg_mgr.reload()
            _fresh = _config_mod.load_overrides()
            if _fresh:
                strategy_apply_overrides(_fresh)

            # ── Self-optimization: auto-adjust trade size on negative rolling EV ──
            # When the last 20 real closes average negative EV, step trade size down
            # one tier. Restores automatically once EV turns positive again.
            if len(_recent_pnl_pcts) >= 20:
                _rolling_ev = sum(_recent_pnl_pcts) / 20
                _current_tier = get_size_tier()
                _step_down_map = {"large": "medium", "medium": "small"}

                if _auto_size_baseline is None:
                    # Not currently auto-managed — check whether to step down
                    if _rolling_ev < 0 and _current_tier in _step_down_map:
                        _next_tier = _step_down_map[_current_tier]
                        _auto_size_baseline = _current_tier
                        set_size_tier(_next_tier)
                        print(
                            f"         [AutoSize] ⬇ Rolling EV {_rolling_ev:+.2f}% over 20 trades "
                            f"→ size tier {_current_tier} → {_next_tier}",
                            flush=True,
                        )
                else:
                    # Auto-managed — check for manual override or recovery
                    _expected_tier = _step_down_map.get(_auto_size_baseline)
                    if _current_tier != _expected_tier:
                        # Operator manually changed tier — stop managing
                        _auto_size_baseline = None
                        print(f"         [AutoSize] Manual override detected — auto-management cleared", flush=True)
                    elif _rolling_ev >= 0:
                        # EV recovered — restore baseline tier
                        set_size_tier(_auto_size_baseline)
                        print(
                            f"         [AutoSize] ↑ Rolling EV {_rolling_ev:+.2f}% recovered "
                            f"→ size tier restored to {_auto_size_baseline}",
                            flush=True,
                        )
                        _auto_size_baseline = None

            # Confidence → size fraction mapping: [mode_min, 1.0] → [SIZE_BASE_FRACTION, 1.0]
            # _size_tier_max is already set above (before _thinking["capital"]) so the
            # capital display and the actual sizing use identical values.
            _conf_min = MODES[mode]["CONFIDENCE_MIN"]
            _conf_span = max(1.0 - _conf_min, 0.01)

            # ── Update per-cycle globals for cost_to_prestige in execute_sell ─────
            global _cur_sol_balance, _cur_sol_in_trades, _cur_milestone_sol, _sniper_gate_active
            _cur_sol_balance   = sol_bal
            _cur_sol_in_trades = sol_in_trades
            _cur_milestone_sol = _cur_milestone["threshold_sol"]

            # ── Session loss sniper gate ──────────────────────────────────────────
            # When session PnL drops below -0.05 SOL, require conf ≥ 0.85 to enter.
            # Prevents the bot from digging deeper on a bad-run day.
            _sniper_gate_active = _session_pnl_sol < _SESSION_LOSS_GATE_SOL
            if _sniper_gate_active:
                print(
                    f"         [SNIPER-GATE] Session ◎{_session_pnl_sol:+.4f} < ◎{_SESSION_LOSS_GATE_SOL}"
                    f" — entries require conf ≥ {_SESSION_SNIPER_CONF_MIN:.0%}",
                    flush=True,
                )

            _entries_blocked = revert_rate > 0.20 or PAUSE_FLAG.exists() or _prestige_pending or _cb_active
            for sig in ([] if _entries_blocked else raw_signals[:max_signals]):
                if remaining < MIN_POSITION_SOL:
                    break
                eff_conf = sig["confidence"]

                # Sniper gate: deep session losses require high conviction
                if _sniper_gate_active and eff_conf < _SESSION_SNIPER_CONF_MIN and not sig.get("is_stack"):
                    print(
                        f"         [SNIPER-GATE] {sig['mint'][:6]}... conf {eff_conf:.2f}"
                        f" < {_SESSION_SNIPER_CONF_MIN:.0%} — skipped",
                        flush=True,
                    )
                    continue

                _conf_norm = max(0.0, min(1.0, (eff_conf - _conf_min) / _conf_span))
                # Square the normalized confidence — exponential size drop-off for low-conv signals.
                # Low confidence (~0.40–0.60) gets a far smaller share of capital than before.
                # High confidence (≥0.80) is barely affected — it already had good sizing.
                _conf_sq   = _conf_norm ** 2
                # S79: regime-aware size posture — distinct stance per market condition
                # (replaces the old continuous max(0.35,(agg/500k)^0.5) vol curve). Each of the
                # 5 regimes gets its own multiplier (_REGIME_SIZE_MULT). Kept in the var name
                # `_vol_scale` so the downstream EV-sizing override (which multiplies by it) is
                # unchanged. Unknown regime → NORMAL default.
                _vol_scale = _REGIME_SIZE_MULT.get(_volatility_regime, _REGIME_SIZE_MULT["normal"])
                # REGIMEPOLICY (staged, default-OFF): when the sizing layer is enabled the
                # tape-level posture comes from the evidence table instead of the 5-band volume
                # mult (whose euphoria/aggressive up-weighting is the INVERTED assumption —
                # research/regime_rethink FINDINGS §2), and the depth-keyed cell mult below
                # replaces BLEED-TRIM's vol-keyed trim. Canary off → _vol_scale byte-identical.
                _rp_sizing = regime_policy.sizing_on(BOT_ID)
                if _rp_sizing:
                    _vol_scale = regime_policy.tape_mult(BOT_ID)
                _size_frac = (SIZE_BASE_FRACTION + _conf_sq * (1.0 - SIZE_BASE_FRACTION)) * _vol_scale

                # S79 (option C): signal-quality nudge — the 0–100 validation score moves size
                # within a tight ±15% band so a stronger signal bets modestly more than a
                # marginal one (C² alone can't, since fresh tokens all default to conf 0.6).
                # Neutral when scoreless (force_fire). Bounded — never the EV-sizing override.
                _vscore = sig.get("validation_score", 0.0) or 0.0
                if _vscore > 0.0:
                    _sn_t = max(0.0, min(1.0, (_vscore - _SCORE_NUDGE_REF_LO)
                                         / (_SCORE_NUDGE_REF_HI - _SCORE_NUDGE_REF_LO)))
                    _score_nudge = round(_SCORE_NUDGE_LO + _sn_t * (_SCORE_NUDGE_HI - _SCORE_NUDGE_LO), 3)
                else:
                    _score_nudge = 1.0
                _size_frac *= _score_nudge

                # S98 (gate-unfreeze): the `normal` deep_pool slice is an UNPROVEN gate-accumulation
                # cohort (canary) — size it DOWN as a precaution. Keyed on deep_pool+normal, which is
                # DEFINITIONALLY the slice (nothing else admits deep_pool in `normal` — observer.py
                # _dp_normal_slice). Applies to C² sizing; if EV-sizing later arms (gate open) it
                # overwrites _size_frac by edge below, so this only governs the proving phase.
                if sig.get("insane_tier") == "deep_pool" and _volatility_regime == "normal":
                    _size_frac *= _NORMAL_DP_SIZE_MULT
                    print(f"         [NORMAL-DP] {sig['mint'][:6]}... gate-accum slice"
                          f" → size ×{_NORMAL_DP_SIZE_MULT} (reduced, unproven)", flush=True)

                # S107: the momentum-override runner lane is an UNPROVEN low-liq cohort (high price-EV,
                # real ghost risk) — size it DOWN while it proves live (the liq belt + rug_screen +
                # trail exit are the other controls). C²-only; EV-sizing supersedes if armed.
                # TAXONOMY: when the v2 policy canary is ON, the RUNNER cell's size_mult=0.5
                # ABSORBS this lane mult (applied via size_mult() below) — skip it here so the
                # net trim stays exactly ×0.5, never ×0.25 (no double-trim).
                if sig.get("momentum_override") and not _rp_sizing:
                    _size_frac *= _MOMENTUM_OVERRIDE_SIZE_MULT
                    print(f"         [MOM-OVERRIDE] {sig['mint'][:6]}... runner lane"
                          f" → size ×{_MOMENTUM_OVERRIDE_SIZE_MULT} (reduced, unproven)", flush=True)

                # BLEEDTRIM: size DOWN the four normal-regime books that carry the realized loss
                # (gem/momentum/relay/bank × normal — each net-negative, killed by a stop-loss tail).
                # Each pocket is net <0 so this strictly shrinks the ◎ bleed; the +EV body keeps
                # contributing at half size. `normal` only (relay×aggressive is +EV, left alone);
                # gem×sniper already skipped upstream; deep_pool/brain_rule never matched here.
                # NOT a normal_slice/momentum_override (those already got their own ×0.5 above).
                if (sig.get("insane_tier") in _BLEED_TRIM_POCKETS
                        and _volatility_regime == "normal"
                        and not sig.get("normal_slice")
                        and not sig.get("momentum_override")
                        and not _rp_sizing):   # REGIMEPOLICY: the depth-keyed cell mult supersedes this vol-keyed trim
                    _size_frac *= _BLEED_TRIM_SIZE_MULT
                    print(f"         [BLEED-TRIM] {sig['mint'][:6]}... {sig.get('insane_tier')}·normal"
                          f" → size ×{_BLEED_TRIM_SIZE_MULT} (−EV pocket, tail-trimmed)", flush=True)

                # TAXONOMY v2: the cell size mult from the 4×3 policy table. Trim-only
                # (clamped ≤1.0 in the module → C² multiplier, THE ONE OPERATOR RULE intact;
                # EV-sizing would supersede if it ever armed). The RUNNER cell's 0.5 absorbs
                # the legacy lane mult (skipped above when _rp_sizing); normal_slice keeps its
                # own ×0.5 (S98 — frozen DEEP row in the module, admission blocks it anyway).
                if _rp_sizing and not sig.get("normal_slice"):
                    _rp_mult, _rp_mwhy = regime_policy.size_mult(
                        BOT_ID, sig.get("insane_tier"),
                        "momentum_override" if sig.get("momentum_override") else None,
                        sig.get("liquidity_usd"))
                    if _rp_mult < 1.0:
                        _size_frac *= _rp_mult
                        print(f"         [REGIME-POLICY] {sig['mint'][:6]}... {_rp_mwhy}"
                              f" → size ×{_rp_mult} (depth-keyed cell)", flush=True)

                # ── EV-weighted sizing (DORMANT) — size a brain-validated entry by the rule's
                # measured edge, not the (defaulted) token confidence. See _EV_WEIGHTED_SIZING_ENABLED.
                # Flag OFF → this block is skipped → _size_frac keeps its C² value (byte-for-byte current).
                # S75: size up ONLY on strong, stable sub-cohorts (filling/strict). The rule is
                # resolved from the admitting brain_rule OR the deep_pool sub-cohort tag — NOT the
                # weak parent insane_tier="deep_pool" (deep_pool_quality ev_lo +1, H2 decaying).
                # ev_strong_fraction_of_cap returns None for plain deep_pool / any decayed rule,
                # so those fall through to the small C² sizing untouched.
                _ev_sized = False
                if (_ev_sizing_on() and _ev_strong_frac is not None
                        and not sig.get("is_stack") and sig.get("size_cap_pct")):
                    _rule = sig.get("brain_rule_name") or sig.get("deep_pool_strong_rule")
                    # S86: pass the live regime so sizing reads the regime-ISOLATED edge
                    # (by_regime_lo) — the blended ev_lo is poisoned by normal/dead obs the fleet
                    # no longer trades, which would otherwise keep the gene from ever expressing.
                    _evfrac = _ev_strong_frac(_rule, _volatility_regime)   # fraction of cap by ev_lo, strong+stable only (regime-aware)
                    if _evfrac is not None:
                        # Target wallet fraction = ev_frac × play_cap × market vol_scale. Re-express as a
                        # fraction of the dashboard tier so the existing raw_size pipeline lands on target;
                        # all downstream caps (disc, per-play ceiling, headroom) still apply as guards.
                        _gene_scale = _ev_sizing_scale()   # SIZE-GENE damping (race): 1.0 full, <1 moderate
                        _target_wallet_frac = _evfrac * sig["size_cap_pct"] * _vol_scale * _gene_scale
                        _size_frac = _target_wallet_frac / max(_size_tier_max, 1e-9)
                        _ev_sized = True
                        _gene_tag = f" gene×{_gene_scale:.2f}" if _gene_scale < 1.0 else ""
                        print(f"         [EV-SIZE] {sig['mint'][:6]}... {_rule} ev-frac {_evfrac:.2f}×cap{_gene_tag}"
                              f" → target {_target_wallet_frac*100:.1f}% wallet (vol×{_vol_scale:.2f})", flush=True)

                # Conviction multiplier — elite signals get a larger initial position.
                # BC whale + high confidence + strong buy pressure = pre-trend entry with edge.
                # Elite percentile (top 20% both momentum and b/s) gets a smaller bonus.
                # Both are capped by `remaining` headroom — 75% ceiling is never exceeded.
                _bc_whale = (sig["mint"] in _bc_hot_set
                             and eff_conf >= 0.75
                             and sig.get("buy_sell_ratio", 0) >= 2.0)
                _elite    = _rank_key(sig)[0] == 0 and eff_conf >= 0.65
                if mode == "insane" and not sig.get("is_stack") and _bc_whale:
                    _conv_mult = CONVICTION_BC_WHALE_MULT
                    print(f"         [CONVICTION] ◆BC whale + conf {eff_conf:.2f} → {_conv_mult}× size", flush=True)
                elif not sig.get("is_stack") and _elite:
                    _conv_mult = CONVICTION_ELITE_MULT
                    print(f"         [CONVICTION] Elite percentile → {_conv_mult}× size", flush=True)
                else:
                    _conv_mult = 1.0

                raw_size = _size_frac * _size_tier_max * sol_bal * _conv_mult
                size_sol = round(min(remaining, max(MIN_POSITION_SOL, raw_size)), 6)

                # Discovery cap: tokens with < 3 trades are unproven.
                # INSANE tiers have their own disc_cap_pct (quick=8%, gem=15%, highconv=30%).
                # Other modes use the flat 8% floor.
                if sig["mint"] not in static_set and not sig.get("is_stack"):
                    _disc_hist = memory.get_stats(sig["mint"])
                    _disc_trades = _disc_hist.get("trades", 0) if _disc_hist else 0
                    if _disc_trades < 3:
                        _disc_frac = sig.get("disc_cap_pct", 0.08)
                        _disc_max  = round(sol_bal * _disc_frac, 6)
                        if size_sol > _disc_max:
                            _tier_tag = f"[{sig['insane_tier'].upper()}] " if sig.get("insane_tier") else ""
                            print(
                                f"         [DISC CAP] {_tier_tag}{sig['mint'][:6]}... "
                                f"{_disc_trades} trades → ◎{size_sol:.4f} → ◎{_disc_max:.4f} "
                                f"({_disc_frac*100:.0f}% cap)",
                                flush=True,
                            )
                            size_sol = max(MIN_POSITION_SOL, _disc_max)

                # S64: per-play wallet ceiling — now applies to ALL modes (was INSANE-only).
                # STOIC bank (15%) stays tiny; INSANE gem (60%)/highconv (no cap) concentrate.
                # A None cap means "use the dashboard size tier" (INSANE highconv only).
                if not sig.get("is_stack"):
                    _tier_cap_pct = sig.get("size_cap_pct")
                    if _tier_cap_pct is not None:
                        _tier_ceil = round(sol_bal * _tier_cap_pct, 6)
                        if size_sol > _tier_ceil:
                            print(
                                f"         [PLAY CAP] {mode.upper()}/{sig.get('insane_tier','').upper()} "
                                f"{sig['mint'][:6]}... ◎{size_sol:.4f} → ◎{_tier_ceil:.4f} "
                                f"({_tier_cap_pct*100:.0f}% wallet ceiling)",
                                flush=True,
                            )
                            size_sol = max(MIN_POSITION_SOL, _tier_ceil)

                # ── S99: dust-shadow override — force the size to DUST regardless of all the
                # sizing/caps above (this is an evidence probe, not a capital allocation). Applied
                # LAST so it deterministically lands on ~◎0.01. Bounded by its own concurrent cap so
                # dust can't sprawl across the wallet/RPC. Default-OFF (observer never tags dust) →
                # this whole block is skipped → byte-for-byte current behaviour.
                if sig.get("dust_shadow"):
                    _dust_open = sum(1 for _p in stoic.positions.values() if _p.get("dust_shadow"))
                    if _dust_open >= _DUST_MAX_CONCURRENT:
                        print(f"         [DUST-CAP] {sig['mint'][:6]}... {_dust_open}/{_DUST_MAX_CONCURRENT}"
                              f" dust positions open — skip", flush=True)
                        continue
                    size_sol = round(max(MIN_POSITION_SOL, _DUST_SHADOW_SOL), 6)
                    print(f"         [DUST-SHADOW] {sig['mint'][:6]}... gate-evidence probe →"
                          f" ◎{size_sol:.4f} ({_volatility_regime[:4]}, {sig.get('deep_pool_strong_rule') or 'deep_pool'})",
                          flush=True)

                _tier_suffix = f" [{sig['insane_tier'].upper()}]" if sig.get("insane_tier") else ""
                print(
                    f"         [SIZE]{_tier_suffix} {sig['mint'][:6]}... "
                    f"conf={eff_conf:.2f} C²={_conf_sq:.2f} {_volatility_regime[:4]}×{_vol_scale:.2f}"
                    f" score={_vscore:.0f}→sn×{_score_nudge:.2f}"
                    f" → {_size_frac*100:.0f}% of {_size_tier_max*100:.0f}% tier "
                    f"× ◎{sol_bal:.4f} = ◎{size_sol:.4f}",
                    flush=True,
                )

                # Stacks use half the computed size — building into a winner, not doubling blind
                if sig.get("is_stack"):
                    size_sol = round(max(MIN_POSITION_SOL, size_sol * STACK_SIZE_MULTIPLIER), 6)
                    pos = stoic.positions[sig["mint"]]
                    print(
                        f"         [STACK #{pos.get('stack_count',0)+1}] {sig['mint'][:6]}... "
                        f"conf={sig['confidence']:.2f} pnl={(((market_data[sig['mint']].get('price_usd',0)-pos['entry_price'])/pos['entry_price'])*100):+.1f}% "
                        f"→ adding ◎{size_sol:.4f}",
                        flush=True,
                    )

                sig["size_sol"] = size_sol
                sig["size_usd"] = round(size_sol * sol_price, 4)
                # S72: size as a share of wallet → drives the size-aware breakeven ratchet
                # in check_exits. Computed here where size_sol + sol_bal are both in scope;
                # carried through the queue into open_position (atomic + TWAP paths).
                if not sig.get("is_stack") and sol_bal > 0:
                    sig["wallet_frac"] = round(size_sol / sol_bal, 4)
                sig["_cycle"]   = cycle   # carried into execute_buy via queue
                signals.append(sig)
                _auditor.push("signal", {"mint": sig["mint"], "momentum": sig["momentum_5m"], "price": sig["price"], "rationale": sig["rationale"], "vol_5m": sig.get("volume_5m")})
                # Force-fire signals jump to the front — they are the most time-sensitive
                if sig.get("insane_tier") == "force_fire":
                    _entry_queue.put_front(sig)
                else:
                    _entry_queue.put(sig)
                remaining = max(0.0, remaining - size_sol)

            # ── S72: brain-driven mid-trade size-up (pyramiding into BE-protected winners) ──
            # Runs AFTER new entries are sized (size-ups use only leftover headroom) and AFTER
            # check_exits armed the BE ratchet this cycle. Dormant unless the per-bot canary is
            # on; insane/wild only; only ever ADDS to a BE-protected winner still making highs in
            # a non-draining pool that shows fresh conviction (fill / surge / whale). Routes
            # through execute_buy's proven stack path (add_to_position) — RugCheck/risk all apply.
            if _sizeup_on() and not _entries_blocked and mode in ("insane", "wild"):
                for _su_mint, _su_pos in list(stoic.positions.items()):
                    if remaining < MIN_POSITION_SOL:
                        break
                    # ── precondition gate (the "not greedy" guard) ──
                    _sl = _su_pos.get("sl_floor_pct")
                    if _sl is None or _sl < 0:                 # must be BE-protected (can't lose)
                        continue
                    if _su_pos.get("sizeup_count", 0) >= _SIZEUP_MAX_ADDS:
                        continue
                    if _su_pos.get("trail_active"):            # trail is already managing the exit
                        continue
                    _md = market_data.get(_su_mint)
                    if not _md:
                        continue
                    _cur   = _md.get("price_usd", 0) or 0
                    _entry = _su_pos.get("entry_price", 0) or 0
                    if _cur <= 0 or _entry <= 0:
                        continue
                    _pnl = (_cur - _entry) / _entry * 100
                    if _pnl <= 0:                              # only add to a live winner
                        continue
                    if _cur < (_su_pos.get("peak_price", _cur) or _cur) * _SIZEUP_NEAR_PEAK:
                        continue                              # must be near its high (move still alive)
                    if memory.is_banned(_su_mint, cycle):
                        continue
                    # don't add into a draining pool (rug guard) — reuse the liq_history feed
                    _lh   = _su_pos.get("liq_history", [])
                    _vliq = ((_lh[-1] - _lh[0]) / _lh[0]) if len(_lh) >= 2 and _lh[0] > 0 else 0.0
                    if _vliq < 0:
                        continue
                    # ── MODERATE trigger: filling OR buy-pressure/vol surge OR fresh whale ──
                    _bs   = _md.get("buy_sell_ratio", 0) or 0
                    _vh   = _su_pos.get("vol_5m_history", [])
                    _vacc = (_vh[-1] / (sum(_vh) / len(_vh))) if len(_vh) >= 3 and sum(_vh) > 0 else 1.0
                    _trigs = []
                    if _vliq > _SIZEUP_FILL_THRESH:
                        _trigs.append("fill")
                    if _bs >= _SIZEUP_BS_SURGE or _vacc >= _SIZEUP_VACC_SURGE:
                        _trigs.append("surge")
                    if _su_mint in _bc_hot_set:
                        _trigs.append("whale")
                    if not _trigs:
                        continue
                    # ── size the add by the rule's measured ev_lo, damped, hard-capped ──
                    _cap = _su_pos.get("size_cap_pct")
                    if not _cap:                              # no play cap → can't bound the add → skip
                        continue
                    _rule   = _su_pos.get("brain_rule_name") or _su_pos.get("insane_tier")
                    _evfrac = (_ev_size_frac(_rule) if _ev_size_frac else None) or _SIZEUP_DEFAULT_FRAC
                    _add_target  = _evfrac * _cap * sol_bal * _SIZEUP_DAMP
                    _tot_ceiling = _SIZEUP_TOTAL_CAP_MULT * _cap * sol_bal
                    _add_room    = max(0.0, _tot_ceiling - (_su_pos.get("size_sol", 0) or 0))
                    _add_sol     = round(min(_add_target, _add_room, remaining), 6)
                    if _add_sol < MIN_POSITION_SOL:
                        continue
                    # ── build + queue the size-up (execute_buy stack path → add_to_position) ──
                    _su_sig = {
                        "mint":             _su_mint,
                        "price":            _cur,
                        "size_sol":         _add_sol,
                        "size_usd":         round(_add_sol * sol_price, 4),
                        "momentum_5m":      _md.get("price_change_5m", 0),
                        "buy_sell_ratio":   round(_bs, 3),
                        "confidence":       round(memory.confidence_score(_su_mint), 2),
                        "is_stack":         True,
                        "sizeup":           True,
                        "insane_tier":      _su_pos.get("insane_tier"),
                        "brain_rule_name":  _su_pos.get("brain_rule_name"),
                        "validation_score": _su_pos.get("validation_score", 999.0),  # confirmed winner — don't re-strict-gate
                        "pair_age_hours":   999.0,                                   # existing position — cold-start must not block
                        "rationale":        f"SIZE-UP {'+'.join(_trigs)} +{_pnl:.1f}%",
                        "_cycle":           cycle,
                    }
                    # optimistic increment for cycle-dedup (errs toward FEWER adds on a failed add)
                    _su_pos["sizeup_count"] = _su_pos.get("sizeup_count", 0) + 1
                    remaining = max(0.0, remaining - _add_sol)
                    _entry_queue.put(_su_sig)
                    print(
                        f"         [SIZE-UP] {_su_mint[:6]}... +{_pnl:.1f}% BE-protected "
                        f"({'+'.join(_trigs)}) → add ◎{_add_sol:.4f} "
                        f"[{_rule} ev-frac {_evfrac:.2f}×cap, #{_su_pos['sizeup_count']}]",
                        flush=True,
                    )

            # ── Execution consumer ────────────────────────────────────────────
            # Drains the entry queue one signal at a time.  After each trade a
            # mandatory 5-second cooldown prevents back-to-back Jupiter/Jito calls.
            # If a previous cycle left signals in the queue (e.g. execution was busy),
            # they are consumed here first before new ones added above are processed,
            # provided they have not aged out (> 60 s stale).
            _exec_fired = 0
            while _entry_queue.depth() > 0:
                if not _entry_queue.ready():
                    # Execution busy or in cooldown — stop consuming this cycle.
                    break
                _queued_sig = _entry_queue.get()
                if _queued_sig is None:
                    break   # queue empty or all remaining signals were stale
                _entry_queue.mark_busy()
                try:
                    await execute_buy(
                        client, keypair, _queued_sig, risk, stoic,
                        _queued_sig.get("_cycle", cycle),
                        fail_counts=_tx_fail_count,
                    )
                finally:
                    _entry_queue.mark_done()
                _exec_fired += 1
                # Respect cooldown before trying next signal in this same cycle.
                _cd = _entry_queue.cooldown_remaining()
                if _cd > 0 and _entry_queue.depth() > 0:
                    await asyncio.sleep(_cd)

            if signals:
                _q_note = f" | queue depth={_entry_queue.depth()}" if _entry_queue.depth() else ""
                print(
                    f"         Queued {len(signals)} signal(s), executed {_exec_fired}{_q_note} | "
                    f"top: {signals[0]['rationale']}",
                    flush=True,
                )

            # --- Live balances ---
            sol_bal  = await payout.get_sol_balance(client, pubkey_str)
            usdc_bal = await payout.get_usdc_balance(client, pubkey_str)

            # RPC glitch guard: a single cycle returning 0 when we had a known-good
            # prior balance is a bad read, not a real drain. Guard fires regardless
            # of open positions — positions don't make the wallet legitimately 0.
            if sol_bal == 0.0 and _last_good_sol_bal > 0.1:
                print(f"[{_B}] ⚠ RPC returned 0 balance (last known ◎{_last_good_sol_bal:.4f}) — skipping write", flush=True)
                sol_bal = _last_good_sol_bal
            elif sol_bal > 0.0:
                _last_good_sol_bal = sol_bal

            # Sweep orphaned tokens on startup only — orphans appear after crashes,
            # not during normal operation. Running every 30s wastes Helius API quota.
            if cycle <= 3:
                await sweep_orphan_tokens(client, keypair, stoic=stoic, risk=risk)

            # Convert any remaining USDC to SOL
            if usdc_bal >= 0.50 and sol_bal >= 0.01:
                print(f"[Convert] Found ${usdc_bal:.2f} USDC — converting to SOL...", flush=True)
                converted = await payout.convert_usdc_to_sol(client, keypair)
                if converted:
                    sol_bal  = await payout.get_sol_balance(client, pubkey_str)
                    usdc_bal = await payout.get_usdc_balance(client, pubkey_str)
                    print(f"[Convert] Done — SOL balance now: {sol_bal:.4f}", flush=True)

            # Milestone status for dashboard
            prestige = payout.get_payout_count()
            _cur_m   = payout.get_milestone(prestige)
            milestone_status = [{
                "id":          _cur_m["id"],
                "label":       _cur_m["label"],
                "threshold":   _cur_m["threshold_sol"],
                "payout_sol":  _cur_m["payout_sol"],
                "cycle":       _cur_m["cycle"],
                "paid":        False,   # dynamic milestones never stay "paid"
                "payout_count": prestige,
            }]

            # Recompute after all buys/sells so status.json and history both reflect
            # the current positions.
            # S74: cost basis of the REMAINING position only. After a partial exit
            # (TP1 sells e.g. 40%), those proceeds are already back in the wallet
            # (sol_bal). The REMAINING cost basis is the already-scaled size_sol
            # (stoic.partial_close_position multiplies size_sol by `remaining` on each
            # partial), so use size_sol directly. S102: the old extra "* remaining_fraction"
            # here scaled it a SECOND time (orig×rem² instead of orig×rem) → it under-counted
            # deployed capital after a partial, which made Total SOL dip on the winning bank
            # and then JUMP UP when the (often red) remainder closed and released the
            # under-count. The S74 "graph drops on a winning trade" fix over-corrected.
            sol_in_trades = sum(   # S102: size_sol is ALREADY scaled by partial_close_position — do NOT re-apply remaining_fraction (that double-reduced deployed capital after a partial)
                p.get("size_sol", p.get("size_usd", 0) / max(sol_price, 1))
                for p in stoic.positions.values()
            )
            portfolio_val = sol_bal + sol_in_trades

            # ── Settlement guard — prevents RPC latency spikes in the chart ────────
            #
            # When a trade settles, there's a 1-2 cycle window where the RPC balance
            # hasn't caught up with on-chain state:
            #   BUY:  sol_bal still shows pre-buy (high) + sol_in_trades already updated
            #         → portfolio double-counts → phantom spike UP
            #   SELL: sol_in_trades drops immediately but sol_bal not yet updated
            #         → portfolio looks like it crashed → phantom dip DOWN
            #
            # Two-layer fix:
            #   1. Stale detection (dashboard): catches obvious mismatches this cycle.
            #      Uses _had_any_trade (buys OR sells) — old guard only caught buys.
            #      Old _stale_down also required sol_in_trades==0 which never fired with
            #      multiple open positions. Now fires on any significant drop after a trade.
            #   2. Post-settle quiet period (chart only): even if the stale guard misses
            #      a subtle transient, the chart skips 2 cycles after any trade to let the
            #      RPC fully settle. Dashboard display still updates every cycle.
            _had_any_trade = bool(signals) or bool(exits)

            _stale_up   = (_last_good_portfolio > 0 and _had_any_trade
                           and portfolio_val > _last_good_portfolio * 1.08)
            _stale_down = (_last_good_portfolio > 0 and _had_any_trade
                           and portfolio_val < _last_good_portfolio * 0.92)
            _is_stale   = _stale_up or _stale_down

            if _stale_up:
                print(f"[{_B}] ⚠ Stale RPC (post-buy): ◎{portfolio_val:.4f} vs last ◎{_last_good_portfolio:.4f} — skipping write", flush=True)
            elif _stale_down:
                print(f"[{_B}] ⚠ Stale RPC (post-trade): ◎{portfolio_val:.4f} vs last ◎{_last_good_portfolio:.4f} — skipping write", flush=True)

            # Post-settle counter: suppress chart recording for 2 cycles after any trade.
            # Decrements toward 0; resets to 2 whenever a new trade happens.
            if _had_any_trade:
                _post_settle_skip = 2
            elif _post_settle_skip > 0:
                _post_settle_skip -= 1

            # When stale: back-derive wallet balance from last known good total so the
            # display doesn't flash an inflated/deflated value for one cycle.
            _write_sol = max(0.0, _last_good_portfolio - sol_in_trades) if _is_stale else sol_bal

            try:
                write_status(pubkey_str, risk, market_data, signals, _write_sol, usdc_bal, milestone_status, sol_price, stoic.positions, sol_in_trades, available_sol, thinking=_thinking, prestige_pending=payout.is_prestige_pending())
            except Exception as e:
                print(f"[Status] Write error: {e}", flush=True)

            # Never write 0 to balance history — that's an RPC failure, not a real balance.
            # Zeroes create massive phantom crashes/recoveries in the chart.
            _portfolio_is_valid = portfolio_val > 0.001 or _last_good_portfolio == 0.0
            if not _is_stale and _portfolio_is_valid:
                _last_good_portfolio = portfolio_val
                # Chart recording: also skip the post-settle quiet period so settlement
                # transients (partial fills, latent RPC reads) never reach the history file.
                if _post_settle_skip == 0:
                    record_history(risk, cycle, portfolio_val, usdc_bal)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
