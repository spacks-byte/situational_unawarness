from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from rxm_data import universe

from tradebot.backtest.simulator import run_backtest
from tradebot.core.config import BacktestConfig
from tradebot.strategy.library.rxm import PRESETS, UNIVERSE, ResidualMomentum

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "binance"


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_no_lookahead(name):
    """Weights up to a time must not change when later bars are added."""
    data = universe()
    cut = next(iter(data.values())).index[96 * 60]
    full = ResidualMomentum.preset(name).generate_weights(data)
    early = ResidualMomentum.preset(name).generate_weights({c: df[df.index <= cut] for c, df in data.items()})
    pd.testing.assert_frame_equal(full.loc[:cut], early)


def test_book_shape_and_gross():
    w = ResidualMomentum(k=3, gross=1.0, tilt=0.3).generate_weights(universe(12))
    daily = w[(w.index.hour == 0) & (w.index.minute == 0)].iloc[-10:]
    assert ((daily > 0).sum(axis=1) == 3).all() and ((daily < 0).sum(axis=1) == 3).all()
    np.testing.assert_allclose(daily.clip(lower=0).sum(axis=1), 0.65)
    np.testing.assert_allclose(daily.clip(upper=0).sum(axis=1), -0.35)


def test_rank_buffer_trades_less():
    data = universe(14)
    plain = ResidualMomentum(k=3).generate_weights(data)
    buffered = ResidualMomentum(k=3, buffer=2).generate_weights(data)
    swaps = lambda w: ((w != 0).astype(int).diff().abs().sum().sum())
    assert swaps(buffered) < swaps(plain)


def test_ineligible_stalled_coin_gets_no_weight():
    data = universe(12)
    frozen = data["C5"].copy()
    frozen.loc[frozen.index[-96 * 3]:, ["close", "volume"]] = [frozen["close"].iloc[-96 * 3], 0.0]
    data["C5"] = frozen
    assert ResidualMomentum(k=3).generate_weights(data)["C5"].iloc[-1] == 0


def test_lockin_scales_exposure_after_the_trigger_only():
    data = universe(14)
    start = next(iter(data.values())).index[96 * 20]
    strategy = ResidualMomentum(k=3, gross=0.9, tilt=0.0)
    base = run_backtest(strategy, data, "15m", BacktestConfig(), trade_start=start)
    trigger = 0.5 * (base.equity / 100_000 - 1).max()
    locked = run_backtest(strategy, data, "15m", BacktestConfig(lockin_return=trigger, lockin_scale=0.3),
                          trade_start=start)
    hit = base.equity.index[np.argmax(base.equity.to_numpy() / 100_000 - 1 >= trigger)]
    np.testing.assert_allclose(locked.equity.loc[:hit], base.equity.loc[:hit])
    assert locked.exposure.iloc[-96:].max() < 0.45


@pytest.mark.skipif(not (DATA_DIR / "klines" / "15m" / "BTCUSDT.parquet").exists(), reason="market data not downloaded")
def test_presets_reproduce_the_validated_weights():
    from tradebot.data.loader import load_universe

    gold = pd.read_csv(Path(__file__).with_name("golden_rxm_weights.csv"), parse_dates=["time"])
    data = load_universe(UNIVERSE, "15m", "2026-06-01", "2026-10-01", data_dir=DATA_DIR)
    for name in PRESETS:
        w = ResidualMomentum.preset(name).generate_weights(data)
        for time, rows in gold[gold["preset"] == name].groupby("time"):
            expected = rows.set_index("symbol")["weight"]
            np.testing.assert_allclose(w.loc[time, expected.index], expected, atol=1e-12)
            assert (w.loc[time].drop(expected.index).abs() < 1e-15).all()
