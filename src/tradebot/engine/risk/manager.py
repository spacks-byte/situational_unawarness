from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradebot.core.config import ExecutionConfig


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskState:
    kill_switch: bool = False
    daily_loss_usd: float = 0.0
    drawdown_pct: float = 0.0


class RiskManager:
    """Evaluate a rebalance plan against configured post-trade limits."""

    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config

    def evaluate(
        self,
        plan: dict[str, Any],
        actual: dict[str, Any],
        total_equity_usd: float,
        state: RiskState | None = None,
    ) -> RiskDecision:
        equity = float(total_equity_usd)
        if equity <= 0:
            return RiskDecision(False, ("invalid_equity",))

        close_longs = self._amounts(plan, "close_longs")
        close_shorts = self._amounts(plan, "close_shorts")
        open_longs = self._amounts(plan, "open_longs")
        open_shorts = self._amounts(plan, "open_shorts")
        actual_longs = self._amounts(actual, "longs")
        actual_shorts = self._amounts(actual, "shorts")
        reasons: list[str] = []
        risk_state = state or RiskState()
        if risk_state.kill_switch:
            reasons.append("kill_switch")
        if risk_state.daily_loss_usd > self.config.max_daily_loss_usd:
            reasons.append("max_daily_loss")
        if risk_state.drawdown_pct > self.config.max_drawdown_pct:
            reasons.append("max_drawdown")

        for amount in (*close_longs.values(), *close_shorts.values(), *open_longs.values(), *open_shorts.values()):
            if amount > self.config.max_order_value_usd:
                reasons.append("max_order_value")
                break

        if open_shorts and not self.config.supports_shorting:
            reasons.append("shorting_disabled")

        short_collateral = self._post_trade_total(actual_shorts, open_shorts, close_shorts)
        if short_collateral > self.config.max_total_short_collateral_usd:
            reasons.append("max_short_collateral")

        cash = float(actual.get("cash_usd", 0.0))
        cash_after = cash
        fees = self.config.fees
        spot_fee = fees.spot_maker if self.config.order_policy == "limit_only" else fees.spot_taker
        cash_after += sum(close_longs.values()) * (1.0 - spot_fee)
        cash_after += sum(close_shorts.values()) * (1.0 - fees.short_close)
        cash_after -= sum(open_longs.values()) * (1.0 + spot_fee)
        cash_after -= sum(open_shorts.values()) * (1.0 + fees.short_open)
        if cash_after < self.config.min_cash_reserve_usd:
            reasons.append("cash_reserve")

        post_longs = self._post_trade_by_symbol(actual_longs, open_longs, close_longs)
        post_shorts = self._post_trade_by_symbol(actual_shorts, open_shorts, close_shorts)
        symbols = set(post_longs) | set(post_shorts)
        gross = 0.0
        net = 0.0
        position_count = 0
        for symbol in symbols:
            long_value = post_longs.get(symbol, 0.0)
            short_value = post_shorts.get(symbol, 0.0)
            gross += long_value + short_value
            net += long_value - short_value
            if long_value > 0 or short_value > 0:
                position_count += 1
            if (long_value + short_value) / equity > self.config.max_per_symbol_exposure:
                reasons.append("max_per_symbol_exposure")
                break

        if gross / equity > self.config.max_gross_exposure:
            reasons.append("max_gross_exposure")
        if abs(net) / equity > self.config.max_net_exposure:
            reasons.append("max_net_exposure")
        if position_count > self.config.max_positions:
            reasons.append("max_positions")

        unique_reasons = tuple(dict.fromkeys(reasons))
        return RiskDecision(not unique_reasons, unique_reasons)

    @staticmethod
    def _amounts(container: dict[str, Any], key: str) -> dict[str, float]:
        values = container.get(key) or {}
        return {str(symbol).upper(): max(0.0, float(amount)) for symbol, amount in values.items()}

    @staticmethod
    def _post_trade_total(actual: dict[str, float], additions: dict[str, float], reductions: dict[str, float]) -> float:
        return sum(max(0.0, actual.get(symbol, 0.0) + additions.get(symbol, 0.0) - reductions.get(symbol, 0.0)) for symbol in set(actual) | set(additions) | set(reductions))

    @staticmethod
    def _post_trade_by_symbol(actual: dict[str, float], additions: dict[str, float], reductions: dict[str, float]) -> dict[str, float]:
        return {
            symbol: max(0.0, actual.get(symbol, 0.0) + additions.get(symbol, 0.0) - reductions.get(symbol, 0.0))
            for symbol in set(actual) | set(additions) | set(reductions)
        }