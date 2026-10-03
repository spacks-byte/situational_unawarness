from datetime import UTC, datetime

from tradebot.engine.schema.models import LongTarget, ShortTarget, TargetPortfolio


def test_reconcile_closes_reductions_before_opens():
    target = TargetPortfolio(
        strategy_id="s",
        strategy_version="v1",
        signal_id="sig-1",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", notional_usd=1000.0), LongTarget(symbol="ETH", notional_usd=2000.0)],
        shorts=[ShortTarget(symbol="SOL", collateral_usd=300.0)],
    )
    actual = {
        "longs": {"BTC": 2000.0, "ETH": 0.0},
        "shorts": {"SOL": 800.0},
        "cash_usd": 5000.0,
    }

    from tradebot.engine.reconcile.plan import compute_rebalance_plan

    plan = compute_rebalance_plan(target, actual, total_equity_usd=10000.0)
    assert plan["close_longs"]["BTC"] == 1000.0
    assert plan["open_longs"]["ETH"] == 2000.0
    assert plan["close_shorts"]["SOL"] == 500.0
    assert plan["open_shorts"] == {}


def test_reconcile_uses_weight_when_notional_is_missing():
    target = TargetPortfolio(
        strategy_id="s",
        strategy_version="v1",
        signal_id="sig-2",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", weight=0.25)],
    )
    actual = {"longs": {}, "shorts": {}, "cash_usd": 10000.0}

    from tradebot.engine.reconcile.plan import compute_rebalance_plan

    plan = compute_rebalance_plan(target, actual, total_equity_usd=10000.0)
    assert plan["open_longs"]["BTC"] == 2500.0


def test_reconcile_does_not_duplicate_pending_open_orders():
    target = TargetPortfolio(
        strategy_id="s",
        strategy_version="v1",
        signal_id="sig-3",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", notional_usd=1000.0)],
        shorts=[ShortTarget(symbol="ETH", collateral_usd=500.0)],
    )
    actual = {
        "longs": {},
        "shorts": {},
        "pending_orders": [
            {"Pair": "BTC/USD", "Side": "BUY", "Type": "LIMIT", "Status": "PENDING", "Quantity": 0.02, "Price": 50000.0},
            {"Pair": "ETH/USD", "Side": "SHORT_OPEN", "Status": "PENDING", "Collateral": 500.0},
        ],
    }

    from tradebot.engine.reconcile.plan import compute_rebalance_plan

    plan = compute_rebalance_plan(target, actual, total_equity_usd=10000.0)
    assert plan["open_longs"] == {}
    assert plan["open_shorts"] == {}
