"""The strategy/engine contract does not require running another strategy."""
import pandas as pd

from tests.test_mm_fluctuation import frames
from tests.test_shared_portfolio import setup_account
from tests.test_strategy_bridge import _universe
from tradebot.engine import Engine
from tradebot.engine.execution.quotes import QuoteExecutor
from tradebot.engine.schema.models import QuoteBatch
from tradebot.engine.state.portfolio import MM
from tradebot.live.account import QuoteBridge
from tradebot.strategy.library.rxm import ResidualMomentum


def test_rxm_generate_preserves_its_existing_weight_output():
    data = _universe(n=10, days=50)
    strategy = ResidualMomentum()
    assert strategy.output_kind == "weights"
    pd.testing.assert_frame_equal(strategy.generate(data), strategy.generate_weights(data))


def test_mm_executes_through_engine_without_an_rxm_strategy(tmp_path):
    account, exchange, clock = setup_account(tmp_path)
    _, data = frames(clock.now())
    for rule in account.rules.values():
        rule["PricePrecision"] = 2
    bridge = QuoteBridge(account, lambda coin, start, end: data[coin].loc[start:end-pd.Timedelta(seconds=1)],
                         clock, account.config)
    batch = bridge({})
    assert isinstance(batch, QuoteBatch)
    assert batch.quotes
    engine = Engine(account.scoped(MM), clock=clock, quote_executor=QuoteExecutor(account, clock),
                    state_path=tmp_path / "mm-engine.db", audit_path=tmp_path / "mm-audit.jsonl")
    try:
        result = engine.run_once(bridge)
        assert result["status"] == "QUOTED"
        assert result["submitted"] > 0
        assert all(order["strategy"] == MM for order in account.active())
        assert exchange.calls.get("open_short", 0) == 0
        assert exchange.calls.get("close_short", 0) == 0
        assert engine.run_once(bridge)["status"] == "HOLD"
    finally:
        engine.close()
        account.store.close()



def test_cli_selects_independent_strategies():
    from tradebot.cli import _live_overrides, build_parser
    from tradebot.core.config import Settings
    from tradebot.live.account import selected_strategies
    for command in ("live", "replay"):
        for selection in ("rxm", MM, f"rxm,{MM}"):
            args = build_parser().parse_args([command, "--strategies", selection])
            config = _live_overrides(args, Settings.load("config/market-making.yaml"))
            assert selected_strategies(config) == selection.split(",")
