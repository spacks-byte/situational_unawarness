from __future__ import annotations

from typing import Any

from src.engine.config import ExecutionConfig
from src.engine.ports.library_port import ExchangePort
from src.engine.reconcile.plan import compute_rebalance_plan
from src.engine.risk.manager import RiskManager, RiskState
from src.engine.schema.models import LongTarget, ShortTarget, TargetPortfolio
from src.engine.state.audit_log import AuditLog
from src.engine.state.intent_store import IntentJournal, IntentRecord
from src.engine.state.snapshot import read_exchange_snapshot


class ExecutionRunner:
    """Translate a normalized rebalance plan into exchange-port operations."""

    def __init__(self, port: ExchangePort, journal: IntentJournal, config: ExecutionConfig | None = None, audit_log: AuditLog | None = None, risk_state: RiskState | None = None) -> None:
        self.port = port
        self.journal = journal
        self.config = config or ExecutionConfig.default()
        self.risk = RiskManager(self.config)
        self.audit_log = audit_log
        self.risk_state = risk_state
        self._marks: dict[str, float] = {}      # snapshot prices the long notionals were valued at

    def execute(
        self,
        target: TargetPortfolio,
        actual: dict[str, Any],
        total_equity_usd: float,
    ) -> dict[str, Any]:
        if self.journal.exists_signal(target.signal_id):
            result = {"status": "DUPLICATE", "signal_id": target.signal_id, "operations": []}
            self._audit("execution", result)
            return result

        plan = self._apply_no_trade_band(
            compute_rebalance_plan(target, actual, total_equity_usd),
            total_equity_usd,
        )
        if getattr(self.port, "is_live", False) and not self.config.dry_run and not self.config.live_mode:
            result = {
                "status": "REJECTED_RISK",
                "signal_id": target.signal_id,
                "reasons": ["live_mode_required"],
                "operations": [],
            }
            self._audit("risk_rejection", result)
            return result
        decision = self.risk.evaluate(plan, actual, total_equity_usd, state=self.risk_state)
        if not decision.allowed:
            result = {
                "status": "REJECTED_RISK",
                "signal_id": target.signal_id,
                "reasons": list(decision.reasons),
                "operations": [],
            }
            self._audit("risk_rejection", result)
            return result
        if not self.journal.claim_signal(target.signal_id):
            result = {"status": "DUPLICATE", "signal_id": target.signal_id, "operations": []}
            self._audit("execution", result)
            return result
        self._marks = {str(k).upper(): float(v) for k, v in (actual.get("prices") or {}).items()}
        long_targets = {item.symbol.upper(): item for item in target.longs}
        short_targets = {item.symbol.upper(): item for item in target.shorts}
        operations: list[dict[str, Any]] = []

        actions = (
            ("close_long", plan["close_longs"], long_targets),
            ("close_short", plan["close_shorts"], short_targets),
            ("open_long", plan["open_longs"], long_targets),
            ("open_short", plan["open_shorts"], short_targets),
        )
        child_index = 0
        for kind, amounts, target_by_symbol in actions:
            for symbol, amount in amounts.items():
                # A full short exit is one close_pct=100 call (see _dispatch), never sliced.
                full_short_exit = kind == "close_short" and symbol not in target_by_symbol
                children = [float(amount)] if full_short_exit else self._split_amount(float(amount), total_equity_usd)
                for child_amount in children:
                    operation = self._execute_one(
                        target,
                        kind,
                        symbol,
                        child_amount,
                        target_by_symbol.get(symbol),
                        child_index,
                    )
                    operations.append(operation)
                    self._audit("operation", operation)
                    child_index += 1
                    if operation["status"] == "UNCERTAIN":
                        result = {"status": "UNCERTAIN", "signal_id": target.signal_id, "operations": operations}
                        self._audit("execution", result)
                        return result

        result = {"status": "EXECUTED", "signal_id": target.signal_id, "operations": operations}
        self._audit("execution", result)
        return result

    def execute_live(self, target: TargetPortfolio) -> dict[str, Any]:
        snapshot = read_exchange_snapshot(self.port)
        return self.execute(target, snapshot, snapshot["equity_usd"])

    def reconcile_uncertain_intent(
        self,
        intent_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> str:
        intent = self.journal.get(intent_id)
        if intent is None:
            return "UNKNOWN"
        if intent.status != "UNCERTAIN":
            return intent.status

        before_values = before.get("longs" if "long" in intent.kind else "shorts", {}) or {}
        after_values = after.get("longs" if "long" in intent.kind else "shorts", {}) or {}
        before_amount = float(before_values.get(intent.symbol, 0.0))
        after_amount = float(after_values.get(intent.symbol, 0.0))
        expected = float(intent.payload.get("amount_usd", 0.0))
        observed_delta = after_amount - before_amount
        if intent.kind.startswith("close_"):
            observed_delta = -observed_delta
        tolerance = max(1.0, expected * 0.01)
        if expected > 0 and observed_delta + tolerance >= expected:
            self.journal.mark_resolved(intent_id)
            return "RESOLVED"
        return "UNCERTAIN"

    def _audit(self, event: str, payload: dict[str, Any]) -> None:
        if self.audit_log is not None:
            self.audit_log.record(event, payload)

    def _execute_one(
        self,
        target: TargetPortfolio,
        kind: str,
        symbol: str,
        amount_usd: float,
        target_config: LongTarget | ShortTarget | None,
        child_index: int,
    ) -> dict[str, Any]:
        side = "SELL" if kind == "close_long" else "BUY" if kind == "open_long" else kind.split("_")[1]
        payload = {"amount_usd": amount_usd}
        intent = IntentRecord.build(
            signal_id=target.signal_id,
            symbol=symbol,
            kind=kind,
            side=side,
            child_index=child_index,
            payload=payload,
            clock=self.journal.clock,
        )
        self.journal.add(intent)

        try:
            response = self._dispatch(kind, symbol, amount_usd, target_config)
        except Exception:
            self.journal.mark_uncertain(intent.intent_id)
            return {"intent_id": intent.intent_id, "kind": kind, "symbol": symbol, "amount_usd": amount_usd, "status": "UNCERTAIN"}

        if not response or response.get("Success") is False:
            self.journal.mark_uncertain(intent.intent_id)
            return {"intent_id": intent.intent_id, "kind": kind, "symbol": symbol, "amount_usd": amount_usd, "status": "UNCERTAIN", "response": response}

        self.journal.mark_sent(intent.intent_id, self._response_id(response))
        if self._is_resolved(response):
            self.journal.mark_resolved(intent.intent_id)
            operation_status = "RESOLVED"
        else:
            operation_status = "SENT"
        return {"intent_id": intent.intent_id, "kind": kind, "symbol": symbol, "amount_usd": amount_usd, "status": operation_status, "response": response}

    def _dispatch(self, kind: str, symbol: str, amount_usd: float, target_config: LongTarget | ShortTarget | None) -> dict[str, Any]:
        if self.config.dry_run:
            return {"Success": True, "DryRun": True}

        pair = f"{symbol}/USD"
        if kind == "close_long" and self._marks.get(symbol, 0.0) > 0:
            # The holding was valued at the snapshot price, so the same price gives back the exact
            # coin quantity. A fresh ticker would oversell after a down-tick and the exchange rejects it.
            quantity = amount_usd / self._marks[symbol]
            return self.port.place_order(pair, "SELL", quantity, price=None, order_type="MARKET")
        price = self._price(symbol, target_config, price_field="MinAsk" if kind == "close_short" else "LastPrice")
        if kind in {"open_long", "close_long"}:
            quantity = amount_usd / price
            side = "BUY" if kind == "open_long" else "SELL"
            limit_price = self._limit_price(target_config) if kind == "open_long" else None
            return self.port.place_order(pair, side, quantity, price=limit_price, order_type="LIMIT" if limit_price else "MARKET")
        if kind == "open_short":
            limit_price = self._limit_price(target_config)
            return self.port.open_short(pair, amount_usd, price=limit_price)
        if target_config is None:
            # Full exit: close_qty = collateral / current price under-closes a losing short
            # (collateral was posted at the lower entry price) and leaves a residual position.
            return self.port.close_short(pair, close_pct=100)
        quantity = amount_usd / price
        return self.port.close_short(pair, close_qty=quantity)

    def _split_amount(self, amount_usd: float, total_equity_usd: float) -> list[float]:
        max_child = total_equity_usd * self.config.max_child_order_pct
        if amount_usd <= 0 or max_child <= 0 or amount_usd <= max_child:
            return [amount_usd]
        children: list[float] = []
        remaining = amount_usd
        while remaining > max_child:
            children.append(max_child)
            remaining -= max_child
        if remaining > 0:
            children.append(remaining)
        return children

    def _apply_no_trade_band(self, plan: dict[str, Any], total_equity_usd: float) -> dict[str, Any]:
        threshold = total_equity_usd * self.config.no_trade_band_pct
        if threshold <= 0:
            return plan
        filtered = dict(plan)
        for key in ("open_longs", "open_shorts"):
            filtered[key] = {
                symbol: amount
                for symbol, amount in plan[key].items()
                if float(amount) >= threshold
            }
        return filtered

    def _price(self, symbol: str, target_config: LongTarget | ShortTarget | None, price_field: str = "LastPrice") -> float:
        limit_price = self._limit_price(target_config)
        if limit_price is not None:
            return float(limit_price)
        pair = f"{symbol}/USD"
        response = self.port.get_ticker(pair)
        pair_data = response.get("Data", {}).get(pair, {})
        price = pair_data.get(price_field, pair_data.get("LastPrice"))
        if price is None or float(price) <= 0:
            raise ValueError(f"No valid price available for {pair}")
        return float(price)

    def _limit_price(self, target_config: LongTarget | ShortTarget | None) -> float | None:
        if target_config is None or not self.config.supports_limit_orders:
            return None
        if getattr(target_config.urgency, "value", target_config.urgency) == "high":
            return None
        return getattr(target_config, "limit_price", None)

    @staticmethod
    def _is_resolved(response: dict[str, Any]) -> bool:
        if response.get("DryRun") is True or response.get("ClosedQty") is not None:
            return True
        if str(response.get("Status", "")).upper() in {"FILLED", "OPEN"}:
            return True
        detail = response.get("OrderDetail")
        return isinstance(detail, dict) and str(detail.get("Status", "")).upper() == "FILLED"

    @staticmethod
    def _response_id(response: dict[str, Any]) -> str | None:
        for key in ("OrderID", "ID", "order_id", "id"):
            if response.get(key) is not None:
                return str(response[key])
        detail = response.get("OrderDetail")
        if isinstance(detail, dict) and detail.get("OrderID") is not None:
            return str(detail["OrderID"])
        return None