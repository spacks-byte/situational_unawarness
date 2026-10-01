from src.engine.config import ExecutionConfig
from src.engine.risk.manager import RiskManager, RiskState


def test_risk_blocks_kill_switch_and_account_loss_limits():
    manager = RiskManager(
        ExecutionConfig(max_daily_loss_usd=200.0, max_drawdown_pct=0.10)
    )
    plan = {"close_longs": {}, "close_shorts": {}, "open_longs": {"BTC": 100.0}, "open_shorts": {}}

    decision = manager.evaluate(
        plan,
        {"cash_usd": 10000.0},
        total_equity_usd=10000.0,
        state=RiskState(kill_switch=True, daily_loss_usd=250.0, drawdown_pct=0.15),
    )

    assert not decision.allowed
    assert decision.reasons == ("kill_switch", "max_daily_loss", "max_drawdown")
