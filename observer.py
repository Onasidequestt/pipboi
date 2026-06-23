"""
Tier 1: The Observer — discovery, market data, and entry pre-qualification.

Encapsulates all the work that runs BEFORE stoic.evaluate():
  - Token discovery (GeckoTerminal, BondingCurve, cross-tier)
  - Market data fetch (DexScreener + GeckoTerminal gap-fill)
  - Hard binary rejects (age, boost, crash)
  - Gem path detection
  - Social scoring
  - ValidationProfile scoring on every candidate

Two-phase API so main.py can update the volatility regime between fetch and score:
  market_data, sol_price, agg_vol = await observer.scan_data(client, cycle)
  observer.score_tokens(regime)          ← uses the fresh regime
  hot_list = observer.get_hot_list()     ← tokens that passed ValidationProfile

stoic.evaluate() (which needs live strategy state) still runs in main.py.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

try:
    from ipc_layer import DiscoveryClient
    _SIDECAR_AVAILABLE = True
except ImportError:
    _SIDECAR_AVAILABLE = False

import admit_guard   # S121-dpgap: allowlist gate for the deep_pool/brain_rule loop (which bypasses the should_skip hook)
import regime_policy  # TAXONOMY: the v2 policy table's DEEP-frozen row gates these loops too (every admission path covered)
import bonding_curve
import dexscreener
import discovery as _disc_mod
import lunarcrush
import memory
import config as _config_mod
from bot_config import DATA_DIR as _DATA_DIR, BOT_ID as _BOT_ID
from config import (
    BASE_MINT, WATCHLIST, WATCHLISTS_BY_CAP, WATCHLISTS_EXACT, WATCHLIST_LOW,
    ACTIVE_WATCHLIST_CAP, DISCOVERY_PAGES_BY_MODE, DISCOVERY_MAX_BY_MODE,
    DISCOVERY_REFRESH_CYCLES,
    GEM_MIN_LIQUIDITY, GEM_MAX_AGE_HOURS, GEM_MIN_BUY_RATIO,
)
from stoic_strategy import MODES, get_mode, get_marketcap, get_viral_weight, MeanReversionStrategy
from validation import ValidationProfile


# ── Sidecar watchdog ──────────────────────────────────────────────────────────

_HEARTBEAT_PATH    = Path("shared_memory/sidecar_heartbeat.json")
_HEARTBEAT_MAX_AGE = 30.0  # seconds — must match sidecar's poll interval headroom

# ── Universal sellability floor (session 57) ──────────────────────────────────
# Applied to EVERY entry candidate — including static watchlist tokens, which
# previously bypassed the gem liquidity gate. A token below this floor cannot be
# reliably exited: the bot can buy but the pool is too thin to sell back into,
# producing -100% "ghost close" losses (e.g. WEN: $41k liq → -◎0.22).
#
# Sellability is a function of POOL DEPTH (liquidity), not 5m volume — a deep
# pool can be swapped out of even with zero recent trades. Gating on volume here
# wrongly blocked blue chips in quiet markets (TRUMP $27M liq / $0 5m vol), so
# the floor is liquidity-only. Momentum/activity is already handled by the
# validation score's volume dimension. This gates buys only; exits are unaffected.
_MIN_SELL_LIQ_USD = 50_000.0   # min pool depth to exit without catastrophic slippage

# ── S67: deep-pool entry path (ships DORMANT) ───────────────────────────────
# When True, the observer admits LIQUID movers that match strategy_brain's
# `deep_pool_quality` rule (m5≥1% & liq/mcap≥0.10 & buy/sell≥1) as `deep_pool`
# entry candidates — even when the regime score-threshold would reject them.
# This is what lets the fleet trade the brain-validated edge in ANY market.
# Default False = byte-for-byte current behavior (no deep_pool tokens created).
# Flip to True + ./run.sh to activate. RECOMMENDED: turn on once the 24h data-
# span clock clears so the edge is fully validated (see strategy_brain --evolve).
# Fully reversible: set back to False + restart.
_DEEP_POOL_ENABLED   = True
_DEEP_POOL_M5_MIN    = 1.0     # 5m momentum ≥ 1% (brain: m5≥1)
_DEEP_POOL_LIQ_MC    = 0.10    # liquidity / market-cap ≥ 0.10 (brain: liq_mc≥0.10 — deep relative to size)
_DEEP_POOL_BS_MIN    = 1.0     # buy/sell ratio ≥ 1 (brain: bs≥1 — buyers ≥ sellers)
# ── S70: drain-at-entry exitability guard (deep_pool + brain_rule) ──────────────
# Don't ENTER a pool whose depth is already falling — it's the leading indicator of
# the rug that produces a ghost_close (unsellable mid-hold = the fleet's #1 live leak).
# Validated on 149 deep_pool-qualifying matured obs (forward_obs.jsonl): pools draining
# at entry (lvel<0) averaged −0.16% fwd-EV with 22% dipping ≤−20% mid-hold, vs +5.83%
# EV / 0% deep-dip for non-draining (lvel≥0). The guard removes the bad cohort WITHOUT
# touching the profitable shallow ($50–75k) pools where most of the edge actually lives
# (raising the liq floor was investigated and REJECTED — it cuts the +9.2% $50–75k bucket).
# Robust against the S60 false-empty read: admission already requires liq≥$50k (a valid,
# large LATEST read), and _liq_momentum's v_liq = (latest−oldest)/oldest, so a 0 read
# fails the floor before this runs; a surviving negative lvel is a genuine decline.
# ⚠ Cross-scale note: lvel here is the observer's ~5×30s window (longer than the sidecar
# `lqv` the analysis used) — same SIGN, coarser magnitude; the −1% cut ignores rounding
# noise while still catching the draining cohort. Complements the LP-drain EXIT guard
# (entry catches the slow bleed already underway; exit catches the acute −15%/cyc rug).
_DEEP_POOL_MAX_DRAIN = -0.01   # reject admission if pool depth fell ≥1% over the lvel window

# ── S87: drain-at-entry exitability guard for the PRICE-ACTION plays ─────────────
# The deep_pool/brain_rule paths skip pools already bleeding depth at entry (S70 guard
# above) — the leading indicator of the rug that produces a ghost_close (unsellable
# mid-hold). The normal scoring path (gem/relay/quick/momentum) had NO such screen, and
# gem even bypasses the look-ahead liq floor → those plays enter with zero exitability
# pre-screening. That's the ghost-prune tail now showing on the S87 gem/relay volume
# (e.g. two −◎0.05/−◎0.07 ghost_prunes). Reuse the SAME validated −1% drain cut (149-obs
# study: draining-at-entry = −0.16% EV / 22% deep-dip vs +5.83% / 0% non-draining). Can
# ONLY skip an entry, never adds risk; ~7% of live pools qualify so activity is barely
# touched. This is ENTRY defense-in-depth; the catastrophic-drain EXIT (stoic_strategy.py,
# S87) handles the deep-at-entry one-cycle rug.
# S88-debug NOTE: post-S87 this is INTENTIONALLY a narrow backstop, not stale/conflicting code.
# The S87 scorer already penalizes drain (−8) and HARD-GATES liq_vel < −0.02 → score 0, so this
# only still fires on the thin −0.02 < lvel < −0.01 band that nonetheless scores ≥ the gate.
# Kept ENABLED on purpose: it can ONLY skip a marginally-draining entry (protective, and bot1
# is the current −EV bleeder). Set False ONLY if you want the scorer to solely own drain-at-
# entry — that LOOSENS admission, so it's an edge-policy change, not cleanup. Revert: True/restart.
_ENTRY_DRAIN_GUARD_ENABLED = True

# ── S74: admit only the ROBUST deep_pool sub-cohorts ────────────────────────────
# The parent gate (deep_pool_quality: m5≥1 & liq/mc≥0.10 & bs≥1) is ev_lo 0.52 / WR 46.5%
# (n=198 forward_obs) — barely +EV on PAPER, and −EV after live leakage (ghosts, slippage,
# exit timing). The strong sub-rules are far better: deep_pool_strict_filling ev_lo 6.15,
# deep_pool_filling 5.78, deep_pool_strict 3.72 (all WR 60–64%). When True, admission requires
# a token to match at least one strong sub-rule (_strong_rule != "") — dropping the weak flat-
# pool tail (the FizdC cohort). Raises net + cuts ghost-rate (filling/strict pools are deep &
# exitable by thesis), at the cost of fewer admits. Can only SKIP entries (never adds risk).
# Flip to False to restore the prior "admit all deep_pool_quality" behavior.
_DEEP_POOL_REQUIRE_STRONG = True   # S82: re-tightened (green_zone.py: plain deep_pool_quality tail = 47% WR / ev_lo ~0, the -EV bulk) — admit only strict/filling sub-cohorts. REVERT -> False to re-admit the tail.
# ── S82: skip deep_pool admission in -EV regimes (green_zone.py regime breakdown) ──
# Per-regime WR/ev_lo on deep_pool_strict_filling forward obs: euphoria 68%/+3.0 · sniper
# 52%/+4.4 (GREEN) · aggressive 44%/-3.3 · normal 42%/-4.8 · DEAD 25%/-20.8 (catastrophic).
# Operator (S82) cut DEAD only — it's pure bleed in a frozen tape. aggressive/normal are also
# -EV but cutting them is a large frequency hit (n=77/152) → left for an explicit operator call.
# Add "aggressive","normal" here to also drop the red mid-regimes. The 55 strict_gate is UNTOUCHED.
# S84: NORMAL was briefly added here, then REVERTED — it strangled deep_pool in a normal/dead-
# dominated tape (deep_pool is the gene-gate play; skipping its most common regime stalled the
# n≥15 gate). The real cause of "normal looks -EV / Bot1 stuck dead" was the regime MIS-SOURCING
# bug (Fix 1): each bot computed regime over its own cap-narrowed slice, so Bot1 read DEAD off ~$2k
# while the market was actually active. With Fix 1+3 the regime is now global+robust, so DEAD is
# a real freeze (correct to skip) and normal/sniper are genuine bands worth trading. Back to dead-only.
# S85: re-added "normal". The S84 reason to KEEP it (need trades to reach the n≥15 gate) is SPENT —
# n=15 is reached, NET is the only remaining gate constraint. The brain's new 5-band by_regime shows
# deep_pool_strict_filling = sniper +35 / euphoria +21 / aggressive +11 but normal −0.99 (−EV): the
# apparent "decay" was a regime-MIX artifact, not a dead edge. Skipping normal lifts realizable ev_lo
# +1.96 → +7.30, removes the −EV cohort dragging gate net < 0, and still keeps ~46% of obs
# (euphoria/aggressive/sniper). Revert: set back to {"dead"}.  (observer.py is UNTRACKED → see .bak.s85.*)
_DEEP_POOL_SKIP_REGIMES = {"dead", "normal"}

# S104: skip the gem×sniper slice — the single worst-EV pocket fleet-wide. edge_report:
# insane gem × sniper regime = n=22, EV −0.00377◎/trade, net −0.083◎ (the standing S87/S93
# "skip the rug's slice" rec). The gem path bypasses the liquidity floor (fresh-token bet) and
# in the sniper regime ($48–110k agg-vol, thin/illiquid) that combination is where the rugs land
# (realized stop-bleed: bot1 gem −0.124◎). Entry-only filter → can only SKIP, never sizes/sends.
# Revert: set False (or remove the guard in the admission block). NOTE: observer.py is UNTRACKED → .bak.s104.*
_GEM_SNIPER_SKIP = True

# ── S71: realign the drain guard to the sidecar's liquidity-velocity window ──────
# The S70 drain-at-entry guard and the brain's deep_pool_filling edge both key off
# liquidity velocity — but the brain VALIDATED that edge on the sidecar's fine-grained
# ~5×10s `liq_velocity`, while the live guard reads the observer's own coarser ~5×30s
# `_liq_momentum` (`lvel`): same sign, ~3× blurrier magnitude. When enabled, the value the
# deep_pool / brain_rule drain guard + brain_rule `lqv` feature read (score_detail["lvel"])
# prefers the sidecar's per-mint velocity, falling back to the self-computed value whenever
# the snapshot lacks the mint or the sidecar is in standalone mode. The validation score's
# liquidity dimension is UNCHANGED — only the deep_pool admission input is realigned, so the
# blast radius is the (INSANE/WILD) deep_pool/brain_rule admission path, which can only SKIP
# entries (more selective, never adds risk). lvel_self is preserved in score_detail for A/B.
# Per-bot hot-reload canary (mirrors strict_gate / ev_sizing): enable on ONE bot via
# bots/botN/sidecar_liqvel.json {"enabled": true} — 30s hot-reload, no restart, reversible
# (rm the file). Module master stays False so the default is byte-for-byte current behavior.
_SIDECAR_LIQVEL_ENABLED = False               # module master (fleet-wide) — default OFF
_SIDECAR_LIQVEL_PATH    = _DATA_DIR / "sidecar_liqvel.json"
_sidecar_liqvel_cache   = (0.0, False)        # (read_ts, enabled) — 30s cache off the hot path

def _sidecar_liqvel_on() -> bool:
    """True when THIS bot should feed the sidecar's liq_velocity into the drain guard."""
    global _sidecar_liqvel_cache
    if _SIDECAR_LIQVEL_ENABLED:
        return True
    if time.time() - _sidecar_liqvel_cache[0] < 30.0:
        return _sidecar_liqvel_cache[1]
    on = False
    try:
        on = bool(json.loads(_SIDECAR_LIQVEL_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _sidecar_liqvel_cache = (time.time(), on)
    return on

# ── S86: gate the INSANE bonding-curve gradient scan (RPC dead-weight cleanup) ──
# The gradient force-fire scan (far below, ~L1050) surfaces ~226 score-0 BC mints/48h
# that convert to ZERO trades while the force_fire exemption stays reverted (S84) — pure
# RPC waste (5 bonding_curve account queries/cycle, Bot1/INSANE only). Default ENABLED so
# this code change is byte-for-byte current behavior; disable per-bot with
# bots/botN/gradient_scan.json {"enabled": false} (30s hot-reload, reversible: rm the file).
# ⚠ Re-enable (rm the canary) BEFORE ever restoring the force_fire exemption.
_GRADIENT_SCAN_PATH  = _DATA_DIR / "gradient_scan.json"
_gradient_scan_cache = (0.0, True)

def _gradient_scan_on() -> bool:
    """True unless a per-bot gradient_scan.json explicitly disables it."""
    global _gradient_scan_cache
    if time.time() - _gradient_scan_cache[0] < 30.0:
        return _gradient_scan_cache[1]
    on = True
    try:
        on = bool(json.loads(_GRADIENT_SCAN_PATH.read_text()).get("enabled", True))
    except Exception:
        on = True
    _gradient_scan_cache = (time.time(), on)
    return on

# ── S94 (rec#3): regime-conditional momentum scoring — canary A/B ──────────────
# When ON for a bot, the validation activity-momentum half rewards FLAT/DIP in the
# ANTI regimes (dead/normal) and up-momentum in the PRO regimes (euphoria/aggressive)
# — the proven dead/normal momentum sign-flip fix. Default OFF (master False) so the
# fleet is byte-for-byte current behaviour; enable on ONE bot for the A/B via
# bots/botN/regime_momentum.json {"enabled": true} (30s hot-reload, reversible: rm it).
_REGIME_MOM_ENABLED = False                    # module master (fleet-wide) — default OFF
_REGIME_MOM_PATH    = _DATA_DIR / "regime_momentum.json"
_regime_mom_cache   = (0.0, False)

def _regime_mom_on() -> bool:
    """True when THIS bot should score momentum regime-conditionally (rec#3 canary)."""
    global _regime_mom_cache
    if _REGIME_MOM_ENABLED:
        return True
    if time.time() - _regime_mom_cache[0] < 30.0:
        return _regime_mom_cache[1]
    on = False
    try:
        on = bool(json.loads(_REGIME_MOM_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _regime_mom_cache = (time.time(), on)
    return on

# ── PIPE12 (R-B1 / R-B2): whale-buy score bonus + regime-SCALED momentum (Bot1 A/B canaries) ──
# Both default OFF (no module master flag = fleet byte-for-byte unchanged). whale_bonus lifts an
# on-chain whale buy (mint in bc_hot) INTO a sellable pool; regime_mom_scale scales the up-momentum
# reward by regime (WS-B: scale, don't flip). Enable per-bot: bots/botN/{whale_bonus,regime_mom_scale}.json
# {"enabled": true} (30s hot-reload, reversible: rm it). ⚠ whale_validate.py currently shows 0 tradeable
# whale closes → keep whale_bonus OFF until the whale×sellable intersection has live evidence.
_WHALE_BONUS_PATH      = _DATA_DIR / "whale_bonus.json"
_REGIME_MOM_SCALE_PATH = _DATA_DIR / "regime_mom_scale.json"
_whale_bonus_cache     = (0.0, False)
_regime_mom_scale_cache = (0.0, False)

def _whale_bonus_on() -> bool:
    global _whale_bonus_cache
    if time.time() - _whale_bonus_cache[0] < 30.0:
        return _whale_bonus_cache[1]
    try:
        on = bool(json.loads(_WHALE_BONUS_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _whale_bonus_cache = (time.time(), on)
    return on

def _regime_mom_scale_on() -> bool:
    global _regime_mom_scale_cache
    if time.time() - _regime_mom_scale_cache[0] < 30.0:
        return _regime_mom_scale_cache[1]
    try:
        on = bool(json.loads(_REGIME_MOM_SCALE_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _regime_mom_scale_cache = (time.time(), on)
    return on

# ── S98 (gate-unfreeze): admit a TIGHT +EV deep_pool slice in the `normal` regime ──
# The deploy gate is frozen at n=5 because deep_pool skips {dead,normal} and `normal` is
# ~75-85% of the tape → it fires ~0×/day. gate_unfreeze.py showed the WHOLE normal cohort
# is -EV (skip stays) BUT the tight slice `filling AND bs>=1.5` is robustly +EV (mean +8.3
# hold / +11.0 ride, both halves +, ghost 0) — the brain's "filling into a deep sellable
# pool" edge in the regime we skip. When ON for a bot, deep_pool ALSO admits that slice in
# `normal` (tagged deep_pool → feeds the gate; the gate's own net>=0/ghost<=10%/n>=15 bars
# are the judge). NOT a wholesale skip removal — `dead` and the -EV normal bulk stay barred.
# Default OFF (master False) = byte-for-byte current behaviour; enable on ONE bot via
# bots/botN/normal_deep_pool.json {"enabled": true} (30s hot-reload, reversible: rm it).
# ⚠ This feeds the deploy gate that arms EV-sizing — does NOT touch ev_sizing.json itself,
# so THE ONE OPERATOR RULE is untouched; the gate stays the gatekeeper.
_NORMAL_DP_ENABLED = False                     # module master (fleet-wide) — default OFF
_NORMAL_DP_PATH    = _DATA_DIR / "normal_deep_pool.json"
_NORMAL_DP_BS_MIN  = 1.5                        # the slice's buy-pressure floor (gate_unfreeze.py)
_normal_dp_cache   = (0.0, False)

def _normal_deep_pool_on() -> bool:
    """True when THIS bot should admit the tight `normal` filling+buy deep_pool slice (S98 canary)."""
    global _normal_dp_cache
    if _NORMAL_DP_ENABLED:
        return True
    if time.time() - _normal_dp_cache[0] < 30.0:
        return _normal_dp_cache[1]
    on = False
    try:
        on = bool(json.loads(_NORMAL_DP_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _normal_dp_cache = (time.time(), on)
    return on

# ── S107 (momentum-override lane): admit the already-RUNNING-momentum runner cohort ──
# the scorer is structurally blind to it. S107 research (research/evolve_s107/): 94% of the
# m5≥10 "Jotchua-shape" cohort is scored 0 because median liq ($23k) < the $30k hard-gate (S87) —
# and it's INVERTED (the m5≥10 tokens the scorer ADMITS avg −5.8% fwd; the 721 it gates to 0 avg
# +11.5%). Under a trail exit the gated slice is hugely +EV (m5≥10 bs≥1.2 v5≥2000 → +35% mean,
# ev_lo +30, survives a liquidity-scaled slippage haircut). The edge LIVES in low liq (a liq floor
# kills it), so exitability is solved another way: rug_screen (fleet-wide), the drain-at-entry
# guard, ×0.5 size (main.py), and the RIDE/trail exit (stoic) — NOT a liquidity floor. This lane
# admits the signature EVEN when score<floor / liq<$30k, behind those guards. Bot1-only canary
# (default OFF); entry-only → can only ADD a screened, size-down, trail-exited position. The
# deep_pool/brain_rule gate cohort is NEVER touched (this is the gem/price-action universe) → no
# S87/89/91 lockout risk. THE ONE OPERATOR RULE intact (no ev_sizing). bots/botN/momentum_override.json.
_MOM_OVERRIDE_ENABLED = False                  # module master (fleet-wide) — default OFF
_MOM_OVERRIDE_PATH    = _DATA_DIR / "momentum_override.json"
# ── RUNNEREDGE (S116, 2026-06-11): re-belted to CAPTURE the validated edge ──
# research/runner_edge/ proved (48k forward_obs, realizable trail+friction+ghost haircut) the
# m5≥15 ∧ bs≥1.3 runner cohort is ev_lo +14% (harsh) to +32% (trail), 220 distinct mints, stable
# OUT-OF-SAMPLE (held-out 40% → +16%), broad across regimes. ★ THE SMOKING GUN: the old liq≥$15k
# belt sat ABOVE the cohort's $9,705 MEDIAN liquidity → it belted the lane out of its own edge
# (adding liq≥15k INVERTS realizable ev_lo +12.7%→−7.0%; liq≥30k → −19%) → 0 fires ever. So:
#   m5 10→15, bs 1.2→1.3 (the cleaner, higher-EV, more-diverse slice),
#   v5 2000→1000 (keep an anti-single-trade-spike floor, well below the cohort's natural volume),
#   liq 15000→3000 (admit the sub-$10k runners where the edge lives; $3k = minimal exitability for
#   a ~$5 / ◎0.03 ×0.5-size bet = ~0.1% of pool). Exitability stays solved by rug_screen + drain
#   guard + ×0.5 size + trail — NOT a high liq floor. Bot1-only live-validation probe of the
#   price-EV→wallet gap (the one thing no backtest can settle). Revert RUNNEREDGE: restore the old
#   constants (10/1.2/2000/15000) or `observer.py.bak.runneredge.*`.
_MOM_OVR_M5_MIN  = 15.0                         # already-running momentum (the validated runner slice)
_MOM_OVR_BS_MIN  = 1.3                          # real buy pressure
_MOM_OVR_V5_MIN  = 1000.0                       # anti-single-trade-spike floor (not a route-depth gate)
_MOM_OVR_LIQ_MIN = 3_000.0                      # minimal exitability floor ONLY — the edge is the low-liq cohort
_mom_override_cache = (0.0, False)

def _momentum_override_on() -> bool:
    """True when THIS bot should run the S107 momentum-override admission lane (canary A/B)."""
    global _mom_override_cache
    if _MOM_OVERRIDE_ENABLED:
        return True
    if time.time() - _mom_override_cache[0] < 30.0:
        return _mom_override_cache[1]
    on = False
    try:
        on = bool(json.loads(_MOM_OVERRIDE_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _mom_override_cache = (time.time(), on)
    return on

# ── S107 (anti-dead entry floor): require real 5m volume on the price-action path ──
# Research/evolve_s107: a v5 floor halves the dead-flat outcome rate (62%@v5<200 → 40%@v5 2-5k)
# and ~doubles the trail-able upside — the ENTRY-side complement to the S105 exit-side dead-pool
# quarantine (quarantine stops RE-entering a known-dead mint; this stops entering a quiet pool in
# the FIRST place, cutting the friction churn before it starts). Canary, default OFF; per-bot
# bots/botN/antidead_v5.json {"enabled": true, "min_v5": 1500}. Entry-only SKIP → can never size/
# send → THE ONE OPERATOR RULE intact. Applies to the gem/relay/quick/momentum score path only;
# deep_pool/brain_rule (the gate cohort) is admitted on a SEPARATE loop and never touched here.
_ANTIDEAD_V5_PATH  = _DATA_DIR / "antidead_v5.json"
_ANTIDEAD_V5_DEFAULT = 1500.0
_antidead_v5_cache = (0.0, (False, _ANTIDEAD_V5_DEFAULT))

def _antidead_v5_floor() -> tuple:
    """(enabled, min_v5). Default (False, 1500) = unchanged behavior."""
    global _antidead_v5_cache
    if time.time() - _antidead_v5_cache[0] < 30.0:
        return _antidead_v5_cache[1]
    out = (False, _ANTIDEAD_V5_DEFAULT)
    try:
        d = json.loads(_ANTIDEAD_V5_PATH.read_text())
        if d.get("enabled"):
            out = (True, float(d.get("min_v5", _ANTIDEAD_V5_DEFAULT)))
    except Exception:
        pass
    _antidead_v5_cache = (time.time(), out)
    return out

# ── S99 (dust shadow executor): fire the deep_pool predicate across the SKIPPED ──
# regimes (dead/normal) at DUST size to manufacture real on-chain closes for the
# evidence base — WITHOUT touching quality or risking capital. The deploy gate is
# frozen at fire-rate (deep_pool fires ~0×/day because it skips dead/normal and the
# market is `normal` 75-85% of the tape). euphoria is ~6% — volume the market
# structurally won't give. The dust lane runs the IDENTICAL strict/filling predicate
# in those skipped regimes but sizes the entry to ~◎0.01, so every fill is a genuine
# close (real friction, real ghost) that — read as return-% (pnl_sol/size_sol, S80) —
# carries the true exitability signal at near-zero capital. This sizes the unproven
# edge DOWN to learn it, never up → THE ONE OPERATOR RULE holds. Dust closes are
# tagged dust_shadow=True and feed a SEPARATE size-normalized shadow gate (dust_gate.py),
# NOT the live arming gate (prestige_tracker excludes them) — so EV-sizing on real
# capital still arms only on real-capital evidence. Default OFF (master False) =
# byte-for-byte current behaviour; enable per-bot via bots/botN/dust_shadow.json
# {"enabled": true} (30s hot-reload, reversible: rm it).
_DUST_SHADOW_ENABLED = False                    # module master (fleet-wide) — default OFF
_DUST_SHADOW_PATH    = _DATA_DIR / "dust_shadow.json"
_dust_shadow_cache   = (0.0, False)

def _dust_shadow_on() -> bool:
    """True when THIS bot should dust-fire the deep_pool predicate in skipped regimes (S99 canary)."""
    global _dust_shadow_cache
    if _DUST_SHADOW_ENABLED:
        return True
    if time.time() - _dust_shadow_cache[0] < 30.0:
        return _dust_shadow_cache[1]
    on = False
    try:
        on = bool(json.loads(_DUST_SHADOW_PATH.read_text()).get("enabled"))
    except Exception:
        on = False
    _dust_shadow_cache = (time.time(), on)
    return on

# ── S67: generalized brain-rule entry registry (ships DORMANT) ──────────────────
# When True, the observer admits LIQUID movers matching ANY brain candidate that
# clears live_rule.admit_rules()'s robustness bar (ev_lo≥2%, n≥30, both time-halves
# stable). This is the deep_pool path generalized: the brain re-evaluates hourly, so
# rules auto-join/drop the live registry with NO code change — a self-extending
# strategy factory. Default False = byte-for-byte current behavior. Flip + ./run.sh.
# Coexists with the deep_pool path (that runs first; matched mints are skipped here).
# Fully reversible: set back to False + restart.
_BRAIN_RULES_ENABLED = True
try:
    import live_rule as _live_rule          # tested bridge: admit_rules() + match_rule()
except Exception:
    _live_rule = None                       # fail toward "no registry" — never break startup


def _is_heartbeat_fresh() -> bool:
    """True when the sidecar heartbeat file exists and was written within 30s.

    Uses the unix_ts field written by discovery_service._write_heartbeat() to
    avoid any timezone/ISO-parse overhead in the hot path.
    Returns False (treat sidecar as stalled) on any read or parse error.
    """
    try:
        with open(_HEARTBEAT_PATH) as fh:
            hb = json.load(fh)
        return (time.time() - hb.get("unix_ts", 0.0)) < _HEARTBEAT_MAX_AGE
    except Exception:
        return False


@dataclass
class ObservedToken:
    mint:        str
    symbol:      str
    score:       float
    threshold:   float
    passes:      bool
    regime:      str
    gem_path:    bool
    viral:       float
    momentum:    float
    buy_sell:    float
    vol5m:       float
    liq_usd:     float
    bc_hot:      bool
    score_detail:    dict  = field(default_factory=dict)  # {vol, vel, liq, soc}
    ts:              float = field(default_factory=time.time)
    probe:           bool  = False   # True when qualified via liq-growth path, not ValidationProfile
    liq_growth_pct:  float = 0.0     # % liq change from prior cycle (probe tokens only)
    mean_reversion:       bool  = False   # True when qualified via MR washout path
    mr_vol_accel:         float = 0.0     # vol acceleration multiple at MR qualification (MR only)
    force_fire:           bool  = False   # True when qualified via gradient Force-FIRE path
    gradient_sol_per_min: float = 0.0     # SOL inflow rate (force_fire only)
    deep_pool:            bool  = False   # S67: qualified via brain-validated deep_pool_quality rule
    deep_pool_liq_mc:     float = 0.0     # liq/mcap at deep_pool qualification (deep_pool only)
    deep_pool_strong_rule: str  = ""      # S75: strongest matching sub-rule (filling/strict) → EV-sizing target
    normal_slice:         bool  = False   # S98-reconcile: this deep_pool entry is the tight `normal` filling+buy slice
    dust_shadow:          bool  = False   # S99: admitted ONLY via the dust lane (skipped regime) → size to dust, tag closes
    momentum_override:    bool  = False   # S107: admitted via the running-momentum lane that bypasses score/$30k gate
    brain_rule:           bool  = False   # S67: qualified via the generalized brain-rule registry
    brain_rule_name:      str   = ""      # which brain candidate admitted it (e.g. momentum_breakout)

    def age_s(self) -> float:
        return time.time() - self.ts


class Observer:
    """
    Tier 1: Pre-qualifies tokens before stoic.evaluate() runs.

    scan_data() fetches data.  score_tokens() scores using a freshly computed regime.
    Main loop then runs stoic.evaluate() only on the hot list — not the full pool.
    """

    def __init__(self) -> None:
        self._hot_cache:        dict[str, ObservedToken] = {}
        self._all_scored:       list[ObservedToken]      = []
        self._token_evals:      list[dict]               = []  # for _thinking
        self._market_data:      dict                     = {}
        self._sol_price:        float                    = 150.0
        self._agg_vol_5m:       float                    = 0.0
        self._last_scan_ms:     int                      = 0
        self._scan_count:       int                      = 0
        self._regime:           str                      = "normal"
        self._discovered:       list                     = []
        self._gecko_pool:       dict                     = {}
        self._bc_hot:           list                     = []
        self._bc_hot_set:       set                      = set()
        self._sources:          list[str]                = []
        self._active_watchlist: list[str]                = []
        self._social_cache:     dict                     = {}
        self._disc_ran:         bool                     = False
        self._static_set:       set                      = set(WATCHLIST)
        self._scan_start_ts:    float                    = 0.0
        self._prev_liq:         dict[str, float]         = {}  # prior cycle's liq snapshot for probe growth calc
        self._vol_history:      dict[str, list]          = {}  # rolling vol5m per token for MR vol-acceleration calc
        self._liq_history:      dict[str, deque]         = {}  # rolling liq_usd per token (last 5 cycles) for momentum calc
        self._sidecar_liq_vel:  dict[str, float]         = {}  # S71: sidecar's fine-grained liq_velocity (mint→v), refreshed each scan
        self._gradient_signals: list                     = []  # force-fire candidates from gradient scan
        # Sidecar client — initialised lazily on first scan_data() call.
        # None means "not yet checked"; False means "sidecar not running".
        self._sidecar: Optional[DiscoveryClient] = (
            DiscoveryClient() if _SIDECAR_AVAILABLE else None
        )
        self._sidecar_ok: bool = False   # True after first successful sidecar fetch

    # ── Private helpers ────────────────────────────────────────────────────────

    def _liq_momentum(self, mint: str, current_liq: float) -> tuple:
        """Append current_liq to the 5-cycle rolling history and return
        (v_liq, a_liq) — velocity and acceleration of pool depth.

        v_liq  = (latest - oldest) / oldest   (fractional rate of change)
        a_liq  = delta_latest - delta_prior    (second-order: is growth speeding up?)

        Returns (0.0, 0.0) when fewer than 3 samples are available or oldest == 0.
        Called once per scored token per cycle — the append happens here.
        """
        if mint not in self._liq_history:
            self._liq_history[mint] = deque(maxlen=5)
        hist = self._liq_history[mint]
        hist.append(current_liq)
        if len(hist) < 3 or hist[0] <= 0:
            return 0.0, 0.0
        v_liq = (hist[-1] - hist[0]) / hist[0]
        a_liq = (hist[-1] - hist[-2]) - (hist[-2] - hist[-3])
        return round(v_liq, 4), round(a_liq, 2)

    # ── Public accessors ───────────────────────────────────────────────────────

    @property
    def market_data(self) -> dict:
        return self._market_data

    @property
    def sol_price(self) -> float:
        return self._sol_price

    @property
    def agg_vol_5m(self) -> float:
        return self._agg_vol_5m

    @property
    def bc_hot(self) -> list:
        return self._bc_hot

    @property
    def bc_hot_set(self) -> set:
        return self._bc_hot_set

    @property
    def token_evals(self) -> list[dict]:
        return self._token_evals

    @property
    def active_watchlist(self) -> list[str]:
        return self._active_watchlist

    @property
    def disc_ran(self) -> bool:
        return self._disc_ran

    @property
    def gradient_signals(self) -> list:
        return self._gradient_signals

    @property
    def sources(self) -> list[str]:
        return self._sources

    @property
    def discovered_count(self) -> int:
        return len(self._discovered)

    def get_hot_list(self) -> list[ObservedToken]:
        """Tokens that passed ValidationProfile this cycle, sorted by score."""
        return sorted(self._hot_cache.values(), key=lambda t: -t.score)

    def get_all_scored(self) -> list[ObservedToken]:
        return self._all_scored

    # ── Phase 1: fetch ────────────────────────────────────────────────────────

    async def scan_data(
        self,
        client: httpx.AsyncClient,
        *,
        cycle: int,
        open_position_mints: Optional[list[str]] = None,
    ) -> tuple[dict, float, float]:
        """
        Fetch discovery + market data.
        Returns (market_data, sol_price, agg_vol_5m).
        Call score_tokens(regime) after updating the volatility regime.

        Sidecar mode (when discovery_service.py is running):
          Reads the pre-built snapshot from the sidecar instead of independently
          polling GeckoTerminal / Birdeye / DexScreener / BondingCurve.
          The sidecar provides the INSANE-depth (widest) discovery pool; this
          method still narrows it to the bot's own cap tier and watchlist cap.

        Standalone mode (sidecar not running):
          Unchanged behaviour — all polling happens here exactly as before.
        """
        self._scan_start_ts = time.monotonic()
        self._disc_ran      = False
        mode = get_mode()

        # ── Try sidecar first ──────────────────────────────────────────────────
        _snap = None
        _sidecar_agg = None   # S84 Fix 1: the sidecar's GLOBAL agg_vol_5m (regime source)
        if self._sidecar is not None:
            if not _is_heartbeat_fresh():
                # Heartbeat stale or missing — sidecar is stalled or not running.
                # Skip the UDS connection entirely and fall through to standalone.
                if self._sidecar_ok:
                    print(
                        "[Observer] Discovery Service Stalled — "
                        "heartbeat > 30s old, switching to standalone mode",
                        flush=True,
                    )
                    self._sidecar_ok = False
            else:
                _snap = await self._sidecar.fetch()
                if _snap and self._sidecar.is_fresh(_snap, max_age_s=30.0):
                    self._sidecar_ok = True
                else:
                    _snap = None   # stale or unavailable — fall through to standalone

        if _snap:
            # ── Sidecar path: use pre-built snapshot ───────────────────────────
            self._bc_hot     = _snap.get("bc_hot", [])
            self._bc_hot_set = set(self._bc_hot)
            # S71: the sidecar's fine-grained per-mint liq_velocity (validated window).
            self._sidecar_liq_vel = _snap.get("liq_velocity", {}) or {}

            # Sidecar provides the INSANE-depth discovery pool.
            # Narrow it to this bot's cap tier before building the active watchlist.
            _cap = get_marketcap()
            _static_list = (WATCHLISTS_EXACT if mode == "insane" else WATCHLISTS_BY_CAP).get(
                _cap, WATCHLIST_LOW if mode == "insane" else WATCHLIST
            )
            _disc_all = _snap.get("discovery_pool", [])
            # Keep only mints that were already in the sidecar's market_data snapshot
            # (ensures we only score tokens with fresh data, same guarantee as standalone)
            _snap_mints = set(_snap.get("market_data", {}).keys())
            self._discovered = [m for m in _disc_all if m in _snap_mints]
            self._gecko_pool = {}   # sidecar already gap-filled gecko pricing into market_data
            self._sources = (
                ["sidecar"]
                + (["bonding_curve"] if self._bc_hot else [])
                + ["geckoterminal", "birdeye", "crosstier"]   # reflect sidecar sources
            )
            self._disc_ran = True

            # Build active watchlist from sidecar pool
            _wl_cap   = ACTIVE_WATCHLIST_CAP.get(mode, 100)
            active_wl = list(dict.fromkeys(_static_list + self._discovered))[:_wl_cap]
            if self._bc_hot:
                _wl_set = set(active_wl)
                _inject = [m for m in self._bc_hot if m and m not in _wl_set]
                if _inject:
                    active_wl = (active_wl + _inject)[:_wl_cap]
            self._active_watchlist = active_wl

            # Use sidecar market_data; always include open positions
            _pos_mints = open_position_mints or []
            _snap_md   = _snap.get("market_data", {})
            market_data: dict = {m: _snap_md[m] for m in active_wl if m in _snap_md}
            for m in _pos_mints:
                if m not in market_data and m in _snap_md:
                    market_data[m] = _snap_md[m]
            # For open positions missing from sidecar (e.g. delisted), fall back to DexScreener
            _missing = [m for m in _pos_mints if m not in market_data]
            if _missing:
                _fallback = await dexscreener.get_watchlist_data(client, _missing)
                market_data.update(_fallback)

            sol_price = _snap.get("sol_price", 150.0)
            _sidecar_agg = _snap.get("agg_vol_5m")   # S84 Fix 1: global, robust (winsor+EMA)

        else:
            # ── Standalone path: unchanged behaviour ───────────────────────────
            if self._sidecar_ok:
                # Was working before — log the first miss so we know sidecar restarted
                print("[Observer] Sidecar unavailable — switching to standalone mode", flush=True)
                self._sidecar_ok = False

            # S71: no sidecar snapshot → drain guard falls back to the self-computed lvel.
            self._sidecar_liq_vel = {}
            self._bc_hot     = bonding_curve.get_hot_mints()
            self._bc_hot_set = set(self._bc_hot)

            if cycle % DISCOVERY_REFRESH_CYCLES == 1:
                _cap = get_marketcap()
                _static = (WATCHLISTS_EXACT if mode == "insane" else WATCHLISTS_BY_CAP).get(
                    _cap, WATCHLIST_LOW if mode == "insane" else WATCHLIST
                )
                _exclude = (set(WATCHLIST) if mode == "insane" else set(_static)) | {BASE_MINT}
                _cfg  = DISCOVERY_PAGES_BY_MODE.get(mode, DISCOVERY_PAGES_BY_MODE["insane"])
                _dmax = DISCOVERY_MAX_BY_MODE.get(mode, 85)
                self._discovered, self._gecko_pool = await _disc_mod.discover_trending_tokens(
                    client,
                    exclude=list(_exclude),
                    max_tokens=_dmax,
                    pages_trending=_cfg["trending"],
                    pages_new_pools=_cfg["new_pools"],
                    include_deep=_cfg["deep"],
                    bonding_curve_mints=self._bc_hot,
                )
                self._sources = (
                    (["bonding_curve"] if self._bc_hot else [])
                    + ["geckoterminal"]
                    + (["birdeye"] if _disc_mod.BIRDEYE_ENABLED else [])
                    + ["crosstier"]
                )
                self._disc_ran = True

            _cap = get_marketcap()
            _static_list = (WATCHLISTS_EXACT if mode == "insane" else WATCHLISTS_BY_CAP).get(
                _cap, WATCHLIST_LOW if mode == "insane" else WATCHLIST
            )
            _wl_cap   = ACTIVE_WATCHLIST_CAP.get(mode, 100)
            active_wl = list(dict.fromkeys(_static_list + self._discovered))[:_wl_cap]
            if self._bc_hot:
                _wl_set = set(active_wl)
                _inject = [m for m in self._bc_hot if m and m not in _wl_set]
                if _inject:
                    active_wl = (active_wl + _inject)[:_wl_cap]
            self._active_watchlist = active_wl

            _pos_mints  = open_position_mints or []
            fetch_mints = list(dict.fromkeys(active_wl + _pos_mints))
            market_data, sol_price = await asyncio.gather(
                dexscreener.get_watchlist_data(client, fetch_mints),
                dexscreener.get_sol_price(client),
            )
            for mint, gdata in self._gecko_pool.items():
                if mint in active_wl and mint not in market_data:
                    market_data[mint] = gdata

        # ── Common tail (both paths) ───────────────────────────────────────────
        self._market_data = market_data
        self._sol_price   = sol_price
        # S84 Fix 1: in sidecar mode use the GLOBAL agg_vol_5m (full discovery pool, robust —
        # winsorized + EMA-smoothed in discovery_service) so ALL bots agree on ONE regime.
        # Recomputing the sum here over each bot's cap-narrowed watchlist made Bot1 (LOW cap)
        # read ~$2k→"dead" while Bot2/3 read ~$98k→"sniper" — same instant, 100x split, and it
        # jammed Bot1 permanently in DEAD. Standalone (no sidecar snapshot) still self-sums.
        self._agg_vol_5m  = (
            _sidecar_agg if _sidecar_agg is not None
            else sum(d.get("volume_5m", 0.0) for d in market_data.values())
        )

        return market_data, sol_price, self._agg_vol_5m

    # ── Phase 2: score ────────────────────────────────────────────────────────

    async def score_tokens(
        self,
        client: httpx.AsyncClient,
        regime: str,
        sol_bal: float = 2.0,
        fleet_wr: float = 1.0,
    ) -> None:
        """
        Run ValidationProfile on every candidate in the active watchlist.
        Call after scan_data() and after updating the volatility regime.

        sol_bal   — current wallet SOL balance; raises the pass threshold when
                    the bot is far below the 2.0 SOL prestige goal.
        fleet_wr  — recent win rate (0–1); Active Probing is gated behind ≥60%.
        """
        # Balance-adjusted threshold penalty: the further from ◎2.0, the harder
        # the entry bar.  At ◎0.5 adds ~11 pts; at ◎1.8 adds ~1.5 pts; at ◎2.0 = 0.
        # Capped at 10 pts so it never fully blocks the AGGRESSIVE regime (threshold 75→85 max).
        # was min(10, * 15) → at ◎0.7 gave +9.75 pts, pushing NORMAL threshold to 89.8
        # which is near-impossible combined with a high momentum floor.  Halved to 5 pts max.
        _bal_penalty = min(5.0, max(0.0, (1.0 - min(sol_bal, 2.0) / 2.0) * 8.0))
        self._regime = regime
        mode = get_mode()
        _vw  = get_viral_weight()
        # S71: read the per-bot canary once per scan (30s-cached) — when on, the deep_pool/
        # brain_rule drain guard reads the sidecar's fine-grained liq_velocity for `lvel`.
        _use_sidecar_lvel = _sidecar_liqvel_on()
        _use_regime_mom   = _regime_mom_on()   # S94 rec#3: regime-conditional momentum (canary A/B)
        _use_normal_dp    = _normal_deep_pool_on()   # S98: admit the tight normal deep_pool slice (canary A/B)
        _use_mom_override = _momentum_override_on()  # S107: admit the running-momentum runner lane (canary A/B)
        _antidead_on, _antidead_min_v5 = _antidead_v5_floor()  # S107: anti-dead v5 entry floor (canary A/B)
        _use_whale_bonus  = _whale_bonus_on()        # PIPE12 R-B1: whale-buy score bonus (canary A/B)
        _use_regime_mscale = _regime_mom_scale_on()  # PIPE12 R-B2: regime-scaled momentum (canary A/B)
        _use_dust_shadow  = _dust_shadow_on()        # S99: dust-fire the deep_pool predicate in skipped regimes (canary A/B)
        market_data = self._market_data

        # Execution-penalty map: loaded once per score_tokens() call.
        # Reduces the liquidity dimension of tokens with a history of bad fills.
        # Populated by auditor.py after each confirmed sell; decays every 20 closes.
        _exec_penalties: dict = memory.get_execution_penalties()

        scored: list[ObservedToken] = []
        evals:  list[dict]          = []
        new_hot: dict[str, ObservedToken] = {}

        for mint in self._active_watchlist:
            if mint == BASE_MINT or mint not in market_data:
                continue
            mdata = market_data[mint]
            sym   = mdata.get("symbol") or mint[:6]

            _b5m  = mdata.get("txns_5m_buys", 0)
            _s5m  = max(mdata.get("txns_5m_sells", 1), 1)
            _bs5m = _b5m / _s5m

            # ── Universal sellability floor (session 57) ──────────────────────
            # Applies to ALL tokens, static included. A pool too thin to exit
            # produces ghost-close total losses, so block the BUY here (exits are
            # unaffected — this path only builds the hot list).
            _sell_liq = mdata.get("liquidity_usd", 0) or 0
            if _sell_liq < _MIN_SELL_LIQ_USD:
                evals.append({
                    "mint": mint, "sym": sym, "action": "skip",
                    "conf": round(memory.confidence_score(mint), 2),
                    "reason": f"unsellable: liq ${_sell_liq/1000:.0f}k < ${_MIN_SELL_LIQ_USD/1000:.0f}k floor",
                })
                continue

            # Hard binary rejects (discovery tokens only)
            if mint not in self._static_set:
                age   = mdata.get("pair_age_hours", 0)
                chg24 = mdata.get("price_change_24h", 0)
                _tok_conf = round(memory.confidence_score(mint), 2)
                if age < 1.0:
                    evals.append({"mint": mint, "sym": sym, "action": "skip",
                                  "reason": f"pair {age:.1f}h < 1h", "conf": _tok_conf})
                    continue
                if mdata.get("paid_boost_active", False):
                    evals.append({"mint": mint, "sym": sym, "action": "skip",
                                  "reason": "paid boost", "conf": _tok_conf})
                    continue
                if chg24 < -35:
                    evals.append({"mint": mint, "sym": sym, "action": "skip",
                                  "reason": f"24h crash {chg24:.0f}%", "conf": _tok_conf})
                    continue

                _gem_vol5m = _config_mod.GEM_MIN_VOLUME_5M
                _gem_path  = (
                    age <= GEM_MAX_AGE_HOURS
                    and mdata["liquidity_usd"] >= GEM_MIN_LIQUIDITY
                    and _bs5m >= GEM_MIN_BUY_RATIO
                    and mdata["volume_5m"] >= _gem_vol5m
                )

                if mint not in self._social_cache:
                    sym_str = mdata.get("symbol", "")
                    if lunarcrush.ENABLED:
                        self._social_cache[mint] = await lunarcrush.get_social_score(client, sym_str)
                _social = self._social_cache.get(mint, -1.0)
                viral   = dexscreener.compute_viral_score(mdata, social_score=_social)
                mdata["viral_score"] = viral
                mdata["gem_path"]    = _gem_path
            else:
                _gem_path = False
                viral     = -1.0
                mdata["viral_score"] = 0.0
                mdata["gem_path"]    = False

            _tok_conf        = round(memory.confidence_score(mint), 2)
            _mode_floor      = MODES[mode]["MOMENTUM_MIN"]
            _mem_floor       = memory.get_momentum_floor(mint, _mode_floor)
            _effective_floor = max(_mode_floor, _mem_floor)

            profile = ValidationProfile(
                momentum_floor=_effective_floor,
                bs_floor=MODES[mode]["BUY_SELL_RATIO_MIN"],
                gem_path=_gem_path,
                viral_weight=_vw,
                mode=mode,
                regime=regime,
            )
            _cur_vol    = mdata.get("volume_5m", 0)
            _prev_vols  = self._vol_history.get(mint, [])
            _vol_accel  = (_cur_vol / (sum(_prev_vols) / len(_prev_vols))
                           if len(_prev_vols) >= 2 and sum(_prev_vols) > 0 else 1.0)
            _cur_liq    = mdata.get("liquidity_usd", 0)
            _liq_vel, _liq_accel = self._liq_momentum(mint, _cur_liq)
            # S71: realign the deep_pool/brain_rule drain-guard input to the sidecar's
            # validated liq-velocity window. Falls back to the self-computed _liq_vel when
            # the canary is off, the sidecar is standalone, or the mint is absent. The
            # validation score below still uses _liq_vel — only the guard input changes.
            _lvel_guard = _liq_vel
            _lvel_src   = "self"
            if _use_sidecar_lvel:
                _sc = self._sidecar_liq_vel.get(mint)
                if isinstance(_sc, (int, float)):
                    _lvel_guard = round(float(_sc), 4)
                    _lvel_src   = "sidecar"
            vscore = profile.score(
                vol5m=_cur_vol,
                momentum=mdata.get("price_change_5m", 0),
                buy_sell=_bs5m,
                liq_usd=_cur_liq,
                viral=viral,
                vol_accel=round(_vol_accel, 2),
                liq_vel=_liq_vel,
                liq_accel=_liq_accel,
                regime_mom=_use_regime_mom,   # S94 rec#3 canary
                regime_mom_scale=_use_regime_mscale,   # PIPE12 R-B2 canary
                is_whale=(mint in self._bc_hot_set),   # PIPE12 R-B1: on-chain whale buy
                whale_bonus=_use_whale_bonus,          # PIPE12 R-B1 canary
            )
            # Slippage-integrity feedback: reduce the liquidity dimension for tokens
            # whose past fills were consistently worse than Jupiter's quote.
            # Max reduction = 20 pts (at penalty 1.0).  Does not affect static watchlist.
            _exec_pen = _exec_penalties.get(mint, 0.0)
            if _exec_pen > 0.0:
                _pen_pts = round(_exec_pen * 20.0, 2)
                vscore.liquidity = round(max(0.0, vscore.liquidity - _pen_pts), 2)
                print(
                    f"  [EXEC-PEN] {sym} liq -{_pen_pts:.1f}pt "
                    f"(penalty={_exec_pen:.3f}, new_liq={vscore.liquidity:.1f})",
                    flush=True,
                )
            mdata["validation_score"] = vscore.total

            tok = ObservedToken(
                mint=mint, symbol=sym,
                score=vscore.total, threshold=vscore.threshold,
                passes=vscore.passes, regime=regime,
                gem_path=_gem_path, viral=round(viral, 2),
                momentum=round(mdata.get("price_change_5m", 0), 2),
                buy_sell=round(_bs5m, 2),
                vol5m=round(mdata.get("volume_5m", 0)),
                liq_usd=round(mdata.get("liquidity_usd", 0)),
                bc_hot=mint in self._bc_hot_set,
                score_detail={
                    "vac": round(vscore.vaccel, 1),    # S87 vol-accel dim (0-35)
                    "buy": round(vscore.buy, 1),       # S87 buy-pressure dim (0-25)
                    "liq": round(vscore.liquidity, 1),
                    "act": round(vscore.activity, 1),  # S87 activity dim (0-15)
                    "vaccel": round(_vol_accel, 2),
                    "lvel":      round(_lvel_guard, 3),   # S71: drain-guard input (sidecar-aligned when canary on)
                    "lvel_self": round(_liq_vel, 3),      # self-computed ~5×30s window (A/B reference)
                    "lvel_src":  _lvel_src,               # "sidecar" | "self" — which window drove the guard
                    "laccel": round(_liq_accel, 1),
                    "epen":   round(_exec_pen, 3),
                },
            )
            scored.append(tok)

            # Rolling vol history — updated for every scored token regardless of pass/fail.
            # Required by MR detection for vol-acceleration baseline (below).
            _ovh = self._vol_history.setdefault(mint, [])
            _ovh.append(tok.vol5m)
            if len(_ovh) > 10:
                _ovh.pop(0)

            # Balance-adjusted pass: when wallet is far below ◎2.0, raise the
            # effective threshold so only higher-quality signals get through.
            _effective_threshold = vscore.threshold + _bal_penalty
            _tok_passes = vscore.total >= _effective_threshold
            # Mirror the tok.passes field so downstream code (probe, MR) is consistent
            tok.passes    = _tok_passes
            tok.threshold = _effective_threshold

            if _tok_passes:
                # WR safety gate (session 52): when fleet win rate drops below 45%,
                # require minimum confidence 0.50 before adding to the hot list.
                # Prevents a wider discovery net from amplifying losses during a bad streak.
                _WR_CONF_FLOOR     = 0.45
                _SAFE_CONF_MIN     = 0.50
                if fleet_wr < _WR_CONF_FLOOR and _tok_conf < _SAFE_CONF_MIN:
                    print(
                        f"  [SAFETY] {sym} conf {_tok_conf:.2f} < {_SAFE_CONF_MIN} "
                        f"(fleet WR {fleet_wr:.0%} < {_WR_CONF_FLOOR:.0%}) — skipped",
                        flush=True,
                    )
                    evals.append({
                        "mint": mint, "sym": sym, "action": "skip",
                        "conf": _tok_conf,
                        "reason": f"WR {fleet_wr:.0%} < {_WR_CONF_FLOOR:.0%} — conf {_tok_conf:.2f} < {_SAFE_CONF_MIN} floor",
                        "score": round(vscore.total, 1),
                    })
                else:
                    # Look-ahead liquidity gate: dynamic floor based on LP velocity.
                    # Filling pools (v_liq > 0) get a lower bar — depth is growing.
                    # Stagnant or draining pools require a larger safety margin.
                    # Only applies to discovery tokens; gem-path and static watchlist exempt.
                    _liq_floor = 40_000.0 if _liq_vel > 0 else 80_000.0
                    if (
                        not _gem_path
                        and mint not in self._static_set
                        and _cur_liq < _liq_floor
                    ):
                        print(
                            f"  [LOOKAHEAD] {sym} liq ${_cur_liq/1000:.0f}k "
                            f"< ${_liq_floor/1000:.0f}k floor "
                            f"(v_liq={_liq_vel:+.3f}) — skipped",
                            flush=True,
                        )
                        tok.passes = False
                        evals.append({
                            "mint": mint, "sym": sym, "action": "skip",
                            "conf": _tok_conf,
                            "reason": (
                                f"look-ahead liq ${_cur_liq/1000:.0f}k "
                                f"< ${_liq_floor/1000:.0f}k floor (v_liq={_liq_vel:+.3f})"
                            ),
                            "score": round(vscore.total, 1),
                        })
                    else:
                        # S87: drain-at-entry exitability guard (price-action plays). A pool
                        # already bleeding depth at entry is the ghost_close leading indicator —
                        # the deep_pool path skips these (S70); the gem/relay/quick path did not.
                        _entry_lvel = tok.score_detail.get("lvel", 0.0)
                        if _ENTRY_DRAIN_GUARD_ENABLED and _entry_lvel < _DEEP_POOL_MAX_DRAIN:
                            print(f"  [DRAIN-GUARD] {sym[:6]} skip — pool draining {_entry_lvel*100:.1f}%"
                                  f" at entry (liq ${_cur_liq/1000:.0f}k, lvel:{tok.score_detail.get('lvel_src','self')})"
                                  f" — exitability/ghost risk", flush=True)
                            tok.passes = False
                            evals.append({
                                "mint": mint, "sym": sym, "action": "skip",
                                "conf": _tok_conf,
                                "reason": f"drain-at-entry {_entry_lvel*100:.1f}% (ghost risk)",
                                "score": round(vscore.total, 1),
                            })
                        elif _GEM_SNIPER_SKIP and _gem_path and regime == "sniper":
                            # S104: the gem×sniper slice is the worst-EV pocket fleet-wide
                            # (net −0.083◎, EV −0.38%/trade). Skip it — entry-only filter.
                            print(f"  [GEM×SNIPER] {sym[:6]} skip — gem path in sniper regime"
                                  f" (the −EV rug slice, S104)", flush=True)
                            tok.passes = False
                            evals.append({
                                "mint": mint, "sym": sym, "action": "skip",
                                "conf": _tok_conf,
                                "reason": "gem×sniper slice (−EV rug pocket, S104)",
                                "score": round(vscore.total, 1),
                            })
                        elif (_antidead_on and mint not in self._static_set
                              and tok.vol5m < _antidead_min_v5):
                            # S107 anti-dead floor: a price-action pool already this quiet at entry
                            # is most likely to dead-pool-exit flat (paying friction) — skip it.
                            print(f"  [ANTI-DEAD] {sym[:6]} skip — v5 ${tok.vol5m/1000:.1f}k"
                                  f" < ${_antidead_min_v5/1000:.1f}k floor (dead-flat risk, S107)", flush=True)
                            tok.passes = False
                            evals.append({
                                "mint": mint, "sym": sym, "action": "skip",
                                "conf": _tok_conf,
                                "reason": f"anti-dead v5 ${tok.vol5m/1000:.1f}k < ${_antidead_min_v5/1000:.1f}k",
                                "score": round(vscore.total, 1),
                            })
                        else:
                            new_hot[mint] = tok
                            evals.append({
                                "mint": mint, "sym": sym, "action": "pre_qualified",
                                "conf": _tok_conf,
                                "score": round(vscore.total, 1), "threshold": _effective_threshold,
                                "mom":  tok.momentum, "bs": tok.buy_sell,
                                "viral": tok.viral, "gem": _gem_path, "bc_hot": tok.bc_hot,
                            })
            else:
                # ── S107 momentum-override lane ─────────────────────────────────────
                # The scorer gates the running-momentum runner cohort to ~0 (its liq is below
                # the $30k floor), but it's the single most +EV entry signal under a trail exit
                # (research/evolve_s107). Admit it HERE — when the score path rejected — for the
                # exact signature, behind exitability guards (drain-at-entry + a liq belt; rug_screen
                # + ×0.5 size + the RIDE exit are applied downstream in main.py/stoic). Non-static
                # only; tagged momentum_override so it's sized down + trail-exited + logged. Can only
                # ADD a screened, size-down position → THE ONE OPERATOR RULE intact.
                _ovr_lvel = tok.score_detail.get("lvel", 0.0)
                if (_use_mom_override
                        and mint not in self._static_set
                        and tok.momentum >= _MOM_OVR_M5_MIN
                        and tok.buy_sell >= _MOM_OVR_BS_MIN
                        and tok.vol5m   >= _MOM_OVR_V5_MIN
                        and _cur_liq    >= _MOM_OVR_LIQ_MIN
                        and not (_ENTRY_DRAIN_GUARD_ENABLED and _ovr_lvel < _DEEP_POOL_MAX_DRAIN)):
                    tok.passes = True
                    tok.momentum_override = True
                    mdata["momentum_override"] = True
                    new_hot[mint] = tok
                    print(f"  [MOM-OVERRIDE] {sym[:6]} ADMIT — m5={tok.momentum:.0f}% "
                          f"bs={tok.buy_sell:.2f} v5=${tok.vol5m/1000:.1f}k liq=${_cur_liq/1000:.0f}k "
                          f"(score {vscore.total:.0f} gated → runner lane, ×0.5 size, trail exit)", flush=True)
                    evals.append({
                        "mint": mint, "sym": sym, "action": "pre_qualified",
                        "conf": _tok_conf, "score": round(vscore.total, 1),
                        "mom": tok.momentum, "bs": tok.buy_sell, "momentum_override": True,
                    })
                else:
                    _penalty_tag = f" +{_bal_penalty:.0f}pt bal-adj" if _bal_penalty >= 0.5 else ""
                    evals.append({
                        "mint": mint, "sym": sym, "action": "skip",
                        "conf": _tok_conf,
                        "reason": vscore.reject_reason() + _penalty_tag,
                        "score": round(vscore.total, 1),
                    })

        # ── Active probing ─────────────────────────────────────────────────────
        # When the market is dead (agg_vol < $50k) and we're in INSANE mode,
        # look for discovery tokens with growing liquidity even though vel=0.
        # These are accumulation setups: someone is adding LP before volume arrives.
        # The probe entry bypasses the velocity gate; position size is capped at 0.5%.
        #
        # WR gate (revised): the original WR≥60% gate created a deadlock — the only
        # bot that can probe (INSANE Bot 1) sits at ~22% WR, so probing was PERMANENTLY
        # suppressed and the fleet never gathered dead-market data. Probe positions are
        # 0.5% of wallet (true scout sizing), so the exploration cost is trivial
        # (~◎0.0025 on a ◎0.5 wallet). The point of probing is to FIND edge in conditions
        # where the momentum signal is silent — gate it off and you can never learn.
        # Edge of probe entries is measured offline by signal_lab.py / edge_report.py.
        # Now enabled for WILD too, giving Bot 2 a dead-market mechanism.
        _PROBE_VOL_GATE    = 50_000   # only probe in dead markets
        _PROBE_LIQ_GROWTH  = 0.0005   # 0.05% liq growth from prior cycle
        _PROBE_LIQ_FLOOR   = 30_000   # minimum $30k pool depth
        _PROBE_MIN_FLEET_WR = 0.0     # exploratory at 0.5% size — no WR gate (was 0.60, deadlocked)
        if mode in ("insane", "wild") and self._agg_vol_5m < _PROBE_VOL_GATE and self._prev_liq and fleet_wr >= _PROBE_MIN_FLEET_WR:
            _probe_n = 0
            for tok in scored:
                mint = tok.mint
                if mint in new_hot or mint in self._static_set:
                    continue
                if tok.momentum > 0.5:
                    continue  # S87: only flat-momentum tokens (was score_detail["vel"]==0)
                if tok.buy_sell < 0.3:
                    continue  # require at least marginal buy presence
                prev_liq = self._prev_liq.get(mint, 0)
                curr_liq = tok.liq_usd
                if prev_liq <= 0 or curr_liq < _PROBE_LIQ_FLOOR:
                    continue
                liq_growth = (curr_liq - prev_liq) / prev_liq
                if liq_growth < _PROBE_LIQ_GROWTH:
                    continue
                lgp = round(liq_growth * 100, 4)
                probe_tok = ObservedToken(
                    mint=tok.mint, symbol=tok.symbol,
                    score=tok.score, threshold=tok.threshold,
                    passes=True, regime=regime,
                    gem_path=tok.gem_path, viral=tok.viral,
                    momentum=tok.momentum, buy_sell=tok.buy_sell,
                    vol5m=tok.vol5m, liq_usd=tok.liq_usd,
                    bc_hot=tok.bc_hot, score_detail=tok.score_detail,
                    probe=True, liq_growth_pct=lgp,
                )
                new_hot[mint] = probe_tok
                for ev in evals:   # promote existing skip entry so dashboard shows pre_qualified
                    if ev.get("mint") == mint:
                        ev["action"]         = "pre_qualified"
                        ev["probe"]          = True
                        ev["liq_growth_pct"] = lgp
                        break
                _probe_n += 1
                print(
                    f"  [PROBE] {tok.symbol[:6]} liq+{lgp:.3f}%"
                    f" (${curr_liq/1000:.0f}k) b/s={tok.buy_sell:.2f}",
                    flush=True,
                )
            if _probe_n:
                print(
                    f"  [PROBE] {_probe_n} accumulation candidate(s)"
                    f" (agg_vol ${self._agg_vol_5m/1000:.0f}k)",
                    flush=True,
                )
        elif mode in ("insane", "wild") and self._agg_vol_5m < _PROBE_VOL_GATE and fleet_wr < _PROBE_MIN_FLEET_WR:
            print(
                f"  [PROBE] suppressed — fleet WR {fleet_wr:.0%} < {_PROBE_MIN_FLEET_WR:.0%} required"
                f" (agg_vol ${self._agg_vol_5m/1000:.0f}k)",
                flush=True,
            )

        # ── Mean-reversion detection ────────────────────────────────────────────
        # After normal scoring and probe detection, look for washout setups:
        # sharp 5m dip (-5% to -10%) with volume accelerating — exhausted sellers.
        # Only INSANE mode; discovery tokens only (static blue chips have different dynamics).
        if mode == "insane":
            _mr_strat = MeanReversionStrategy()
            _mr_n     = 0
            for tok in scored:
                mint = tok.mint
                if mint in new_hot or mint in self._static_set:
                    continue
                _ok, _reason, _vol_accel = _mr_strat.qualify(
                    mint, market_data[mint], self._vol_history.get(mint, [])
                )
                if not _ok:
                    continue
                mr_tok = ObservedToken(
                    mint=tok.mint, symbol=tok.symbol,
                    score=tok.score, threshold=tok.threshold,
                    passes=True, regime=regime,
                    gem_path=tok.gem_path, viral=tok.viral,
                    momentum=tok.momentum, buy_sell=tok.buy_sell,
                    vol5m=tok.vol5m, liq_usd=tok.liq_usd,
                    bc_hot=tok.bc_hot, score_detail=tok.score_detail,
                    mean_reversion=True, mr_vol_accel=_vol_accel,
                )
                new_hot[mint] = mr_tok
                for ev in evals:   # promote existing skip entry
                    if ev.get("mint") == mint:
                        ev["action"]       = "pre_qualified"
                        ev["mean_rev"]     = True
                        ev["mr_vol_accel"] = _vol_accel
                        break
                _mr_n += 1
                print(
                    f"  [MR] {tok.symbol[:6]}"
                    f" dip={tok.momentum:.1f}% vol×{_vol_accel:.1f}"
                    f" liq=${tok.liq_usd/1000:.0f}k",
                    flush=True,
                )
            if _mr_n:
                print(f"  [MR] {_mr_n} washout candidate(s)", flush=True)

        # ── Deep-pool momentum detection (S67) ───────────────────────────────────
        # The brain-validated edge as an entry path: admit LIQUID movers that match
        # deep_pool_quality (m5≥1% & liq/mcap≥0.10 & buy/sell≥1) even if the regime
        # score-threshold rejected them. This lets the fleet trade the proven edge in
        # ANY market (the normal path only fires it when the whole market is hot).
        # Tokens already in new_hot (passed normal scoring) are skipped — no dupes.
        # RugCheck + ban + risk checks still run downstream in execute_buy().
        _dp_normal_slice = (regime == "normal" and _use_normal_dp)   # S98: tight-slice canary path
        if (_DEEP_POOL_ENABLED and mode in ("insane", "wild", "stoic")  # S74-bot3: STOIC joins the race (deep_pool edge)
                and not admit_guard.allowlist_blocks("deep_pool", bot=_BOT_ID)  # S121-dpgap: allowlist freeze covers the disproven deep_pool book
                and not regime_policy.observer_blocks(_BOT_ID, "deep_pool")     # TAXONOMY: DEEP row frozen in the v2 table (fail-open False when canary off)
                and (regime not in _DEEP_POOL_SKIP_REGIMES               # S82: no deep_pool entries in -EV regimes (DEAD)
                     or _dp_normal_slice                                # S98: allow the proven tight normal slice (canary)
                     or _use_dust_shadow)):                             # S99: dust lane runs the predicate in skipped regimes (at dust size)
            _dp_n = 0
            for tok in scored:
                mint = tok.mint
                if mint in new_hot or mint in self._static_set:
                    continue
                if tok.liq_usd < _MIN_SELL_LIQ_USD:
                    continue  # must be exitable (defensive — scored is already sellable-only)
                # S70 drain-at-entry exitability guard — skip pools already bleeding depth
                # (the ghost predictor; see _DEEP_POOL_MAX_DRAIN). liq≥50k above guarantees a
                # valid latest read, so a negative lvel here is a genuine decline, not a glitch.
                _lvel = tok.score_detail.get("lvel", 0.0)
                if _lvel < _DEEP_POOL_MAX_DRAIN:
                    print(f"  [DEEP-POOL] {tok.symbol[:6]} skip — pool draining {_lvel*100:.1f}%"
                          f" (liq ${tok.liq_usd/1000:.0f}k, lvel:{tok.score_detail.get('lvel_src','self')})"
                          f" — exitability risk", flush=True)
                    continue
                if tok.momentum < _DEEP_POOL_M5_MIN or tok.buy_sell < _DEEP_POOL_BS_MIN:
                    continue
                _mc = (market_data.get(mint, {}) or {}).get("market_cap", 0) or 0
                _liq_mc = (tok.liq_usd / _mc) if _mc > 0 else 0.0
                if _liq_mc < _DEEP_POOL_LIQ_MC:
                    continue
                # S75: identify the STRONGEST brain sub-cohort this token also matches, so
                # EV-sizing can size it by that sub-rule's measured (large) edge instead of the
                # weak parent deep_pool_quality. Admission already guarantees liq_mc≥0.10 & bs≥1,
                # so strict reduces to m5≥2; filling = pool gaining depth (lvel>0.01, brain #1 edge).
                _filling = _lvel > 0.01
                _strict  = tok.momentum >= 2.0 and _liq_mc >= 0.10 and tok.buy_sell >= 1.0
                # S98: on the normal-canary path admit ONLY the proven tight slice
                # (filling AND buy-pressure≥1.5 = the +EV core; the -EV normal bulk stays barred).
                # S99: BUT if the dust lane is on, a normal token that ISN'T the real tight slice
                # is NOT dropped — it falls through to become a DUST evidence probe (so dust covers
                # the whole skipped normal tape, not just `dead`, even when normal_dp is co-enabled).
                _tok_is_real_slice = (_dp_normal_slice and _filling and tok.buy_sell >= _NORMAL_DP_BS_MIN)
                if _dp_normal_slice and not _tok_is_real_slice and not _use_dust_shadow:
                    continue
                if _strict and _filling:
                    _strong_rule = "deep_pool_strict_filling"
                elif _filling:
                    _strong_rule = "deep_pool_filling"
                elif _strict:
                    _strong_rule = "deep_pool_strict"
                else:
                    _strong_rule = ""   # plain deep_pool_quality → keeps small C² sizing
                # S74: drop the weak flat-pool tail — admit only robust sub-cohorts.
                if _DEEP_POOL_REQUIRE_STRONG and not _strong_rule:
                    print(f"  [DEEP-POOL] {tok.symbol[:6]} skip — plain deep_pool_quality"
                          f" (no strict/filling sub-edge, weak tail ev_lo 0.5):"
                          f" m5={tok.momentum:.1f}% lvel={_lvel:+.3f} liq/mc={_liq_mc:.2f}",
                          flush=True)
                    continue
                # S99: a token reaching here in a SKIPPED regime that is NOT the real
                # normal-slice was admitted ONLY because the dust lane is on → size it to
                # dust + tag the close. The predicate above is IDENTICAL (strict/filling,
                # drain guard, liq/mc, bs, m5) — only the regime gate is opened and the
                # size differs downstream. Dust closes feed the shadow gate, never the
                # live arming gate (prestige_tracker excludes dust_shadow). Skipped-regime
                # closes are ALSO dropped by the live gate's own regime filter, so this is
                # belt-and-suspenders.
                _is_dust = (_use_dust_shadow
                            and regime in _DEEP_POOL_SKIP_REGIMES
                            and not _tok_is_real_slice)
                dp_tok = ObservedToken(
                    mint=tok.mint, symbol=tok.symbol,
                    score=tok.score, threshold=tok.threshold,
                    passes=True, regime=regime,
                    gem_path=tok.gem_path, viral=tok.viral,
                    momentum=tok.momentum, buy_sell=tok.buy_sell,
                    vol5m=tok.vol5m, liq_usd=tok.liq_usd,
                    bc_hot=tok.bc_hot, score_detail=tok.score_detail,
                    deep_pool=True, deep_pool_liq_mc=round(_liq_mc, 4),
                    deep_pool_strong_rule=_strong_rule,
                    normal_slice=_tok_is_real_slice,  # S98-reconcile/S99: TOKEN-level real-slice flag (a dusted non-slice normal token must NOT be tagged normal_slice)
                    dust_shadow=_is_dust,            # S99: dust lane entry (skipped-regime, dust-sized, shadow-gate only)
                )
                new_hot[mint] = dp_tok
                for ev in evals:   # promote existing skip entry so dashboard shows pre_qualified
                    if ev.get("mint") == mint:
                        ev["action"]    = "pre_qualified"
                        ev["deep_pool"] = True
                        ev["liq_mc"]    = round(_liq_mc, 4)
                        break
                _dp_n += 1
                print(
                    f"  [DEEP-POOL{'·DUST' if _is_dust else ''}] {tok.symbol[:6]} m5={tok.momentum:.1f}%"
                    f" liq/mc={_liq_mc:.2f} b/s={tok.buy_sell:.2f}"
                    f" liq=${tok.liq_usd/1000:.0f}k reg={regime[:4]}"
                    f"{' ◆'+_strong_rule if _strong_rule else ''}",
                    flush=True,
                )
            if _dp_n:
                print(f"  [DEEP-POOL] {_dp_n} liquid-mover candidate(s)", flush=True)

        # ── Generalized brain-rule registry (S67) ────────────────────────────────
        # The deep_pool path made universal: admit LIQUID movers matching ANY brain
        # candidate that currently clears the robustness bar (live_rule.admit_rules()).
        # As the hourly brain re-evaluates, the live rule set auto-updates with no code
        # change. Tokens already admitted (normal scoring or deep_pool) are skipped.
        if (_BRAIN_RULES_ENABLED and mode in ("insane", "wild", "stoic") and _live_rule is not None  # S74-bot3
                and not admit_guard.allowlist_blocks("brain_rule", bot=_BOT_ID)  # S121-dpgap: allowlist freeze covers brain_rule too
                and not regime_policy.observer_blocks(_BOT_ID, "brain_rule")):   # TAXONOMY: DEEP row frozen in the v2 table
            try:
                _admit = _live_rule.admit_rules()
            except Exception:
                _admit = []
            if _admit:
                _br_n = 0
                for tok in scored:
                    mint = tok.mint
                    if mint in new_hot or mint in self._static_set:
                        continue
                    if tok.liq_usd < _MIN_SELL_LIQ_USD:
                        continue  # must be exitable
                    # S70 drain-at-entry exitability guard (same rationale as deep_pool path)
                    _lvel = tok.score_detail.get("lvel", 0.0)
                    if _lvel < _DEEP_POOL_MAX_DRAIN:
                        print(f"  [BRAIN-RULE] {tok.symbol[:6]} skip — pool draining {_lvel*100:.1f}%"
                              f" (liq ${tok.liq_usd/1000:.0f}k, lvel:{tok.score_detail.get('lvel_src','self')})"
                              f" — exitability risk", flush=True)
                        continue
                    _mc = (market_data.get(mint, {}) or {}).get("market_cap", 0) or 0
                    _liq_mc = (tok.liq_usd / _mc) if _mc > 0 else 0.0
                    _feats = {
                        "score":  tok.score,
                        "m5":     tok.momentum,
                        "bs":     tok.buy_sell,
                        "liq_mc": _liq_mc,
                        "vacc":   tok.score_detail.get("vaccel", 1.0),
                        "whale":  0,
                        "lqv":    tok.score_detail.get("lvel", 0.0),
                        "dbt":    0,
                    }
                    _m = _live_rule.match_rule(_feats, _admit)
                    if not _m:
                        continue
                    br_tok = ObservedToken(
                        mint=tok.mint, symbol=tok.symbol,
                        score=tok.score, threshold=tok.threshold,
                        passes=True, regime=regime,
                        gem_path=tok.gem_path, viral=tok.viral,
                        momentum=tok.momentum, buy_sell=tok.buy_sell,
                        vol5m=tok.vol5m, liq_usd=tok.liq_usd,
                        bc_hot=tok.bc_hot, score_detail=tok.score_detail,
                        brain_rule=True, brain_rule_name=_m["name"],
                    )
                    new_hot[mint] = br_tok
                    for ev in evals:   # promote existing skip entry so dashboard shows pre_qualified
                        if ev.get("mint") == mint:
                            ev["action"]     = "pre_qualified"
                            ev["brain_rule"] = _m["name"]
                            break
                    _br_n += 1
                    print(
                        f"  [BRAIN-RULE] {tok.symbol[:6]} via {_m['name']}"
                        f" (ev_lo +{_m['ev_lo']:.1f}%) m5={tok.momentum:.1f}%"
                        f" liq/mc={_liq_mc:.2f} b/s={tok.buy_sell:.2f}",
                        flush=True,
                    )
                if _br_n:
                    print(
                        f"  [BRAIN-RULE] {_br_n} candidate(s) from {len(_admit)} live rule(s)",
                        flush=True,
                    )

        # ── Force-fire gradient signals ─────────────────────────────────────────
        # Query on-chain bonding curve accounts for the top-N hot mints and surface
        # high-gradient / early-stage tokens as Force-FIRE candidates.
        # These tokens have no DexScreener data yet — they bypass ValidationProfile
        # entirely.  RugCheck still runs inside execute_buy(); ban check is here.
        # Only INSANE mode: WILD/STOIC universes don't overlap with pump.fun BC tokens.
        self._gradient_signals = []
        if mode == "insane" and _gradient_scan_on():   # S86: skip when dead-weight-disabled
            try:
                self._gradient_signals = await bonding_curve.scan_gradient(
                    client,
                    _config_mod.HELIUS_RPC_URL,
                    self._sol_price,
                    n=5,
                )
            except Exception as _ge:
                print(f"  [GRADIENT] scan error: {_ge}", flush=True)

            for _gs in self._gradient_signals:
                _gm = _gs["mint"]
                if _gm in new_hot or _gm in self._static_set:
                    continue
                if memory.is_banned(_gm, 0):
                    continue
                ff_tok = ObservedToken(
                    mint=_gm, symbol=_gm[:6],
                    score=0.0, threshold=0.0, passes=True,
                    regime=regime, gem_path=False, viral=-1.0,
                    momentum=0.0,
                    buy_sell=2.0,  # WS confirmed buys only — bias assumed bullish
                    vol5m=0.0, liq_usd=0.0, bc_hot=True,
                    score_detail={},
                    force_fire=True,
                    gradient_sol_per_min=_gs.get("gradient_sol_per_min", 0.0),
                )
                new_hot[_gm] = ff_tok
                evals.append({
                    "mint": _gm, "sym": _gm[:6], "action": "pre_qualified",
                    "conf": round(memory.confidence_score(_gm), 2),
                    "force_fire": True,
                    "gradient":   _gs.get("gradient_sol_per_min", 0),
                    "real_sol":   _gs.get("real_sol", 0),
                    "score": 0.0,
                })

        # Tag BC hot mints across all evals
        for ev in evals:
            if ev.get("mint") in self._bc_hot_set:
                ev["bc_hot"] = True

        # Capture current liquidity for next cycle's probe growth computation
        self._prev_liq = {
            m: market_data[m].get("liquidity_usd", 0)
            for m in self._active_watchlist
            if m in market_data
        }

        self._hot_cache   = new_hot
        self._all_scored  = scored
        self._token_evals = evals

        elapsed = time.monotonic() - self._scan_start_ts
        self._last_scan_ms = int(elapsed * 1000)
        self._scan_count  += 1

    # ── Dashboard snapshot ─────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        hot = self.get_hot_list()
        bc  = bonding_curve
        return {
            "regime":          self._regime,
            "agg_vol_5m":      round(self._agg_vol_5m),
            "pool_size":       len(self._active_watchlist),
            "qualified_count": len(hot),
            "probe_count":     sum(1 for t in hot if t.probe),
            "mr_count":        sum(1 for t in hot if t.mean_reversion),
            "ff_count":        sum(1 for t in hot if t.force_fire),
            "skipped_count":   sum(1 for t in self._all_scored if not t.passes),
            "scan_count":      self._scan_count,
            "last_scan_ms":    self._last_scan_ms,
            "disc_ran":        self._disc_ran,
            "sources":         self._sources,
            "hot_list": [
                {
                    "mint":   t.mint,     "sym":    t.symbol,
                    "score":  round(t.score, 1), "threshold": t.threshold,
                    "mom":    t.momentum, "bs":     t.buy_sell,
                    "vol5m":  int(t.vol5m), "liq":  int(t.liq_usd),
                    "gem":    t.gem_path, "viral":  t.viral,
                    "bc":     t.bc_hot,   "detail": t.score_detail,
                }
                for t in hot[:12]
            ],
            "bonding_curve": {
                "connected":  bc.connected,
                "hot_count":  len(self._bc_hot),
                "hot_mints": [
                    {"mint": m, **(bc.get_buy_activity(m) or {})}
                    for m in self._bc_hot[:8]
                    if bc.get_buy_activity(m)
                ],
                "top_whales": bc.get_top_whales(3),
                "stats":      dict(bc._stats),
            },
            # Passed through to _thinking["tokens"] for backward compat
            "token_evals": self._token_evals,
        }
