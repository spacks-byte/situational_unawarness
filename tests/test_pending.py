from datetime import UTC, datetime, timedelta

from tradebot.core.clock import SimClock
from tradebot.engine.reconcile.pending import cancel_stale_orders
from tradebot.exchange.mock import MockExchangePort


def test_cancel_stale_orders_uses_injected_clock():
    clock = SimClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    port = MockExchangePort(clock=clock)
    port.orders = [
        {"OrderID": 1, "Status": "PENDING", "CreateTimestamp": int((clock.now() - timedelta(seconds=100)).timestamp() * 1000)},
        {"OrderID": 2, "Status": "PENDING"},
        {"OrderID": 3, "Status": "PENDING", "CreatedAt": (clock.now() - timedelta(seconds=10)).isoformat()},
    ]

    cancelled = cancel_stale_orders(port, clock, timeout_seconds=60)

    assert cancelled == [1]
