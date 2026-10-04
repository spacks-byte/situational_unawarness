from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from tradebot.core.clock import Clock

log = logging.getLogger(__name__)


def cancel_stale_orders(port: Any, clock: Clock, timeout_seconds: int, grace_seconds: float = 0.0) -> list[Any]:
    """Cancel resting orders older than timeout_seconds - grace_seconds.

    grace_seconds = the loop interval: an order that would expire before the next loop is cancelled now.
    Otherwise an order sent a few seconds into a loop is still (900 - d) s old at the next bar's first
    loop, outlives that bar's re-quote, is cancelled one loop later and sits out the rest of the bar."""
    if timeout_seconds <= 0:
        return []
    raw_orders = port.list_open_orders()
    orders = raw_orders.get("OrderMatched", []) if isinstance(raw_orders, dict) else raw_orders
    cancelled: list[Any] = []
    for order in orders or []:
        if not isinstance(order, dict) or str(order.get("Status", "PENDING")).upper() != "PENDING":
            continue                  # only pending orders can be cancelled (history rows cost a request each)
        created_at = _order_time(order)
        order_id = order.get("OrderID", order.get("order_id", order.get("ID")))
        if created_at is None or order_id is None:
            # An order we can't age is never cancelled, and the strategy counts it as exposure forever
            log.warning("resting order without a usable id/timestamp is never cancelled: %s", order)
            continue
        age_seconds = (clock.now().astimezone(UTC) - created_at).total_seconds()
        # strict with a grace: an order placed exactly on the bar lives the whole bar (the replay fills at bar end)
        stale = age_seconds > timeout_seconds - grace_seconds if grace_seconds else age_seconds >= timeout_seconds
        if stale:
            response = port.cancel_order(order_id=order_id)
            if response and response.get("Success") is not False:
                cancelled.append(order_id)
            else:
                log.warning("cancel of stale order %s (%.0f s old) failed: %s", order_id, age_seconds, response)
    return cancelled


def _order_time(order: dict[str, Any]) -> datetime | None:
    raw_value = next((order.get(key) for key in ("CreateTimestamp", "CreatedAt", "CreateTime", "Timestamp", "Time") if order.get(key) is not None), None)
    if isinstance(raw_value, datetime):
        return raw_value.astimezone(UTC)
    if isinstance(raw_value, (int, float)):
        seconds = float(raw_value) / 1000.0 if raw_value > 10_000_000_000 else float(raw_value)
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(raw_value, str) and raw_value.strip().isdigit():
        return _order_time({"CreateTimestamp": int(raw_value.strip())})
    if isinstance(raw_value, str):
        try:
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None