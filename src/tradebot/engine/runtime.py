from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tradebot.core.clock import Clock, RealClock
from tradebot.engine.execution.runner import ExecutionRunner
from tradebot.engine.monitor.position_monitor import PositionMonitor
from tradebot.engine.reconcile.pending import cancel_stale_orders
from tradebot.engine.schema.models import TargetPortfolio, StrategyOutput
from tradebot.engine.state.snapshot import read_exchange_snapshot


class EngineRuntime:
    """Clock-driven orchestration shared by live execution and backtests."""

    def __init__(self, runner: ExecutionRunner, *, clock: Clock | None = None, poll_interval_seconds: int = 5, monitor: PositionMonitor | None = None, pending_timeout_seconds: int | None = None, quote_executor=None) -> None:
        self.quote_executor = quote_executor
        self.runner = runner
        self.clock = clock or RealClock()
        self.poll_interval_seconds = poll_interval_seconds
        self.monitor = monitor
        self.pending_timeout_seconds = pending_timeout_seconds or runner.config.fill_timeout_seconds

    def run_once(self, strategy: Callable[[dict[str, Any]], StrategyOutput]) -> dict[str, Any]:
        if getattr(strategy, "output_kind", "weights") == "quotes":
            if self.quote_executor is None:
                raise ValueError("quote strategy requires a configured quote executor")
            return self.quote_executor.run_once(strategy)
        cancelled = cancel_stale_orders(self.runner.port, self.clock, self.pending_timeout_seconds,
                                        grace_seconds=self.poll_interval_seconds)
        if cancelled and self.runner.config.cancel_settle_seconds > 0:
            self.clock.sleep(self.runner.config.cancel_settle_seconds)
        snapshot = read_exchange_snapshot(self.runner.port)
        self.runner.reconcile_uncertain_intents(snapshot)
        target = strategy(snapshot)
        alerts = self.monitor.evaluate(target, snapshot.get("prices", {}), snapshot.get("entry_prices", {})) if self.monitor else []
        if alerts:
            flatten = list(target.flatten)
            flatten.extend(alert["symbol"] for alert in alerts)
            target = target.model_copy(update={"flatten": list(dict.fromkeys(flatten))})
        result = self.runner.execute(target, snapshot, snapshot["equity_usd"])
        if alerts:
            result["alerts"] = alerts
        return result

    def run(
        self,
        strategy: Callable[[dict[str, Any]], StrategyOutput],
        *,
        max_iterations: int | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        iteration = 0
        while max_iterations is None or iteration < max_iterations:
            results.append(self.run_once(strategy))
            iteration += 1
            if max_iterations is None or iteration < max_iterations:
                self.clock.sleep(self.poll_interval_seconds)
        return results