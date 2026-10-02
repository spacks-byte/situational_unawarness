"""Rate-limit guard around any ExchangePort (live Roostoo limit: 30 calls/min).

Each port method is weighted by the number of HTTP requests the raw client in
``crypto-roostoo-api/`` actually makes (every signed call first GETs /v3/serverTime;
place_order, limit open_short and close_short(close_qty) also GET /v3/exchangeInfo for
precision). Before a call that would push the trailing-60s total over ``max_per_minute``, the
wrapper sleeps on the injected clock (SimClock in tests/demo, RealClock live).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from src.engine.clock import Clock

# HTTP requests per port call, from crypto-roostoo-api/*.py
HTTP_WEIGHTS = {
    "get_exchange_info": 1,
    "get_server_time": 1,
    "get_ticker": 2,            # serverTime + /v3/ticker
    "get_balance": 2,           # serverTime + /v3/balance
    "get_pending_count": 2,
    "query_order": 2,
    "list_open_orders": 2,      # = query_order(pending_only=True)
    "cancel_order": 2,
    "get_short_positions": 2,
    "place_order": 3,           # exchangeInfo + serverTime + /v3/place_order
    "open_short": 3,            # (+exchangeInfo only for LIMIT; counted conservatively)
    "close_short": 3,           # exchangeInfo (precision) + serverTime + /v6/short_close
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

    def _acquire(self, weight: int) -> None:
        while True:
            now = self._clock.monotonic()
            while self._window and now - self._window[0][0] >= 60.0:
                self._window.popleft()
            used = sum(w for _, w in self._window)
            if used + weight <= self.max_per_minute or not self._window:
                self._window.append((now, weight))
                self.peak = max(self.peak, used + weight)
                self.total_http += weight
                return
            wait = 60.0 - (now - self._window[0][0]) + 0.01
            self.total_sleep += wait
            self._clock.sleep(wait)
