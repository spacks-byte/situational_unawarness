from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.engine.clock import Clock, RealClock
from src.engine.config import ExecutionConfig
from src.engine.execution.runner import ExecutionRunner
from src.engine.monitor.position_monitor import PositionMonitor
from src.engine.ports.library_port import ExchangePort
from src.engine.runtime import EngineRuntime
from src.engine.schema.models import TargetPortfolio
from src.engine.state.audit_log import AuditLog
from src.engine.state.intent_store import IntentJournal


class Engine:
    """Public application façade for live and simulated strategy execution."""

    def __init__(
        self,
        port: ExchangePort,
        *,
        config: ExecutionConfig | None = None,
        state_path: str | Path = "engine_state.db",
        audit_path: str | Path = "engine_audit.jsonl",
        clock: Clock | None = None,
        monitor: PositionMonitor | None = None,
    ) -> None:
        self.clock = clock or RealClock()
        self.config = config or ExecutionConfig.default()
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
        )

    def run_once(self, strategy: Callable[[dict[str, Any]], TargetPortfolio]) -> dict[str, Any]:
        return self.runtime.run_once(strategy)

    def run(
        self,
        strategy: Callable[[dict[str, Any]], TargetPortfolio],
        *,
        max_iterations: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.runtime.run(strategy, max_iterations=max_iterations)

    def close(self) -> None:
        self.journal.close()