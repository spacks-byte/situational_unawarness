from datetime import UTC, datetime

from tradebot.core.config import ExecutionConfig
from tradebot.exchange.mock import MockExchangePort
from tradebot.engine.schema.models import LongTarget, ShortTarget, TargetPortfolio, Urgency
from tradebot.engine.state.intent_store import IntentJournal


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
    from tradebot.engine.execution.runner import ExecutionRunner

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
    from tradebot.engine.execution.runner import ExecutionRunner

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
    from tradebot.engine.execution.runner import ExecutionRunner

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
    from tradebot.engine.execution.runner import ExecutionRunner

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


def _urgent_target(signal_id: str) -> TargetPortfolio:
    return TargetPortfolio(
        strategy_id="strategy",
        strategy_version="v1",
        signal_id=signal_id,
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", notional_usd=1000.0, limit_price=49000.0, urgency=Urgency.HIGH)],
    )


def test_limit_only_policy_keeps_urgent_orders_as_limits():
    from tradebot.engine.execution.runner import ExecutionRunner

    port = MockExchangePort(initial_wallet={"USD": 10000.0})
    runner = ExecutionRunner(port, IntentJournal(memory=True), ExecutionConfig(dry_run=False, no_trade_band_pct=0.0))

    result = runner.execute(_urgent_target("urgent-limit"), {"longs": {}, "shorts": {}, "cash_usd": 10000.0}, 10000.0)

    detail = result["operations"][0]["response"]["OrderDetail"]
    assert result["status"] == "EXECUTED"
    assert detail["Type"] == "LIMIT" and detail["Price"] == 49000.0


def test_limit_or_market_policy_routes_high_urgency_as_market_order():
    from tradebot.engine.execution.runner import ExecutionRunner

    port = MockExchangePort(initial_wallet={"USD": 10000.0})
    config = ExecutionConfig(dry_run=False, no_trade_band_pct=0.0, order_policy="limit_or_market")
    runner = ExecutionRunner(port, IntentJournal(memory=True), config)

    result = runner.execute(_urgent_target("urgent-market"), {"longs": {}, "shorts": {}, "cash_usd": 10000.0}, 10000.0)

    assert result["status"] == "EXECUTED"
    assert result["operations"][0]["response"]["OrderDetail"]["Type"] == "MARKET"


def test_limit_only_closes_are_limit_at_snapshot_price_and_short_closes_market():
    from tradebot.engine.execution.runner import ExecutionRunner

    port = MockExchangePort(initial_wallet={"USD": 10000.0, "BTC": 0.04}, tickers={"BTC/USD": 50000.0, "ETH/USD": 2500.0})
    port.open_short("ETH", 1000.0)
    runner = ExecutionRunner(port, IntentJournal(memory=True), ExecutionConfig(dry_run=False))
    target = TargetPortfolio(strategy_id="s", strategy_version="v1", signal_id="flat", timestamp=datetime.now(UTC))
    actual = {"longs": {"BTC": 2000.0}, "shorts": {"ETH": 1000.0}, "cash_usd": 10000.0,
              "prices": {"BTC": 51000.0, "ETH": 2500.0}}

    result = runner.execute(target, actual, 13000.0)

    sell, cover = result["operations"]
    assert sell["kind"] == "close_long" and sell["response"]["OrderDetail"]["Type"] == "LIMIT"
    assert sell["response"]["OrderDetail"]["Price"] == 51000.0  # snapshot last price, no extra ticker call
    assert cover["kind"] == "close_short" and "ClosedQty" in cover["response"]


def test_full_long_exit_sells_the_exact_holding_even_if_the_price_ticked_down():
    from tradebot.engine.execution.runner import ExecutionRunner
    from tradebot.engine.state.snapshot import read_exchange_snapshot

    port = MockExchangePort(initial_wallet={"USD": 0.0, "ETH": 0.5}, tickers={"ETH/USD": 2000.0})
    snap = read_exchange_snapshot(port)
    port.tickers["ETH/USD"] = 1900.0                       # moves after the snapshot, before the sell
    runner = ExecutionRunner(port, IntentJournal(memory=True), ExecutionConfig(dry_run=False))
    target = TargetPortfolio(strategy_id="t", strategy_version="v", signal_id="exit", timestamp=datetime.now(UTC))
    result = runner.execute(target, snap, snap["equity_usd"])
    assert {op["status"] for op in result["operations"]} == {"RESOLVED"}
    assert abs(port.wallet["ETH"]) < 1e-12                 # quantity sized at the snapshot price


def test_engine_priced_exits_rest_passively_by_the_configured_offset():
    from tradebot.engine.execution.runner import ExecutionRunner

    port = MockExchangePort(initial_wallet={"USD": 0.0, "ETH": 0.5}, tickers={"ETH/USD": 2000.0})
    runner = ExecutionRunner(port, IntentJournal(memory=True), ExecutionConfig(dry_run=False, limit_offset_bps=5))
    target = TargetPortfolio(strategy_id="t", strategy_version="v", signal_id="exit5", timestamp=datetime.now(UTC))
    result = runner.execute(target, {"longs": {"ETH": 1000.0}, "shorts": {}, "cash_usd": 0.0,
                                     "prices": {"ETH": 2000.0}}, 1000.0)
    detail = result["operations"][0]["response"]["OrderDetail"]
    assert detail["Type"] == "LIMIT" and detail["Price"] == 2000.0 * (1 + 5e-4)   # sells rest above the market


