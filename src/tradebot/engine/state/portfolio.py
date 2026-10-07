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
import json
import math
from pathlib import Path
import sqlite3
import uuid

from tradebot.core import locking
from tradebot.core.symbols import to_coin, to_pair
from tradebot.engine.state.restrictions import Restrictions
from tradebot.engine.state.snapshot import normalize_exchange_snapshot, remaining_qty

MM = "mm-10m-fluctuation"
ACTIVE = {"READY", "SUBMITTING", "PENDING", "CANCELING"}
TERMINAL = {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}


class AccountBlocked(RuntimeError):
    pass


class AccountLock(locking.AccountLock):
    """Portable account lock (tradebot.core.locking) raising AccountBlocked when it is held."""

    def __init__(self, path, *, state_dir=None):
        try:
            super().__init__(path, state_dir=state_dir)
        except locking.AccountLockError as exc:
            raise AccountBlocked(str(exc)) from None


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

    def completed_all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM completed_orders")]

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
        self.blocked = None          # only for errors that invalidate the whole ledger (configuration)
        self.dirty = False           # orders sent/cancelled since the last sync
        self.read_error = None       # last failed venue read, until a read succeeds
        self.balance_stale = False   # a confirmed cancel released a reservation after the last read
        self._sync_fills = []
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
        # Resting rows count their unfilled part; a PENDING row reporting FilledQuantity == Quantity
        # (the shape shown in the Roostoo docs) is read as unfilled, like the engine snapshot does.
        free = float(wallet_of(self.balance)["USD"]["Free"])
        pending = 0.0
        for row in self.pending:
            if row["Side"] in {"BUY", "SHORT_OPEN"}:
                pending += remaining_qty(row) * float(row["Price"])
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
        pending_fees = sum(remaining_qty(r) * float(r["Price"]) * self.fees.spot_maker
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
            "restrictions": {}, "adjustments": {"cash_rounding_usd": 0.0, "count": 0},
        }
        for row in self.pending:
            o = self._record("rxm", to_coin(row["Pair"]), row["Side"], float(row["Quantity"]), float(row["Price"]))
            filled = float(row.get("FilledQuantity", 0)) if remaining_qty(row) < float(row["Quantity"]) else 0.0
            o.update(order_id=str(row["OrderID"]), status="PENDING", filled=filled,
                     value=filled*float(row.get("FilledAverPrice") or row["Price"]),
                     fee=float(row.get("CommissionChargeValue", 0)), row=row)
            self.orders[o["intent_id"]] = o
        self.store.save("capital_allocated", {"mm": capital, "rxm": equity-capital})

    # ------------------------------------------------------------------ synchronization
    def sync(self):
        """Read the venue, apply fills, reconcile and update scoped restrictions. Never stops trading
        globally on a reconciliation finding: only the affected strategy, coin, pair or cash use is
        restricted (engine/state/restrictions.py). Raises only before the ledger exists (startup) or
        on a configuration error that makes the whole ledger invalid. Returns False if a venue read
        failed (new orders are refused until a read succeeds; `read_error` says why)."""
        findings = self._sync_once()
        if findings is None:                       # a read failed: retried next loop
            return False
        if findings and self.state is not None:
            # A fill landing between two non-atomic venue reads looks like a mismatch: read once more
            # (fills are cumulative, so re-applying is idempotent) before restricting anything.
            findings = self._sync_once()
            if findings is None:
                return False
        self.read_error = None
        self._commit(findings)
        return True

    def _restrictions(self):
        return Restrictions(self.state, clear_after=self.config.restriction_clear_syncs, clock=self.clock)

    def refusal(self, strategy, coin, side, *, full_close=False):
        """Why a NEW order may not be sent now (None = allowed)."""
        if self.blocked:
            return self.blocked
        if self.state is None:
            return "ledger not initialized"
        if self.paused(strategy):
            return "strategy paused"
        return self._restrictions().refusal(strategy, coin, side, full_close=full_close)

    def _check_config(self):
        if self.state is None:
            return
        if self.state["capital_fraction"] != self.config.capital.mm_fraction:
            self.blocked = "capital allocation changed: restart preserves budgets; explicit migration required"
        elif self.state["run_mode"] != ("dry-run" if self.dry_run else "execute"):
            self.blocked = "use separate state directories for dry-run and execution"
        else:
            self.blocked = None
        if self.blocked:
            raise AccountBlocked(self.blocked)

    def _read_failed(self, exc):
        if self.state is None:
            raise AccountBlocked(f"account read failed during startup: {exc}") from exc
        r = self._restrictions()
        r.begin()
        r.flag("reads", f"account read failed: {exc}", "transient")
        self.read_error = str(exc)
        self.store.save("read_failed", {"reason": str(exc)})
        return None

    def _sync_once(self):
        """One read of the venue. Returns the reconciliation findings [(scope, reason, severity)],
        or None if a read failed. Applied fills are committed as they are read."""
        self._check_config()
        findings = []
        try:
            if self.clock.monotonic()-self.rules_read_at >= 3600:
                self.rules = self.port.get_exchange_info()["TradePairs"]
                self.rules_read_at = self.clock.monotonic()
            self.pending = order_rows(self.port.list_open_orders())
        except Exception as exc:
            return self._read_failed(exc)
        if self.state is not None:
            findings += self._process_orders()
        try:
            # Validate before keeping: a partial read must never replace the last good view
            balance = self.port.get_balance()
            shorts = self.port.get_short_positions()
            tickers = self.port.get_ticker()
            wallet_of(balance)
            if shorts.get("Success") is False or "Positions" not in shorts or tickers.get("Success") is False or "Data" not in tickers:
                raise AccountBlocked("incomplete account read")
        except Exception as exc:
            return self._read_failed(exc)
        self.balance, self.shorts, self.tickers = balance, shorts, tickers
        self.balance_stale = False
        if self.state is None:
            self._bootstrap()
        findings += self._recover_shorts()
        findings += self._reconcile()
        return findings

    def _commit(self, findings):
        r = self._restrictions()
        r.begin()
        for scope, reason, severity in findings:
            r.flag(scope, reason, severity)
        lifted = r.end()
        self.dirty = False
        self.read_error = None
        self._sync_fills = []        # fills since the last committed sync (incl. cancels and re-reads)
        payload = {"restrictions": r.report()}
        if lifted:
            payload["lifted"] = lifted
        self.store.save("reconciled", payload)

    def _process_orders(self):
        findings = []
        lookup = {str(r["OrderID"]): r for r in self.pending}
        findings += self._recover_submissions(lookup)
        known = {o.get("order_id") for o in self.orders.values()}
        for key, row in lookup.items():
            if key not in known:
                findings.append((f"coin:{to_coin(row.get('Pair', ''))}",
                                 f"unowned pending order {key} ({row.get('Side')})", "material"))
        for o in self.active():
            if o["status"] == "READY":
                # A crash before send is known safe to abandon, never replay.
                o["status"] = "REJECTED"
                continue
            if o.get("order_id") is None:
                continue                             # unresolved submission: _recover_submissions flags it
            row = lookup.get(o["order_id"])
            if row is None:
                # Left the resting list: its final state decides fills. Until the venue shows it,
                # keep the reservation and restrict only this coin (a delayed update, not a halt).
                try:
                    found = order_rows(self.port.query_order(order_id=o["order_id"]))
                except Exception as exc:
                    findings.append((f"coin:{o['coin']}", f"order {o['order_id']} final state unreadable: {exc}", "delayed"))
                    continue
                if len(found) != 1:
                    findings.append((f"coin:{o['coin']}", f"order {o['order_id']} not found in venue history", "delayed"))
                    continue
                row = found[0]
            problem = self._apply(o, row)
            if problem:
                findings.append((f"coin:{o['coin']}", f"order {o['order_id']}: {problem}", "material"))
        if self._cancel_retries_due():
            self.dirty = True
        return findings

    def _cancel_retries_due(self):
        now = self.clock.now().timestamp()
        return any(o["status"] == "CANCELING" and now - o.get("cancel_requested_at", now) >= self.config.cancel_retry_seconds
                   for o in self.active())

    def _history_since(self, since_ts):
        """Order history back to `since_ts` (or 5 pages), and whether it is provably complete.

        The venue's page order is not documented, so completeness never assumes it: the history is
        complete at a short (last) page, or early only when a page is newest-first and already
        reaches back past `since_ts`. Anything else is incomplete, and an unmatched intent then stays
        unresolved instead of being declared never executed."""
        rows, offset, page = [], 0, 100
        for _ in range(5):
            batch = order_rows(self.port.query_order(offset=offset, limit=page))
            rows += batch
            if len(batch) < page:
                return rows, True
            stamps = [float(r.get("CreateTimestamp", 0)) / 1000 for r in batch]
            if all(x >= y for x, y in zip(stamps, stamps[1:])) and stamps[-1] < since_ts:
                return rows, True
            offset += page
        return rows, False

    def _recover_submissions(self, lookup):
        """Lost spot responses: match the intent to exactly one venue order using authoritative history.
        Never resubmits. No match once the evidence window has passed = never executed."""
        unresolved = [o for o in self.active() if o["status"] == "SUBMITTING" and not o.get("order_id")
                      and o["side"] in {"BUY", "SELL"}]
        if not unresolved:
            return []
        window = self.config.evidence_window_seconds
        now = self.clock.now().timestamp()
        try:
            history, complete = self._history_since(min(o["submitted_at"] for o in unresolved) - window)
        except Exception as exc:
            return [(f"coin:{o['coin']}", f"unresolved submission {o['intent_id']}: history unreadable ({exc})", "delayed")
                    for o in unresolved]
        rows = {str(r.get("OrderID")): r for r in [*history, *lookup.values()]}
        owned = {o.get("order_id") for o in self.orders.values()} | {o.get("order_id") for o in self.store.completed_all()}
        findings = []
        for o in unresolved:
            candidates = [r for k, r in rows.items() if k not in owned and r.get("Pair") == to_pair(o["coin"])
                          and r.get("Side") == o["side"] and r.get("Type", "LIMIT") == "LIMIT"
                          and math.isclose(float(r.get("Price", 0)), o["price"], rel_tol=1e-10)
                          and math.isclose(float(r.get("Quantity", 0)), o["quantity"], rel_tol=1e-10)
                          and abs(float(r.get("CreateTimestamp", 0))/1000-o["submitted_at"]) <= window]
            if len(candidates) == 1:
                o["order_id"] = str(candidates[0]["OrderID"])
                owned.add(o["order_id"])
                self._apply(o, candidates[0])
                self.store.save("submission_recovered", {"intent": o["intent_id"], "order": o["order_id"]})
            elif not candidates and complete and now - o["submitted_at"] > 2 * window:
                o["status"] = "REJECTED"         # complete history has no such order: never executed
                self.store.save("submission_not_executed", {"intent": o["intent_id"]})
            else:
                findings.append((f"coin:{o['coin']}", f"unresolved submission {o['intent_id']}: "
                                 f"{len(candidates)} matching venue orders", "delayed"))
        return findings

    def _recover_shorts(self):
        """Lost short responses, resolved from the position and resting-order evidence (Roostoo has no
        client order id). Never resubmits; ambiguous evidence restricts only that pair."""
        unresolved = [o for o in self.active() if o["status"] == "SUBMITTING" and not o.get("order_id")
                      and o["side"] in {"SHORT_OPEN", "SHORT_CLOSE"}]
        findings = []
        now = self.clock.now().timestamp()
        positions = {p["Pair"]: p for p in self.shorts.get("Positions", [])}
        for o in unresolved:
            pair = to_pair(o["coin"])
            before = float(o.get("before_short_qty", self.state["short_quantity"].get(pair, 0)))
            actual = float(positions.get(pair, {}).get("ShortQty", 0))
            moved = actual - before
            expected = o["quantity"] if o["side"] == "SHORT_OPEN" else -o["quantity"]
            resting = [r for r in self.pending if r.get("Side") == "SHORT_OPEN" and r.get("Pair") == pair
                       and str(r.get("OrderID")) not in {x.get("order_id") for x in self.orders.values()}]
            if o["side"] == "SHORT_OPEN" and o.get("limit") and len(resting) == 1 and abs(moved) < 1e-12:
                o.update(order_id=str(resting[0]["OrderID"]), status="PENDING", row=deepcopy(resting[0]))
                fee = o["collateral"] * self.fees.short_open
                self.state["expected_cash_assets"] -= fee
                self.state["rxm_fees"] += fee
                self.store.save("short_recovered", {"intent": o["intent_id"], "order": o["order_id"], "fee_estimated": fee})
            elif expected and abs(moved - expected) <= 0.02 * abs(expected) + 1e-9 and not resting:
                self._apply_recovered_short(o, pair, positions.get(pair, {}), moved)
            elif abs(moved) < 1e-12 and not resting and now - o["submitted_at"] > 2 * self.config.evidence_window_seconds:
                o["status"] = "REJECTED"
                self.store.save("short_not_executed", {"intent": o["intent_id"]})
            else:
                findings.append((f"short:{pair}", f"unresolved {o['side']} {o['intent_id']}: position moved "
                                 f"{moved:+.8g}, expected {expected:+.8g}", "delayed"))
        return findings

    def _apply_recovered_short(self, o, pair, position, moved):
        """Position evidence shows the lost short request executed: book it from the venue's numbers."""
        self.state["short_quantity"][pair] = float(position.get("ShortQty", 0))
        if o["side"] == "SHORT_OPEN":
            fee = o["collateral"] * self.fees.short_open
            self.state["short_basis"][pair] = self.state["short_basis"].get(pair, 0) + moved * float(position.get("EntryPrice", 0) or 0)
            self.state["expected_cash_assets"] -= fee
            self.state["rxm_fees"] += fee
        else:
            ratio = min(1.0, -moved / max(float(o.get("before_short_qty") or 0), 1e-12))
            self.state["short_basis"][pair] = self.state["short_basis"].get(pair, 0) * max(0.0, 1 - ratio)
            # Realized P&L and fee are not known without the response: re-baseline RXM's residual cash
            # to the venue in this sync (MM books are untouched; RXM cash is the account remainder).
            # Kept in the durable state: the FILLED intent leaves the active set at the next save.
            self.state.setdefault("rebaseline_for", []).append(o["intent_id"])
        o["status"] = "FILLED"
        self.store.save("short_recovered", {"intent": o["intent_id"], "side": o["side"], "moved": moved})

    def _reconcile(self):
        findings = []
        wallet = wallet_of(self.balance)
        expected = dict(self.state["rxm_quantity"])
        for c, b in self.state["mm"].items():
            expected[c] = expected.get(c, 0) + b["quantity"]
            if b["cash"] < -1e-7 or b["quantity"] < -1e-7:
                findings.append((f"strategy:{MM}", f"MM book {c} negative (cash {b['cash']:.8f}, qty {b['quantity']!r})", "material"))
        for c in set(expected) | (set(wallet)-{"USD"}):
            actual, ledger = total_quantity(wallet, c), expected.get(c, 0)
            # Adding/subtracting billion-unit meme-coin lots can leave a few
            # micro-units when a book returns to flat. Permit at most 1e-8 USD of additional
            # roundoff. Coin quantities do not round on the venue: anything else is a real gap.
            price = float(self.tickers.get("Data", {}).get(to_pair(c), {}).get("LastPrice", 0))
            negligible = math.isfinite(price) and price > 0 and abs(actual-ledger)*price <= 1e-8
            if not math.isclose(actual, ledger, rel_tol=1e-9, abs_tol=1e-7) and not negligible:
                findings.append((f"coin:{c}", f"inventory mismatch: actual {actual!r}, ledger {ledger!r}", "material"))
        actual_shorts = {p["Pair"]: float(p["ShortQty"]) for p in self.shorts["Positions"]}
        unresolved_pairs = {to_pair(o["coin"]) for o in self.active() if o["status"] == "SUBMITTING"
                            and o["side"] in {"SHORT_OPEN", "SHORT_CLOSE"}}
        for pair in set(actual_shorts) | set(self.state["short_quantity"]):
            if pair in unresolved_pairs:
                continue                                  # already restricted by _recover_shorts
            if not math.isclose(actual_shorts.get(pair, 0), self.state["short_quantity"].get(pair, 0), rel_tol=1e-8, abs_tol=1e-7):
                findings.append((f"short:{pair}", f"short mismatch: actual {actual_shorts.get(pair, 0)!r}, "
                                 f"ledger {self.state['short_quantity'].get(pair, 0)!r}", "material"))
        findings += self._reconcile_cash()
        snapshot = normalize_exchange_snapshot(self.balance, self.shorts, self.tickers, self.pending)
        if snapshot["lock_unexplained_usd"] > max(self.config.cash_tolerance_floor_usd, 0.01):
            findings.append(("cash:account", f"unexplained USD lock {snapshot['lock_unexplained_usd']:.4f}", "delayed"))
        if self.rxm_free_cash() < -0.01:
            findings.append(("cash:rxm", f"RXM cash exhausted ({self.rxm_free_cash():.2f})", "material"))
        return findings

    def _reconcile_cash(self):
        cash = self._physical_cash_assets()
        diff = cash - self.state["expected_cash_assets"]
        if self.state.get("rebaseline_for"):
            self.state["expected_cash_assets"] = cash
            self.state.pop("cash_gap_scope", None)
            self.store.save("cash_rebaselined_after_short_recovery", {"diff": diff, "intents": self.state.pop("rebaseline_for")})
            return []
        if abs(diff) <= 1e-9:
            self.state.pop("cash_gap_scope", None)
            return []
        notional = sum(n for _, _, n in self._sync_fills)
        tolerance = max(self.config.cash_tolerance_floor_usd, self.config.cash_tolerance_bps / 1e4 * notional)
        owners = {s for s, _, _ in self._sync_fills}
        if abs(diff) <= tolerance:
            self._absorb_cash(diff, notional)
            self.state.pop("cash_gap_scope", None)
            return []
        if len(owners) == 1:
            scope = f"cash:{next(iter(owners))}"
        elif not owners:
            # The same gap seen again with no new fills keeps the owner it was traced to
            scope = self.state.get("cash_gap_scope", "cash:account")
        else:
            scope = "cash:account"
        self.state["cash_gap_scope"] = scope
        return [(scope, f"cash mismatch {diff:+.4f} USD (tolerance {tolerance:.4f}, fills {notional:,.2f})", "material")]

    def _absorb_cash(self, diff, notional):
        """Fee/proceeds rounding inside tolerance: book it to the strategies whose fills caused it."""
        self.state["expected_cash_assets"] += diff
        adj = self.state.setdefault("adjustments", {"cash_rounding_usd": 0.0, "count": 0})
        adj["cash_rounding_usd"] += diff
        adj["count"] += 1
        fills = self._sync_fills or [("rxm", None, 1.0)]
        total = sum(n for _, _, n in fills) or 1.0
        for strategy, coin, n in fills:
            share = diff * n / total
            if strategy == MM and coin in self.state["mm"]:
                b = self.owner(coin)
                b["cash"] += share
                b["fees"] -= share
            else:
                self.state["rxm_fees"] -= share
        self.store.save("cash_rounding_adjustment", {"diff": diff, "fill_notional": notional,
                                                     "owners": sorted({s for s, _, _ in fills})})

    def _record(self, strategy, coin, side, quantity, price):
        return dict(intent_id=uuid.uuid4().hex, strategy=strategy, coin=coin, side=side,
                    quantity=quantity, price=price, filled=0.0, value=0.0, fee=0.0,
                    status="READY", order_id=None, submitted_at=self.clock.now().timestamp())

    def _apply(self, o, row):
        """Apply a venue order row (cumulative fills). Returns a problem description instead of
        applying when the row cannot be trusted; the caller restricts only that order's coin."""
        status = str(row.get("Status", "")).upper()
        if status not in TERMINAL | {"PENDING"}:
            return f"unsupported order status {status}"
        try:
            quantity = float(row["Quantity"])
            filled = float(row.get("FilledQuantity", 0))
            price = float(row.get("FilledAverPrice") or row.get("Price", 0))
        except (KeyError, TypeError, ValueError):
            return "malformed order row"
        # Some simulation adapters omit cumulative quantity on a terminal fill.
        if status == "FILLED" and filled == 0:
            filled = quantity
        if status == "PENDING" and filled >= quantity:
            # The Roostoo docs show resting rows with FilledQuantity == Quantity: read as no new fill
            # (as the engine snapshot does). A real partial fill always reports 0 < filled < quantity.
            if not o.get("pending_shape_noted"):
                o["pending_shape_noted"] = True
                self.store.save("pending_row_full_filled_quantity", {"order": o.get("order_id")})
            filled = o["filled"]
        if filled < o["filled"]-1e-8 or filled > o["quantity"]*(1+1e-9):
            return "non-monotonic cumulative fill"
        if not all(math.isfinite(v) for v in (quantity, filled, price)) or quantity <= 0 or filled < 0 or price <= 0:
            return "invalid numerical fill data"
        if o["side"] in {"BUY", "SELL"} and float(row.get("CommissionChargeValue", 0) or 0) \
                and row.get("CommissionCoin", "USD") != "USD":
            return "non-USD spot commission requires explicit accounting support"
        value = filled * price
        delta, value_delta = filled-o["filled"], value-o["value"]
        fee_delta = 0.0
        if o["side"] in {"BUY", "SELL"}:
            rate = self.fees.spot_maker if row.get("Type", "LIMIT") == "LIMIT" else self.fees.spot_taker
            reported = row.get("CommissionChargeValue")
            fee = float(reported) if reported not in (None, "") else value*rate
            if reported in (None, "") and delta:
                o["fee_estimated"] = True        # replaced by the venue's figure when a later row has it
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
            if abs(value_delta) > 0:
                self._sync_fills.append((o["strategy"], o["coin"], abs(value_delta)))
        elif o["side"] == "SHORT_OPEN":
            pair = to_pair(o["coin"])
            self.state["short_quantity"][pair] = self.state["short_quantity"].get(pair, 0) + delta
            self.state["short_basis"][pair] = self.state["short_basis"].get(pair, 0) + value_delta
            if status in {"CANCELED", "CANCELLED", "REJECTED"} and o["status"] not in TERMINAL:
                refund = (o["quantity"]-filled)*o["price"]*self.fees.short_open
                self.state["expected_cash_assets"] += refund
                self.state["rxm_fees"] -= refund
        # A cancel stays pending until the venue shows a terminal state: keep its reservation
        new_status = "CANCELING" if status == "PENDING" and o["status"] == "CANCELING" else status
        o.update(filled=filled, value=value, status=new_status, row=deepcopy(row))
        if delta or value_delta:
            self.store.save("fill", {"strategy": o["strategy"], "coin": o["coin"], "side": o["side"],
                                     "quantity": delta, "value": value_delta, "fee_delta": fee_delta,
                                     "execution_timestamp_ms": row.get("FinishTimestamp"), "cumulative_fee": o["fee"],
                                     "fee_estimated": bool(o.get("fee_estimated")), "order_id": o["order_id"]})
        return None

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
            reason = self.refusal(MM, q.symbol, q.side)
            if reason:
                self.store.save("quote_restricted", {"quote": q.model_dump(), "reason": reason})
                continue
            if any(o["coin"] == q.symbol and o["side"] == q.side for o in self.active(MM)):
                # The previous quote on this side is not confirmed cancelled yet: no duplicate
                self.store.save("replacement_deferred", {"quote": q.model_dump()})
                continue
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
        reason = self._restrictions().refusal(o["strategy"], o["coin"], o["side"]) if self.state else None
        if reason:
            o["status"] = "REJECTED"
            self.store.save("restricted_skip", {"intent": o["intent_id"], "reason": reason})
            return {"Success": False, "ErrMsg": reason}
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
        if self.paused(o["strategy"]):
            o["status"] = "REJECTED"
            self.store.save("paused_or_expired", o)
            return {"Success": False, "ErrMsg": "strategy paused"}
        if o["strategy"] == MM and expiry is not None and self.clock.now().timestamp() >= expiry:
            o["status"] = "REJECTED"           # the venue answered after the quote's deadline
            self.store.save("paused_or_expired", o)
            return {"Success": False, "ErrMsg": "quote expired", "Expired": True}
        if self._crosses(o["coin"], o["side"], o["price"], o):
            o["status"] = "REJECTED"
            self.store.save("self_cross_skipped", o)
            return {"Success": False, "ErrMsg": "account self-cross"}
        o.update(status="SUBMITTING", submitted_at=self.clock.now().timestamp())
        self.store.save("submitting", o)       # intent is durable before the request leaves
        self.dirty = True
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
            # Unknown outcome: keep the intent SUBMITTING (its reservation stays) and restrict only
            # this coin until history proves it executed or not. Never resubmit blindly.
            reason = f"uncertain submission {o['intent_id']}: {exc}"
            r = self._restrictions()
            r.flag(f"coin:{o['coin']}", reason, "delayed")
            self.store.save("submission_uncertain", {"intent": o["intent_id"], "reason": reason})
            raise AccountBlocked(reason) from exc

    def cancel(self, strategy, order_id):
        """Request cancellation. The order stays CANCELING (reservation kept, no replacement on that
        side) until the venue shows a terminal state; an unconfirmed cancel is retried by sync."""
        candidates = list(self.orders.values()) + self.store.completed(strategy, order_id)
        o = next((o for o in candidates if o.get("order_id") == str(order_id) and o["strategy"] == strategy), None)
        if o is None:
            return {"Success": False, "ErrMsg": "order does not belong to strategy"}
        if self.dry_run:
            return {"Success": False, "ErrMsg": "dry-run: cancellation transport disabled"}
        if o["status"] in TERMINAL:
            return {"Success": True}
        o["status"] = "CANCELING"
        o["cancel_requested_at"] = self.clock.now().timestamp()
        self.store.save("canceling", {"order_id": str(order_id)})
        self.dirty = True
        try:
            response = self.port.cancel_order(order_id=order_id)
        except Exception as exc:
            self.store.save("cancel_uncertain", {"order_id": str(order_id), "reason": str(exc)})
            return {"Success": False, "Pending": True, "ErrMsg": f"cancel not acknowledged: {exc}"}
        # The ACK alone never releases a reservation. Query cumulative final fills.
        try:
            rows = order_rows(self.port.query_order(order_id=order_id))
        except Exception as exc:
            rows = []
            self.store.save("cancel_unconfirmed", {"order_id": str(order_id), "reason": str(exc)})
        if len(rows) == 1:
            problem = self._apply(o, rows[0])
            if problem:
                self._restrictions().flag(f"coin:{o['coin']}", f"order {order_id}: {problem}", "material")
        if o["status"] in ACTIVE:
            o["status"] = "CANCELING"
            self.store.save("cancel_pending", {"order_id": str(order_id)})
            return {"Success": True, "Pending": True}
        self.balance_stale = True         # the venue released the reservation; the cached Lock still has it
        self.store.save("cancel_result", response)
        return response

    def refresh_balance_if_stale(self):
        """Re-read only the wallet (1 request) when a confirmed cancel released a reservation since the
        last sync. Without it the cached Lock would still hold the released USD and the RXM snapshot's
        Lock attribution would count it as short collateral (understated equity). Lazy, so MM's cancel
        batch (always followed by a full sync) costs nothing extra."""
        if not self.balance_stale:
            return
        try:
            balance = self.port.get_balance()
            wallet_of(balance)
        except Exception as exc:
            self.store.save("balance_refresh_failed", {"reason": str(exc)})
            return
        self.balance, self.balance_stale = balance, False

    def report(self):
        out = {"initial_equity": self.state["initial_equity"], "rxm_capital": self.state["rxm_capital"],
               "blocked": self.blocked, "restrictions": self._restrictions().report(),
               "adjustments": self.state.get("adjustments", {}),
               "quote_stats": self.state.get("quote_stats", {}), "mm": {}}
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
        self.account.refresh_balance_if_stale()
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
        result = self.account.cancel(self.strategy, order_id)   # the loop's next sync confirms a pending one
        # RXM sizes its next orders and shorts from the wallet right after cancelling stale orders
        self.account.refresh_balance_if_stale()
        return result

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        a, coin = self.account, to_coin(pair_or_coin)
        if self.strategy != "rxm":
            return {"Success": False, "ErrMsg": "MM requires engine quote reservation"}
        if a.blocked or price is None or (order_type or "LIMIT") != "LIMIT" or side not in {"BUY", "SELL"}:
            return {"Success": False, "ErrMsg": "account blocked or unsupported spot order"}
        reason = a.refusal("rxm", coin, side)
        if reason:
            return {"Success": False, "ErrMsg": reason}
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
        return a.submit(o)       # cached wallet is adjusted; the loop's next sync reconciles

    def _short_uncertain(self, o, reason):
        """Unknown outcome of a short request: keep the intent SUBMITTING, restrict only this pair.
        The next syncs resolve it from position/resting-order evidence (AccountCoordinator._recover_shorts)."""
        a = self.account
        pair = to_pair(o["coin"])
        a._restrictions().flag(f"short:{pair}", f"uncertain {o['side']} {o['intent_id']}: {reason}", "delayed")
        a.store.save("short_uncertain", {"intent": o["intent_id"], "side": o["side"], "reason": reason})
        return AccountBlocked(f"uncertain {o['side'].lower()} on {pair}: {reason}")

    def open_short(self, pair_or_coin, collateral, price=None):
        a = self.account
        if self.strategy != "rxm" or a.dry_run or a.blocked or a.paused("rxm"):
            return {"Success": False, "ErrMsg": "short mutation prohibited"}
        collateral = float(collateral)
        coin = to_coin(pair_or_coin)
        pair = to_pair(coin)
        reason = a.refusal("rxm", coin, "SHORT_OPEN")
        if reason:
            return {"Success": False, "ErrMsg": reason}
        if collateral <= 0 or not math.isfinite(collateral) or collateral*(1+a.fees.short_open) > a.rxm_free_cash():
            return {"Success": False, "ErrMsg": "RXM short exceeds available cash"}
        if a._crosses(coin, "SHORT_OPEN", float(price) if price is not None else None):
            return {"Success": False, "ErrMsg": "account self-cross"}
        if price is not None:
            price = canonical(price, int(a.rules[pair]["PricePrecision"]))
        px = float(price or a.tickers["Data"][pair]["LastPrice"])
        o = a._record("rxm", coin, "SHORT_OPEN", collateral/px, px)
        o.update(collateral=collateral, limit=price is not None,
                 before_short_qty=a.state["short_quantity"].get(pair, 0.0))
        a.orders[o["intent_id"]] = o
        o["status"] = "SUBMITTING"
        a.store.save("short_submitting", o)     # durable before the request leaves
        a.dirty = True
        try:
            r = a.port.open_short(coin, collateral, price=price)
        except Exception as exc:
            raise self._short_uncertain(o, str(exc)) from exc
        if r.get("Success") is False:
            o["status"] = "REJECTED"
            a.store.save("short_result", r)
            return r
        try:
            fee = float(r["OpenFee"])
            if price is None:
                # Position ID is distinct from a resting order ID.
                before = next((p for p in a.shorts["Positions"] if p["Pair"] == pair), {})
                previous_entry_value = float(before.get("ShortQty", 0))*float(before.get("EntryPrice", 0))
                added_entry_value = float(r["ShortQty"])*float(r["EntryPrice"])-previous_entry_value
                new_qty = float(r["ShortQty"])
            else:
                order_id = str(r["ID"])
        except (KeyError, TypeError, ValueError) as exc:
            # Accepted but not bookable from the response: resolve from the venue's position evidence
            raise self._short_uncertain(o, f"accepted with an incomplete response ({exc})") from exc
        a.state["expected_cash_assets"] -= fee
        a.state["rxm_fees"] += fee
        if price is None:
            a.state["short_basis"][pair] = a.state["short_basis"].get(pair, 0)+added_entry_value
            a.state["short_quantity"][pair] = new_qty
            o["status"] = "FILLED"
        else:
            o.update(order_id=order_id, status="PENDING")
        # Keep the cached wallet conservative until the next sync: collateral and fee have left Free
        wallet = wallet_of(a.balance)
        wallet["USD"]["Free"] = float(wallet["USD"]["Free"]) - collateral - fee
        a.store.save("short_result", r)
        return r

    def close_short(self, pair_or_coin, close_qty=None, close_pct=None):
        a = self.account
        if self.strategy != "rxm" or a.dry_run or a.blocked or a.paused("rxm"):
            return {"Success": False, "ErrMsg": "short mutation prohibited"}
        coin, pair = to_coin(pair_or_coin), to_pair(pair_or_coin)
        full = close_pct is not None and float(close_pct) >= 100
        reason = a.refusal("rxm", coin, "SHORT_CLOSE", full_close=full)
        if reason:
            return {"Success": False, "ErrMsg": reason}
        if a._crosses(coin, "SHORT_CLOSE", None):
            return {"Success": False, "ErrMsg": "account self-cross"}
        before = a.state["short_quantity"].get(pair, 0.0)
        expected = float(close_qty) if close_qty is not None else before * float(close_pct or 0) / 100
        o = a._record("rxm", coin, "SHORT_CLOSE", expected, 0)
        o.update(before_short_qty=before)
        a.orders[o["intent_id"]] = o
        o["status"] = "SUBMITTING"
        a.store.save("cover_submitting", o)
        a.dirty = True
        try:
            r = a.port.close_short(coin, close_qty=close_qty, close_pct=close_pct)
        except Exception as exc:
            raise self._short_uncertain(o, str(exc)) from exc
        if r.get("Success") is False:
            o["status"] = "REJECTED"
            a.store.save("cover_result", r)
            return r
        try:
            pnl, fee, closed = float(r["RealizedPNL"]), float(r["CloseFee"]), float(r["ClosedQty"])
        except (KeyError, TypeError, ValueError) as exc:
            raise self._short_uncertain(o, f"accepted with an incomplete response ({exc})") from exc
        a.state["expected_cash_assets"] += pnl - fee
        a.state["rxm_fees"] += fee
        ratio = closed / before if before else 1.0
        a.state["short_basis"][pair] = a.state["short_basis"].get(pair, 0) * max(0.0, 1-ratio)
        a.state["short_quantity"][pair] = before - closed
        o["status"] = "FILLED"
        a.store.save("cover_result", r)
        return r

    def get_pending_count(self):
        return {"Success": True, "TotalPending": len(self.account.active(self.strategy))}

    def requests_last_minute(self):
        return self.account.port.requests_last_minute()

    def get_server_time(self):
        return self.account.port.get_server_time()
