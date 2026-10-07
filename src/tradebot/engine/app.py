from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tradebot.core.clock import Clock, RealClock
from tradebot.core.config import ExecutionConfig, Settings
from tradebot.engine.execution.runner import ExecutionRunner
from tradebot.engine.monitor.position_monitor import PositionMonitor
from tradebot.exchange.port import ExchangePort
from tradebot.engine.runtime import EngineRuntime
from tradebot.engine.schema.models import TargetPortfolio, StrategyOutput
from tradebot.engine.state.audit_log import AuditLog
from tradebot.engine.state.intent_store import IntentJournal


class Engine:
    """Public application façade for live and simulated strategy execution."""

    def __init__(
        self,
        port: ExchangePort,
        *,
        config: ExecutionConfig | None = None,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
        audit_path: str | Path | None = None,
        clock: Clock | None = None,
        monitor: PositionMonitor | None = None,
        quote_executor=None,
    ) -> None:
        """
        config: explicit ExecutionConfig; otherwise the `execution` section of `config_path`
        ($TRADEBOT_CONFIG or config/default.yaml). State and audit files default to config.state_dir.
        """
        self.clock = clock or RealClock()
        self.config = config or Settings.load(config_path).execution
        state_dir = Path(self.config.state_dir)
        state_path = state_path or state_dir / "engine_state.db"
        audit_path = audit_path or state_dir / "engine_audit.jsonl"
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        self.journal = IntentJournal(state_path, clock=self.clock)
        self.audit_log = AuditLog(audit_path, clock=self.clock)
        self.runner = ExecutionRunner(
            port,
            self.journal,
            self.config,
            audit_log=self.audit_log,
        )
        self.runtime = EngineRuntime(
            self.runner,
            clock=self.clock,
            poll_interval_seconds=self.config.strategy_poll_interval_seconds,
            monitor=monitor or PositionMonitor(),
            pending_timeout_seconds=self.config.fill_timeout_seconds,
            quote_executor=quote_executor,
        )

    def run_once(self, strategy: Callable[[dict[str, Any]], StrategyOutput]) -> dict[str, Any]:
        return self.runtime.run_once(strategy)

    def run(
        self,
        strategy: Callable[[dict[str, Any]], StrategyOutput],
        *,
        max_iterations: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.runtime.run(strategy, max_iterations=max_iterations)

    def close(self) -> None:
        self.journal.close()