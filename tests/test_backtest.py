import pandas as pd
import pytest

from tradebot.backtest.simulator import run_backtest
from tradebot.backtest.windows import evaluate_windows
from tradebot.core.config import BacktestConfig, FeeSchedule
from tradebot.strategy.base import Strategy

IDX = pd.date_range("2026-01-01", periods=6, freq="15min", tz="UTC")


class Weights(Strategy):
    """Constant weight per coin, or an explicit weights frame."""

    name = "weights"

    def __init__(self, w):
        super().__init__(w=w)
        self.w = w

    def generate_weights(self, data):
        if isinstance(self.w, pd.DataFrame):
            return self.w
        index = next(iter(data.values())).index
        return pd.DataFrame({s: self.w for s in data}, index=index)


def bars(o, h, l, c, coin="X"):
    return {coin: pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=IDX[: len(o)], dtype=float)}


def filled(result):
    t = result.trades
    return t[t["filled"]].reset_index(drop=True)


def test_limit_buy_fills_only_when_price_trades_through():
    through = run_backtest(Weights(1.0), bars([100] * 3, [101] * 3, [99.9] * 3, [100] * 3), "15m",
                           BacktestConfig(rebalance_band=0.2))
    at_limit = bars([100] * 3, [101] * 3, [100] * 3, [100] * 3)
    strict = run_backtest(Weights(1.0), at_limit, "15m", BacktestConfig(rebalance_band=0.2))
    touch = run_backtest(Weights(1.0), at_limit, "15m", BacktestConfig(rebalance_band=0.2, limit_fill="touch"))

    buy = filled(through).iloc[0]
    assert buy["price"] == 100.0 and buy["order_type"] == "LIMIT"
    assert buy["fee"] == pytest.approx(buy["value"] * 0.0005)
    assert filled(strict).empty
    assert len(filled(touch)) == 1


def test_unfilled_order_is_cancelled_and_retried_at_new_close():
    data = bars([100, 100, 102, 104, 104], [101, 103, 105, 105, 105], [100, 101, 103, 103.5, 103],
                [100, 102, 104, 104, 104])
    result = run_backtest(Weights(1.0), data, "15m", BacktestConfig(rebalance_band=0.2))

    assert list(result.trades["filled"]) == [False, False, True]
    assert list(result.trades["price"]) == [100.0, 102.0, 104.0]


def test_rotation_waits_one_bar_because_cash_is_locked():
    flat = bars([100] * 5, [101] * 5, [99] * 5, [100] * 5)
    data = {"A": flat["X"], "B": flat["X"].copy()}
    w = pd.DataFrame({"A": [1, 1, 0, 0, 0], "B": [0, 0, 1, 1, 1]}, index=IDX[:5], dtype=float)
    trades = filled(run_backtest(Weights(w), data, "15m", BacktestConfig(rebalance_band=0.2)))

    sell = trades[(trades.symbol == "A") & (trades.side == "SELL")].time.iloc[0]
    buy = trades[(trades.symbol == "B") & (trades.side == "BUY")].time.iloc[0]
    assert buy - sell == pd.Timedelta("15min")


def test_short_opens_pay_short_open_fee_and_close_at_market():
    w = pd.DataFrame({"X": [-0.5, -0.5, 0, 0, 0]}, index=IDX[:5], dtype=float)
    data = bars([100, 100, 95, 90, 90], [101, 101, 96, 91, 91], [99, 99, 94, 89, 89], [100, 96, 92, 90, 90])
    result = run_backtest(Weights(w), data, "15m", BacktestConfig(rebalance_band=0.2))
    short, cover = filled(result).to_dict("records")

    assert short["side"] == "SHORT" and short["fee"] == pytest.approx(50_000 * 0.001)
    assert cover["side"] == "COVER" and cover["order_type"] == "MARKET" and cover["price"] == 90.0
    assert cover["fee"] == pytest.approx(45_000 * 0.001)
    assert result.equity.iloc[-1] == pytest.approx(100_000 + 500 * 10 - 50 - 45)


@pytest.mark.parametrize("opens,highs,expected_px", [
    ([100, 100, 110, 210], [100, 101, 111, 215], 210.0),   # gapped through liquidation at the open
    ([100, 100, 110, 110], [100, 101, 111, 205], 200.0),   # intrabar wick: liquidated at 2x entry
])
def test_one_x_short_is_liquidated_and_loss_capped_at_collateral(opens, highs, expected_px):
    data = bars(opens, highs, [o - 1 for o in opens], opens)
    result = run_backtest(Weights(-0.5), data, "15m", BacktestConfig(rebalance_band=0.3))
    liquidation = filled(result).query("side == 'LIQUIDATE'").iloc[0]

    assert liquidation["price"] == expected_px
    assert result.equity.iloc[3] <= 50_000  # the $50k collateral is gone; no more than that is lost
    assert result.equity.iloc[3] >= 49_900


def test_signal_fills_no_earlier_than_next_bar():
    index = pd.date_range("2026-01-01", periods=300, freq="15min", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=index)
    w = pd.DataFrame({"X": 0.0}, index=index)
    w.iloc[100:] = 1.0
    first = run_backtest(Weights(w), {"X": df}, "15m", BacktestConfig()).trades.iloc[0]

    assert index.get_loc(first["time"]) == 101


def test_fee_schedule_is_configurable():
    config = BacktestConfig(rebalance_band=0.2, fees=FeeSchedule(spot_maker=0.0))
    result = run_backtest(Weights(1.0), bars([100] * 3, [101] * 3, [99] * 3, [100] * 3), "15m", config)

    assert filled(result)["fee"].sum() == 0.0


def test_windows_start_flat_and_ignore_warmup_signals():
    index = pd.date_range("2026-01-01", periods=96 * 20, freq="15min", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=index)
    windows = evaluate_windows(Weights(1.0), {"X": df}, "15m", BacktestConfig(),
                               window_days=7, step_days=1, warmup_days=5)

    assert len(windows) > 0
    assert (windows["total_return"] <= 0).all()  # flat prices: only fees, starting from $100k cash
    assert windows["total_return"].min() > -0.001
