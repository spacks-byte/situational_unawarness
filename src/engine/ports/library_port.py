from __future__ import annotations

from typing import Protocol

from roostoo_api import (
    cancel_order,
    check_server_time,
    close_short,
    get_balance,
    get_exchange_info,
    get_pending_count,
    get_short_positions,
    get_ticker,
    open_short,
    place_order,
    query_order,
)


class ExchangePort(Protocol):
    """Minimal exchange interface consumed by the engine."""

    def get_exchange_info(self):
        ...

    def get_ticker(self, pair=None):
        ...

    def get_balance(self):
        ...

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        ...

    def cancel_order(self, order_id=None, pair=None):
        ...

    def query_order(self, order_id=None, pair=None, pending_only=None, offset=None, limit=None):
        ...

    def list_open_orders(self):
        ...

    def open_short(self, pair_or_coin, collateral, price=None):
        ...

    def close_short(self, pair_or_coin, close_qty=None, close_pct=None):
        ...

    def get_short_positions(self):
        ...

    def get_pending_count(self):
        ...

    def get_server_time(self):
        ...


class LibraryExchangePort:
    """Thin wrapper around the existing Roostoo client modules.

    This is the only engine module that imports the raw Roostoo API package. The
    rest of the engine depends only on the `ExchangePort` interface and its model
    contracts.
    """

    is_live = True

    def __init__(self) -> None:
        self._api = {
            "get_exchange_info": get_exchange_info,
            "get_ticker": get_ticker,
            "get_balance": get_balance,
            "place_order": place_order,
            "cancel_order": cancel_order,
            "query_order": query_order,
            "open_short": open_short,
            "close_short": close_short,
            "get_short_positions": get_short_positions,
            "get_pending_count": get_pending_count,
            "get_server_time": check_server_time,
        }

    def get_exchange_info(self):
        return self._api["get_exchange_info"]()

    def get_ticker(self, pair=None):
        return self._api["get_ticker"](pair=pair)

    def get_balance(self):
        return self._api["get_balance"]()

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        return self._api["place_order"](pair_or_coin, side, quantity, price, order_type)

    def cancel_order(self, order_id=None, pair=None):
        return self._api["cancel_order"](order_id=order_id, pair=pair)

    def query_order(self, order_id=None, pair=None, pending_only=None, offset=None, limit=None):
        return self._api["query_order"](
            order_id=order_id,
            pair=pair,
            pending_only=pending_only,
            offset=offset,
            limit=limit,
        )

    def list_open_orders(self):
        return self._api["query_order"](pending_only=True)

    def open_short(self, pair_or_coin, collateral, price=None):
        return self._api["open_short"](pair_or_coin, collateral, price=price)

    def close_short(self, pair_or_coin, close_qty=None, close_pct=None):
        return self._api["close_short"](
            pair_or_coin,
            close_qty=close_qty,
            close_pct=close_pct,
        )

    def get_short_positions(self):
        return self._api["get_short_positions"]()

    def get_pending_count(self):
        return self._api["get_pending_count"]()

    def get_server_time(self):
        return self._api["get_server_time"]()
