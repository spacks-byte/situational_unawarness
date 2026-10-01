from src.engine.config import ExecutionConfig
from src.engine.reconcile.plan import compute_rebalance_plan
from src.engine.risk.manager import RiskManager


def test_risk_rejects_order_that_breaches_cash_reserve():
    config = ExecutionConfig(min_cash_reserve_usd=500.0, max_order_value_usd=5000.0)
    manager = RiskManager(config)
    plan = {
        "close_longs": {},
        "close_shorts": {},
        "open_longs": {"BTC": 9600.0},
        "open_shorts": {},
    }

    decision = manager.evaluate(plan, {"cash_usd": 1000.0}, total_equity_usd=10000.0)

    assert not decision.allowed
    assert "cash_reserve" in decision.reasons


def test_risk_rejects_shorting_when_disabled():
    config = ExecutionConfig(supports_shorting=False)
    manager = RiskManager(config)
    plan = {
        "close_longs": {},
        "close_shorts": {},
        "open_longs": {},
        "open_shorts": {"BTC": 100.0},
    }

    decision = manager.evaluate(plan, {"cash_usd": 10000.0}, total_equity_usd=10000.0)

    assert not decision.allowed
    assert "shorting_disabled" in decision.reasons
