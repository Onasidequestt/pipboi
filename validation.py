"""
Token entry quality scorer — four-dimension validation before Jupiter quote attempts.

FIVE volatility regimes (S79 ladder), each a fully self-contained profile per mode.
Do not mix them.  Selected by agg_vol_5m in main.py with hysteresis on every boundary.

  EUPHORIA    Blow-off frenzy (agg_vol_5m ≥ $600k).
              Widest score net, real momentum floor — ride strength. Exits protect.
  AGGRESSIVE  High-volatility, risk-on ($280k–$600k).
              Lower floors/targets; catch breakouts early.
  NORMAL      Neutral band ($110k–$280k) or startup default.
              Balanced floors and targets, threshold 80.
  SNIPER      Quiet, low-momentum ($48k–$110k).
              Reachable bar (recalibrated S79); lean on the deep_pool edge.
  DEAD        Near-frozen (< $48k).
              Lowest bar so something can qualify; EDGE-ONLY by intent, small + careful.

  ⚠ S79 -EV NOTE: SNIPER & DEAD were deliberately loosened to keep the fleet trading in
  quiet/dead tapes (operator request). Pure-momentum entries there are structurally -EV;
  the deep_pool/brain_rule paths are the real edge. High liquidity targets + the separate
  hard $50k sellability floor keep ghosts out even at the lower score bars.

Scoring dimensions (0–25 each, total 0–100):
  Volume    log scale — early vol gains rewarded more than marginal top-end gains
  Velocity  linear — momentum (0–15) + buy/sell pressure (0–10)
  Liquidity log scale — depth for fills without slippage
  Social    linear — viral signal quality; neutral 18.75 when data unavailable

Momentum floor is enforced via scoring: velocity = 0 when mom < floor.
Max score without velocity = 75. EUPHORIA/AGGRESSIVE/NORMAL thresholds (68–80) and the
recalibrated SNIPER/DEAD thresholds (64–76) are reachable on volume+liquidity+social, so
a strong, sellable but flat token CAN qualify in a quiet tape (intended, S79).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

# Anchors for log scales — near-zero positives so log() is always defined.
_VOL_REF = 100.0       # $100 vol5m
_LIQ_REF = 10_000.0   # $10k liquidity

# ── S87 scorer-rework constants ──────────────────────────────────────────────
# Dimensions reweighted by MEASURED realizable-EV predictiveness (forward_obs,
# deep-pool subset): vol-acceleration leads (Q5-Q1 +4.2 on deep pools), momentum
# demoted (~+0.3), liquidity reframed as a saturating SELLABILITY qualifier.
# S94: applies the S93 score-quest recommendations. (1) flat threshold 60→65 —
# prod @65 is the best-supported lever (ev_lo −0.03, h2 +0.44, ghost 0, ~22% admit).
# (2) modest rebalance TOWARD depth, AWAY from vol_accel (liq is the one stable
# load-bearing dim; vaccel/momentum sign-flips with regime). Caps re-weighted but the
# total still sums to 100 (the freed 15 pts go to liq) so the 65 bar keeps its analyzed
# ~22% admit rate — NOT stacked onto a shrunken max (avoids the S89/S91 starvation trap).
_S87_FLAT_THRESHOLD = 65.0       # S94: 60→65 (was operator S87 flat-60)
_SELL_FLOOR_USD     = 30_000.0   # hard exitability gate — below this you can't exit
_LIQ_FULL_USD       = 120_000.0  # liquidity dim saturates here (depth beyond = no EV)
# S104 dimension caps (sum of non-bonus dims = 100): vaccel 25→15, buy 25→30, liq 40 (kept),
# activity 10→15. Direct from the timing analysis (forward_obs path-extremes, 20.5k obs):
#   • vol_accel is WEAK in peak-reach — flat across buckets — despite being the lead dim
#     (corroborates S93's "vaccel sign-flips / fragile") → demote 25→15.
#   • buy-pressure (bs 1.0–2.0+) strongly predicts peaks (P(max≥15%) 17–19% vs 6% at bs<0.8) → 25→30.
#   • ★ EXTREME 5m momentum (m5>10) is the single strongest entry signal: meanFwd +9.79%, 26% reach
#     +30%, stable across both time-halves, 151 distinct mints. Stacked with bs≥1.2 & lqv≥0 → +15.73%.
#     The activity dim is rebuilt 10→15 with a continuation reward that maxes at m5≈8% (rec C).
# Sum still = 100 (15+30+40+15) so threshold 65 keeps its admit rate (no S89/S91 starvation).
_VACC_MAX_PTS       = 15.0       # S104: vol_accel cap 25→15 (weak in path-extremes)
_VACC_NEUTRAL_PTS   = 11.16      # vol_accel=1.0 anchor, rescaled 18.6→11.16 (=18.6·15/25)
_VACC_SLOPE         = 3.86       # per-unit slope, rescaled 6.43→3.86 (=6.43·15/25); reaches 15 at vacc≈2.0
_BUY_MAX_PTS        = 30.0       # S104: buy-pressure cap 25→30 — robust monotonic peak predictor
_LIQ_MAX_PTS        = 40.0       # liquidity (sellability) cap — the one stable dim (kept)
_ACT_VOL_MAX        = 6.67       # activity volume half (kept)
_ACT_MOM_MAX        = 8.33       # S104: activity momentum half 3.33→8.33 — carries the m5>10 continuation edge
_ACT_MOM_FULL_PCT   = 8.0        # S104: momentum reward maxes at m5≈8% (so the +10%+ continuation cohort scores full)
# S94 rec#3: regime-conditional momentum (canary A/B). In PRO regimes reward up-momentum
# (current); in ANTI regimes reward flat/dip (the proven dead/normal sign-flip). Gated.
_PRO_MOMENTUM_REGIMES  = frozenset({"euphoria", "aggressive"})
_ANTI_MOMENTUM_REGIMES = frozenset({"dead", "normal"})

# PIPE12 (R-B1) — whale bonus. `whale` = a pump.fun on-chain whale buy (≥0.5 SOL single entry)
# detected via the Helius WS bonding-curve monitor (the strongest both-halves-stable signal in
# forward_obs: +48% paper-ride EV). It's a tie-break BONUS on top of the gated score — a gated
# (unsellable/draining) token stays 0, so the bonus only lifts a whale buy INTO a SELLABLE pool
# (the Jotchua intersection). Default-OFF canary; validate-first via research/pipe12/whale_validate.py
# (which currently shows 0 tradeable whale closes — keep OFF until the intersection has live evidence).
_WHALE_BONUS_PTS       = 8.0   # whale present → up to +8; +full only with buy confirmation (bs≥1.5)

# PIPE12 (R-B2) — regime-SCALED momentum (refines S94: SCALE the up-momentum weight by regime,
# don't FLIP its sign). WS-B on 20,508 rows: high-vacc is +EV in EVERY regime (no flip), but the
# payoff is regime-scaled (euphoria Δ+3.79 ≫ aggressive +1.62 ≫ normal +1.29 ≫ sniper/dead ≈+0.17).
# Distinct canary from regime_momentum (the S94 anti-flip) so they don't collide. Default-OFF.
_REGIME_MOM_SCALE = {"euphoria": 1.0, "aggressive": 0.8, "normal": 0.6, "sniper": 0.3, "dead": 0.2}

Regime = Literal["euphoria", "aggressive", "normal", "sniper", "dead"]  # S79: 5-regime ladder


# ── Per-regime, per-mode parameter sets ──────────────────────────────────────
# Every field is explicit.  Parameters are never inherited from another regime.

@dataclass(frozen=True)
class _RegimeConfig:
    pass_threshold: float  # score required to proceed to a Jupiter quote
    momentum_floor: float  # % — velocity = 0 when mom < this
    bs_floor:       float  # buy/sell floor — velocity b/s component = 0 below this
    mom_target:     float  # % — at this level, velocity-mom component = 15/15
    bs_target:      float  # at this b/s, velocity-b/s component = 10/10
    vol_target:     float  # $ vol5m — std path full score
    liq_target:     float  # $ liq_usd — std path full score
    gem_vol_target: float  # $ vol5m — gem-path full score
    gem_liq_target: float  # $ liq_usd — gem-path full score
    viral_target:   float  # viral ≥ this → social = 25/25


# S79 — EUPHORIA: blow-off frenzy (agg_vol >= $600k). Everything is pumping, so the
# score bar is the widest of any regime (lots qualifies), but the momentum floor stays
# real so we ride genuine strength, not the flat tail. Blow-off reversals are vicious —
# exits/trailing (handled in stoic_strategy/tiers) do the protecting, not the entry gate.
_EUPHORIA: dict[str, _RegimeConfig] = {
    "insane": _RegimeConfig(
        pass_threshold=68.0,
        momentum_floor=3.0,    bs_floor=0.3,
        mom_target=10.0,       bs_target=1.5,
        vol_target=15_000,     liq_target=120_000,
        gem_vol_target=5_000,  gem_liq_target=45_000,
        viral_target=0.50,
    ),
    "wild": _RegimeConfig(
        pass_threshold=72.0,
        momentum_floor=0.5,    bs_floor=0.4,
        mom_target=3.0,        bs_target=1.5,
        vol_target=10_000,     liq_target=80_000,
        gem_vol_target=4_000,  gem_liq_target=35_000,
        viral_target=0.50,
    ),
    "stoic": _RegimeConfig(
        # Vault Keeper refuses to FOMO even in euphoria — stays near the NORMAL bar.
        pass_threshold=78.0,
        momentum_floor=2.0,    bs_floor=1.0,
        mom_target=4.0,        bs_target=1.8,
        vol_target=18_000,     liq_target=200_000,
        gem_vol_target=7_000,  gem_liq_target=60_000,
        viral_target=0.55,
    ),
}

_AGGRESSIVE: dict[str, _RegimeConfig] = {
    "insane": _RegimeConfig(
        # S64 Gem Hunter: in a hot market INSANE presses hardest — lowest bar of any
        # (mode, regime) cell so it casts the widest net for the race-winning gem.
        pass_threshold=72.0,
        momentum_floor=2.5,   bs_floor=0.3,
        mom_target=8.0,       bs_target=1.5,
        vol_target=12_000,    liq_target=120_000,
        gem_vol_target=4_000, gem_liq_target=40_000,
        viral_target=0.55,
    ),
    "wild": _RegimeConfig(
        pass_threshold=75.0,
        momentum_floor=0.4,   bs_floor=0.4,
        mom_target=2.5,       bs_target=1.5,
        vol_target=8_000,     liq_target=80_000,
        gem_vol_target=3_000, gem_liq_target=35_000,
        viral_target=0.55,
    ),
    "stoic": _RegimeConfig(
        # S64 Vault Keeper: refuses to FOMO even when the market is hot — stays at the
        # NORMAL-grade bar (80) and a real momentum/buy-pressure floor while INSANE goes wild.
        pass_threshold=80.0,
        momentum_floor=1.5,   bs_floor=1.2,
        mom_target=3.0,       bs_target=1.8,
        vol_target=15_000,    liq_target=200_000,
        gem_vol_target=6_000, gem_liq_target=60_000,
        viral_target=0.55,
    ),
}

# S79 — SNIPER: quiet, low-momentum market (agg_vol $48k–110k). RECALIBRATED for
# reachability: the old 84/88 bar + 5% momentum floor was unreachable when typical 5m
# momentum is ~0.4% (max score without velocity is 75) → qualified_count=0, zero trades.
# Now the score bar is reachable on volume+liquidity+social, and the momentum floor is
# low enough to capture the small real moves. Liquidity targets stay HIGH (sellability —
# the deep_pool edge is the thing that actually works in a quiet tape; the separate hard
# $50k liq floor still blocks ghosts). ⚠ -EV TRADEOFF: this re-admits flat-momentum
# entries that pure-price-action backtests show are structurally -EV. Lean on edge.
_SNIPER: dict[str, _RegimeConfig] = {
    "insane": _RegimeConfig(
        pass_threshold=68.0,
        momentum_floor=1.0,    bs_floor=0.4,
        mom_target=4.0,        bs_target=2.0,
        vol_target=15_000,     liq_target=200_000,
        gem_vol_target=6_000,  gem_liq_target=70_000,
        viral_target=0.65,
    ),
    "wild": _RegimeConfig(
        pass_threshold=70.0,
        momentum_floor=0.5,    bs_floor=0.6,
        mom_target=2.5,        bs_target=2.0,
        vol_target=12_000,     liq_target=150_000,
        gem_vol_target=6_000,  gem_liq_target=60_000,
        viral_target=0.65,
    ),
    "stoic": _RegimeConfig(
        pass_threshold=76.0,
        momentum_floor=1.0,    bs_floor=1.0,
        mom_target=3.0,        bs_target=2.0,
        vol_target=25_000,     liq_target=350_000,
        gem_vol_target=9_000,  gem_liq_target=100_000,
        viral_target=0.70,
    ),
}

# S79 — DEAD: near-frozen market (agg_vol < $48k — where we are now). The tape barely
# moves; momentum is ~0. The bar is the lowest of any regime so SOMETHING can qualify,
# but the design intent is EDGE-ONLY: the deep_pool/brain_rule paths (drain-guarded,
# sellable) are what we want here, and naked-momentum entries should be rare, small, and
# tightly stopped. Liquidity targets stay high so only sellable tokens score well.
# ⚠ STRONGEST -EV TRADEOFF of the ladder — this is the bleed zone. Trade minimal.
_DEAD: dict[str, _RegimeConfig] = {
    "insane": _RegimeConfig(
        pass_threshold=64.0,
        momentum_floor=0.4,    bs_floor=0.3,
        mom_target=2.5,        bs_target=1.5,
        vol_target=10_000,     liq_target=150_000,
        gem_vol_target=5_000,  gem_liq_target=60_000,
        viral_target=0.60,
    ),
    "wild": _RegimeConfig(
        pass_threshold=66.0,
        momentum_floor=0.4,    bs_floor=0.5,
        mom_target=2.0,        bs_target=1.8,
        vol_target=9_000,      liq_target=120_000,
        gem_vol_target=5_000,  gem_liq_target=50_000,
        viral_target=0.60,
    ),
    "stoic": _RegimeConfig(
        pass_threshold=72.0,
        momentum_floor=0.8,    bs_floor=0.8,
        mom_target=2.5,        bs_target=1.8,
        vol_target=18_000,     liq_target=280_000,
        gem_vol_target=7_000,  gem_liq_target=85_000,
        viral_target=0.65,
    ),
}

# Normal regime — derived per-mode, not a frozen preset (targets depend on gem_path)
_NORMAL_PASS_THRESHOLD = 80.0


# ── Maths helpers ─────────────────────────────────────────────────────────────

def _log_norm(value: float, ref: float, target: float) -> float:
    if value <= ref or target <= ref:
        return 0.0
    return min(1.0, math.log(value / ref) / math.log(target / ref))


def _lin_norm(value: float, floor: float, target: float) -> float:
    if value <= floor or target <= floor:
        return 0.0
    return min(1.0, (value - floor) / (target - floor))


# ── Score ─────────────────────────────────────────────────────────────────────

@dataclass
class ValidationScore:
    # S87 rework — dimensions reweighted by MEASURED realizable-EV predictiveness.
    # vol-acceleration leads; momentum demoted; the liquidity dim is a saturating
    # SELLABILITY qualifier (not a return reward). Hard gates zero unsellable/draining.
    vaccel:    float           # 0–15  (S104: vol-accel demoted 25→15 — weak in path-extremes)
    buy:       float           # 0–30  (S104: buy pressure 25→30 — robust peak predictor)
    liquidity: float           # 0–40  (saturating sellability + drain penalty)
    activity:  float           # 0–15  (S104: vol_5m + momentum 10→15 — carries the m5>10 continuation edge)
    social_bonus: float = 0.0  # 0–5   (S87: social demoted from a 0–25 dim; dormant)
    whale_bonus: float = 0.0   # 0–8   (PIPE12 R-B1: on-chain whale-buy tie-break; canary, default 0)
    gated:     bool  = False    # S87 hard gate (unsellable / draining at entry) → total 0
    threshold: float = _S87_FLAT_THRESHOLD       # S87: flat across regimes
    regime:    str   = "normal"                  # for logging / sizing
    vol_accel: float = 1.0                       # raw vol-accel ratio (debug)

    @property
    def total(self) -> float:
        if self.gated:
            return 0.0
        return round(self.vaccel + self.buy + self.liquidity + self.activity
                     + self.social_bonus + self.whale_bonus, 1)

    @property
    def passes(self) -> bool:
        return self.total >= self.threshold

    def reject_reason(self) -> str:
        tag = f" [{self.regime.upper()}]" if self.regime != "normal" else ""
        if self.gated:
            return f"score 0/{self.threshold:.0f}{tag} [GATED: unsellable/draining]"
        sb = f" +s{self.social_bonus:.0f}" if self.social_bonus > 0 else ""
        return (
            f"score {self.total:.0f}/{self.threshold:.0f}{tag} "
            f"[vac={self.vaccel:.0f} buy={self.buy:.0f} "
            f"liq={self.liquidity:.0f} act={self.activity:.0f}{sb}]"
        )


# ── Profile ───────────────────────────────────────────────────────────────────

class ValidationProfile:
    """Encapsulates all scoring parameters for one (regime, mode, path) combination.

    When regime is a preset (euphoria/aggressive/sniper/dead), every parameter comes
    exclusively from the regime preset — nothing is inherited from base mode config.
    When regime is "normal", mode-derived defaults apply.
    """

    def __init__(
        self,
        *,
        momentum_floor: float,
        bs_floor: float,
        gem_path: bool = False,
        viral_weight: str = "normal",
        mode: str = "insane",
        regime: Regime = "normal",
    ) -> None:
        self._regime = regime
        self._vw     = viral_weight

        # S79 — preset-driven regimes (euphoria/aggressive/sniper/dead). NORMAL stays
        # mode-derived below. Every preset is a full {mode: _RegimeConfig} table.
        _preset = {
            "euphoria":   _EUPHORIA,
            "aggressive": _AGGRESSIVE,
            "sniper":     _SNIPER,
            "dead":       _DEAD,
        }.get(regime)

        if _preset is not None:
            cfg = _preset.get(mode, _preset["insane"])
            self._pass_threshold = cfg.pass_threshold
            self._mom_floor      = cfg.momentum_floor
            self._bs_floor       = cfg.bs_floor
            self._mom_target     = cfg.mom_target
            self._bs_target      = cfg.bs_target
            self._vol_target     = cfg.gem_vol_target if gem_path else cfg.vol_target
            self._liq_target     = cfg.gem_liq_target if gem_path else cfg.liq_target
            self._viral_target   = cfg.viral_target

        else:  # "normal"
            self._pass_threshold = _NORMAL_PASS_THRESHOLD
            self._mom_floor      = momentum_floor
            self._bs_floor       = bs_floor
            # Velocity target: 2× floor, at least +3 pp above it
            self._mom_target     = max(momentum_floor * 2.0, momentum_floor + 3.0)
            self._bs_target      = 2.0
            if gem_path:
                self._vol_target = 8_000.0
                self._liq_target = 80_000.0
            else:
                self._vol_target = {"stoic": 35_000.0, "wild": 20_000.0, "insane": 25_000.0}.get(mode, 25_000.0)
                self._liq_target = {"stoic": 500_000.0, "wild": 200_000.0, "insane": 300_000.0}.get(mode, 300_000.0)
            self._viral_target   = 0.70

    # ── Dimension scorers ─────────────────────────────────────────────────────

    def _score_vaccel(self, vol_accel: float) -> float:
        # S94: DEMOTED 0–35 → 0–25. vol_accel = vol_5m / rolling-avg; still a clean
        # forward-EV predictor on deep pools but it ranks near base-rate and its sign is
        # regime-fragile (score-quest), so depth absorbs the freed weight. Neutral 1.0
        # → 18.6 (admission-preserving, rescaled); accelerating → 25 (at vacc≈2.0); collapsing → 0.
        if vol_accel >= 1.0:
            return round(min(_VACC_MAX_PTS, _VACC_NEUTRAL_PTS + (vol_accel - 1.0) * _VACC_SLOPE), 2)
        return round(max(0.0, _VACC_NEUTRAL_PTS * vol_accel), 2)

    def _score_buy(self, buy_sell: float) -> float:
        # Buy pressure (S104: 0–30). Robust monotonic peak predictor across all liquidity tiers
        # (forward_obs: bs 1.0–2.0+ reaches +15% peak ~3× more often than bs<0.8).
        # 0.8 (sells dominate) → 0 ; 2.0 (≈4:1 buys) → 30.
        return round(min(_BUY_MAX_PTS, max(0.0, (buy_sell - 0.8) / 1.2 * _BUY_MAX_PTS)), 2)

    def _score_liq(self, liq_usd: float, liq_vel: float = 0.0) -> float:
        # S94: PROMOTED 0–25 → 0–40 — the ONE stable load-bearing dim (score-quest: present
        # in every top model across every regime window). SELLABILITY qualifier, NOT a return
        # reward: ramps from the $30k floor, SATURATES at $120k (depth beyond "exitable" adds
        # no EV → stops over-rewarding flat blue-chips). Draining LP penalised (ghost risk).
        base = _log_norm(liq_usd, _SELL_FLOOR_USD, _LIQ_FULL_USD) * _LIQ_MAX_PTS
        if liq_vel < 0.0:
            base -= 8.0
        return round(max(0.0, min(_LIQ_MAX_PTS, base)), 2)

    def _score_whale_bonus(self, is_whale: bool, buy_sell: float) -> float:
        # PIPE12 (R-B1): tie-break bonus for an on-chain whale buy. Half on whale-present,
        # full with buy confirmation (bs≥1.5 → the whale+buy cohort, the strongest in WS-B).
        # Lives on TOP of the gated score, so it never lifts an unsellable/draining token.
        if not is_whale:
            return 0.0
        return round(_WHALE_BONUS_PTS * (1.0 if buy_sell >= 1.5 else 0.5), 2)

    def _score_activity(self, vol5m: float, momentum: float, regime_mom: bool = False,
                        regime_mom_scale: bool = False) -> float:
        # S94: SOFT activity 0–15 → 0–10. vol_5m and momentum predict only weakly on
        # realizable pools → a small tie-break, not the engine. The momentum half is
        # REGIME-CONDITIONAL when the regime_mom canary is on (rec#3): reward up-momentum in
        # PRO regimes (euphoria/aggressive), reward flat/dip in ANTI regimes (dead/normal) —
        # the proven sign-flip fix. Canary OFF / neutral regimes → reward up-momentum (current).
        v_pts = min(_ACT_VOL_MAX, math.log1p(max(0.0, vol5m)) / math.log1p(20_000.0) * _ACT_VOL_MAX)
        if regime_mom and self._regime in _ANTI_MOMENTUM_REGIMES:
            m_signal = max(0.0, -momentum)      # ANTI: reward flat/dip
        else:
            m_signal = max(0.0, momentum)       # PRO / neutral / canary-off: reward up-move
        # S104: momentum half maxes at m5≈8% (was /10), so the m5>10 continuation cohort
        # (meanFwd +9.79%, 26% reach +30%) scores the full momentum reward — the single
        # strongest entry signal in the path data. _ACT_MOM_MAX raised 3.33→8.33.
        m_pts = min(_ACT_MOM_MAX, m_signal / _ACT_MOM_FULL_PCT * _ACT_MOM_MAX)
        # PIPE12 (R-B2): SCALE the up-momentum reward by regime (don't flip the sign). Only the
        # up-momentum branch is scaled (the anti branch is the separate S94 canary). Canary OFF
        # → factor 1.0 (unchanged). Applied after the m_signal selection.
        if regime_mom_scale and not (regime_mom and self._regime in _ANTI_MOMENTUM_REGIMES):
            m_pts *= _REGIME_MOM_SCALE.get(self._regime, 1.0)
        return round(v_pts + m_pts, 2)

    def _score_social_bonus(self, viral: float) -> float:
        # S87 — social demoted to a capped +0..5 tie-break (was a full 0–25 dim).
        # Absent data → 0 (no bonus, not the old 18.75 floor). Dormant while LunarCrush
        # is down; forward-compatible if it's revived.
        if viral < 0.0 or self._vw == "off":
            return 0.0
        return round(min(viral / self._viral_target, 1.0) * 5.0, 2)

    # ── Public API ────────────────────────────────────────────────────────────

    def score(
        self,
        *,
        vol5m: float,
        momentum: float,
        buy_sell: float,
        liq_usd: float,
        viral: float = -1.0,
        vol_accel: float = 1.0,
        liq_vel: float = 0.0,
        liq_accel: float = 0.0,
        regime_mom: bool = False,
        regime_mom_scale: bool = False,   # PIPE12 R-B2 canary (scale momentum by regime)
        is_whale: bool = False,           # PIPE12 R-B1: on-chain whale buy detected
        whale_bonus: bool = False,        # PIPE12 R-B1 canary (off → bonus stays 0)
    ) -> ValidationScore:
        # S87 hard gates: must be exitable, and must not be draining LP at entry (= the ghost).
        gated = (liq_usd < _SELL_FLOOR_USD) or (liq_vel < -0.02)
        return ValidationScore(
            vaccel=self._score_vaccel(vol_accel),
            buy=self._score_buy(buy_sell),
            liquidity=self._score_liq(liq_usd, liq_vel),
            activity=self._score_activity(vol5m, momentum, regime_mom, regime_mom_scale),
            social_bonus=self._score_social_bonus(viral),
            whale_bonus=(self._score_whale_bonus(is_whale, buy_sell) if whale_bonus else 0.0),
            gated=gated,
            threshold=_S87_FLAT_THRESHOLD,
            regime=self._regime,
            vol_accel=round(vol_accel, 2),
        )
