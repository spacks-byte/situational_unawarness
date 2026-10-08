"""Durable pair decisions and fill attribution. This module never sends orders.

The shared account runs it in observation mode. The explicit reference-fill
method is solely for offline backtests; it models the handoff's prices
and costs and must not be wired to the live account loop.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

from tradebot.engine.state.ownership import OwnershipLedger, encoded
from tradebot.strategy.library.cointegration import CointegrationPairs, STEP, common_closes


class PairRuntime:
    def __init__(self, config, path, *, clock, fetch=None):
        self.config, self.clock, self.fetch = config, clock, fetch
        self.strategy = CointegrationPairs(config)
        self.name = self.strategy.name
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.ledger = OwnershipLedger(self.db)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pair_runtime (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT, state TEXT);
            CREATE TABLE IF NOT EXISTS pair_decisions (
                candle TEXT NOT NULL, pair_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(candle,pair_id));
            CREATE TABLE IF NOT EXISTS pair_intents (
                intent_id TEXT PRIMARY KEY, pair_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pair_trades (exit_intent TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        row = self.db.execute("SELECT config,state FROM pair_runtime WHERE id=1").fetchone()
        self.config_json = encoded(config.model_dump(mode="json"))
        if row and row[0] != self.config_json:
            self.db.close()
            raise ValueError("saved pair configuration differs; use an explicit new cycle/database")
        self.state = json.loads(row[1]) if row else None

    def close(self):
        self.db.close()

    def _save(self, state):
        self.db.execute("INSERT OR REPLACE INTO pair_runtime VALUES(1,?,?)", (self.config_json, encoded(state)))

    def initialize(self, data):
        if self.state is not None:
            return
        if self.config.cycle_start is None:
            raise ValueError("choose an explicit cointegration cycle_start")
        start = pd.Timestamp(self.config.cycle_start)
        if pd.Timestamp(self.clock.now()) < start:
            raise ValueError("cycle training boundary is in the future")
        models = self.strategy.fit(data, start)
        cycle_id = hashlib.sha256(self.config_json.encode()).hexdigest()[:20]
        state = dict(cycle_id=cycle_id, last_candle=(start-STEP).isoformat(), models=models,
                     positions={}, pending={}, finished=False)
        with self.ledger.atomic():
            for pair, model in models.items():
                self.ledger.allocate(self.name, pair, model["budget"])
            self._save(state)
        self.state = state

    def pending(self):
        return [dict(json.loads(p), intent_id=i, status=s) for i, s, p in self.db.execute(
            "SELECT intent_id,status,payload FROM pair_intents WHERE status='PENDING' ORDER BY intent_id")]

    def process_close(self, candle, closes):
        """Consume a single synchronized completed candle, atomically with intents."""
        if self.state is None:
            raise ValueError("pair runtime is not initialized")
        candle = pd.Timestamp(candle)
        if candle.tz is None or candle != candle.floor(STEP):
            raise ValueError("invalid candle boundary")
        previous = pd.Timestamp(self.state["last_candle"])
        if candle <= previous:
            return []
        if self.state["finished"]:
            return []
        if candle != previous+STEP:
            raise ValueError("missing pair candle; repair history before advancing")
        closed_at = candle+STEP
        if closed_at > pd.Timestamp(self.clock.now()):
            raise ValueError("unfinished candle")
        if set(closes) != set(self.config.assets) or any(not np.isfinite(p) or p <= 0 for p in closes.values()):
            raise ValueError("all configured assets require a positive finite completed close")
        terminal = self.config.cycle_end is not None and closed_at >= pd.Timestamp(self.config.cycle_end)
        state, observations = deepcopy(self.state), []
        with self.ledger.atomic():
            for spec in self.config.pairs:
                pair, model = spec.pair, state["models"][spec.pair]
                position = state["positions"].get(pair, {})
                obs = self.strategy.decide(spec, model, closes, direction=position.get("direction", 0),
                    entered_at=position.get("entry_time"), closed_at=closed_at, terminal=terminal)
                model["history"] = (model["history"]+[obs["spread"]])[-960:]
                obs.update(pair=pair, candle=candle.isoformat(), closed_at=closed_at.isoformat())
                # Unacknowledged instructions remain pending across candles/restarts.
                # An order acknowledgement is never inferred from the next bar.
                if pair in state["pending"]:
                    obs["blocked_by_intent"] = state["pending"][pair]
                elif obs["target"] != position.get("direction", 0):
                    intent_id = f"{state['cycle_id']}:{pair}:{closed_at.isoformat()}"
                    payload = dict(pair=pair, target=obs["target"], reason=obs["reason"],
                                   signal_time=closed_at.isoformat(), signal_z=obs["z"],
                                   budget=model["budget"], completed_legs=[], order_type="MARKET")
                    self.db.execute("INSERT INTO pair_intents VALUES(?,?,?,?)",
                                    (intent_id, pair, "PENDING", encoded(payload)))
                    self.ledger.reserve(intent_id, self.name, pair,
                        model["budget"] if obs["target"] else 0., payload, enforce_cash=False)
                    state["pending"][pair] = intent_id
                    obs["intent_id"] = intent_id
                self.db.execute("INSERT INTO pair_decisions VALUES(?,?,?)", (candle.isoformat(), pair, encoded(obs)))
                observations.append(obs)
            state["last_candle"], state["finished"] = candle.isoformat(), terminal
            self._save(state)
        self.state = state
        return observations

    def record_fill(self, intent_id, leg, *, fill_id, quantity, price, timestamp, fee=0., slippage=0.):
        """Receive confirmed, whole-leg executions; no exchange requests here."""
        row = self.db.execute("SELECT pair_id,status,payload FROM pair_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if row is None or leg not in {"A", "B"}:
            raise ValueError("unknown intent or leg")
        pair, status, payload = row[0], row[1], json.loads(row[2])
        state = deepcopy(self.state)
        target, symbol = payload["target"], pair.split("-")[0 if leg == "A" else 1]
        opening = target != 0
        if opening:
            side = "long" if (target == 1) == (leg == "A") else "short"
        else:
            p = next((p for p in self.ledger.positions(self.name, pair) if p["leg"] == leg), None)
            # A duplicate close can arrive after the position has been removed.
            original = state["positions"].get(pair, {}).get("legs", {}).get(leg)
            if p is None and original is None:
                old = self.db.execute("SELECT payload FROM owner_fills WHERE fill_id=?", (fill_id,)).fetchone()
                if old is None:
                    raise ValueError("exit has no owned position")
                side = json.loads(old[0])["side"]
            else:
                side = (p or original)["side"]
            if p and not np.isclose(quantity, p["quantity"], rtol=1e-12, atol=0):
                raise ValueError("pair exit must cover exactly its owned leg")
        timestamp = pd.Timestamp(timestamp)
        if timestamp.tz is None or timestamp < pd.Timestamp(payload["signal_time"]):
            raise ValueError("fill predates the signal or lacks timezone")
        with self.ledger.atomic():
            old_fill = self.db.execute("SELECT 1 FROM owner_fills WHERE fill_id=?", (fill_id,)).fetchone()
            if (status != "PENDING" or leg in payload["completed_legs"]) and old_fill is None:
                raise ValueError("intent leg already completed")
            applied = self.ledger.apply_fill(fill_id, strategy=self.name, pair_id=pair, leg=leg,
                symbol=symbol, side=side, action="open" if opening else "close", quantity=quantity,
                price=price, fee=fee, slippage=slippage)
            if not applied:
                return False
            if opening:
                position = state["positions"].setdefault(pair, dict(direction=target, entry_time=timestamp.isoformat(),
                    entry_signal_time=payload["signal_time"], entry_signal_z=payload["signal_z"],
                    legs={}, fees=0., slippage=0., gross_pnl=0.))
                position["legs"][leg] = dict(symbol=symbol, side=side, quantity=quantity, entry_price=price)
            else:
                position = state["positions"][pair]
                entry = position["legs"][leg]
                position["gross_pnl"] += quantity*(price-entry["entry_price"])*(1 if side == "long" else -1)
                position["legs"][leg]["exit_price"] = price
            position["fees"] += fee
            position["slippage"] += slippage
            payload["completed_legs"].append(leg)
            if len(payload["completed_legs"]) == 2:
                status = "FILLED"
                self.ledger.release(intent_id)
                del state["pending"][pair]
                if not opening:
                    trade = dict(position, pair=pair, exit_time=timestamp.isoformat(), exit_reason=payload["reason"],
                        exit_signal_time=payload["signal_time"], exit_signal_z=payload["signal_z"],
                        net_pnl=position["gross_pnl"]-position["fees"]-position["slippage"])
                    self.db.execute("INSERT INTO pair_trades VALUES(?,?)", (intent_id, encoded(trade)))
                    del state["positions"][pair]
            self.db.execute("UPDATE pair_intents SET status=?,payload=? WHERE intent_id=?", (status, encoded(payload), intent_id))
            self._save(state)
        self.state = state
        return True

    def fill_reference(self, timestamp, prices, *, terminal=False, long_fee=.0005,
                       short_open_fee=.001, short_close_fee=.001, slippage_bps=2.):
        """Offline next-open/terminal-close fills, with explicit modeled costs."""
        if any(not np.isfinite(v) or v < 0 for v in (long_fee, short_open_fee, short_close_fee, slippage_bps)):
            raise ValueError("backtest costs must be finite and nonnegative")
        for intent in self.pending():
            if (intent["reason"] == "end_of_data") != terminal:
                continue
            if pd.Timestamp(timestamp) != pd.Timestamp(intent["signal_time"]):
                raise ValueError("reference fill must use the immediately following open/terminal close")
            owned = {p["leg"]: p for p in self.ledger.positions(self.name, intent["pair"])}
            for leg, symbol in zip(("A", "B"), intent["pair"].split("-")):
                if leg in intent["completed_legs"]:
                    continue
                price = prices[symbol]
                if intent["target"]:
                    qty = intent["budget"]/2/price
                    long = (intent["target"] == 1) == (leg == "A")
                else:
                    qty, long = owned[leg]["quantity"], owned[leg]["side"] == "long"
                self.record_fill(intent["intent_id"], leg, fill_id=f"reference:{intent['intent_id']}:{leg}",
                    quantity=qty, price=price, timestamp=timestamp,
                    fee=qty*price*(long_fee if long else short_open_fee if intent['target'] else short_close_fee),
                    slippage=qty*price*slippage_bps/10_000)

    def run_once(self):
        """Shared-feed observation: consume completed bars and persist intents only."""
        if self.fetch is None:
            raise ValueError("runtime needs a shared 30m fetch view")
        end = pd.Timestamp(self.clock.now()).floor(STEP)
        if self.config.cycle_end is not None:
            end = min(end, pd.Timestamp(self.config.cycle_end))
        if self.state is None:
            start = pd.Timestamp(self.config.cycle_start) if self.config.cycle_start else None
            if start is None:
                raise ValueError("choose an explicit cointegration cycle_start")
            data = {c: self.fetch(c, start-pd.Timedelta(days=60)-STEP, start) for c in self.config.assets}
            self.initialize(data)
        start = pd.Timestamp(self.state["last_candle"])+STEP
        if end <= start or self.state["finished"]:
            return dict(status="OBSERVING", candles=0, pending=len(self.pending()))
        data = {c: self.fetch(c, start, end) for c in self.config.assets}
        closes = common_closes(data, self.config.assets, start, end)
        for candle, row in closes.iterrows():
            self.process_close(candle, row.to_dict())
        return dict(status="OBSERVING", candles=len(closes), pending=len(self.pending()),
                    cycle_id=self.state["cycle_id"], last_candle=self.state["last_candle"])
