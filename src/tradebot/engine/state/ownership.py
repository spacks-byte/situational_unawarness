"""Strategy/pair/leg ownership, independent of exchange transport.

All amounts are quote-currency units. Long and short quantities are positive and
kept separately; opposing virtual positions must never erase one another.
Writes participate in the caller's SQLite transaction. ``atomic`` also supports
nested calls, so decisions, reservations and ownership can commit together.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import uuid


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


class OwnershipLedger:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS owner_accounts (
                strategy TEXT NOT NULL, pair_id TEXT NOT NULL,
                capital REAL NOT NULL, cash REAL NOT NULL,
                fees REAL NOT NULL DEFAULT 0, slippage REAL NOT NULL DEFAULT 0,
                realized_gross REAL DEFAULT 0,
                PRIMARY KEY(strategy, pair_id));
            CREATE TABLE IF NOT EXISTS owner_positions (
                strategy TEXT NOT NULL, pair_id TEXT NOT NULL, leg TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('long','short')),
                quantity REAL NOT NULL, basis REAL NOT NULL, collateral REAL NOT NULL,
                PRIMARY KEY(strategy, pair_id, leg));
            CREATE TABLE IF NOT EXISTS owner_reservations (
                intent_id TEXT PRIMARY KEY, strategy TEXT NOT NULL, pair_id TEXT NOT NULL,
                cash REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS owner_fills (
                fill_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS owner_migrations (
                migration_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL);
        """)

    @contextmanager
    def atomic(self):
        name = "ownership_" + uuid.uuid4().hex
        self.db.execute(f"SAVEPOINT {name}")
        try:
            yield
            self.db.execute(f"RELEASE {name}")
        except BaseException:
            self.db.execute(f"ROLLBACK TO {name}")
            self.db.execute(f"RELEASE {name}")
            raise

    def accounts(self, strategy=None):
        sql, args = "SELECT * FROM owner_accounts", ()
        if strategy is not None:
            sql += " WHERE strategy=?"
            args = (strategy,)
        names = ("strategy", "pair_id", "capital", "cash", "fees", "slippage", "realized_gross")
        return [dict(zip(names, row)) for row in self.db.execute(sql + " ORDER BY strategy,pair_id", args)]

    def account(self, strategy, pair_id):
        return next((a for a in self.accounts(strategy) if a["pair_id"] == pair_id), None)

    def allocate(self, strategy, pair_id, capital):
        if not strategy or not pair_id or not math.isfinite(capital) or capital <= 0:
            raise ValueError("owner and positive finite capital are required")
        self.db.execute("INSERT INTO owner_accounts(strategy,pair_id,capital,cash) VALUES(?,?,?,?)",
                        (strategy, pair_id, capital, capital))

    def positions(self, strategy=None, pair_id=None):
        sql, args, conditions = "SELECT * FROM owner_positions", [], []
        for key, value in (("strategy", strategy), ("pair_id", pair_id)):
            if value is not None:
                conditions.append(key + "=?")
                args.append(value)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        keys = ("strategy", "pair_id", "leg", "symbol", "side", "quantity", "basis", "collateral")
        return [dict(zip(keys, row)) for row in self.db.execute(sql + " ORDER BY strategy,pair_id,leg", args)]

    def reserve(self, intent_id, strategy, pair_id, cash, payload, *, enforce_cash=True):
        """Reserve an entire pair intent, or an exit's owned quantities.

        Confirmed fills are always bookable even if the venue overspent. Cash
        checks belong here, before submitting. Reference OHLC replays can disable
        the check explicitly because their fixed budgets exclude fee headroom.
        """
        account = self.account(strategy, pair_id)
        if account is None or not math.isfinite(cash) or cash < 0:
            raise ValueError("invalid reservation owner/cash")
        value = (strategy, pair_id, float(cash), encoded(payload))
        old = self.db.execute("SELECT strategy,pair_id,cash,payload FROM owner_reservations WHERE intent_id=?",
                              (intent_id,)).fetchone()
        if old is not None:
            if tuple(old) != value:
                raise ValueError("reservation ID reused with different contents")
            return
        reserved = self.db.execute("SELECT COALESCE(SUM(cash),0) FROM owner_reservations WHERE strategy=? AND pair_id=?",
                                   (strategy, pair_id)).fetchone()[0]
        if enforce_cash and cash > account["cash"] - reserved + 1e-8:
            raise ValueError("owner has insufficient unreserved cash")
        self.db.execute("INSERT INTO owner_reservations VALUES(?,?,?,?,?)", (intent_id, *value))

    def release(self, intent_id):
        self.db.execute("DELETE FROM owner_reservations WHERE intent_id=?", (intent_id,))

    def apply_fill(self, fill_id, *, strategy, pair_id, leg, symbol, side, action,
                   quantity, price, fee=0.0, slippage=0.0, collateral=None, settlement_pnl=None):
        """Book one confirmed fill exactly once, closing only the named owner's leg."""
        payload = dict(strategy=strategy, pair_id=pair_id, leg=leg, symbol=symbol, side=side,
                       action=action, quantity=quantity, price=price, fee=fee, slippage=slippage,
                       collateral=collateral, settlement_pnl=settlement_pnl)
        fingerprint = hashlib.sha256(encoded(payload).encode()).hexdigest()
        old = self.db.execute("SELECT fingerprint FROM owner_fills WHERE fill_id=?", (fill_id,)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise ValueError("fill ID reused with different contents")
            return False
        if (not fill_id or not leg or not symbol or side not in {"long", "short"}
                or action not in {"open", "close"}
                or not all(math.isfinite(v) for v in (quantity, price, fee, slippage))
                or quantity <= 0 or price <= 0 or fee < 0 or slippage < 0):
            raise ValueError("invalid fill")
        account = self.account(strategy, pair_id)
        if account is None:
            raise ValueError("fill has no allocated owner")
        existing = next((p for p in self.positions(strategy, pair_id) if p["leg"] == leg), None)
        if existing and (existing["symbol"] != symbol or existing["side"] != side):
            raise ValueError("leg already owns a different instrument/side")
        value, cost, realized = quantity * price, fee + slippage, 0.0
        if action == "open":
            locked = (value if collateral is None else float(collateral)) if side == "short" else 0.0
            if not math.isfinite(locked) or locked < 0:
                raise ValueError("invalid collateral")
            position = dict(existing or dict(quantity=0., basis=0., collateral=0.))
            position.update(quantity=position["quantity"] + quantity, basis=position["basis"] + value,
                            collateral=position["collateral"] + locked)
            cash_change = -(value if side == "long" else locked) - cost
        else:
            if not existing or quantity > existing["quantity"] * (1 + 1e-12):
                raise ValueError("cannot close quantity owned by another pair/strategy")
            ratio = min(1., quantity / existing["quantity"])
            basis, released = existing["basis"] * ratio, existing["collateral"] * ratio
            realized = value - basis if side == "long" else basis - value
            cash_change = (value if side == "long" else released + realized) - cost
            position = dict(quantity=existing["quantity"] * (1-ratio),
                            basis=existing["basis"] * (1-ratio), collateral=existing["collateral"] * (1-ratio))
        with self.atomic():
            self.db.execute("UPDATE owner_accounts SET cash=cash+?,fees=fees+?,slippage=slippage+?,"
                            "realized_gross=realized_gross+? WHERE strategy=? AND pair_id=?",
                            (cash_change, fee, slippage, realized, strategy, pair_id))
            if position["quantity"] == 0:
                self.db.execute("DELETE FROM owner_positions WHERE strategy=? AND pair_id=? AND leg=?",
                                (strategy, pair_id, leg))
            else:
                self.db.execute("INSERT OR REPLACE INTO owner_positions VALUES(?,?,?,?,?,?,?,?)",
                                (strategy, pair_id, leg, symbol, side, position["quantity"],
                                 position["basis"], position["collateral"]))
            self.db.execute("INSERT INTO owner_fills VALUES(?,?,?)", (fill_id, fingerprint, encoded(payload)))
        return True

    def mark(self, prices, strategy=None):
        """Actual fixed-quantity net/gross exposures and marked equity. No caps."""
        accounts = self.accounts(strategy)
        equity = sum(a["cash"] for a in accounts)
        exposure = {}
        for p in self.positions(strategy):
            price = prices.get(p["symbol"])
            if price is None or not math.isfinite(price) or price <= 0:
                raise ValueError(f"missing valid mark for {p['symbol']}")
            value = p["quantity"] * price
            long = p["side"] == "long"
            equity += value if long else p["collateral"] + p["basis"] - value
            e = exposure.setdefault(p["symbol"], dict(net_quantity=0., net_notional=0., gross_notional=0.))
            e["net_quantity"] += p["quantity"] * (1 if long else -1)
            e["net_notional"] += value * (1 if long else -1)
            e["gross_notional"] += value
        capital = sum(a["capital"] for a in accounts)
        return dict(capital=capital, equity=equity, net_pnl=equity-capital, exposure=exposure,
                    fees=sum(a["fees"] for a in accounts), slippage=sum(a["slippage"] for a in accounts))
