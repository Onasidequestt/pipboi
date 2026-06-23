"""
Debater — multi-persona signal validation engine.

Intercepts signals that have already passed the initial momentum/liquidity/
confidence gates in stoic_strategy.evaluate() and runs three specialized logic
personas before wallet.py is allowed to build a swap transaction.

  TrendAnalyst        — verifies the 1h trend guard against volume momentum
  RiskAuditor         — on-chain sanity check using bonding curve state + market ratios
  SentimentContrarian — detects exhaustion signals the entry gates miss

Aggregate score = weighted sum of three persona scores:
  aggregate = 0.35 × trend + 0.30 × risk + 0.35 × sentiment

Execution requires BOTH:
  1. aggregate > mode-specific CONFIDENCE_MIN
     (QUICK tier uses CONFIDENCE_MIN × QUICK_CONF_MULT = stricter threshold)
  2. RiskAuditor.clean_status == True  (hard veto regardless of score)

This replaces the single-gate confidence check with a weighted consensus model
specifically tuned to block QUICK-tier entries at high-volatility reversal points.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import bonding_curve
from stoic_strategy import MODES

# ── Weights ───────────────────────────────────────────────────────────────────
WEIGHT_TREND     = 0.35
WEIGHT_RISK      = 0.30
WEIGHT_SENTIMENT = 0.35

# QUICK tier multiplier — raises the aggregate threshold for low-conviction entries
# to prevent impulsive buys at high-volatility reversal points.
# INSANE CONFIDENCE_MIN (0.40) × 1.15 = 0.46 effective threshold for QUICK.
QUICK_CONF_MULT  = 1.15


# ── Result container ──────────────────────────────────────────────────────────

@dataclass
class DebateResult:
    passed:          bool
    aggregate:       float
    threshold:       float
    trend_score:     float
    risk_score:      float
    sentiment_score: float
    risk_clean:      bool
    veto_reason:     Optional[str]    = None
    notes:           List[str]        = field(default_factory=list)


# ── Persona 1: Trend Analyst ──────────────────────────────────────────────────

class _TrendAnalyst:
    """
    Verifies that 5m momentum aligns with the 1h trend direction and that volume
    is building, not fading.

    Specifically catches weak continuations: a token that already ran +8% in the
    1h window with only +1% 5m momentum is distributing, not accumulating. Entering
    there is a late-stage entry with asymmetric downside.
    """

    def score(self, sig: dict, mdata: dict) -> tuple:
        notes  = []
        score  = 0.50   # neutral baseline — confirmed uptrend adds, weakness subtracts

        mom_5m  = sig.get("momentum_5m", 0.0)
        chg_1h  = sig.get("price_change_1h", mdata.get("price_change_1h", 0.0))
        vol_acc = sig.get("vol_acceleration", 1.0)

        # Confirmed uptrend: 1h positive and 5m momentum still live
        if chg_1h > 5.0 and mom_5m > 1.0:
            score += 0.20
            notes.append(f"trend_aligned 1h={chg_1h:.1f}% mom={mom_5m:.1f}%")
        elif chg_1h > 0.0 and mom_5m > 0.0:
            score += 0.12
            notes.append(f"mild_uptrend 1h={chg_1h:.1f}%")

        # Volume building = real momentum; volume fading = move losing energy
        if vol_acc >= 1.5:
            score += 0.10
            notes.append(f"vol_accelerating ×{vol_acc:.1f}")
        elif vol_acc < 0.6:
            score -= 0.10
            notes.append(f"vol_fading ×{vol_acc:.1f}")

        # Weak continuation: big 1h run, 5m momentum slowing — buyers distributing
        if chg_1h > 8.0 and mom_5m < 1.5:
            score -= 0.15
            notes.append(f"weak_cont 1h={chg_1h:.1f}% mom={mom_5m:.2f}%")
        elif chg_1h > 6.0 and mom_5m < 1.5:
            score -= 0.10
            notes.append(f"soft_cont 1h={chg_1h:.1f}% mom={mom_5m:.2f}%")

        # Overbought: move already ran far this hour — late-stage entry risk
        if chg_1h > 12.0:
            score -= 0.20
            notes.append(f"1h_overbought {chg_1h:.1f}%")

        # 1h downtrend with only a weak 5m bounce — not a real reversal
        if chg_1h < -3.0 and mom_5m < 3.0:
            score -= 0.10
            notes.append(f"1h_downtrend {chg_1h:.1f}% weak_reversal")

        return round(max(0.0, min(1.0, score)), 3), notes


# ── Persona 2: Risk Auditor ───────────────────────────────────────────────────

class _RiskAuditor:
    """
    On-chain sanity check using bonding curve buy activity (streamed live by
    bonding_curve.py) and DexScreener market microstructure ratios.

    Returns (score, clean_status). clean_status=False is a hard veto: it means
    the on-chain data is fundamentally suspect and execution must not proceed
    regardless of the aggregate score.

    Hard-veto conditions:
      - buy/sell ratio > 5.5 (suspicious pump concentration, near the 6.0 filter ceiling)
      - Single whale wallet responsible for all BC buys on a token < 4h old
    """

    def audit(self, sig: dict, mdata: dict) -> tuple:
        notes  = []
        score  = 0.60   # positive prior — most signals passing evaluate() are legitimate
        clean  = True

        mint      = sig["mint"]
        bs_ratio  = sig.get("buy_sell_ratio", 1.0)
        age_hours = mdata.get("pair_age_hours", 9999.0)

        # ── Buy/sell ratio sanity ─────────────────────────────────────────────
        # > 6.0 is already filtered by evaluate(). > 5.5 slipped through barely
        # and is strongly associated with coordinated pump activity.
        if bs_ratio > 5.5:
            clean = False
            score -= 0.30
            notes.append(f"bs_ratio {bs_ratio:.1f} suspicious (near pump ceiling 6.0)")
        elif bs_ratio > 4.5:
            score -= 0.15
            notes.append(f"bs_ratio {bs_ratio:.1f} elevated")
        elif bs_ratio > 3.5:
            score -= 0.05
            notes.append(f"bs_ratio {bs_ratio:.1f} high")

        # ── Bonding curve on-chain state ──────────────────────────────────────
        # get_buy_activity() reads from the in-memory stream updated by the
        # public Solana WebSocket — this is a real-time on-chain signal, no
        # extra API calls required.
        bc = bonding_curve.get_buy_activity(mint)
        if bc:
            buyers  = bc.get("unique_buyers", 0)
            buys_5m = bc.get("buy_count_5m", 0)
            largest = bc.get("largest_buy", 0.0)
            total   = bc.get("total_sol_5m", 0.0)

            if buyers >= 3:
                score += 0.20
                notes.append(f"bc_organic {buyers}_buyers")
            elif buyers == 2:
                score += 0.10
                notes.append("bc_2_buyers")
            elif buyers == 1 and buys_5m >= 3:
                # Single wallet doing repeated buys — possible wash trade or lone whale
                score -= 0.15
                notes.append(f"bc_single_buyer {buys_5m}_buys")

            # Hard veto: lone whale entry on a very young token — highest rug probability
            if largest >= 2.0 and age_hours < 4.0 and buyers == 1:
                clean = False
                notes.append(f"bc_whale_single ◎{largest:.1f} age={age_hours:.1f}h")

            if total > 0:
                score += min(0.10, total * 0.05)   # +0.05 per SOL, capped at 0.10
                notes.append(f"bc_total ◎{total:.2f}")
        else:
            # No BC data — not on pump.fun, or whale activity not yet detected.
            # Penalise only very young tokens where BC data is most informative.
            if age_hours > 24:
                score += 0.05
                notes.append("bc_none_established")
            elif age_hours < 6:
                score -= 0.05
                notes.append(f"bc_none_young age={age_hours:.1f}h")

        return round(max(0.0, min(1.0, score)), 3), clean, notes


# ── Persona 3: Sentiment Contrarian ──────────────────────────────────────────

class _SentimentContrarian:
    """
    Scores the signal from the perspective of an adversarial analyst looking for
    reasons NOT to enter. High score = no objection (green). Low score = objection.

    Catches:
      - Peak exhaustion: big 1h run + decelerating 5m = buyers already positioned
      - Dead cat bounce: negative 24h with temporary 1h recovery
      - Volume distribution: 1h buy/sell ratio declining vs the 5m snapshot
      - Volume pace fading: 1h volume pace below 70% of the 6h average hourly rate

    Specifically designed to block QUICK tier entries when all three signals above
    align during a high-volatility reversal.
    """

    def score(self, sig: dict, mdata: dict) -> tuple:
        notes   = []
        score   = 0.60   # default: no objection

        mom_5m  = sig.get("momentum_5m", 0.0)
        chg_1h  = sig.get("price_change_1h", mdata.get("price_change_1h", 0.0))
        chg_24h = mdata.get("price_change_24h", 0.0)
        vol_1h  = mdata.get("volume_1h", 0.0)
        vol_6h  = mdata.get("volume_6h", 0.0)
        b_1h    = mdata.get("txns_1h_buys", 0)
        s_1h    = max(mdata.get("txns_1h_sells", 1), 1)
        bs_1h   = b_1h / s_1h
        bs_5m   = sig.get("buy_sell_ratio", 1.0)
        age     = mdata.get("pair_age_hours", 9999.0)
        vol_acc = sig.get("vol_acceleration", 1.0)

        # ── Peak exhaustion: big 1h run, 5m slowing ───────────────────────────
        if chg_1h > 15.0 and mom_5m < 2.0:
            score -= 0.25
            notes.append(f"peak_exhaustion 1h={chg_1h:.1f}% mom={mom_5m:.2f}%")
        elif chg_1h > 8.0 and mom_5m < 1.5:
            score -= 0.15
            notes.append(f"late_entry 1h={chg_1h:.1f}% mom={mom_5m:.2f}%")

        # ── Dead cat bounce: 24h negative with shallow 1h recovery ───────────
        if chg_24h < -20.0 and chg_1h > 0.0:
            score -= 0.20
            notes.append(f"dead_cat 24h={chg_24h:.1f}% 1h={chg_1h:.1f}%")
        elif chg_24h < -10.0 and chg_1h > 0.0:
            score -= 0.10
            notes.append(f"partial_bounce 24h={chg_24h:.1f}%")

        # ── Volume pace fading on the 1h move ─────────────────────────────────
        # If the 1h pace is below 70% of the 6h average hourly rate, crowd is leaving
        if vol_6h > 0 and vol_1h > 0:
            avg_h6_per_hour = vol_6h / 6.0
            if vol_1h < avg_h6_per_hour * 0.70:
                score -= 0.15
                notes.append(f"vol_fading_1h pace=${vol_1h:.0f} vs avg=${avg_h6_per_hour:.0f}")

        # Volume acceleration soft fade — momentum positive but buying pressure weakening
        if vol_acc < 0.7:
            score -= 0.10
            notes.append(f"vol_acc_low ×{vol_acc:.1f}")

        # ── Buy pressure distribution: 5m b/s ratio falling vs 1h baseline ───
        # Smart money distributing while 5m momentum still looks positive — classic
        # exit pattern. Only meaningful when 1h has a real baseline (b_1h + s_1h > 5).
        if (b_1h + s_1h) >= 5 and bs_1h > 1.0 and bs_5m < bs_1h * 0.70:
            score -= 0.15
            notes.append(f"bs_distribution 5m={bs_5m:.2f} vs 1h={bs_1h:.2f}")

        # ── Healthy continuation: fresh move, volume building ─────────────────
        if chg_1h > 0.0 and mom_5m > 1.0 and vol_acc >= 1.2:
            score += 0.20
            notes.append(f"healthy_cont 1h={chg_1h:.1f}% mom={mom_5m:.1f}% vol×{vol_acc:.1f}")
        elif age < 6.0 and chg_1h > 3.0 and mom_5m > 0.5:
            score += 0.10
            notes.append(f"fresh_traction age={age:.1f}h 1h={chg_1h:.1f}%")

        return round(max(0.0, min(1.0, score)), 3), notes


# ── Engine ────────────────────────────────────────────────────────────────────

_trend_analyst = _TrendAnalyst()
_risk_auditor  = _RiskAuditor()
_contrarian    = _SentimentContrarian()


def evaluate(sig: dict, mdata: dict, mode: str) -> DebateResult:
    """
    Run all three personas and return a DebateResult.

    Called synchronously from the main signal-firing loop in main.py — pure
    in-memory computation on data already fetched this cycle. No I/O.

    The aggregate threshold is mode-specific. QUICK tier signals face a 15%
    stricter threshold (CONFIDENCE_MIN × QUICK_CONF_MULT) because they are the
    most prone to impulsive entries during high-volatility market reversals.
    """
    trend_score,                trend_notes = _trend_analyst.score(sig, mdata)
    risk_score,  risk_clean,    risk_notes  = _risk_auditor.audit(sig, mdata)
    sent_score,                 sent_notes  = _contrarian.score(sig, mdata)

    aggregate = round(
        WEIGHT_TREND      * trend_score
        + WEIGHT_RISK     * risk_score
        + WEIGHT_SENTIMENT * sent_score,
        3,
    )

    conf_min  = MODES[mode]["CONFIDENCE_MIN"]
    tier      = sig.get("insane_tier")
    threshold = round(conf_min * QUICK_CONF_MULT, 3) if tier == "quick" else conf_min

    all_notes = (
        [f"T:{n}" for n in trend_notes]
        + [f"R:{n}" for n in risk_notes]
        + [f"C:{n}" for n in sent_notes]
    )

    passed = aggregate > threshold and risk_clean

    veto_reason: Optional[str] = None
    if not risk_clean:
        dirty = [n for n in risk_notes if any(
            kw in n for kw in ("suspicious", "whale_single", "pump")
        )]
        veto_reason = f"risk_dirty [{'; '.join(dirty)}]" if dirty else "risk_auditor_veto"
    elif not passed:
        veto_reason = (
            f"agg {aggregate:.3f} ≤ thr {threshold:.3f} "
            f"(T={trend_score:.2f} R={risk_score:.2f} C={sent_score:.2f})"
        )

    return DebateResult(
        passed          = passed,
        aggregate       = aggregate,
        threshold       = threshold,
        trend_score     = trend_score,
        risk_score      = risk_score,
        sentiment_score = sent_score,
        risk_clean      = risk_clean,
        veto_reason     = veto_reason,
        notes           = all_notes,
    )
