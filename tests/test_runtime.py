from datetime import UTC, datetime

from tradebot.core.clock import SimClock
from tradebot.core.config import ExecutionConfig
from tradebot.engine.execution.runner import ExecutionRunner
from tradebot.exchange.mock import MockExchangePort
from tradebot.engine.runtime import EngineRuntime
from tradebot.engine.schema.models import LongTarget, TargetPortfolio
from tradebot.engine.state.intent_store import IntentJournal


def test_runtime_runs_strategy_through_shared_runner():
    clock = SimClock(datetime(2026, 1, 1, tzinfo=UTC))
    port = MockExchangePort(initial_wallet={"USD": 10000.0}, clock=clock)
    runner = ExecutionRunner(port, IntentJournal(memory=True, clock=clock), ExecutionConfig(dry_run=False))
    runtime = EngineRuntime(runner, clock=clock, poll_interval_seconds=5)
    calls = []

    def strategy(snapshot):
        calls.append(snapshot["equity_usd"])
        return TargetPortfolio(
            strategy_id="s",
            strategy_version="v1",
            signal_id=f"sig-{len(calls)}",
            timestamp=clock.now(),
            longs=[LongTarget(symbol="BTC", notional_usd=1000.0)],
        )

    results = runtime.run(strategy, max_iterations=2)

    assert len(results) == 2
    assert all(result["status"] == "EXECUTED" for result in results)
    assert calls == [10000.0, 9999.5]  # second snapshot reflects the 0.05% maker fee on the $1,000 buy
    assert clock.now().timestamp() == datetime(2026, 1, 1, 0, 0, 5).replace(tzinfo=UTC).timestamp()
