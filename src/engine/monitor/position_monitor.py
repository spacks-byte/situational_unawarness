from __future__ import annotations

from typing import Any

from src.engine.schema.models import TargetPortfolio


class PositionMonitor:
    """Evaluate configured stop-loss and take-profit thresholds."""

    def evaluate(
        self,
        target: TargetPortfolio,
        prices: dict[str, float],
        entry_prices: dict[str, float],
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        for position in target.longs:
            alert = self._long_alert(position.symbol, position.stop_loss_pct, position.take_profit_pct, prices, entry_prices)
            if alert:
                alerts.append({"symbol": position.symbol.upper(), "side": "LONG", "action": "FLATTEN", "reason": alert})
        for position in target.shorts:
            alert = self._short_alert(position.symbol, position.stop_loss_pct, position.take_profit_pct, prices, entry_prices)
            if alert:
                alerts.append({"symbol": position.symbol.upper(), "side": "SHORT", "action": "FLATTEN", "reason": alert})
        return alerts

    @staticmethod
    def _long_alert(symbol: str, stop_loss_pct: float | None, take_profit_pct: float | None, prices: dict[str, float], entries: dict[str, float]) -> str | None:
        price = _price(prices, symbol)
        entry = _price(entries, symbol)
        if price is None or entry is None or entry <= 0:
            return None
        if stop_loss_pct is not None and price <= entry * (1.0 - stop_loss_pct / 100.0):
            return "STOP_LOSS"
        if take_profit_pct is not None and price >= entry * (1.0 + take_profit_pct / 100.0):
            return "TAKE_PROFIT"
        return None

    @staticmethod
    def _short_alert(symbol: str, stop_loss_pct: float | None, take_profit_pct: float | None, prices: dict[str, float], entries: dict[str, float]) -> str | None:
        price = _price(prices, symbol)
        entry = _price(entries, symbol)
        if price is None or entry is None or entry <= 0:
            return None
        if stop_loss_pct is not None and price >= entry * (1.0 + stop_loss_pct / 100.0):
            return "STOP_LOSS"
        if take_profit_pct is not None and price <= entry * (1.0 - take_profit_pct / 100.0):
            return "TAKE_PROFIT"
        return None


def _price(values: dict[str, float], symbol: str) -> float | None:
    value = values.get(symbol.upper())
    return float(value) if value is not None else None