from datetime import UTC, datetime

from src.engine.config import ExecutionConfig
from src.engine.ports.mock_port import MockExchangePort
from src.engine.schema.models import LongTarget, ShortTarget, TargetPortfolio, Urgency
from src.engine.state.intent_store import IntentJournal


def make_target(signal_id: str) -> TargetPortfolio:
    return TargetPortfolio(
        strategy_id="strategy",
        strategy_version="v1",
        signal_id=signal_id,
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", notional_usd=1000.0)],
        shorts=[ShortTarget(symbol="ETH", collateral_usd=500.0)],
    )


def test_runner_executes_reductions_before_additions():
    from src.engine.execution.runner import ExecutionRunner

    port = MockExchangePort(
        initial_wallet={"USD": 10000.0, "BTC": 0.04},
        tickers={"BTC/USD": 50000.0, "ETH/USD": 2500.0, "SOL/USD": 100.0},
    )
    port.open_short("ETH", 1000.0)
    journal = IntentJournal(memory=True)
    runner = ExecutionRunner(port, journal, ExecutionConfig(dry_run=False))
    target = TargetPortfolio(
        strategy_id="strategy",
        strategy_version="v1",
        signal_id="signal-1",
        timestamp=datetime.now(UTC),
        longs=[
            LongTarget(symbol="BTC", notional_usd=1000.0),
            LongTarget(symbol="ETH", notional_usd=1000.0),
        ],
        shorts=[
            ShortTarget(symbol="ETH", collateral_usd=500.0),
            ShortTarget(symbol="SOL", collateral_usd=500.0),
        ],
    )

    result = runner.execute(
        target,
        {"longs": {"BTC": 2000.0}, "shorts": {"ETH": 1000.0}, "cash_usd": 10000.0},
        total_equity_usd=10000.0,
    )

    assert result["status"] == "EXECUTED"
    assert [item["kind"] for item in result["operations"]] == [
        "close_long",
        "close_short",
        "open_long",
        "open_short",
    ]
    assert port.wallet["BTC"] == 0.02
    assert port.short_positions[0]["ShortQty"] == 0.2
    assert journal.get(result["operations"][0]["intent_id"]).status == "RESOLVED"


def test_runner_deduplicates_signal_id():
    from src.engine.execution.runner import ExecutionRunner

    port = MockExchangePort(initial_wallet={"USD": 10000.0})
    journal = IntentJournal(memory=True)
    runner = ExecutionRunner(port, journal, ExecutionConfig(dry_run=False))
    target = make_target("same-signal")
    actual = {"longs": {}, "shorts": {}, "cash_usd": 10000.0}

    first = runner.execute(target, actual, total_equity_usd=10000.0)
    second = runner.execute(target, actual, total_equity_usd=10000.0)

    assert first["status"] == "EXECUTED"
    assert second == {"status": "DUPLICATE", "signal_id": "same-signal", "operations": []}


def test_runner_slices_large_delta_into_bounded_child_orders():
    from src.engine.execution.runner import ExecutionRunner

    port = MockExchangePort(initial_wallet={"USD": 10000.0})
    journal = IntentJournal(memory=True)
    config = ExecutionConfig(
        dry_run=False,
        min_cash_reserve_usd=0.0,
        max_per_symbol_exposure=1.0,
        max_order_value_usd=10000.0,
    )
    runner = ExecutionRunner(port, journal, config)
    target = TargetPortfolio(
        strategy_id="strategy",
        strategy_version="v1",
        signal_id="sliced-signal",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", notional_usd=6000.0)],
    )

    result = runner.execute(target, {"longs": {}, "shorts": {}, "cash_usd": 10000.0}, 10000.0)

    assert result["status"] == "EXECUTED"
    assert [operation["amount_usd"] for operation in result["operations"]] == [2500.0, 2500.0, 1000.0]


def test_runner_ignores_small_opening_delta_inside_no_trade_band():
    from src.engine.execution.runner import ExecutionRunner

    port = MockExchangePort(initial_wallet={"USD": 10000.0})
    runner = ExecutionRunner(
        port,
        IntentJournal(memory=True),
        ExecutionConfig(dry_run=False, no_trade_band_pct=0.01),
    )
    target = TargetPortfolio(
        strategy_id="strategy",
        strategy_version="v1",
        signal_id="small-delta",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", notional_usd=50.0)],
    )

    result = runner.execute(target, {"longs": {}, "shorts": {}, "cash_usd": 10000.0}, 10000.0)

    assert result["status"] == "EXECUTED"
    assert result["operations"] == []


def test_high_urgency_routes_limit_target_as_market_order():
    from src.engine.execution.runner import ExecutionRunner

    port = MockExchangePort(initial_wallet={"USD": 10000.0})
    runner = ExecutionRunner(
        port,
        IntentJournal(memory=True),
        ExecutionConfig(dry_run=False, no_trade_band_pct=0.0),
    )
    target = TargetPortfolio(
        strategy_id="strategy",
        strategy_version="v1",
        signal_id="urgent-signal",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", notional_usd=1000.0, limit_price=49000.0, urgency=Urgency.HIGH)],
    )

    result = runner.execute(target, {"longs": {}, "shorts": {}, "cash_usd": 10000.0}, 10000.0)

    assert result["status"] == "EXECUTED"
    assert result["operations"][0]["response"]["OrderDetail"]["Type"] == "MARKET"
