"""Simulation-only ExchangePort that replays historical 15m bars (never touches the network).

Why not plain ``MockExchangePort``: its prices are static, limit buys fill instantly, limit
shorts never fill, cancel doesn't refund short collateral and short P&L is never marked to market. To exercise the live path over several days we need all of those.
Response shapes follow the Roostoo API / MockExchangePort so the engine code path is unchanged.

Fill model (same as tradebot.backtest.simulator, conservative variant): LastPrice is the close of the last
completed bar; a resting limit BUY fills at its limit if a bar ending after placement trades
*through* it (low < limit), a limit SELL / SHORT_OPEN if high > limit. Market orders fill at
LastPrice. Fees come from the shared FeeSchedule (limit 0.05%, market 0.1%, short open/close 0.1%).
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pandas as pd

from tradebot.core.clock import Clock
from tradebot.core.config import FeeSchedule
from tradebot.core.symbols import to_coin, to_pair

BAR = pd.Timedelta(minutes=15)


class ReplayExchangePort:
    is_live = False

    def __init__(self, bars: dict[str, pd.DataFrame], clock: Clock, initial_usd: float = 100_000.0,
                 fees: FeeSchedule | None = None) -> None:
        self.clock = clock
        self.fees = fees or FeeSchedule()
        self.bars = {to_pair(s): df.sort_index() for s, df in bars.items()}
        self.free_usd = float(initial_usd)
        self.coins: dict[str, float] = {}
        self.orders: list[dict[str, Any]] = []          # resting (PENDING) orders
        self.history: list[dict[str, Any]] = []         # every order ever placed
        self.shorts: dict[str, dict[str, Any]] = {}
        self.fills: list[dict[str, Any]] = []
        self.calls: dict[str, int] = {}
        self._next_id = 1
        self._synced_to: pd.Timestamp | None = None
        self._px_cache: dict[tuple[str, pd.Timestamp], float] = {}

    # ------------------------------------------------------------------ market replay
    def _now(self) -> pd.Timestamp:
        return pd.Timestamp(self.clock.now()).tz_convert("UTC")

    def _last_closed(self) -> pd.Timestamp:
        return self._now().floor("15min") - BAR

    def last_price(self, pair: str) -> float:
        key = (pair, self._last_closed())
        if key not in self._px_cache:
            if len(self._px_cache) > 10_000:
                self._px_cache.clear()
            df = self.bars.get(pair)
            upto = df.loc[: key[1], "close"] if df is not None else ()
            self._px_cache[key] = float(upto.iloc[-1]) if len(upto) else 0.0
        return self._px_cache[key]

    def _sync(self) -> None:
        """Match resting orders against every bar completed since the last sync."""
        last = self._last_closed()
        if self._synced_to is not None and last <= self._synced_to:
            return
        start = self._synced_to + BAR if self._synced_to is not None else last
        self._synced_to = last
        for order in list(self.orders):
            df = self.bars.get(order["Pair"])
            if df is None:
                continue
            placed = pd.Timestamp(order["CreateTimestamp"], unit="ms", tz="UTC")
            # bars that end after the order was placed (the bar it was placed in counts)
            window = df.loc[max(start, placed.floor("15min")): last]
            for ts, bar in window.iterrows():
                if self._try_fill(order, bar, ts):
                    break

    def _try_fill(self, order: dict[str, Any], bar: pd.Series, ts: pd.Timestamp) -> bool:
        px = order["Price"]
        side = order["Side"]
        if side == "BUY" and bar["low"] < px:
            base = to_coin(order["Pair"])
            self.coins[base] = self.coins.get(base, 0.0) + order["Quantity"]
            self.free_usd -= order["Quantity"] * px * self.fees.spot_maker
        elif side == "SELL" and bar["high"] > px:
            self.free_usd += order["Quantity"] * px * (1 - self.fees.spot_maker)
        elif side == "SHORT_OPEN" and bar["high"] > px:
            self._add_short(order["Pair"], order["Collateral"], px)
        else:
            return False
        order["Status"] = "FILLED"
        order["FilledAverPrice"] = px
        self.orders.remove(order)
        self._fill(order["Pair"], side, "LIMIT", order["Quantity"], px, fill_bar=ts)
        return True

    def _fill(self, pair, side, kind, qty, price, fill_bar=None):
        self.fills.append({"time": self._now() if fill_bar is None else fill_bar + BAR, "pair": pair, "side": side,
                           "type": kind, "qty": qty, "price": price, "value": qty * price})

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        self._sync()

    # ------------------------------------------------------------------ ExchangePort
    def get_exchange_info(self):
        self._count("get_exchange_info")
        return {"IsRunning": True, "InitialWallet": {"USD": self.free_usd},
                "TradePairs": {p: {"Coin": to_coin(p), "Unit": "USD", "CanTrade": True, "PricePrecision": 8,
                                   "AmountPrecision": 8, "MiniOrder": 1.0} for p in self.bars}}

    def get_ticker(self, pair=None):
        self._count("get_ticker")
        pairs = [pair] if pair else list(self.bars)
        data = {}
        for p in pairs:
            last = self.last_price(p)
            if last > 0:
                data[p] = {"LastPrice": last, "MaxBid": last, "MinAsk": last}
        return {"Success": True, "Data": data}

    def get_balance(self):
        self._count("get_balance")
        locked = sum(o["Quantity"] * o["Price"] for o in self.orders if o["Side"] == "BUY")
        locked += sum(o["Collateral"] for o in self.orders if o["Side"] == "SHORT_OPEN")
        wallet = {"USD": {"Free": self.free_usd, "Lock": locked}}
        sell_locked: dict[str, float] = {}
        for o in self.orders:
            if o["Side"] == "SELL":
                base = to_coin(o["Pair"])
                sell_locked[base] = sell_locked.get(base, 0.0) + o["Quantity"]
        for coin, qty in self.coins.items():
            wallet[coin] = {"Free": qty, "Lock": sell_locked.get(coin, 0.0)}
        return {"Success": True, "SpotWallet": wallet}

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        self._count("place_order")
        pair = to_pair(pair_or_coin)
        side = str(side).upper()
        qty = float(quantity)
        order_type = (order_type or ("LIMIT" if price is not None else "MARKET")).upper()
        last = self.last_price(pair)
        if last <= 0 or qty <= 0:
            return {"Success": False, "ErrMsg": "no price / bad quantity"}
        base = to_coin(pair)
        if side == "SELL":
            held = self.coins.get(base, 0.0)
            if qty > held * 1.001:
                return {"Success": False, "ErrMsg": "insufficient base balance"}
            qty = min(qty, held)
        order = {"OrderID": self._next_id, "Pair": pair, "Side": side, "Type": order_type,
                 "Price": float(price) if order_type == "LIMIT" else last, "Quantity": qty,
                 "CreateTimestamp": int(self._now().timestamp() * 1000)}
        self._next_id += 1
        if order_type == "MARKET":
            if side == "BUY":
                cost = qty * last * (1 + self.fees.spot_taker)
                if cost > self.free_usd:
                    return {"Success": False, "ErrMsg": "insufficient USD"}
                self.free_usd -= cost
                self.coins[base] = self.coins.get(base, 0.0) + qty
            else:
                self.coins[base] -= qty
                self.free_usd += qty * last * (1 - self.fees.spot_taker)
            order.update(Status="FILLED", FilledQuantity=qty, FilledAverPrice=last)
            self._fill(pair, side, "MARKET", qty, last)
        else:
            if side == "BUY":
                if qty * order["Price"] > self.free_usd:
                    return {"Success": False, "ErrMsg": "insufficient USD"}
                self.free_usd -= qty * order["Price"]
            else:
                self.coins[base] -= qty
            order.update(Status="PENDING", FilledQuantity=0.0)
            self.orders.append(order)
        self.history.append(order)
        return {"Success": True, "OrderDetail": deepcopy(order)}

    def cancel_order(self, order_id=None, pair=None):
        self._count("cancel_order")
        cancelled = []
        for o in list(self.orders):
            if (order_id is not None and o["OrderID"] == order_id) or (order_id is None and pair in (None, o["Pair"])):
                self.orders.remove(o)
                o["Status"] = "CANCELED"
                if o["Side"] == "BUY":
                    self.free_usd += o["Quantity"] * o["Price"]
                elif o["Side"] == "SELL":
                    base = to_coin(o["Pair"])
                    self.coins[base] = self.coins.get(base, 0.0) + o["Quantity"]
                elif o["Side"] == "SHORT_OPEN":
                    self.free_usd += o["Collateral"] + o["OpenFee"]   # DESIGN.md: collateral + fee released
                cancelled.append(o["OrderID"])
        return {"Success": True, "CanceledList": cancelled}

    def query_order(self, order_id=None, pair=None, pending_only=None, offset=None, limit=None):
        self._count("query_order")
        found = self.orders if pending_only else self.history
        found = [o for o in found if (order_id is None or o["OrderID"] == order_id) and (pair is None or o["Pair"] == pair)]
        return {"Success": True, "OrderMatched": deepcopy(found)}

    def list_open_orders(self):
        # RoostooExchangePort implements this as query_order(pending_only=True): same shape here
        return self.query_order(pending_only=True)

    def open_short(self, pair_or_coin, collateral, price=None):
        self._count("open_short")
        pair = to_pair(pair_or_coin)
        collateral = float(collateral)
        fee = collateral * self.fees.short_open
        last = self.last_price(pair)
        if last <= 0 or collateral + fee > self.free_usd:
            return {"Success": False, "ErrMsg": "insufficient USD / no price"}
        self.free_usd -= collateral + fee
        if price is not None:
            order = {"OrderID": self._next_id, "ID": self._next_id, "Pair": pair, "Side": "SHORT_OPEN", "Type": "LIMIT",
                     "Status": "PENDING", "Price": float(price), "Quantity": collateral / float(price),
                     "Collateral": collateral, "OpenFee": fee, "CreateTimestamp": int(self._now().timestamp() * 1000)}
            self._next_id += 1
            self.orders.append(order)
            self.history.append(order)
            return {"Success": True, **{k: order[k] for k in ("ID", "Pair", "Status", "Collateral", "OpenFee", "CreateTimestamp")},
                    "OrderType": "LIMIT", "EntryPrice": order["Price"], "ShortQty": order["Quantity"]}
        pos = self._add_short(pair, collateral, last)
        self._fill(pair, "SHORT_OPEN", "MARKET", collateral / last, last)
        return {"Success": True, "ID": pos["ID"], "Pair": pair, "OrderType": "MARKET", "EntryPrice": pos["EntryPrice"],
                "ShortQty": pos["ShortQty"], "Collateral": pos["Collateral"], "OpenFee": fee, "Status": "OPEN"}

    def _add_short(self, pair: str, collateral: float, price: float) -> dict[str, Any]:
        qty = collateral / price
        pos = self.shorts.get(pair)
        if pos is None:
            pos = self.shorts[pair] = {"ID": self._next_id, "Pair": pair, "EntryPrice": price, "ShortQty": qty,
                                       "Collateral": collateral, "PositionStatus": "OPEN"}
            self._next_id += 1
        else:
            total = pos["ShortQty"] + qty
            pos["EntryPrice"] = (pos["ShortQty"] * pos["EntryPrice"] + qty * price) / total
            pos["ShortQty"] = total
            pos["Collateral"] += collateral
        return pos

    def close_short(self, pair_or_coin, close_qty=None, close_pct=None):
        self._count("close_short")
        pair = to_pair(pair_or_coin)
        pos = self.shorts.get(pair)
        price = self.last_price(pair)
        if pos is None or price <= 0:
            return {"Success": False, "ErrMsg": "no open short"}
        qty = pos["ShortQty"] * float(close_pct) / 100.0 if close_qty is None else min(float(close_qty), pos["ShortQty"])
        ratio = qty / pos["ShortQty"]
        pnl = qty * (pos["EntryPrice"] - price)
        fee = qty * price * self.fees.short_close
        ret = pos["Collateral"] * ratio + pnl - fee
        self.free_usd += ret
        pos["ShortQty"] -= qty
        pos["Collateral"] -= pos["Collateral"] * ratio
        if pos["ShortQty"] <= 1e-12 or ratio >= 0.999999:
            del self.shorts[pair]
        self._fill(pair, "SHORT_CLOSE", "MARKET", qty, price)
        return {"Success": True, "ClosePrice": price, "RealizedPNL": pnl, "CloseFee": fee, "ReturnAmount": ret,
                "ClosedQty": qty, "FullyClosed": pair not in self.shorts}

    def get_short_positions(self):
        self._count("get_short_positions")
        out = []
        for pos in self.shorts.values():
            price = self.last_price(pos["Pair"])
            pnl = pos["ShortQty"] * (pos["EntryPrice"] - price)
            out.append({**pos, "CurrentPrice": price, "UnrealizedPNL": pnl,
                        "UnrealizedPNLPct": pnl / pos["Collateral"] if pos["Collateral"] else 0.0})
        return {"Success": True, "Positions": deepcopy(out)}

    def get_pending_count(self):
        self._count("get_pending_count")
        return {"Success": True, "TotalPending": len(self.orders), "OrderPairs": {}}

    def get_server_time(self):
        self._count("get_server_time")
        return {"ServerTime": int(self._now().timestamp() * 1000)}

    # ------------------------------------------------------------------ helpers
    def equity(self) -> float:
        self._sync()
        value = self.free_usd
        value += sum(o["Quantity"] * o["Price"] for o in self.orders if o["Side"] == "BUY")
        value += sum(o["Collateral"] for o in self.orders if o["Side"] == "SHORT_OPEN")
        value += sum(q * self.last_price(to_pair(c)) for c, q in self.coins.items())
        value += sum(o["Quantity"] * self.last_price(o["Pair"]) for o in self.orders if o["Side"] == "SELL")
        value += sum(p["Collateral"] + p["ShortQty"] * (p["EntryPrice"] - self.last_price(p["Pair"]))
                     for p in self.shorts.values())
        return value
