from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.engine.clock import Clock


def cancel_stale_orders(port: Any, clock: Clock, timeout_seconds: int) -> list[Any]:
    if timeout_seconds <= 0:
        return []
    raw_orders = port.list_open_orders()
    orders = raw_orders.get("OrderMatched", []) if isinstance(raw_orders, dict) else raw_orders
    cancelled: list[Any] = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        created_at = _order_time(order)
        order_id = order.get("OrderID", order.get("order_id", order.get("ID")))
        if created_at is None or order_id is None:
            continue
        age_seconds = (clock.now().astimezone(UTC) - created_at).total_seconds()
        if age_seconds >= timeout_seconds:
            response = port.cancel_order(order_id=order_id)
            if response and response.get("Success") is not False:
                cancelled.append(order_id)
    return cancelled


def _order_time(order: dict[str, Any]) -> datetime | None:
    raw_value = next((order.get(key) for key in ("CreateTimestamp", "CreatedAt", "CreateTime", "Timestamp", "Time") if order.get(key) is not None), None)
    if isinstance(raw_value, datetime):
        return raw_value.astimezone(UTC)
    if isinstance(raw_value, (int, float)):
        seconds = float(raw_value) / 1000.0 if raw_value > 10_000_000_000 else float(raw_value)
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(raw_value, str):
        try:
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None