from collections import deque
from config import (
    TOTAL_CAPITAL_USD,
    MAX_POSITION_PCT,
    MAX_DAILY_LOSS_PCT,
    MIN_LIQUIDITY_USD,
    MAX_SLIPPAGE_BPS,
)

# Negative-EV auto-pause: if the last N full closes are ALL losses AND the net SOL PnL
# falls below the threshold, write PAUSE_FLAG so no new entries fire until manual review.
_EV_HALT_TRADES     = 10    # rolling window of full closes
_EV_HALT_SOL_LIMIT  = -0.1  # net SOL threshold (≈ 1% of a ◎1.0 wallet)


class RiskManager:
    def __init__(self) -> None:
        self.daily_pnl: float = 0.0
        self.open_positions: dict[str, float] = {}  # mint -> usd size
        self._capital_usd: float = TOTAL_CAPITAL_USD  # updated each cycle via update_capital
        self._recent_trade_pnl_sol: deque = deque(maxlen=_EV_HALT_TRADES)

    def update_capital(self, sol_balance: float, sol_price: float) -> None:
        """Call once per cycle after fetching live balances so halt/cap thresholds stay accurate."""
        self._capital_usd = max(sol_balance * sol_price, TOTAL_CAPITAL_USD)

    @property
    def max_position_usd(self) -> float:
        return self._capital_usd * MAX_POSITION_PCT

    @property
    def daily_loss_limit(self) -> float:
        return self._capital_usd * MAX_DAILY_LOSS_PCT

    def is_halted(self) -> bool:
        return self.daily_pnl <= -self.daily_loss_limit

    def record_pnl(self, usd_delta: float) -> None:
        self.daily_pnl += usd_delta

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0

    def check_trade(
        self,
        mint: str,
        size_usd: float,
        liquidity_usd: float,
        estimated_slippage_bps: int,
        allow_stack: bool = False,
    ) -> tuple[bool, str]:
        if self.is_halted():
            return False, f"Daily loss limit reached (PnL: ${self.daily_pnl:.2f})"

        if size_usd > self.max_position_usd:
            return False, f"Size ${size_usd:.2f} exceeds cap ${self.max_position_usd:.2f}"

        if liquidity_usd < MIN_LIQUIDITY_USD:
            return False, f"Liquidity ${liquidity_usd:.0f} below floor ${MIN_LIQUIDITY_USD:.0f}"

        if estimated_slippage_bps > MAX_SLIPPAGE_BPS:
            return False, f"Slippage {estimated_slippage_bps}bps exceeds max {MAX_SLIPPAGE_BPS}bps"

        if mint in self.open_positions and not allow_stack:
            return False, f"Already have open position in {mint}"

        return True, "ok"

    def open_position(self, mint: str, size_usd: float) -> None:
        self.open_positions[mint] = size_usd

    def stack_position(self, mint: str, add_usd: float) -> None:
        """Add to an existing tracked position size (stacking, not a new entry)."""
        self.open_positions[mint] = self.open_positions.get(mint, 0.0) + add_usd

    def partial_close_position(self, mint: str, fraction: float, exit_usd: float) -> float:
        """Reduce tracked position by fraction and book partial PnL. Position stays open."""
        if mint not in self.open_positions:
            return 0.0
        full_entry = self.open_positions[mint]
        partial_entry = full_entry * fraction
        pnl = exit_usd - partial_entry
        self.record_pnl(pnl)
        self.open_positions[mint] = round(full_entry * (1.0 - fraction), 4)
        return pnl

    def close_position(self, mint: str, exit_usd: float) -> float:
        entry = self.open_positions.pop(mint, 0.0)
        pnl = exit_usd - entry
        self.record_pnl(pnl)
        return pnl

    def evaluate_market_impact(
        self,
        impact_pct: float,
        size_sol: float,
        threshold_pct: float,
        n_tranches: int,
    ) -> tuple[bool, str]:
        """
        Returns (should_twap, reason).

        True when the dual-sample quote shows measured price impact above threshold_pct.
        The reason string is used directly in the [MIP] log line and audit trail.

        Called after measure_price_impact() returns and before swap transaction build.
        The decision is: route to TWAP execution or proceed with atomic swap.
        """
        if impact_pct > threshold_pct:
            tranche_sol = size_sol / n_tranches if n_tranches > 0 else size_sol
            return True, (
                f"impact {impact_pct:.2f}% > {threshold_pct:.1f}% "
                f"→ TWAP {n_tranches}×◎{tranche_sol:.4f}"
            )
        return False, f"impact {impact_pct:.2f}% ≤ {threshold_pct:.1f}% — atomic"

    def record_trade_result(self, pnl_sol: float) -> None:
        """Record a full-close SOL PnL into the rolling EV window.
        Only call for complete position closes (not partial TP1 exits).
        """
        self._recent_trade_pnl_sol.append(pnl_sol)

    def ev_pause_triggered(self) -> bool:
        """Return True when the last 10 full closes are ALL losses AND net SOL < -0.1.
        The caller (main.py) is responsible for writing PAUSE_FLAG — risk.py stays
        filesystem-free so it can be unit-tested independently.
        """
        if len(self._recent_trade_pnl_sol) < _EV_HALT_TRADES:
            return False
        net_sol = sum(self._recent_trade_pnl_sol)
        return all(p < 0 for p in self._recent_trade_pnl_sol) and net_sol < _EV_HALT_SOL_LIMIT
