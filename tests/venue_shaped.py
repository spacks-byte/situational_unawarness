"""A ReplayExchangePort that answers in the venue's documented shapes (tests/fixtures/roostoo).

The replay simulator has the right economics but tidier responses than Roostoo. This wrapper adds
the documented quirks the coordinator has to survive: PENDING rows reporting FilledQuantity ==
Quantity, `{"Success": false, "ErrMsg": "no order matched"}` for an empty query, offset/limit
paging (oldest first, the order the coordinator must not rely on), commission fields on fills, and
optional failure injection (lost responses, delayed cancel settlement, commission rounding).
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path

import pandas as pd

from tradebot.core.clock import SimClock
from tradebot.core.config import Settings
from tradebot.engine.state.portfolio import AccountCoordinator, PortfolioStore
from tradebot.exchange.replay import ReplayExchangePort

FIXTURES = Path(__file__).parent / "fixtures" / "roostoo"
COINS = ["PEPE", "BONK", "1000CHEEMS"]


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


_ROW = {k: v for k, v in fixture("query_order_mixed.json")["OrderMatched"][1].items()
        if k in {"Role", "ServerTimeUsage", "StopType", "CommissionCoin", "CommissionPercent", "Note"}}


class VenueShapedPort:
    def __init__(self, sim, fees, *, commission_delta=0.0):
        self.sim, self.fees = sim, fees
        self.commission_delta = commission_delta   # venue-reported fee minus the fee actually charged
        self.lose = set()                           # methods whose next response is lost after executing
        self.fail = set()                           # methods whose next call fails before reaching the venue
        self.cancel_delay = 0                       # venue reads before an acknowledged cancel takes effect
        self._deferred = []                         # [(reads left, order_id)]
        self.calls = sim.calls

    def __getattr__(self, name):
        return getattr(self.sim, name)

    # ---------------------------------------------------------------- shaping
    def _row(self, order):
        row = {**_ROW, **deepcopy(order)}
        row.setdefault("FilledQuantity", 0.0)
        row.setdefault("FilledAverPrice", 0.0)
        row["OrderValue"] = row["Quantity"] * row["Price"]
        row.setdefault("FinishTimestamp", row["CreateTimestamp"])
        if row["Status"] == "PENDING":
            row["FilledQuantity"] = row["Quantity"]          # documented PENDING shape (= unfilled)
            row["CommissionChargeValue"] = 0.0
        elif row["Side"] in {"BUY", "SELL"}:
            filled = float(order.get("FilledQuantity", 0.0))
            price = float(order.get("FilledAverPrice") or row["Price"])
            row["CommissionChargeValue"] = round(filled * price * self.fees.spot_maker + self.commission_delta, 6) if filled else 0.0
        return row

    def _call(self, name, *args, **kwargs):
        if name in self.fail:
            self.fail.discard(name)
            raise ConnectionError(f"{name}: connection reset before the request was sent")
        result = getattr(self.sim, name)(*args, **kwargs)
        if name in self.lose:
            self.lose.discard(name)
            raise TimeoutError(f"{name}: read timed out (the venue executed it)")
        return result

    # ---------------------------------------------------------------- endpoints
    def place_order(self, *args, **kwargs):
        r = self._call("place_order", *args, **kwargs)
        if r.get("Success") and "OrderDetail" in r:
            r = {"Success": True, "ErrMsg": "", "OrderDetail": self._row(r["OrderDetail"])}
        return r

    def cancel_order(self, order_id=None, pair=None):
        if self.cancel_delay and order_id is not None and "cancel_order" not in self.fail:
            self.sim._count("cancel_order")
            self._deferred.append([self.cancel_delay, str(order_id)])
            return {"Success": True, "ErrMsg": "", "CanceledList": [int(order_id)]}   # acknowledged only
        return self._call("cancel_order", order_id=order_id, pair=pair)

    def _settle(self):
        for item in list(self._deferred):
            item[0] -= 1
            if item[0] <= 0:
                self._deferred.remove(item)
                self.sim.cancel_order(order_id=item[1])

    def open_short(self, *args, **kwargs):
        return self._call("open_short", *args, **kwargs)

    def close_short(self, *args, **kwargs):
        return self._call("close_short", *args, **kwargs)

    def query_order(self, order_id=None, pair=None, pending_only=None, offset=None, limit=None):
        self._settle()
        rows = order_rows_of(self.sim.query_order(order_id=order_id, pair=pair, pending_only=pending_only))
        rows = sorted(rows, key=lambda r: (r["CreateTimestamp"], r["OrderID"]))
        start = offset or 0
        rows = rows[start:start + limit] if limit else rows[start:]
        if not rows:
            return {"Success": False, "ErrMsg": "no order matched"}
        return {"Success": True, "ErrMsg": "", "OrderMatched": [self._row(r) for r in rows]}

    def list_open_orders(self):
        return self.query_order(pending_only=True)


def order_rows_of(response):
    return response.get("OrderMatched", []) if response.get("Success") else []


def frames(start="2026-09-21"):
    idx = pd.date_range(start, periods=20_000, freq="s", tz="UTC")
    return {c: pd.DataFrame({"open": 100., "high": 100., "low": 100., "close": 100., "trades": 1}, index=idx) for c in COINS}


def venue_account(tmp_path, **venue_kwargs):
    """Coordinator over a venue-shaped replay account. Returns (account, venue, clock, reopen) where
    reopen() builds a fresh coordinator on the same portfolio.db (a process restart)."""
    clock = SimClock(datetime(2026, 9, 21, 1, tzinfo=UTC))
    sim = ReplayExchangePort(frames(), clock, initial_usd=100_000, intervals={c: "1s" for c in COINS})
    settings = Settings()
    venue = VenueShapedPort(sim, settings.fees, **venue_kwargs)
    stores = []

    def open_account():
        if stores:
            stores[-1].close()
        stores.append(PortfolioStore(tmp_path / "portfolio.db", clock))
        account = AccountCoordinator(venue, stores[-1], settings.market_making, settings.fees, clock)
        account.sync()
        return account
    return open_account(), venue, clock, open_account


def fill_at(venue, clock, price, coin="PEPE"):
    """Make the next one-second bar trade at `price` (touch-fills resting limits at that price)."""
    ts = pd.Timestamp(clock.now()).ceil("s")
    venue.sim.bars[f"{coin}/USD"].loc[ts, ["low", "high", "close"]] = [price, price, price]
    clock.advance(1)
