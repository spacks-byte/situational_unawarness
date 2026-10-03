from datetime import UTC, datetime

from tradebot.core.config import ExecutionConfig
from tradebot.engine.execution.runner import ExecutionRunner
from tradebot.exchange.mock import MockExchangePort
from tradebot.engine.schema.models import LongTarget, TargetPortfolio
from tradebot.engine.state.intent_store import IntentJournal


class LiveMarkedMockPort(MockExchangePort):
    is_live = True


def test_live_adapter_requires_explicit_live_mode():
    runner = ExecutionRunner(
        LiveMarkedMockPort(initial_wallet={"USD": 10000.0}),
        IntentJournal(memory=True),
        ExecutionConfig(dry_run=False, live_mode=False, no_trade_band_pct=0.0),
    )
    target = TargetPortfolio(
        strategy_id="s",
        strategy_version="v1",
        signal_id="live-guard",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", notional_usd=1000.0)],
    )

    result = runner.execute(target, {"longs": {}, "shorts": {}, "cash_usd": 10000.0}, 10000.0)

    assert result["status"] == "REJECTED_RISK"
    assert result["reasons"] == ["live_mode_required"]
