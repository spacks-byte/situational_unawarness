from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradebot.core.config import Settings
from tradebot.core.symbols import to_coin
from tradebot.data.binance_vision import klines_path

from tradebot.core.config import BacktestConfig
from tradebot.strategy.library.rxm import PRESETS, ResidualMomentum, preset
from tradebot.backtest.windows import evaluate_windows

NEUTRAL = preset("neutral")[0]
COMP = preset("comp")[0]


def _universe(n_coins=12, bars=24 * 4 * 40, seed=0):
    """15m bars, random walks with different drifts so momentum ranks are non-trivial."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=bars, freq="15min", tz="UTC")
    data = {}
    for i in range(n_coins):
        r = rng.normal((i - n_coins / 2) * 2e-5, 0.004, bars)
        close = 100 * np.exp(np.cumsum(r))
        vol = rng.uniform(50, 150, bars)
        data["BTCUSDT" if i == 0 else f"C{i}USDT"] = pd.DataFrame({
            "open": close, "high": close * 1.001, "low": close * 0.999, "close": close,
            "volume": vol, "taker_buy_base": vol * 0.5}, index=idx)
    return data


@pytest.mark.parametrize("params", [NEUTRAL, COMP])
def test_no_lookahead(params):
    data = _universe(n_coins=14)
    w = ResidualMomentum(**params).generate_weights(data)
    cut = len(w) // 2
    rng = np.random.default_rng(1)
    shocked = {s: df.copy() for s, df in data.items()}
    for df in shocked.values():
        df.iloc[cut:, :4] *= rng.uniform(0.5, 1.5, size=(len(df) - cut, 1))   # scramble the future
    w2 = ResidualMomentum(**params).generate_weights(shocked)
    pd.testing.assert_frame_equal(w.iloc[:cut], w2.iloc[:cut])


def test_presets_shape_the_book():
    data = _universe(n_coins=14)
    last = ResidualMomentum(**COMP).generate_weights(data).iloc[-1]
    assert (last > 0).sum() == 3 and (last < 0).sum() == 3
    assert abs(last[last > 0].sum() - 0.65) < 1e-9 and abs(last[last < 0].sum() + 0.35) < 1e-9   # tilt 0.3
    w = ResidualMomentum(**NEUTRAL).generate_weights(data)
    assert (w.abs().sum(axis=1) <= 1 + 1e-9).all()
    assert abs(w.iloc[-1].sum()) < 1e-9                           # neutral: long = short


def test_weights_change_only_at_rebalance():
    w = ResidualMomentum(**COMP).generate_weights(_universe())
    changed = w.diff().abs().sum(axis=1) > 0
    assert ((changed.index.hour == 0) & (changed.index.minute == 0))[changed.to_numpy()].all()


def test_stalled_feed_gets_no_weight():
    data = _universe(n_coins=14)
    frozen = "C3USDT"
    df = data[frozen]
    t0 = len(df) // 2
    df.iloc[t0:, :4] = df.iloc[t0, 3]          # price stuck from t0 on
    df.iloc[t0:, df.columns.get_loc("volume")] = 0.0
    w = ResidualMomentum(**NEUTRAL).generate_weights(data)
    assert (w[frozen].iloc[t0 + 24 * 4 * 2:] == 0).all()          # ineligible once stale
    assert (w.abs().max(axis=1) < 0.45 + 1e-9).all()              # nobody takes a whole side


def test_small_universe_never_long_and_short_same_coin():
    w = ResidualMomentum(**NEUTRAL).generate_weights(_universe(n_coins=7))
    last = w.iloc[-1]
    assert (last > 0).sum() == 3 and (last < 0).sum() == 3       # k_eff = min(5, 7 // 2) = 3
    assert abs(last.abs().sum() - 0.9) < 1e-9


def test_precomputed_weights_match_per_window():
    """backtest/tune.py reuses full-history weights; this must equal recomputing per window."""
    from tradebot.research.tune import FixedWeights
    data = _universe(n_coins=10, bars=24 * 4 * 75)
    cfg = BacktestConfig(**preset("comp")[1])
    strat = ResidualMomentum(**COMP)
    a = evaluate_windows(strat, data, "15m", cfg, window_days=14, step_days=7, warmup_days=45)
    b = evaluate_windows(FixedWeights(strat.generate_weights(data)), data, "15m", cfg,
                         window_days=14, step_days=7, warmup_days=45)
    assert len(a) >= 2
    pd.testing.assert_frame_equal(a, b)


GOLDEN = Path(__file__).with_name("golden_rxm_weights.csv")
KLINES_15M = klines_path(Settings.load().data.dir, "BTC", "15m")


@pytest.mark.skipif(not KLINES_15M.exists(), reason="local parquet data not downloaded")
def test_frozen_presets_match_golden_weights():
    """The frozen presets must reproduce the validated weights (spec v1.0) on real data."""
    from tradebot.data.loader import load_universe
    from tradebot.strategy.library.rxm import UNIVERSE
    gold = pd.read_csv(GOLDEN, parse_dates=["time"])
    gold["symbol"] = gold["symbol"].map(to_coin)          # golden file uses Binance symbols (ETHUSDT)
    data = load_universe(UNIVERSE, "15m", "2026-06-01", "2026-10-01")
    for name in PRESETS:
        w = ResidualMomentum(**preset(name)[0]).generate_weights(data)
        g = gold[gold.preset == name]
        for t, rows in g.groupby("time"):
            got = w.loc[pd.Timestamp(t).tz_convert("UTC") if pd.Timestamp(t).tzinfo else pd.Timestamp(t, tz="UTC")]
            exp = rows.set_index("symbol")["weight"]
            np.testing.assert_allclose(got.reindex(exp.index).to_numpy(), exp.to_numpy(), atol=1e-12)
            assert (got.drop(exp.index).abs() < 1e-15).all()
