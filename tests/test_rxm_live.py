"""RXM on the live engine: the adapter, the bar buffer and the limit-only engine fixes. No network."""
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from rxm_data import universe

from tradebot.core.clock import SimClock
from tradebot.core.config import ExecutionConfig
from tradebot.engine import Engine
from tradebot.engine.execution.runner import ExecutionRunner
from tradebot.engine.schema import LongTarget, ShortTarget, TargetPortfolio
from tradebot.engine.state.intent_store import IntentJournal
from tradebot.exchange.mock import MockExchangePort
from tradebot.live.bars import BarBuffer
from tradebot.live.rxm import CompetitionStrategy, SnapshotRejected
from tradebot.strategy.library.rxm import ResidualMomentum

START = datetime(2026, 2, 25, 0, 16, tzinfo=UTC)


def local_fetch(frames, hold_back=None):
    def fetch(coin, start, end):
        df = frames[coin]
        if hold_back and coin in hold_back:
            df = df[df.index < hold_back[coin]]
        return df[(df.index >= start) & (df.index < end)]
    return fetch


def make(tmp_path, frames=None, mode="comp", hold_back=None, now=START):
    frames = frames or universe()
    clock = SimClock(now)
    strategy = CompetitionStrategy(BarBuffer(list(frames), local_fetch(frames, hold_back)), mode, clock=clock,
                                   state_path=tmp_path / "state.json")
    return strategy, clock, frames


def snapshot(equity=100_000.0, prices=None, longs=None, shorts=None, pending=None):
    return {"equity_usd": equity, "cash_usd": equity, "cash_free_usd": equity, "longs": longs or {},
            "shorts": shorts or {}, "prices": prices or {}, "entry_prices": {}, "pending_orders": pending or []}


def prices_of(frames):
    return {c: float(df["close"].iloc[-1]) for c, df in frames.items()}


# ---------------------------------------------------------------------------- the adapter
def test_live_weights_equal_backtest_weights(tmp_path):
    strategy, _, frames = make(tmp_path)
    strategy(snapshot(prices=prices_of(frames)))
    backtest = ResidualMomentum.preset("comp").generate_weights(
        {c: df[df.index < pd.Timestamp("2026-02-25T00:15", tz="UTC")] for c, df in frames.items()}).iloc[-1]
    pd.testing.assert_series_equal(strategy.weights, backtest.fillna(0.0), check_names=False)


def test_targets_are_priced_limits_and_the_book_is_held_inside_the_band(tmp_path):
    strategy, _, frames = make(tmp_path)
    prices = prices_of(frames)
    first = strategy(snapshot(prices=prices))
    assert first.longs and first.shorts
    assert all(t.limit_price == pytest.approx(prices[t.symbol] * (1 - 5e-4)) for t in first.longs)
    assert all(t.limit_price == pytest.approx(prices[t.symbol] * (1 + 5e-4)) for t in first.shorts)
    held = {t.symbol: t.weight * 100_000 for t in first.longs}
    again = strategy(snapshot(prices=prices, longs=held, shorts={t.symbol: t.collateral_usd for t in first.shorts}))
    assert again.signal_id == first.signal_id                               # nothing off target: no new signal


def test_requotes_an_unfilled_coin_at_the_next_bar(tmp_path):
    strategy, clock, frames = make(tmp_path)
    first = strategy(snapshot(prices=prices_of(frames)))
    clock.advance(15 * 60)
    second = strategy(snapshot(prices=prices_of(frames)))                   # nothing filled
    assert second.signal_id.startswith(first.signal_id + "-r")


def test_no_data_holds_the_book_instead_of_an_empty_target(tmp_path):
    strategy, _, _ = make(tmp_path, frames={"BTC": universe()["BTC"].iloc[:10]})
    target = strategy(snapshot(longs={"ETH": 5_000.0}, shorts={"SOL": 3_000.0}))
    assert [t.symbol for t in target.longs] == ["ETH"] and [t.symbol for t in target.shorts] == ["SOL"]


def test_a_late_decision_bar_is_waited_for(tmp_path):
    frames = universe()
    decision = pd.Timestamp("2026-02-25T00:00", tz="UTC")
    strategy, clock, _ = make(tmp_path, frames, hold_back={"C3": decision})
    strategy(snapshot(prices=prices_of(frames)))
    assert strategy.weights is None                                         # waiting for C3's 00:00 bar
    clock.advance(30 * 60)
    strategy(snapshot(prices=prices_of(frames)))
    assert strategy.weights is not None and strategy.weights["C3"] == 0     # grace over: decided without it


def test_lockin_scales_and_survives_a_restart(tmp_path):
    strategy, clock, frames = make(tmp_path)
    prices = prices_of(frames)
    strategy(snapshot(prices=prices))
    full = dict(strategy.scaled_weights())
    strategy(snapshot(equity=105_900, prices=prices))
    strategy(snapshot(equity=106_100, prices=prices))
    assert not strategy.locked
    target = strategy(snapshot(equity=106_200, prices=prices))
    assert strategy.locked and target.signal_id.endswith("-L")
    raw = strategy.weights.abs().sum()
    assert sum(abs(w) for w in strategy.scaled_weights().values()) == pytest.approx(0.3 * raw)
    assert sum(abs(w) for w in full.values()) == pytest.approx(min(raw, 0.98))
    restarted, _, _ = make(tmp_path, frames, now=START + timedelta(hours=1))
    restarted(snapshot(equity=101_000, prices=prices))
    assert restarted.locked and restarted.state.get("start_equity") == 100_000


