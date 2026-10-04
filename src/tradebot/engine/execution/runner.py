from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tradebot.core.config import ExecutionConfig
from tradebot.core.symbols import to_pair
from tradebot.exchange.port import ExchangePort
from tradebot.engine.reconcile.plan import compute_rebalance_plan
from tradebot.engine.risk.manager import RiskManager, RiskState
from tradebot.engine.schema.models import LongTarget, ShortTarget, TargetPortfolio
from tradebot.engine.state.audit_log import AuditLog
from tradebot.engine.state.intent_store import IntentJournal, IntentRecord
from tradebot.engine.state.snapshot import read_exchange_snapshot


class ExecutionRunner:
    """Translate a normalized rebalance plan into exchange-port operations.

    Order policy (config.order_policy):
      - "limit_only" (default): spot buys, spot sells and short opens are LIMIT orders at the
        target's limit_price, or else the snapshot's last price moved limit_offset_bps to the passive
        side (buys below, sells and short opens above). Unfilled orders are cancelled after
        fill_timeout_seconds and the next signal re-places them at the new price (the backtest models
        the same one-bar lifetime). Short closes are always market: Roostoo's API takes no price,
        and a full short exit is one close_pct=100 call, so a losing short leaves no residual.
      - "limit_or_market": LIMIT only when the target sets limit_price and urgency isn't high.

    A rejected order (Success:false, or refused by the client before sending) is REJECTED and the
    rest of the plan still runs; only an unknown outcome (a transport error) is UNCERTAIN and stops
    it. Buys and short opens are capped to the USD that is free now, so they never depend on sells
    that haven't filled yet; the next signal sends the rest.
    """

    def __init__(self, port: ExchangePort, journal: IntentJournal, config: ExecutionConfig | None = None, audit_log: AuditLog | None = None, risk_state: RiskState | None = None) -> None:
        self.port = port
        self.journal = journal
        self.config = config or ExecutionConfig.default()
        self.risk = RiskManager(self.config)
        self.audit_log = audit_log
        self.risk_state = risk_state
        self._prices: dict[str, float] = {}
        # Optional extra gate on the whole plan, run after the risk manager:
        # plan_check(plan, actual, equity) -> list of rejection reasons ([] = allowed)
        self.plan_check: Callable[[dict[str, Any], dict[str, Any], float], list[str]] | None = None

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
        reasons = list(decision.reasons)
        if decision.allowed and self.plan_check is not None:
            reasons = list(self.plan_check(plan, actual, total_equity_usd))
        if reasons:
            result = {
                "status": "REJECTED_RISK",
                "signal_id": target.signal_id,
                "reasons": reasons,
                "operations": [],
            }
            self._audit("risk_rejection", result)
            return result
        if not self.journal.claim_signal(target.signal_id):
            result = {"status": "DUPLICATE", "signal_id": target.signal_id, "operations": []}
            self._audit("execution", result)
            return result
        self._prices = {str(k).upper(): float(v) for k, v in (actual.get("prices") or {}).items()}
        long_targets = {item.symbol.upper(): item for item in target.longs}
        short_targets = {item.symbol.upper(): item for item in target.shorts}
        operations: list[dict[str, Any]] = []

        actions = (
            ("close_long", plan["close_longs"], long_targets),
            ("close_short", plan["close_shorts"], short_targets),
            ("open_long", plan["open_longs"], long_targets),
            ("open_short", plan["open_shorts"], short_targets),
        )
        # Live snapshots carry cash_free_usd; hand-built ones may only have cash_usd (Free + Lock)
        budget = float(actual.get("cash_free_usd", actual.get("cash_usd", 0.0))) - self.config.min_cash_reserve_usd
        child_index = 0
        for kind, amounts, target_by_symbol in actions:
            if kind.startswith("open_"):
                amounts = self._fund(amounts, budget, kind)
            for symbol, amount in amounts.items():
                # A full short exit is one close_pct=100 call (see _dispatch), never sliced
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
                        actual,
                    )
                    operations.append(operation)
                    self._audit("operation", operation)
                    child_index += 1
                    if kind.startswith("open_") and operation["status"] in ("SENT", "RESOLVED"):
                        budget -= child_amount * (1 + self._open_fee(kind))
                    if operation["status"] == "UNCERTAIN":
                        result = {"status": "UNCERTAIN", "signal_id": target.signal_id, "operations": operations}
                        self._audit("execution", result)
                        return result

        result = {"status": "EXECUTED", "signal_id": target.signal_id, "operations": operations}
        self._audit("execution", result)
        return result

    def reconcile_uncertain_intents(self, snapshot: dict[str, Any]) -> list[str]:
        """Resolve crash-window intents from the first fresh exchange snapshot."""
        resolved: list[str] = []
        for intent in self.journal.list_by_status("UNCERTAIN"):
            before = intent.payload.get("before")
            if not isinstance(before, dict):
                continue
            status = self.reconcile_uncertain_intent(intent.intent_id, before, snapshot)
            if status == "RESOLVED":
                resolved.append(intent.intent_id)
        return resolved

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
        actual: dict[str, Any],
    ) -> dict[str, Any]:
        side = {"open_long": "BUY", "close_long": "SELL", "open_short": "SHORT_OPEN", "close_short": "SHORT_CLOSE"}[kind]
        exposure_key = "longs" if kind.endswith("long") else "shorts"
        payload = {
            "amount_usd": amount_usd,
            "before": {exposure_key: {symbol: float((actual.get(exposure_key) or {}).get(symbol, 0.0))}},
        }
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
        except ValueError as e:                     # refused by the client: nothing was sent
            self.journal.mark_rejected(intent.intent_id)
            return {"intent_id": intent.intent_id, "kind": kind, "symbol": symbol, "amount_usd": amount_usd, "status": "REJECTED", "reason": str(e)}
        except Exception:
            self.journal.mark_uncertain(intent.intent_id)
            return {"intent_id": intent.intent_id, "kind": kind, "symbol": symbol, "amount_usd": amount_usd, "status": "UNCERTAIN"}

        if not response or response.get("Success") is False:
            self.journal.mark_rejected(intent.intent_id)
            return {"intent_id": intent.intent_id, "kind": kind, "symbol": symbol, "amount_usd": amount_usd, "status": "REJECTED", "response": response}

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

        pair = to_pair(symbol)
        if kind == "close_short":
            if target_config is None:
                # Full exit. Sizing by collateral / price under-closes a losing short (the collateral
                # was posted at the lower entry price) and would leave a residual position.
                return self.port.close_short(pair, close_pct=100)
            quantity = amount_usd / self._market_price(symbol, "MinAsk")
            return self.port.close_short(pair, close_qty=quantity)

        limit_price = self._order_price(symbol, target_config, passive_above=kind != "open_long")
        if kind == "open_short":
            return self.port.open_short(pair, amount_usd, price=limit_price)

        quantity = amount_usd / (limit_price or self._market_price(symbol))
        side = "BUY" if kind == "open_long" else "SELL"
        return self.port.place_order(pair, side, quantity, price=limit_price,
                                     order_type="LIMIT" if limit_price else "MARKET")

    def _fund(self, amounts: dict[str, float], budget: float, kind: str) -> dict[str, float]:
        """Scale opening amounts down to what the free cash pays for, fees included."""
        cost = sum(amounts.values()) * (1 + self._open_fee(kind))
        if cost <= max(budget, 0.0):
            return amounts
        scale = max(budget, 0.0) / cost
        return {symbol: amount * scale for symbol, amount in amounts.items() if amount * scale >= 1.0}

    def _open_fee(self, kind: str) -> float:
        return self.config.fees.short_open if kind == "open_short" else self.config.fees.spot_maker

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

    def _order_price(self, symbol: str, target_config: LongTarget | ShortTarget | None,
                     passive_above: bool = False) -> float | None:
        """Limit price for spot orders and short opens; None means a market order.
        passive_above: True for sells and short opens (rest above the market), False for buys."""
        explicit = getattr(target_config, "limit_price", None) if target_config is not None else None
        if self.config.order_policy == "limit_only":
            if explicit:
                return float(explicit)
            offset = self.config.limit_offset_bps / 1e4
            return self._market_price(symbol) * (1 + offset if passive_above else 1 - offset)
        if target_config is None or not self.config.supports_limit_orders:
            return None
        if getattr(target_config.urgency, "value", target_config.urgency) == "high":
            return None
        return explicit

    def _market_price(self, symbol: str, price_field: str = "LastPrice") -> float:
        """Last price from the current snapshot; the ticker is only queried for other fields (e.g. MinAsk)."""
        if price_field == "LastPrice" and self._prices.get(symbol.upper(), 0) > 0:
            return self._prices[symbol.upper()]
        pair = to_pair(symbol)
        response = self.port.get_ticker(pair)
        pair_data = response.get("Data", {}).get(pair, {})
        price = pair_data.get(price_field, pair_data.get("LastPrice"))
        if price is None or float(price) <= 0:
            raise ValueError(f"No valid price available for {pair}")
        return float(price)

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