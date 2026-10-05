"""Account coordination with a temporary MM/RXM-specific ledger.

The intended ownership model supports arbitrary independent strategies. A future
position table will record strategy, symbol, position (quantity), and price.
Allocation, reservations and reconciliation will use those strategy-owned records.

TEMPORARY: this implementation uses a version-1 JSON snapshot with hardcoded MM
and RXM books, name-based execution policies, and an RXM-specific bootstrap. These
are transitional integration choices; they must not define ownership for future
strategies. The general ledger and its migration are deferred; see the deferred
position-ownership section in docs/MARKET_MAKING.md.

Shared invariants remain: one coordinator owns the transport, physical balances
reconcile before new risk is permitted, and SQLite commits precede submissions.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_DOWN
import fcntl
import json
import math
from pathlib import Path
import sqlite3
import uuid

from tradebot.core.symbols import to_coin, to_pair
from tradebot.engine.state.snapshot import normalize_exchange_snapshot

MM = "mm-10m-fluctuation"
ACTIVE = {"READY", "SUBMITTING", "PENDING", "CANCELING"}
TERMINAL = {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}


class AccountBlocked(RuntimeError):
    pass


class AccountLock:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise AccountBlocked(f"account coordinator already running: {path}") from None

    def close(self):
        if not self.handle.closed:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()


class PortfolioStore:
    """Persist the temporary version-1 snapshot and durable order/event journals.

    TODO (deferred ledger build): replace the strategy-specific position state
    with strategy/symbol/position/price records. Migrate existing ownership and
    accounting explicitly, preserving pending reservations and order history.
    """

    def __init__(self, path, clock):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS portfolio (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp TEXT, kind TEXT, payload TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS completed_orders (intent_id TEXT PRIMARY KEY, order_id TEXT, strategy TEXT, payload TEXT)")
        self.clock = clock
        row = self.db.execute("SELECT payload FROM portfolio WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else None
        if self.state is not None and self.state.get("version") != 1:
            raise AccountBlocked("unsupported/corrupt portfolio state")

    def save(self, kind, payload=None):
        # Keep active state bounded over long runs; history and accounting commit
        # together. Restore the durable state if a write fails (e.g. full disk).
        completed = [o for o in self.state["orders"].values() if o["status"] in TERMINAL]
        active_state = dict(self.state, orders={k: o for k, o in self.state["orders"].items()
                                               if o["status"] not in TERMINAL})
        try:
            with self.db:
                for order in completed:
                    self.db.execute("INSERT OR REPLACE INTO completed_orders VALUES (?,?,?,?)",
                                    (order["intent_id"], order.get("order_id"), order["strategy"],
                                     json.dumps(order, allow_nan=False)))
                self.db.execute("INSERT OR REPLACE INTO portfolio VALUES (1, ?)",
                                (json.dumps(active_state, allow_nan=False),))
                self.db.execute("INSERT INTO events(timestamp,kind,payload) VALUES(?,?,?)",
                                (self.clock.now().isoformat(), kind, json.dumps(payload or {}, allow_nan=False)))
        except Exception:
            row = self.db.execute("SELECT payload FROM portfolio WHERE id=1").fetchone()
            self.state = json.loads(row[0]) if row else None
            raise
        for order in completed:
            del self.state["orders"][order["intent_id"]]

    def completed(self, strategy, order_id=None):
        query = "SELECT payload FROM completed_orders WHERE strategy=?"
        args = [strategy]
        if order_id is not None:
            query += " AND order_id=?"
            args.append(str(order_id))
        return [json.loads(row[0]) for row in self.db.execute(query, args)]

    def close(self):
        self.db.close()


def order_rows(response):
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        raise AccountBlocked("malformed order response")
    if response.get("Success") is False:
        if "no order" in str(response.get("ErrMsg", "")).lower():
            return []
        raise AccountBlocked(f"order query failed: {response.get('ErrMsg')}")
    rows = response.get("OrderMatched")
    if rows is None and isinstance(response.get("OrderDetail"), dict):
        rows = [response["OrderDetail"]]
    if not isinstance(rows, list):
        raise AccountBlocked("order response missing rows")
    return rows


def wallet_of(balance):
    wallet = balance.get("SpotWallet", balance.get("Wallet"))
    if balance.get("Success") is False or not isinstance(wallet, dict) or "USD" not in wallet:
        raise AccountBlocked("missing physical wallet")
    return wallet


def total_quantity(wallet, coin):
    entry = wallet.get(coin, {})
    return float(entry.get("Free", 0)) + float(entry.get("Lock", entry.get("Locked", 0)))


def remaining(order):
    return max(0.0, order["quantity"] - order.get("filled", 0.0))


def canonical(value, precision):
    """Fixed decimals prevent a second float flooring from moving an aligned tick."""
    return format(Decimal(str(value)).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN), "f")


class AccountCoordinator:
    """Coordinate one account through the temporary MM/RXM ownership adapter."""

    def __init__(self, port, store, config, fees, clock, *, dry_run=False, import_order_ids=(), paused=lambda strategy: False):
        self.port, self.store, self.config, self.fees, self.clock = port, store, config, fees, clock
        self.dry_run = dry_run
        self.paused = paused
        self.import_order_ids = {str(x) for x in import_order_ids}
        self.balance, self.shorts, self.tickers, self.pending = {}, {}, {}, []
        self.rules = port.get_exchange_info()["TradePairs"]
        self.rules_read_at = clock.monotonic()
        self.blocked = None
        self.sync()

    @property
    def state(self):
        return self.store.state

    @property
    def orders(self):
        return self.state["orders"]

    def owner(self, coin):
        """Temporary MM-book lookup; this is not general position ownership."""
        return self.state["mm"][coin]

    def active(self, strategy=None):
        return [o for o in self.orders.values() if o["status"] in ACTIVE and
                (strategy is None or o["strategy"] == strategy)]

    def _physical_cash_assets(self):
        # Free USD + all attributable collateral/principal. No double count of
        # the venue's optional Lock display and no spendable short-sale proceeds.
        free = float(wallet_of(self.balance)["USD"]["Free"])
        pending = 0.0
        for row in self.pending:
            qty = float(row["Quantity"])
            filled = float(row.get("FilledQuantity", 0))
            if not 0 <= filled < qty:
                raise AccountBlocked("ambiguous cumulative fill on pending order")
            if row["Side"] in {"BUY", "SHORT_OPEN"}:
                pending += (qty-filled) * float(row["Price"])
        return free + pending + sum(float(p["Collateral"]) for p in self.shorts["Positions"])

    def _bootstrap(self):
        # Temporary legacy import: the current integration attributes existing
        # holdings to RXM. The future ledger must import explicit strategy owners
        # instead of treating RXM as the owner of every remaining position.
        unknown = [r["OrderID"] for r in self.pending if str(r["OrderID"]) not in self.import_order_ids]
        if unknown:
            raise AccountBlocked(f"unknown pending order ownership: {unknown}; import the RXM journal first")
        snapshot = normalize_exchange_snapshot(self.balance, self.shorts, self.tickers, self.pending)
        if snapshot["unpriced"] or snapshot["lock_unexplained_usd"] > 0.01:
            raise AccountBlocked("bootstrap has unpriced inventory or unexplained USD locks")
        equity = snapshot["equity_usd"]
        capital = equity * self.config.capital.mm_fraction
        # Include pending spot fees in the headroom: RXM cannot commit MM capital.
        pending_fees = sum((float(r["Quantity"])-float(r.get("FilledQuantity", 0))) * float(r["Price"]) * self.fees.spot_maker
                           for r in self.pending if r["Side"] == "BUY")
        free = float(wallet_of(self.balance)["USD"]["Free"]) - pending_fees
        if not math.isfinite(equity) or equity <= 0 or capital > free:
            raise AccountBlocked(f"MM funding shortfall: requires ${capital:.2f}, unreserved cash ${free:.2f}; no automatic liquidation")
        self.store.state = {
            "version": 1, "capital_fraction": self.config.capital.mm_fraction,
            "initial_equity": equity, "rxm_capital": equity-capital,
            "expected_cash_assets": self._physical_cash_assets(),
            "rxm_fees": 0.0,
            "short_basis": {p["Pair"]: float(p["ShortQty"])*float(p["CurrentPrice"]) for p in self.shorts["Positions"]},
            "rxm_cost": {c: total_quantity(wallet_of(self.balance), c)*snapshot["prices"].get(c, 0) for c in wallet_of(self.balance) if c != "USD"},
            "rxm_quantity": {c: total_quantity(wallet_of(self.balance), c) for c in wallet_of(self.balance) if c != "USD"},
            "short_quantity": {p["Pair"]: float(p["ShortQty"]) for p in self.shorts["Positions"]},
            "mm": {c: {"capital": capital*w, "cash": capital*w, "quantity": 0.0,
                       "cost": 0.0, "fees": 0.0, "realized_pnl": 0.0} for c, w in self.config.allocations.items()},
            "orders": {}, "features": {}, "next_refresh": None, "batch": None,
            "rxm_transferred": False, "run_mode": "dry-run" if self.dry_run else "execute",
        }
        for row in self.pending:
            o = self._record("rxm", to_coin(row["Pair"]), row["Side"], float(row["Quantity"]), float(row["Price"]))
            o.update(order_id=str(row["OrderID"]), status="PENDING", filled=float(row.get("FilledQuantity", 0)),
                     value=float(row.get("FilledQuantity", 0))*float(row.get("FilledAverPrice") or row["Price"]),
                     fee=float(row.get("CommissionChargeValue", 0)), row=row)
            self.orders[o["intent_id"]] = o
        self.store.save("capital_allocated", {"mm": capital, "rxm": equity-capital})

    def sync(self):
        for attempt in range(3):
            try:
                return self._sync_once()
            except AccountBlocked as exc:
                # These can result from fills between non-atomic venue reads.
                # Retry reads only; never repeat a mutation or permit risk in between.
                if attempt == 2 or not any(x in str(exc) for x in ("mismatch", "unexplained USD lock", "RXM cash exhausted")):
                    raise

    def _sync_once(self):
        """Read all owned resting orders and query final cumulative fills on removal.

        A partial/non-atomic venue read may pause a loop. Applied fills are committed
        even on a mismatch, so retrying the read never applies them twice.
        """
        self.blocked = None
        try:
            if self.clock.monotonic()-self.rules_read_at >= 3600:
                self.rules = self.port.get_exchange_info()["TradePairs"]
                self.rules_read_at = self.clock.monotonic()
            self.pending = order_rows(self.port.list_open_orders())
            if self.state is not None:
                if self.state["capital_fraction"] != self.config.capital.mm_fraction:
                    raise AccountBlocked("capital allocation changed: restart preserves budgets; explicit migration required")
                if self.state["run_mode"] != ("dry-run" if self.dry_run else "execute"):
                    raise AccountBlocked("use separate state directories for dry-run and execution")
                lookup = {str(r["OrderID"]): r for r in self.pending}
                known = {o.get("order_id") for o in self.orders.values()}
                if any(key not in known for key in lookup):
                    self._recover_submissions()
                    known = {o.get("order_id") for o in self.orders.values()}
                    if any(key not in known for key in lookup):
                        raise AccountBlocked("unowned physical pending order")
                self._recover_submissions()
                for o in self.active():
                    if o["status"] == "READY":
                        # A crash before send is known safe to abandon, never replay.
                        o["status"] = "REJECTED"
                        continue
                    if o.get("order_id") is None:
                        raise AccountBlocked("unresolved submission; no blind retries")
                    row = lookup.get(o["order_id"])
                    if row is None:
                        found = order_rows(self.port.query_order(order_id=o["order_id"]))
                        if len(found) != 1:
                            raise AccountBlocked(f"missing owned order {o['order_id']}")
                        row = found[0]
                    self._apply(o, row)
            self.balance = self.port.get_balance()
            self.shorts = self.port.get_short_positions()
            self.tickers = self.port.get_ticker()
            wallet_of(self.balance)
            if self.shorts.get("Success") is False or "Positions" not in self.shorts or self.tickers.get("Success") is False or "Data" not in self.tickers:
                raise AccountBlocked("incomplete account read")
            if self.state is None:
                self._bootstrap()
            self._reconcile()
        except Exception as exc:
            self.blocked = str(exc)
            if self.state is not None:
                self.store.save("reconciliation_blocked", {"reason": self.blocked})
            raise
        self.store.save("reconciled")

    def _recover_submissions(self):
        unresolved = [o for o in self.active() if o["status"] == "SUBMITTING" and not o.get("order_id")]
        if not unresolved:
            return
        history = order_rows(self.port.query_order(limit=100))
        owned = {o.get("order_id") for o in self.orders.values()}
        for o in unresolved:
            if o["side"] not in {"BUY", "SELL"}:
                raise AccountBlocked("ambiguous RXM short mutation needs operator reconciliation")
            candidates = [r for r in history if str(r.get("OrderID")) not in owned and r.get("Pair") == to_pair(o["coin"])
                          and r.get("Side") == o["side"] and r.get("Type") == "LIMIT"
                          and math.isclose(float(r.get("Price", 0)), o["price"], rel_tol=1e-10)
                          and math.isclose(float(r.get("Quantity", 0)), o["quantity"], rel_tol=1e-10)
                          and abs(float(r.get("CreateTimestamp", 0))/1000-o["submitted_at"]) <= 5]
            if len(candidates) != 1:
                raise AccountBlocked("ambiguous submission: history did not identify exactly one order")
            o["order_id"] = str(candidates[0]["OrderID"])
            owned.add(o["order_id"])
            self._apply(o, candidates[0])
            self.store.save("submission_recovered", {"intent": o["intent_id"], "order": o["order_id"]})

    def _reconcile(self):
        wallet = wallet_of(self.balance)
        expected = dict(self.state["rxm_quantity"])
        for c, b in self.state["mm"].items():
            expected[c] = expected.get(c, 0) + b["quantity"]
            if b["cash"] < -1e-7 or b["quantity"] < -1e-7:
                raise AccountBlocked("insolvent MM book")
        for c in set(expected) | (set(wallet)-{"USD"}):
            actual, ledger = total_quantity(wallet, c), expected.get(c, 0)
            # Adding/subtracting billion-unit meme-coin lots can leave a few
            # micro-units when a book returns to flat. The unit-only tolerance
            # becomes too strict there. Permit at most 1e-8 USD of additional
            # roundoff, without changing cash, quantities or position ownership.
            price = float(self.tickers.get("Data", {}).get(to_pair(c), {}).get("LastPrice", 0))
            negligible = math.isfinite(price) and price > 0 and abs(actual-ledger)*price <= 1e-8
            if not math.isclose(actual, ledger, rel_tol=1e-9, abs_tol=1e-7) and not negligible:
                raise AccountBlocked(f"unexplained {c} inventory mismatch: actual {actual!r}, ledger {ledger!r}")
        actual_shorts = {p["Pair"]: float(p["ShortQty"]) for p in self.shorts["Positions"]}
        for pair in set(actual_shorts) | set(self.state["short_quantity"]):
            if not math.isclose(actual_shorts.get(pair, 0), self.state["short_quantity"].get(pair, 0), rel_tol=1e-8, abs_tol=1e-7):
                raise AccountBlocked(f"unexplained {pair} short mismatch")
        cash = self._physical_cash_assets()
        if not math.isclose(cash, self.state["expected_cash_assets"], rel_tol=1e-9, abs_tol=0.01):
            raise AccountBlocked(f"unexplained cash mismatch: actual {cash:.8f}, ledger {self.state['expected_cash_assets']:.8f}")
        snapshot = normalize_exchange_snapshot(self.balance, self.shorts, self.tickers, self.pending)
        if snapshot["lock_unexplained_usd"] > 0.01:
            raise AccountBlocked("unexplained USD lock")
        if self.rxm_free_cash() < -0.01:
            raise AccountBlocked("RXM cash exhausted")

    def _record(self, strategy, coin, side, quantity, price):
        return dict(intent_id=uuid.uuid4().hex, strategy=strategy, coin=coin, side=side,
                    quantity=quantity, price=price, filled=0.0, value=0.0, fee=0.0,
                    status="READY", order_id=None, submitted_at=self.clock.now().timestamp())

    def _apply(self, o, row):
        status = str(row.get("Status", "")).upper()
        if status not in TERMINAL | {"PENDING"}:
            raise AccountBlocked(f"unsupported order status {status}")
        quantity = float(row["Quantity"])
        filled = float(row.get("FilledQuantity", 0))
        # Some simulation adapters omit cumulative quantity on a terminal fill.
        if status == "FILLED" and filled == 0:
            filled = quantity
        if status == "PENDING" and filled == quantity:
            raise AccountBlocked("ambiguous pending fill quantity")
        if filled < o["filled"]-1e-8 or filled > o["quantity"]*(1+1e-9):
            raise AccountBlocked("non-monotonic cumulative fill")
        price = float(row.get("FilledAverPrice") or row.get("Price", 0))
        if not all(math.isfinite(v) for v in (quantity, filled, price)) or quantity <= 0 or filled < 0 or price <= 0:
            raise AccountBlocked("invalid numerical fill data")
        value = filled * price
        delta, value_delta = filled-o["filled"], value-o["value"]
        fee_delta = 0.0
        if o["side"] in {"BUY", "SELL"}:
            if float(row.get("CommissionChargeValue", 0)) and row.get("CommissionCoin", "USD") != "USD":
                raise AccountBlocked("non-USD spot commission requires explicit accounting support")
            rate = self.fees.spot_maker if row.get("Type", "LIMIT") == "LIMIT" else self.fees.spot_taker
            fee = float(row.get("CommissionChargeValue", value*rate))
            fee_delta = fee-o["fee"]
            change = (-value_delta if o["side"] == "BUY" else value_delta) - fee_delta
            self.state["expected_cash_assets"] += change
            if o["strategy"] == MM:
                b = self.owner(o["coin"])
                b["cash"] += change
                b["fees"] += fee_delta
                if o["side"] == "BUY":
                    b["quantity"] += delta
                    b["cost"] += value_delta + fee_delta
                else:
                    cost = b["cost"] * delta / b["quantity"] if b["quantity"] else 0
                    b["quantity"] -= delta
                    b["cost"] -= cost
                    b["realized_pnl"] += value_delta-fee_delta-cost
            else:
                q, costs = self.state["rxm_quantity"], self.state["rxm_cost"]
                self.state["rxm_fees"] += fee_delta
                if o["side"] == "BUY":
                    costs[o["coin"]] = costs.get(o["coin"], 0)+value_delta+fee_delta
                else:
                    cost = costs.get(o["coin"], 0)*delta/q[o["coin"]] if q.get(o["coin"], 0) else 0
                    costs[o["coin"]] = costs.get(o["coin"], 0)-cost
                q[o["coin"]] = q.get(o["coin"], 0) + (delta if o["side"] == "BUY" else -delta)
            o["fee"] = fee
        elif o["side"] == "SHORT_OPEN":
            pair = to_pair(o["coin"])
            self.state["short_quantity"][pair] = self.state["short_quantity"].get(pair, 0) + delta
            self.state["short_basis"][pair] = self.state["short_basis"].get(pair, 0) + value_delta
            if status in {"CANCELED", "CANCELLED", "REJECTED"} and o["status"] not in TERMINAL:
                refund = (o["quantity"]-filled)*o["price"]*self.fees.short_open
                self.state["expected_cash_assets"] += refund
                self.state["rxm_fees"] -= refund
        o.update(filled=filled, value=value, status=status, row=deepcopy(row))
        if delta or value_delta:
            self.store.save("fill", {"strategy": o["strategy"], "coin": o["coin"], "side": o["side"],
                                     "quantity": delta, "value": value_delta, "fee_delta": fee_delta,
                                     "execution_timestamp_ms": row.get("FinishTimestamp"), "cumulative_fee": o["fee"],
                                     "order_id": o["order_id"]})

    def reservations(self, strategy=None, coin=None):
        cash, coins = 0.0, {}
        for o in self.active(strategy):
            if coin is not None and o["coin"] != coin:
                continue
            rem = remaining(o)
            if o["side"] in {"BUY", "SHORT_OPEN"}:
                fee = self.fees.spot_maker if o["side"] == "BUY" else self.fees.short_open
                cash += rem*o["price"]*(1+fee)
            elif o["side"] == "SELL":
                coins[o["coin"]] = coins.get(o["coin"], 0)+rem
        return cash, coins

    def mm_free_cash(self):
        return sum(b["cash"] for b in self.state["mm"].values()) - self.reservations(MM)[0]

    def rxm_free_cash(self):
        # The physical exchange does not reserve pending spot fees, our ledgers do.
        fees = sum(remaining(o)*o["price"]*self.fees.spot_maker for o in self.active() if o["side"] == "BUY")
        return float(wallet_of(self.balance)["USD"]["Free"]) - self.mm_free_cash() - fees

    def view_balance(self, strategy):
        if strategy != "rxm":
            raise ValueError("MM uses explicit per-coin books")
        wallet = deepcopy(wallet_of(self.balance))
        rxm_fees = sum(remaining(o)*o["price"]*self.fees.spot_maker for o in self.active("rxm") if o["side"] == "BUY")
        # Pending fees are a local spending reservation, not a realized loss.
        wallet["USD"]["Free"] = max(0.0, self.rxm_free_cash()+rxm_fees)
        # Keep RXM's short collateral display, subtract MM resting principal only.
        mm_locked = sum(remaining(o)*o["price"] for o in self.active(MM) if o["side"] == "BUY")
        wallet["USD"]["Lock"] = max(0.0, float(wallet["USD"].get("Lock", 0))-mm_locked)
        _, held = self.reservations("rxm")
        for c in set(wallet)-{"USD"} | set(self.state["rxm_quantity"]):
            total = self.state["rxm_quantity"].get(c, 0)
            wallet[c] = {"Free": max(0.0, total-held.get(c, 0)), "Lock": held.get(c, 0)}
        return {"Success": True, "SpotWallet": wallet}

    def _crosses(self, coin, side, price, exclude=None):
        buy = side in {"BUY", "SHORT_CLOSE"}
        for o in self.active():
            if o is exclude or o["coin"] != coin or (o["side"] == "BUY") == buy:
                continue
            if price is None or buy and price >= o["price"] or not buy and price <= o["price"]:
                return True
        return False

    def prepare_quotes(self, batch):
        if self.blocked:
            raise AccountBlocked(self.blocked)
        prepared = []
        for q in batch.quotes:
            b = self.owner(q.symbol)
            reserved, held = self.reservations(MM, q.symbol)
            if q.side == "BUY" and q.quantity*q.price*1.0005 > b["cash"]-reserved+1e-8:
                continue
            if q.side == "SELL" and q.quantity > b["quantity"]-held.get(q.symbol, 0)+1e-8:
                continue
            if self._crosses(q.symbol, q.side, q.price):
                self.store.save("self_cross_skipped", q.model_dump())
                continue
            o = self._record(MM, q.symbol, q.side, q.quantity, q.price)
            self.orders[o["intent_id"]] = o
            prepared.append(o)
        self.store.save("quotes_reserved", {"batch": batch.model_dump(mode="json"),
                                            "intents": [o["intent_id"] for o in prepared]})
        return prepared

    def submit(self, o):
        if self.blocked:
            raise AccountBlocked(self.blocked)
        if self.dry_run:
            o["status"] = "REJECTED"
            self.store.save("dry_run_order", o)
            return {"Success": False, "ErrMsg": "dry-run: transport disabled"}
        rule = self.rules[to_pair(o["coin"])]
        # The strategy produced a tick coordinate; render that coordinate exactly.
        px = (format(o["price"], f".{int(rule['PricePrecision'])}f") if o["strategy"] == MM
              else canonical(o["price"], int(rule["PricePrecision"])))
        qty = canonical(o["quantity"], int(rule["AmountPrecision"]))
        if float(qty) <= 0 or float(qty)*float(px) < float(rule["MiniOrder"]) or not rule.get("CanTrade"):
            o["status"] = "REJECTED"
            self.store.save("precision_rejected", o)
            return {"Success": False, "ErrMsg": "precision/minimum"}
        o.update(quantity=float(qty), price=float(px))
        # Fresh venue best prices, not historical Binance prices, protect passivity.
        if o["strategy"] == MM:
            capacity = getattr(self.port, "wait_for_capacity", None)
            if capacity is not None:
                # Avoid another limiter sleep between the passivity/deadline
                # checks and submission. Only this coordinator calls the port.
                capacity(2)
            ticker = self.port.get_ticker(to_pair(o["coin"]))
            t = ticker.get("Data", {}).get(to_pair(o["coin"]), {})
            best = float(t.get("MinAsk" if o["side"] == "BUY" else "MaxBid", 0))
            if not math.isfinite(best) or best <= 0 or o["side"] == "BUY" and o["price"] >= best or o["side"] == "SELL" and o["price"] <= best:
                o["status"] = "REJECTED"
                self.store.save("marketable_quote_skipped", o)
                return {"Success": False, "ErrMsg": "fresh ticker passivity check"}
        expiry = self.state.get("next_refresh")
        if self.paused(o["strategy"]) or (o["strategy"] == MM and expiry is not None and self.clock.now().timestamp() >= expiry):
            o["status"] = "REJECTED"
            self.store.save("paused_or_expired", o)
            return {"Success": False, "ErrMsg": "strategy paused or quote expired"}
        if self._crosses(o["coin"], o["side"], o["price"], o):
            o["status"] = "REJECTED"
            self.store.save("self_cross_skipped", o)
            return {"Success": False, "ErrMsg": "account self-cross"}
        o.update(status="SUBMITTING", submitted_at=self.clock.now().timestamp())
        self.store.save("submitting", o)
        try:
            response = self.port.place_order(o["coin"], o["side"], qty, price=px, order_type="LIMIT")
            if response.get("Success") is False:
                o["status"] = "REJECTED"
            else:
                row = response.get("OrderDetail")
                if not isinstance(row, dict) or row.get("OrderID") is None:
                    raise AccountBlocked("submission response missing order identity")
                o["order_id"] = str(row["OrderID"])
                self._apply(o, row)
            self.store.save("submission_result", {"intent": o["intent_id"], "response": response})
            # Keep cached wallet conservative between sends; a full read follows batch.
            if response.get("Success"):
                wallet = wallet_of(self.balance)
                if o["side"] == "BUY":
                    wallet["USD"]["Free"] -= o["quantity"]*o["price"]
                    wallet["USD"]["Lock"] = float(wallet["USD"].get("Lock", 0))+o["quantity"]*o["price"]
            return response
        except Exception as exc:
            self.blocked = f"uncertain submission: {exc}"
            self.store.save("submission_uncertain", {"intent": o["intent_id"], "reason": self.blocked})
            raise AccountBlocked(self.blocked) from exc

    def cancel(self, strategy, order_id):
        candidates = list(self.orders.values()) + self.store.completed(strategy, order_id)
        o = next((o for o in candidates if o.get("order_id") == str(order_id) and o["strategy"] == strategy), None)
        if o is None:
            return {"Success": False, "ErrMsg": "order does not belong to strategy"}
        if self.dry_run:
            return {"Success": False, "ErrMsg": "dry-run: cancellation transport disabled"}
        if o["status"] in TERMINAL:
            return {"Success": True}
        o["status"] = "CANCELING"
        self.store.save("canceling", {"order_id": str(order_id)})
        response = self.port.cancel_order(order_id=order_id)
        # The ACK alone never releases a reservation. Query cumulative final fills.
        rows = order_rows(self.port.query_order(order_id=order_id))
        if len(rows) != 1:
            raise AccountBlocked("cancellation final state unavailable")
        self._apply(o, rows[0])
        self.store.save("cancel_result", response)
        if o["status"] not in TERMINAL:
            raise AccountBlocked("cancellation not settled")
        return response

    def report(self):
        out = {"initial_equity": self.state["initial_equity"], "rxm_capital": self.state["rxm_capital"],
               "blocked": self.blocked, "mm": {}}
        for coin, b in self.state["mm"].items():
            price = float(self.tickers.get("Data", {}).get(to_pair(coin), {}).get("LastPrice", 0))
            reserved, held = self.reservations(MM, coin)
            value = b["quantity"]*price
            out["mm"][coin] = dict(b, free_cash=b["cash"]-reserved, reserved_cash=reserved,
                                    reserved_quantity=held.get(coin, 0), equity=b["cash"]+value,
                                    gross_inventory=value, net_inventory=value, collateral=0.0,
                                    spot_fees=b["fees"], short_open_fees=0.0, short_close_fees=0.0,
                                    unrealized_pnl=value-b["cost"], net_pnl=b["cash"]+value-b["capital"])
        out["rxm"] = normalize_exchange_snapshot(self.view_balance("rxm"), self.shorts, self.tickers,
                                                  self.scoped("rxm").list_open_orders())
        rxm = out["rxm"]
        unrealized = (sum(rxm["longs"].values())-sum(self.state["rxm_cost"].values())
                      + sum(self.state["short_basis"].get(p["Pair"], 0)-float(p["ShortQty"])*float(p["CurrentPrice"])
                            for p in self.shorts["Positions"]))
        net_pnl = rxm["equity_usd"]-self.state["rxm_capital"]
        rxm.update(capital=self.state["rxm_capital"], available_cash=self.rxm_free_cash(),
                   fees=self.state["rxm_fees"], realized_pnl=net_pnl-unrealized, unrealized_pnl=unrealized,
                   net_pnl=net_pnl,
                   gross_inventory=sum(rxm["longs"].values())+sum(rxm["shorts"].values()),
                   net_inventory=sum(rxm["longs"].values())-sum(rxm["shorts"].values()))
        return out

    def scoped(self, strategy):
        return StrategyAccountPort(self, strategy)


class StrategyAccountPort:
    """Temporary MM/RXM transport views and strategy-specific order restrictions."""
    def __init__(self, account, strategy):
        self.account, self.strategy = account, strategy
        self.is_live = bool(getattr(account.port, "is_live", False))

    def get_exchange_info(self):
        return {"TradePairs": self.account.rules, "IsRunning": True}

    def get_ticker(self, pair=None):
        # Repeg explicitly requests one pair: fetch fresh via the single limiter.
        return self.account.port.get_ticker(pair) if pair else deepcopy(self.account.tickers)

    def get_balance(self):
        return self.account.view_balance(self.strategy)

    def get_short_positions(self):
        return deepcopy(self.account.shorts) if self.strategy == "rxm" else {"Success": True, "Positions": []}

    def list_open_orders(self):
        return {"Success": True, "OrderMatched": [deepcopy(o["row"]) for o in self.account.active(self.strategy) if "row" in o]}

    def query_order(self, order_id=None, pair=None, pending_only=None, offset=None, limit=None):
        candidates = list(self.account.orders.values())
        if not pending_only:
            candidates += self.account.store.completed(self.strategy, order_id)
        rows = [deepcopy(o["row"]) for o in candidates if o["strategy"] == self.strategy and "row" in o
                and (order_id is None or str(order_id) == o.get("order_id"))
                and (pair is None or to_pair(pair) == to_pair(o["coin"]))
                and (not pending_only or o["status"] in ACTIVE)]
        return {"Success": True, "OrderMatched": rows}

    def cancel_order(self, order_id=None, pair=None):
        if order_id is None:
            return {"Success": False, "ErrMsg": "scoped cancellation requires an order ID"}
        result = self.account.cancel(self.strategy, order_id)
        self.account.sync()
        return result

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        a, coin = self.account, to_coin(pair_or_coin)
        if self.strategy != "rxm":
            return {"Success": False, "ErrMsg": "MM requires engine quote reservation"}
        if a.blocked or price is None or (order_type or "LIMIT") != "LIMIT" or side not in {"BUY", "SELL"}:
            return {"Success": False, "ErrMsg": "account blocked or unsupported spot order"}
        quantity, price = float(quantity), float(price)
        if not all(math.isfinite(v) and v > 0 for v in (quantity, price)):
            return {"Success": False, "ErrMsg": "invalid size/price"}
        if side == "BUY" and quantity*price*(1+a.fees.spot_maker) > a.rxm_free_cash()+1e-8:
            return {"Success": False, "ErrMsg": "RXM allocated cash exceeded"}
        _, held = a.reservations("rxm")
        if side == "SELL" and quantity > a.state["rxm_quantity"].get(coin, 0)-held.get(coin, 0)+1e-8:
            return {"Success": False, "ErrMsg": "RXM owned inventory exceeded"}
        o = a._record("rxm", coin, side, quantity, price)
        a.orders[o["intent_id"]] = o
        result = a.submit(o)
        a.sync()
        return result

    def open_short(self, pair_or_coin, collateral, price=None):
        a = self.account
        if self.strategy != "rxm" or a.dry_run or a.blocked or a.paused("rxm"):
            return {"Success": False, "ErrMsg": "short mutation prohibited"}
        collateral = float(collateral)
        coin = to_coin(pair_or_coin)
        if collateral <= 0 or not math.isfinite(collateral) or collateral*(1+a.fees.short_open) > a.rxm_free_cash():
            return {"Success": False, "ErrMsg": "RXM short exceeds available cash"}
        if a._crosses(coin, "SHORT_OPEN", float(price) if price is not None else None):
            return {"Success": False, "ErrMsg": "account self-cross"}
        if price is not None:
            price = canonical(price, int(a.rules[to_pair(coin)]["PricePrecision"]))
        px = float(price or a.tickers["Data"][to_pair(coin)]["LastPrice"])
        o = a._record("rxm", coin, "SHORT_OPEN", collateral/px, px)
        a.orders[o["intent_id"]] = o
        o["status"] = "SUBMITTING"
        a.store.save("short_submitting", o)
        try:
            r = a.port.open_short(coin, collateral, price=price)
            if r.get("Success") is False:
                o["status"] = "REJECTED"
            else:
                a.state["expected_cash_assets"] -= float(r["OpenFee"])
                a.state["rxm_fees"] += float(r["OpenFee"])
                if price is None:
                    # Position ID is distinct from a resting order ID.
                    pair = to_pair(coin)
                    before = next((p for p in a.shorts["Positions"] if p["Pair"] == pair), {})
                    previous_entry_value = float(before.get("ShortQty", 0))*float(before.get("EntryPrice", 0))
                    added_entry_value = float(r["ShortQty"])*float(r["EntryPrice"])-previous_entry_value
                    a.state["short_basis"][pair] = a.state["short_basis"].get(pair, 0)+added_entry_value
                    a.state["short_quantity"][pair] = float(r["ShortQty"])
                    o["status"] = "FILLED"
                else:
                    o.update(order_id=str(r["ID"]), status="PENDING")
            a.store.save("short_result", r)
        except Exception as exc:
            a.blocked = "uncertain short open"
            raise AccountBlocked(a.blocked) from exc
        a.sync()
        return r

    def close_short(self, pair_or_coin, close_qty=None, close_pct=None):
        a = self.account
        if self.strategy != "rxm" or a.dry_run or a.blocked or a.paused("rxm"):
            return {"Success": False, "ErrMsg": "short mutation prohibited"}
        coin, pair = to_coin(pair_or_coin), to_pair(pair_or_coin)
        if a._crosses(coin, "SHORT_CLOSE", None):
            return {"Success": False, "ErrMsg": "account self-cross"}
        o = a._record("rxm", coin, "SHORT_CLOSE", 0, 0)
        a.orders[o["intent_id"]] = o
        o["status"] = "SUBMITTING"
        a.store.save("cover_submitting", o)
        try:
            r = a.port.close_short(coin, close_qty=close_qty, close_pct=close_pct)
            if r.get("Success") is False:
                o["status"] = "REJECTED"
            else:
                a.state["expected_cash_assets"] += float(r["RealizedPNL"])-float(r["CloseFee"])
                a.state["rxm_fees"] += float(r["CloseFee"])
                ratio = float(r["ClosedQty"])/a.state["short_quantity"][pair]
                a.state["short_basis"][pair] *= max(0.0, 1-ratio)
                a.state["short_quantity"][pair] -= float(r["ClosedQty"])
                o["status"] = "FILLED"
            a.store.save("cover_result", r)
        except Exception as exc:
            a.blocked = "uncertain short close"
            raise AccountBlocked(a.blocked) from exc
        a.sync()
        return r

    def get_pending_count(self):
        return {"Success": True, "TotalPending": len(self.account.active(self.strategy))}

    def requests_last_minute(self):
        return self.account.port.requests_last_minute()

    def get_server_time(self):
        return self.account.port.get_server_time()
