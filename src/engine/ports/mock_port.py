from __future__ import annotations

from copy import deepcopy
from typing import Any

from src.engine.clock import Clock, RealClock


class MockExchangePort:
    """In-memory exchange port used for unit tests and dry runs.

    This is intentionally small but sufficient for engine conformance tests and
    contract validation. It simulates wallet balances, a simple ticker, matching
    spot orders, and a minimal short position model.
    """

    def __init__(self, initial_wallet: dict[str, float] | None = None, tickers: dict[str, float] | None = None, clock: Clock | None = None) -> None:
        wallet = {"USD": 50000.0, "BTC": 0.0, "ETH": 0.0}
        if initial_wallet:
            wallet.update(initial_wallet)

        self.clock = clock or RealClock()
        self.wallet = wallet
        self.tickers = tickers or {"BTC/USD": 50000.0, "ETH/USD": 3000.0}
        self.orders: list[dict[str, Any]] = []
        self.short_positions: list[dict[str, Any]] = []
        self._order_id = 1

    def get_exchange_info(self):
        return {
            "IsRunning": True,
            "InitialWallet": {"USD": 50000.0},
            "TradePairs": {
                "BTC/USD": {"Coin": "BTC", "Unit": "USD", "CanTrade": True, "PricePrecision": 2, "AmountPrecision": 6, "MiniOrder": 10.0},
                "ETH/USD": {"Coin": "ETH", "Unit": "USD", "CanTrade": True, "PricePrecision": 2, "AmountPrecision": 6, "MiniOrder": 10.0},
            },
        }

    def get_ticker(self, pair=None):
        if pair is None:
            return {"Success": True, "Data": {k: {"LastPrice": v} for k, v in self.tickers.items()}}
        return {"Success": True, "Data": {pair: {"LastPrice": self.tickers.get(pair, 0.0)}}}

    def get_balance(self):
        return {"Success": True, "Wallet": {coin: {"Free": value, "Lock": 0.0} for coin, value in self.wallet.items()}}

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        pair = pair_or_coin if "/" in str(pair_or_coin) else f"{pair_or_coin}/USD"
        qty = float(quantity)
        order_type = order_type or ("LIMIT" if price is not None else "MARKET")
        price_value = float(price) if price is not None else self.tickers.get(pair, 0.0)

        if side == "BUY":
            required_usd = qty * price_value
            if self.wallet.get("USD", 0.0) < required_usd:
                return {"Success": False, "ErrMsg": "insufficient USD"}
            self.wallet["USD"] -= required_usd
            base = pair.split("/")[0]
            self.wallet[base] = self.wallet.get(base, 0.0) + qty
        elif side == "SELL":
            base = pair.split("/")[0]
            if self.wallet.get(base, 0.0) < qty:
                return {"Success": False, "ErrMsg": "insufficient base balance"}
            self.wallet[base] -= qty
            self.wallet["USD"] = self.wallet.get("USD", 0.0) + qty * price_value
        else:
            return {"Success": False, "ErrMsg": "invalid side"}

        order = {
            "OrderID": self._order_id,
            "Pair": pair,
            "Status": "FILLED",
            "Side": side,
            "Type": order_type,
            "Price": price_value,
            "Quantity": qty,
            "FilledQuantity": qty,
            "FilledAverPrice": price_value,
            "CreateTimestamp": int(self.clock.now().timestamp() * 1000),
        }
        self._order_id += 1
        self.orders.append(order)
        return {"Success": True, "OrderDetail": deepcopy(order)}

    def cancel_order(self, order_id=None, pair=None):
        remaining = [order for order in self.orders if order.get("OrderID") != order_id]
        cancelled = [order for order in self.orders if order.get("OrderID") == order_id]
        self.orders = remaining
        return {"Success": True, "CanceledList": [order.get("OrderID") for order in cancelled]}

    def query_order(self, order_id=None, pair=None, pending_only=None, offset=None, limit=None):
        found = self.orders
        if order_id is not None:
            found = [order for order in found if order.get("OrderID") == order_id]
        if pair is not None:
            found = [order for order in found if order.get("Pair") == pair]
        if pending_only:
            found = [order for order in found if order.get("Status") == "PENDING"]
        return {"Success": True, "OrderMatched": deepcopy(found)}

    def list_open_orders(self):
        return deepcopy([order for order in self.orders if order.get("Status") == "PENDING"])

    def open_short(self, pair_or_coin, collateral, price=None):
        pair = pair_or_coin if "/" in str(pair_or_coin) else f"{pair_or_coin}/USD"
        collateral_value = float(collateral)
        open_fee = collateral_value * 0.001
        if self.wallet.get("USD", 0.0) < collateral_value + open_fee:
            return {"Success": False, "ErrMsg": "insufficient USD"}
        self.wallet["USD"] -= collateral_value + open_fee
        market_price = self.tickers.get(pair, 0.0)
        entry = float(price) if price is not None else market_price
        qty = collateral_value / entry
        if price is not None:
            order = {
                "OrderID": self._order_id,
                "Pair": pair,
                "Status": "PENDING",
                "Side": "SHORT_OPEN",
                "Type": "LIMIT",
                "Price": entry,
                "Quantity": qty,
                "Collateral": collateral_value,
                "OpenFee": open_fee,
                "CreateTimestamp": int(self.clock.now().timestamp() * 1000),
            }
            self._order_id += 1
            self.orders.append(order)
            return {"Success": True, "ID": order["OrderID"], "Pair": pair, "OrderType": "LIMIT", "EntryPrice": entry, "ShortQty": qty, "Collateral": collateral_value, "OpenFee": open_fee, "Status": "PENDING", "CreateTimestamp": order["CreateTimestamp"]}

        existing = next((position for position in self.short_positions if position["Pair"] == pair), None)
        if existing is not None:
            total_qty = existing["ShortQty"] + qty
            existing["EntryPrice"] = ((existing["ShortQty"] * existing["EntryPrice"]) + (qty * entry)) / total_qty
            existing["ShortQty"] = total_qty
            existing["Collateral"] += collateral_value
            existing["OpenFee"] = open_fee
            return {"Success": True, "ID": existing["ID"], "Pair": pair, "OrderType": "MARKET", "EntryPrice": existing["EntryPrice"], "ShortQty": total_qty, "Collateral": existing["Collateral"], "OpenFee": open_fee, "Status": "OPEN"}
        position = {
            "ID": len(self.short_positions) + 1,
            "Pair": pair,
            "EntryPrice": entry,
            "ShortQty": qty,
            "Collateral": collateral_value,
            "CurrentPrice": market_price,
            "UnrealizedPNL": 0.0,
            "UnrealizedPNLPct": 0.0,
            "PositionStatus": "OPEN",
            "OpenFee": open_fee,
        }
        self.short_positions.append(position)
        return {"Success": True, "ID": position["ID"], "Pair": pair, "OrderType": "MARKET", "EntryPrice": entry, "ShortQty": qty, "Collateral": collateral_value, "OpenFee": open_fee, "Status": "OPEN"}

    def close_short(self, pair_or_coin, close_qty=None, close_pct=None):
        pair = pair_or_coin if "/" in str(pair_or_coin) else f"{pair_or_coin}/USD"
        remaining = []
        closed = 0.0
        return_amount = 0.0
        realized_pnl = 0.0
        close_fee = 0.0
        for pos in self.short_positions:
            if pos["Pair"] == pair:
                if close_qty is not None:
                    close_amount = float(close_qty)
                    close_amount = min(close_amount, pos["ShortQty"])
                    ratio = close_amount / pos["ShortQty"]
                    collateral_return = pos["Collateral"] * ratio
                    pnl = close_amount * (pos["EntryPrice"] - self.tickers.get(pair, 0.0))
                    fee = close_amount * self.tickers.get(pair, 0.0) * 0.001
                    closed += close_amount
                    realized_pnl += pnl
                    close_fee += fee
                    return_amount += collateral_return + pnl - fee
                    if pos["ShortQty"] <= close_amount:
                        continue
                    pos["ShortQty"] -= close_amount
                    pos["Collateral"] -= collateral_return
                    remaining.append(pos)
                    continue
                if close_pct is not None:
                    fraction = float(close_pct) / 100.0
                    close_amount = pos["ShortQty"] * fraction
                    collateral_return = pos["Collateral"] * fraction
                    pnl = close_amount * (pos["EntryPrice"] - self.tickers.get(pair, 0.0))
                    fee = close_amount * self.tickers.get(pair, 0.0) * 0.001
                    closed += close_amount
                    realized_pnl += pnl
                    close_fee += fee
                    return_amount += collateral_return + pnl - fee
                    if pos["ShortQty"] <= close_amount:
                        continue
                    pos["ShortQty"] -= close_amount
                    pos["Collateral"] -= collateral_return
                    remaining.append(pos)
                    continue
            else:
                remaining.append(pos)
        self.short_positions = remaining
        self.wallet["USD"] = self.wallet.get("USD", 0.0) + return_amount
        pair_remaining = [pos for pos in remaining if pos["Pair"] == pair]
        return {"Success": True, "ClosePrice": self.tickers.get(pair, 0.0), "RealizedPNL": realized_pnl, "CloseFee": close_fee, "ReturnAmount": return_amount, "ClosedQty": closed, "FullyClosed": not pair_remaining, "RemainingQty": sum(pos["ShortQty"] for pos in pair_remaining), "RemainingCollateral": sum(pos["Collateral"] for pos in pair_remaining)}

    def get_short_positions(self):
        return {"Success": True, "Positions": deepcopy(self.short_positions)}

    def get_pending_count(self):
        return {"Success": True, "TotalPending": 0, "OrderPairs": {}}

    def get_server_time(self):
        return {"ServerTime": int(self.clock.now().timestamp() * 1000)}