def test_broken_snapshot_is_rejected(tmp_path):
    strategy, _, frames = make(tmp_path)
    strategy(snapshot(prices=prices_of(frames)))
    with pytest.raises(SnapshotRejected):
        strategy(snapshot(equity=20_000, prices=prices_of(frames)))        # a partial read, not a real loss


def test_mode_switch_is_refused(tmp_path):
    strategy, _, frames = make(tmp_path)
    strategy(snapshot(prices=prices_of(frames)))
    with pytest.raises(ValueError):
        make(tmp_path, frames, mode="neutral")


def test_engine_runs_the_strategy_end_to_end(tmp_path):
    strategy, clock, frames = make(tmp_path)
    port = MockExchangePort(initial_wallet={"USD": 100_000.0}, clock=clock,
                            tickers={f"{c}/USD": p for c, p in prices_of(frames).items()})
    config = ExecutionConfig(dry_run=False, max_per_symbol_exposure=0.6, max_total_short_collateral_usd=60_000,
                             max_order_value_usd=100_000, max_child_order_pct=0.5, min_cash_reserve_usd=200)
    engine = Engine(port, config=config, clock=clock, state_path=tmp_path / "e.db", audit_path=tmp_path / "a.jsonl")
    result = engine.run_once(strategy)
    engine.close()
    assert result["status"] == "EXECUTED"
    assert {o["kind"] for o in result["operations"]} == {"open_long", "open_short"}


# ---------------------------------------------------------------------------- limit-only engine fixes
def target(signal, longs=(), shorts=()):
    return TargetPortfolio(strategy_id="t", strategy_version="1", signal_id=signal, timestamp=datetime.now(UTC),
                           longs=list(longs), shorts=list(shorts))


def runner(port):
    config = ExecutionConfig(dry_run=False, min_cash_reserve_usd=0, max_per_symbol_exposure=1.0, max_order_value_usd=1e6,
                             max_total_short_collateral_usd=1e6, max_child_order_pct=1.0)
    return ExecutionRunner(port, IntentJournal(memory=True), config)


def test_a_rejected_order_does_not_stop_the_rest_of_the_plan():
    port = MockExchangePort(initial_wallet={"USD": 10_000.0})
    place = port.place_order
    port.place_order = lambda pair, side, qty, price=None, order_type=None: (
        {"Success": False, "ErrMsg": "insufficient balance"} if pair == "BTC/USD" else place(pair, side, qty, price, order_type))
    result = runner(port).execute(target("s", longs=[LongTarget(symbol="BTC", notional_usd=1_000.0),
                                                     LongTarget(symbol="ETH", notional_usd=1_000.0)]),
                                  {"cash_usd": 10_000.0, "cash_free_usd": 10_000.0}, 10_000.0)
    statuses = {o["symbol"]: o["status"] for o in result["operations"]}
    assert statuses == {"BTC": "REJECTED", "ETH": "RESOLVED"} and result["status"] == "EXECUTED"


def test_buys_are_capped_to_the_cash_that_is_free_now():
    port = MockExchangePort(initial_wallet={"USD": 1_000.0, "BTC": 1.0})
    snap = {"cash_usd": 1_000.0, "cash_free_usd": 1_000.0, "longs": {"BTC": 50_000.0}}
    result = runner(port).execute(target("s", longs=[LongTarget(symbol="ETH", notional_usd=40_000.0)]), snap, 51_000.0)
    buy = next(o for o in result["operations"] if o["kind"] == "open_long")
    assert buy["amount_usd"] <= 1_000.0 and buy["status"] == "RESOLVED"


def test_a_resting_sell_is_not_sent_twice():
    port = MockExchangePort(initial_wallet={"USD": 0.0, "BTC": 1.0})
    resting = [{"Pair": "BTC/USD", "Side": "SELL", "Status": "PENDING", "Quantity": 1.0, "Price": 50_000.0}]
    result = runner(port).execute(target("s"), {"cash_usd": 0.0, "longs": {"BTC": 50_000.0}, "pending_orders": resting},
                                  50_000.0)
    assert result["operations"] == []


def test_a_full_short_exit_closes_100_percent():
    port = MockExchangePort(initial_wallet={"USD": 10_000.0}, tickers={"ETH/USD": 2_000.0})
    port.open_short("ETH", 1_000.0)
    port.tickers["ETH/USD"] = 2_500.0                                       # losing: collateral / price would under-close
    runner(port).execute(target("s"), {"cash_usd": 9_000.0, "shorts": {"ETH": 1_000.0}}, 9_750.0)
    assert port.short_positions == []


def test_a_resting_short_open_without_collateral_is_not_sent_twice():
    port = MockExchangePort(initial_wallet={"USD": 10_000.0})
    resting = [{"Pair": "ETH/USD", "Side": "SHORT_OPEN", "Status": "PENDING", "Quantity": 0.5, "Price": 2_000.0}]
    result = runner(port).execute(target("s", shorts=[ShortTarget(symbol="ETH", collateral_usd=1_000.0)]),
                                  {"cash_usd": 9_000.0, "cash_free_usd": 9_000.0, "pending_orders": resting}, 10_000.0)
    assert result["operations"] == []
