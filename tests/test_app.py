from datetime import UTC, datetime

from tradebot.engine.app import Engine
from tradebot.core.clock import SimClock
from tradebot.core.config import ExecutionConfig
from tradebot.exchange.mock import MockExchangePort
from tradebot.engine.schema.models import TargetPortfolio


def test_engine_facade_wires_runtime_state_and_audit(tmp_path):
    clock = SimClock(datetime(2026, 1, 1, tzinfo=UTC))
    engine = Engine(
        MockExchangePort(initial_wallet={"USD": 10000.0}, clock=clock),
        config=ExecutionConfig(dry_run=False),
        state_path=tmp_path / "state.db",
        audit_path=tmp_path / "audit.jsonl",
        clock=clock,
    )

    def strategy(snapshot):
        return TargetPortfolio(
            strategy_id="s",
            strategy_version="v1",
            signal_id="app-signal",
            timestamp=clock.now(),
        )

    result = engine.run_once(strategy)

    assert result["status"] == "EXECUTED"
    assert (tmp_path / "state.db").exists()
    assert (tmp_path / "audit.jsonl").exists()
    engine.close()
