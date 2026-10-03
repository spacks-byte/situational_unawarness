import numpy as np
import pandas as pd
import pytest
from rxm_data import universe

from tradebot.research import validation as v


def test_permutation_keeps_returns_and_cross_section_and_leaves_the_past_alone():
    data = universe(5, days=20)
    start = next(iter(data.values())).index[96 * 10]
    shuffled = v.permute_bars(data, start, seed=7)
    for coin, df in data.items():
        before = df["close"][df.index < start]
        pd.testing.assert_series_equal(shuffled[coin]["close"][df.index < start], before, check_freq=False)
        real = np.log(df["close"]).diff()[df.index >= start]
        fake = np.log(shuffled[coin]["close"]).diff()[df.index >= start]
        np.testing.assert_allclose(np.sort(real), np.sort(fake))                 # same returns, new order
    order_btc = np.argsort(np.log(shuffled["BTC"]["close"]).diff()[shuffled["BTC"].index >= start].to_numpy())
    order_c1 = np.argsort(np.log(data["C1"]["close"]).diff()[data["C1"].index >= start].to_numpy())
    assert not np.array_equal(order_btc, order_c1)
    joint_real = np.corrcoef(np.log(data["BTC"]["close"]).diff()[1:], np.log(data["C1"]["close"]).diff()[1:])[0, 1]
    joint_fake = np.corrcoef(np.log(shuffled["BTC"]["close"]).diff()[1:], np.log(shuffled["C1"]["close"]).diff()[1:])[0, 1]
    assert joint_fake == pytest.approx(joint_real, abs=1e-9)                    # one shuffle for every coin


def test_walk_forward_picks_each_config_from_its_past_only():
    days = pd.date_range("2025-01-01", periods=100, freq="D", tz="UTC")
    returns = pd.DataFrame({"a": np.r_[np.full(50, 0.01), np.full(50, -0.01)],
                            "b": np.r_[np.full(50, -0.01), np.full(50, 0.01)]}, index=days)
    returns += np.random.default_rng(0).normal(0, 1e-4, returns.shape)
    oos, picks = v.walk_forward(returns, train_days=40, test_days=10)
    assert [p["config"] for p in picks][:2] == ["a", "a"]                       # it can't know "a" turns bad at day 50
    assert oos.index[0] == days[40] and len(oos) == 60


def test_in_sample_table_reports_the_plateau():
    grid = v.Grid({"k": [2, 3, 4], "tilt": [0.0]})
    days = pd.date_range("2025-01-01", periods=60, freq="D", tz="UTC")
    rng = np.random.default_rng(1)
    returns = pd.DataFrame({grid.label(c): rng.normal(0.001 * i, 0.01, 60) for i, c in enumerate(grid.configs)}, index=days)
    table = v.in_sample(returns, grid)
    middle = grid.label({"k": 3, "tilt": 0.0})
    assert table.at[middle, "plateau"] == pytest.approx(table.loc[[grid.label({"k": 2, "tilt": 0.0}),
                                                                   grid.label({"k": 4, "tilt": 0.0})], "sharpe"].mean())


def test_p_value_counts_the_real_result():
    assert v._p_value(np.array([0.1, 0.2, 0.3]), 1.0) == pytest.approx(0.25)
    assert v._p_value(np.array([2.0, 2.0, 0.3]), 1.0) == pytest.approx(0.75)


def test_config_returns_has_one_column_per_config():
    grid = v.Grid({"k": [2, 3], "tilt": [0.0, 0.3], "buffer": [0]})
    returns = v.config_returns(universe(10, days=70), grid, cost_bps=10)
    assert list(returns.columns) == [grid.label(c) for c in grid.configs]
    assert returns.index.hour.unique().tolist() == [0] and returns.notna().all().all()
