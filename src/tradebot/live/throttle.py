"""
Rate-limit guard around any ExchangePort (Roostoo allows 30 requests per minute).

Each port method is weighted by the HTTP requests `tradebot.exchange.client.RoostooClient` makes for
it: one. The client caches exchangeInfo (hourly) and the server-time offset (every 10 minutes), so
those refreshes are rare and fit inside the headroom between `max_per_minute` (25) and the limit.
Before a call that would push the trailing-60 s total over `max_per_minute`, the wrapper sleeps on the
injected clock (SimClock in tests and replays, RealClock live).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from tradebot.core.clock import Clock

# HTTP requests per port call with RoostooClient
HTTP_WEIGHTS = {
    "get_exchange_info": 1,
    "get_server_time": 1,
    "get_ticker": 1,
    "get_balance": 1,
    "get_pending_count": 1,
    "query_order": 1,
    "list_open_orders": 1,      # = query_order(pending_only=True)
    "cancel_order": 1,
    "get_short_positions": 1,
    "place_order": 1,
    "open_short": 1,
    "close_short": 1,
}


class ThrottledPort:
    is_live = False

    def __init__(self, port: Any, clock: Clock, max_per_minute: int = 25, weights: dict[str, int] | None = None) -> None:
        self._port = port
        self._clock = clock
        self.max_per_minute = max_per_minute
        self.weights = dict(HTTP_WEIGHTS, **(weights or {}))
        self._window: deque[tuple[float, int]] = deque()
        self.total_http = 0
        self.total_sleep = 0.0
        self.peak = 0
        self.is_live = bool(getattr(port, "is_live", False))

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._port, name)
        if name not in self.weights or not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            self._acquire(self.weights[name])
            return attr(*args, **kwargs)

        return wrapped

    def requests_last_minute(self) -> int:
        now = self._clock.monotonic()
        return sum(w for t, w in self._window if now - t < 60.0)

    def wait_for_capacity(self, weight: int) -> None:
        """Wait for a consecutive group of calls on this single-threaded port.

        This does not count or reserve calls. Each subsequent call is still
        acquired normally. The account coordinator is the sole transport caller.
        """
        if not 0 < weight <= self.max_per_minute:
            raise ValueError("request group exceeds rate limit")
        while True:
            now = self._clock.monotonic()
            while self._window and now - self._window[0][0] >= 60.0:
                self._window.popleft()
            if sum(w for _, w in self._window) + weight <= self.max_per_minute:
                return
            wait = 60.0 - (now - self._window[0][0]) + 0.01
            self.total_sleep += wait
            self._clock.sleep(wait)

    def _acquire(self, weight: int) -> None:
        self.wait_for_capacity(weight)
        now = self._clock.monotonic()
        self._window.append((now, weight))
        self.peak = max(self.peak, sum(w for _, w in self._window))
        self.total_http += weight
