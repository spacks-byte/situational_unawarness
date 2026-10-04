import numpy as np
import pandas as pd

from tradebot.backtest.simulator import run_backtest
from tradebot.core.config import BacktestConfig
from tradebot.strategy.library.rxm import ResidualMomentum, preset
from tradebot.strategy.base import Strategy
from tests.test_rxm import _universe

FINAL = preset("neutral")[0]


class _BuyAndHold(Strategy):
    """Decides to buy at the close of the second bar, so the first order goes out during the third."""

    def generate_weights(self, data):
        idx = next(iter(data.values())).index
        return pd.DataFrame({s: np.where(idx >= idx[1], 0.5, 0.0) for s in data}, index=idx)


def _bars(opens, highs, lows, closes):
    idx = pd.date_range("2026-01-01", periods=len(opens), freq="15min", tz="UTC")
    return {"AAAUSDT": pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1.0},
                                    index=idx)}


def test_latency_off_is_identical():
    data = _universe(n_coins=14)
    a = run_backtest(ResidualMomentum(**FINAL), data, "15m", BacktestConfig())
    b = run_backtest(ResidualMomentum(**FINAL), data, "15m", BacktestConfig(latency_bars=0))
    np.testing.assert_array_equal(a.equity.to_numpy(), b.equity.to_numpy())


def test_stale_limit_crossed_on_arrival_pays_taker_at_the_limit():
    # The price falls 1% while the order is in flight: the stale buy limit (99.95) is above the market (99)
    data = _bars([100, 100, 99, 99], [100.1, 100.1, 99.1, 99.1], [99.9, 99.9, 98.9, 98.9], [100, 99, 99, 99])
    cfg = BacktestConfig(limit_offset_bps=5, latency_bars=1)
    buy = run_backtest(_BuyAndHold(), data, "15m", cfg).trades.query("filled").iloc[0]
    assert buy.price == 100 * (1 - 5e-4)                    # no price improvement
    np.testing.assert_allclose(buy.fee, buy.value * cfg.fees.spot_taker)


def test_stale_limit_left_behind_does_not_fill():
    # The price rises 1% while the order is in flight: the stale buy limit never trades
    data = _bars([100, 100, 101, 101], [100.1, 101, 101.1, 101.1], [99.9, 99.9, 100.9, 100.9], [100, 101, 101, 101])
    trades = run_backtest(_BuyAndHold(), data, "15m", BacktestConfig(limit_offset_bps=5, latency_bars=1)).trades
    assert not trades[trades.time == data["AAAUSDT"].index[2]].filled.any()
