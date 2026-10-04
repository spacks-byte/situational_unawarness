"""
Re-price passive limit orders at send time (latency guard).

The strategy prices its limits off the snapshot ticker, but the engine sends a batch one order at a
time behind the rate limiter, so the last orders of a rebalance can leave a minute or more after
that price was read. A coin moves ~13 bp in a minute against a 5 bp offset, so a stale limit is
either left behind or already crossed (taker fee, bad fill). `tradebot.research.experiments
--latency 1` shows the edge does not survive stale quotes.

This wrapper re-reads the pair's ticker immediately before every limit order and pegs the limit to
that price: buys below it (keeping the order's USD size), sells and short opens above it (a sell keeps
its coin quantity: it sells what is held). Market orders pass through untouched. If the fresh price is
missing, or has moved more than `max_move` from the price the order was built with, the order is not
sent (the strategy re-quotes it at the next 15m bar).
"""
from __future__ import annotations

import logging
from typing import Any

from tradebot.core.symbols import to_pair

log = logging.getLogger(__name__)


class RepegPort:
    is_live = False

    def __init__(self, port: Any, offset_bps: float, max_move: float = 0.03) -> None:
        self._port = port
        self.offset = offset_bps / 1e4
        self.max_move = max_move
        self.is_live = bool(getattr(port, "is_live", False))
        self.repegged = 0
        self.skipped = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._port, name)

    def _fresh(self, pair: str, sign: int, stale_limit: float) -> float | None:
        """Limit pegged to the current last price; sign -1 = below (buy), +1 = above (sell, short open)."""
        try:
            last = float(self._port.get_ticker(pair).get("Data", {}).get(pair, {}).get("LastPrice") or 0.0)
        except Exception:
            last = 0.0
        stale_ref = stale_limit / (1 + sign * self.offset)
        if last <= 0 or abs(last / stale_ref - 1) > self.max_move:
            self.skipped += 1
            log.warning("re-peg %s: fresh price %s vs %.8g used by the strategy; order not sent", pair, last, stale_ref)
            return None
        self.repegged += 1
        return last * (1 + sign * self.offset)

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        limit = price is not None and (order_type or "LIMIT").upper() == "LIMIT"
        if not limit:
            return self._port.place_order(pair_or_coin, side, quantity, price=price, order_type=order_type)
        pair = to_pair(pair_or_coin)
        buy = str(side).upper() == "BUY"
        fresh = self._fresh(pair, -1 if buy else +1, float(price))
        if fresh is None:
            return {"Success": False, "ErrMsg": "re-peg: no usable fresh price"}
        if buy:
            quantity = float(quantity) * float(price) / fresh          # same USD size at the new price
        return self._port.place_order(pair_or_coin, side, quantity, price=fresh, order_type=order_type)

    def open_short(self, pair_or_coin, collateral, price=None):
        if price is None:
            return self._port.open_short(pair_or_coin, collateral, price=price)
        fresh = self._fresh(to_pair(pair_or_coin), +1, float(price))
        if fresh is None:
            return {"Success": False, "ErrMsg": "re-peg: no usable fresh price"}
        return self._port.open_short(pair_or_coin, collateral, price=fresh)
