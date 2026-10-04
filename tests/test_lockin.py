import numpy as np

from tradebot.backtest.simulator import run_backtest
from tradebot.core.config import BacktestConfig
from tradebot.strategy.library.rxm import ResidualMomentum, preset
from tests.test_rxm import _universe

FINAL = preset("neutral")[0]


def test_lockin_off_is_identical():
    data = _universe(n_coins=14)
    a = run_backtest(ResidualMomentum(**FINAL), data, "15m", BacktestConfig())
    b = run_backtest(ResidualMomentum(**FINAL), data, "15m", BacktestConfig(lockin_return=0.0))
    np.testing.assert_array_equal(a.equity.to_numpy(), b.equity.to_numpy())


def test_lockin_cuts_exposure_after_trigger_only():
    data = _universe(n_coins=14)
    start = next(iter(data.values())).index[24 * 4 * 20]
    base = run_backtest(ResidualMomentum(**FINAL), data, "15m", BacktestConfig(), trade_start=start)
    ret = base.equity / 100_000 - 1
    trig = 0.5 * ret.max()                                  # a threshold the path is sure to cross
    assert trig > 0
    lock = run_backtest(ResidualMomentum(**FINAL), data, "15m",
                        BacktestConfig(lockin_return=trig, lockin_scale=0.3), trade_start=start)
    first = ret.index[np.argmax(ret.to_numpy() >= trig)]
    before = lock.exposure[lock.exposure.index <= first]
    after = lock.exposure[lock.exposure.index > first + (first - first.floor("D")) + np.timedelta64(2, "D")]
    np.testing.assert_allclose(before.to_numpy(), base.exposure[base.exposure.index <= first].to_numpy())
    assert after.max() < 0.45                                # ~0.3 x 0.9 gross after lock (drift tolerance)
