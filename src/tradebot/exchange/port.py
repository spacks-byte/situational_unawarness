"""
ExchangePort: the exchange interface the live engine depends on.

Two implementations:
  - RoostooExchangePort (here): the real exchange through RoostooClient
  - MockExchangePort (exchange/mock.py): in-memory exchange for tests and dry runs
Responses are Roostoo-shaped dicts (PascalCase keys such as Success, OrderDetail, Wallet).
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from tradebot.exchange.client import RoostooClient


class ExchangePort(Protocol):
    is_live: bool

    def get_exchange_info(self) -> dict[str, Any]: ...

    def get_ticker(self, pair: Optional[str] = None) -> dict[str, Any]: ...

    def get_balance(self) -> dict[str, Any]: ...

    def place_order(self, pair_or_coin: str, side: str, quantity: float,
                    price: Optional[float] = None, order_type: Optional[str] = None) -> dict[str, Any]: ...

    def cancel_order(self, order_id: Any = None, pair: Optional[str] = None) -> dict[str, Any]: ...

    def query_order(self, order_id: Any = None, pair: Optional[str] = None, pending_only: Optional[bool] = None,
                    offset: Optional[int] = None, limit: Optional[int] = None) -> dict[str, Any]: ...

    def list_open_orders(self) -> dict[str, Any] | list[dict[str, Any]]: ...

    def open_short(self, pair_or_coin: str, collateral: float, price: Optional[float] = None) -> dict[str, Any]: ...

    def close_short(self, pair_or_coin: str, close_qty: Optional[float] = None,
                    close_pct: Optional[float] = None) -> dict[str, Any]: ...

    def get_short_positions(self) -> dict[str, Any]: ...

    def get_pending_count(self) -> dict[str, Any]: ...

    def get_server_time(self) -> dict[str, Any]: ...


class RoostooExchangePort:
    """ExchangePort backed by the real Roostoo API."""

    is_live = True

    def __init__(self, client: Optional[RoostooClient] = None) -> None:
        self.client = client or RoostooClient()

    def get_exchange_info(self):
        return self.client.exchange_info()

    def get_ticker(self, pair=None):
        return self.client.ticker(pair)

    def get_balance(self):
        return self.client.balance()

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        return self.client.place_order(pair_or_coin, side, quantity, price=price, order_type=order_type)

    def cancel_order(self, order_id=None, pair=None):
        return self.client.cancel_order(order_id=order_id, pair=pair)

    def query_order(self, order_id=None, pair=None, pending_only=None, offset=None, limit=None):
        return self.client.query_order(order_id=order_id, pair=pair, pending_only=pending_only,
                                       offset=offset, limit=limit)

    def list_open_orders(self):
        return self.client.query_order(pending_only=True)

    def open_short(self, pair_or_coin, collateral, price=None):
        return self.client.open_short(pair_or_coin, collateral, price=price)

    def close_short(self, pair_or_coin, close_qty=None, close_pct=None):
        return self.client.close_short(pair_or_coin, close_qty=close_qty, close_pct=close_pct)

    def get_short_positions(self):
        return self.client.short_positions()

    def get_pending_count(self):
        return self.client.pending_count()

    def get_server_time(self):
        return self.client.server_time()
