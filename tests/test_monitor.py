from src.engine.monitor.position_monitor import PositionMonitor
from src.engine.schema.models import LongTarget, ShortTarget, TargetPortfolio


def test_monitor_emits_long_and_short_exit_alerts():
    monitor = PositionMonitor()
    target = TargetPortfolio(
        strategy_id="s",
        strategy_version="v1",
        signal_id="sig",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        longs=[LongTarget(symbol="BTC", notional_usd=1000.0, stop_loss_pct=5.0)],
        shorts=[ShortTarget(symbol="ETH", collateral_usd=500.0, take_profit_pct=10.0)],
    )

    alerts = monitor.evaluate(
        target,
        prices={"BTC": 90.0, "ETH": 80.0},
        entry_prices={"BTC": 100.0, "ETH": 100.0},
    )

    assert alerts == [
        {"symbol": "BTC", "side": "LONG", "action": "FLATTEN", "reason": "STOP_LOSS"},
        {"symbol": "ETH", "side": "SHORT", "action": "FLATTEN", "reason": "TAKE_PROFIT"},
    ]