# ---------------------------------------------------------------------------- limit-only order handling
def _runner(port):
    from tradebot.engine.execution.runner import ExecutionRunner

    config = ExecutionConfig(dry_run=False, min_cash_reserve_usd=0, max_per_symbol_exposure=1.0, max_order_value_usd=1e6,
                             max_total_short_collateral_usd=1e6, max_child_order_pct=1.0)
    return ExecutionRunner(port, IntentJournal(memory=True), config)


def _target(longs=(), shorts=()):
    return TargetPortfolio(strategy_id="t", strategy_version="1", signal_id="s", timestamp=datetime.now(UTC),
                           longs=list(longs), shorts=list(shorts))


def test_a_rejected_order_does_not_stop_the_rest_of_the_plan():
    port = MockExchangePort(initial_wallet={"USD": 10_000.0})
    place = port.place_order
    port.place_order = lambda pair, side, qty, price=None, order_type=None: (
        {"Success": False, "ErrMsg": "insufficient balance"} if pair == "BTC/USD" else place(pair, side, qty, price, order_type))
    result = _runner(port).execute(_target(longs=[LongTarget(symbol="BTC", notional_usd=1_000.0),
                                                  LongTarget(symbol="ETH", notional_usd=1_000.0)]),
                                   {"cash_usd": 10_000.0, "cash_free_usd": 10_000.0}, 10_000.0)
    assert {o["symbol"]: o["status"] for o in result["operations"]} == {"BTC": "REJECTED", "ETH": "RESOLVED"}
    assert result["status"] == "EXECUTED"


def test_buys_are_capped_to_the_cash_that_is_free_now():
    port = MockExchangePort(initial_wallet={"USD": 1_000.0, "BTC": 1.0})
    snapshot = {"cash_usd": 1_000.0, "cash_free_usd": 1_000.0, "longs": {"BTC": 50_000.0}}
    result = _runner(port).execute(_target(longs=[LongTarget(symbol="ETH", notional_usd=40_000.0)]), snapshot, 51_000.0)
    buy = next(o for o in result["operations"] if o["kind"] == "open_long")
    assert buy["amount_usd"] <= 1_000.0 and buy["status"] == "RESOLVED"


def test_a_resting_sell_is_not_sent_twice():
    port = MockExchangePort(initial_wallet={"USD": 0.0, "BTC": 1.0})
    resting = [{"Pair": "BTC/USD", "Side": "SELL", "Status": "PENDING", "Quantity": 1.0, "Price": 50_000.0}]
    snapshot = {"cash_usd": 0.0, "longs": {"BTC": 50_000.0}, "pending_orders": resting}
    assert _runner(port).execute(_target(), snapshot, 50_000.0)["operations"] == []


def test_a_resting_short_open_without_collateral_is_not_sent_twice():
    port = MockExchangePort(initial_wallet={"USD": 10_000.0})
    resting = [{"Pair": "ETH/USD", "Side": "SHORT_OPEN", "Status": "PENDING", "Quantity": 0.5, "Price": 2_000.0}]
    snapshot = {"cash_usd": 9_000.0, "cash_free_usd": 9_000.0, "pending_orders": resting}
    result = _runner(port).execute(_target(shorts=[ShortTarget(symbol="ETH", collateral_usd=1_000.0)]), snapshot, 10_000.0)
    assert result["operations"] == []


def test_an_order_the_client_refuses_is_rejected_and_the_plan_continues():
    port = MockExchangePort(initial_wallet={"USD": 10_000.0})
    place = port.place_order

    def refuse_btc(pair, side, qty, price=None, order_type=None):
        if pair == "BTC/USD":
            raise ValueError("order notional is below the BTC/USD minimum")
        return place(pair, side, qty, price, order_type)

    port.place_order = refuse_btc
    result = _runner(port).execute(_target(longs=[LongTarget(symbol="BTC", notional_usd=1_000.0),
                                                  LongTarget(symbol="ETH", notional_usd=1_000.0)]),
                                   {"cash_usd": 10_000.0, "cash_free_usd": 10_000.0}, 10_000.0)
    assert [o["status"] for o in result["operations"]] == ["REJECTED", "RESOLVED"]


def test_snapshot_free_cash_excludes_locked_usd():
    from tradebot.engine.state.snapshot import normalize_exchange_snapshot

    snapshot = normalize_exchange_snapshot({"SpotWallet": {"USD": {"Free": 700.0, "Lock": 300.0}}}, {"Positions": []}, {})
    assert snapshot["cash_usd"] == 1_000.0 and snapshot["cash_free_usd"] == 700.0
