from datetime import UTC, datetime

from tradebot.core.config import ExecutionConfig
from tradebot.engine.execution.runner import ExecutionRunner
from tradebot.exchange.mock import MockExchangePort
from tradebot.engine.schema.models import LongTarget, TargetPortfolio
from tradebot.engine.state.intent_store import IntentJournal


def test_same_signal_is_noop_even_when_first_plan_has_no_orders():
    port = MockExchangePort(initial_wallet={"USD": 10000.0})
    journal = IntentJournal(memory=True)
    runner = ExecutionRunner(port, journal, ExecutionConfig(dry_run=False))
    target = TargetPortfolio(
        strategy_id="s",
        strategy_version="v1",
        signal_id="already-flat",
        timestamp=datetime.now(UTC),
    )
    actual = {"longs": {}, "shorts": {}, "cash_usd": 10000.0}

    first = runner.execute(target, actual, total_equity_usd=10000.0)
    second = runner.execute(target, actual, total_equity_usd=10000.0)

    assert first["status"] == "EXECUTED"
    assert second["status"] == "DUPLICATE"


def test_risk_rejected_signal_can_be_retried():
    port = MockExchangePort(initial_wallet={"USD": 10000.0})
    journal = IntentJournal(memory=True)
    target = TargetPortfolio(
        strategy_id="s",
        strategy_version="v1",
        signal_id="retryable-signal",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", notional_usd=2000.0)],
    )
    actual = {"longs": {}, "shorts": {}, "cash_usd": 10000.0}

    rejected = ExecutionRunner(
        port,
        journal,
        ExecutionConfig(dry_run=False, min_cash_reserve_usd=20000.0),
    ).execute(target, actual, 10000.0)
    retried = ExecutionRunner(
        port,
        journal,
        ExecutionConfig(dry_run=False),
    ).execute(target, actual, 10000.0)

    assert rejected["status"] == "REJECTED_RISK"
    assert retried["status"] == "EXECUTED"
