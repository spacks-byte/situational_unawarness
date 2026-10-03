"""
Four-step strategy validation, after Timothy Masters' permutation tests (neurotrader,
"How I Develop Trading Strategies"):

1. In-sample excellence. Optimise a parameter grid on the in-sample period. The winner must be
   clearly good, and its grid neighbours too: a plateau, not a lone spike.
2. In-sample permutation test. Shuffle the order of the in-sample bars, with one shuffle shared by
   every coin, so the cross-section survives and every time-series pattern is destroyed. Re-run the
   same optimisation, many times. p = the share of shuffles whose best score matches or beats the
   real best score. The test charges for the selection bias of picking the best config from the
   grid. Momentum lives in the time order, so on shuffled data a real edge should vanish.
3. Walk-forward test. Re-optimise on a rolling training window, trade the next block with the winner,
   and roll on. Only out-of-sample blocks count.
4. Walk-forward permutation test. Shuffle only the bars after the first training window and re-run
   the whole walk-forward. p = the share of shuffles that match or beat the real walk-forward score.

Fast path: the strategy decides once a day, so each config is scored on daily closes. The weights
decided at the 00:00 UTC bar earn the next 24 h close-to-close return, minus `cost_bps` on every unit
of turnover. The signal itself runs on the real 15m bars. Path-dependent parts are left to the full
simulator (`tradebot research windows`): the lock-in, limit-order fills and short liquidation. So
this tests the signal and the portfolio rules, not the execution.
"""
from __future__ import annotations

import itertools
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from tradebot.strategy.library.rxm import ResidualMomentum

DAYS_PER_YEAR = 365
DEFAULT_GRID = {"k": [2, 3, 4, 5], "tilt": [0.0, 0.3], "buffer": [0, 2]}


@dataclass(frozen=True)
class Grid:
    params: dict[str, list]

    @property
    def configs(self) -> list[dict]:
        keys = list(self.params)
        return [dict(zip(keys, values)) for values in itertools.product(*self.params.values())]

    def label(self, config: dict) -> str:
        return " ".join(f"{k}={v}" for k, v in config.items())


# ---------------------------------------------------------------------------- daily returns
def config_returns(data: dict[str, pd.DataFrame], grid: Grid, cost_bps: float) -> pd.DataFrame:
    """Daily net returns of every grid config (columns are config labels). Scores are computed once."""
    score, sigma = ResidualMomentum().scores(data)
    days = score.index[(score.index.hour == 0) & (score.index.minute == 0)]
    close = pd.DataFrame({c: df["close"] for c, df in data.items()}).reindex(score.index).ffill()
    forward = close.loc[days].shift(-1) / close.loc[days] - 1          # earned by the weights decided at day t
    out = {}
    for config in grid.configs:
        w = ResidualMomentum(**config).weights(score.loc[days], sigma.loc[days])
        turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
        out[grid.label(config)] = ((w * forward).sum(axis=1) - turnover * cost_bps / 1e4).where(w.abs().sum(axis=1) > 0)
    returns = pd.DataFrame(out).iloc[:-1]                              # the last day has no forward return
    return returns.loc[returns.notna().any(axis=1).idxmax():].fillna(0.0)   # from the first day with a position


def sharpe(returns: pd.Series | pd.DataFrame):
    return returns.mean() / returns.std() * np.sqrt(DAYS_PER_YEAR)


def profit_factor(returns: pd.Series) -> float:
    gains, losses = returns[returns > 0].sum(), -returns[returns < 0].sum()
    return gains / losses if losses > 0 else np.inf


# ---------------------------------------------------------------------------- permutation
def permute_bars(data: dict[str, pd.DataFrame], start: pd.Timestamp, seed: int) -> dict[str, pd.DataFrame]:
    """Shuffle the bar order from `start` on, with one shuffle shared by every coin.

    Each coin's log returns and volumes are reordered by the same permutation and chained back into
    prices from the last unshuffled close. Distributions and cross-coin correlation are unchanged;
    trends, momentum and volatility clustering are destroyed.
    """
    close = pd.DataFrame({c: df["close"] for c, df in data.items()}).sort_index().ffill()
    volume = pd.DataFrame({c: df["volume"] for c, df in data.items()}).reindex(close.index).fillna(0.0)
    after = close.index >= start
    first = int(np.argmax(after))
    order = np.random.default_rng(seed).permutation(int(after.sum()))
    log_ret = np.log(close).diff().to_numpy()[after][order]
    base = np.log(close.to_numpy()[first - 1]) if first > 0 else np.log(close.to_numpy()[0])
    prices = close.to_numpy().copy()
    prices[after] = np.exp(base + np.nancumsum(log_ret, axis=0))
    vols = volume.to_numpy().copy()
    vols[after] = vols[after][order]
    return {c: pd.DataFrame({"close": prices[:, i], "volume": vols[:, i]}, index=close.index)
            for i, c in enumerate(close.columns)}


# ---------------------------------------------------------------------------- the four steps
def in_sample(returns: pd.DataFrame, grid: Grid) -> pd.DataFrame:
    """Step 1: every config's in-sample Sharpe, profit factor and plateau (mean Sharpe of its grid neighbours)."""
    table = pd.DataFrame({"sharpe": sharpe(returns),
                          "profit_factor": returns.apply(profit_factor),
                          "total_return": np.expm1(np.log1p(returns).sum())})
    configs = {grid.label(c): c for c in grid.configs}
    levels = {k: sorted(v) for k, v in grid.params.items()}

    def plateau(label):
        config, neighbours = configs[label], []
        for key, values in levels.items():
            i = values.index(config[key])
            for j in (i - 1, i + 1):
                if 0 <= j < len(values):
                    neighbours.append(table.at[grid.label({**config, key: values[j]}), "sharpe"])
        return float(np.mean(neighbours)) if neighbours else np.nan

    table["plateau"] = [plateau(label) for label in table.index]
    return table.sort_values("sharpe", ascending=False)


def walk_forward(returns: pd.DataFrame, train_days: int, test_days: int) -> tuple[pd.Series, list[dict]]:
    """Step 3: pick the best config on each training window, trade it on the next block."""
    oos, picks = [], []
    for start in range(train_days, len(returns), test_days):
        train = returns.iloc[start - train_days:start]
        best = sharpe(train).idxmax()
        block = returns[best].iloc[start:start + test_days]
        oos.append(block)
        picks.append({"from": block.index[0], "to": block.index[-1], "config": best,
                      "train_sharpe": float(sharpe(train[best])), "test_return": float(np.expm1(np.log1p(block).sum()))})
    return pd.concat(oos), picks


_DATA: dict | None = None


def _init(data):
    global _DATA
    _DATA = data


def _best_in_sample(job) -> float:
    seed, start, end, grid, cost = job
    returns = config_returns(permute_bars(_DATA, start, seed), grid, cost)
    return float(sharpe(returns.loc[start:end]).max())


def _walk_forward_score(job) -> float:
    seed, start, grid, cost, train, test = job
    returns = config_returns(permute_bars(_DATA, start, seed), grid, cost)
    return float(sharpe(walk_forward(returns, train, test)[0]))


def in_sample_permutation_test(data, grid: Grid, start, end, real_best: float, n: int, cost_bps: float,
                               workers: int) -> tuple[float, np.ndarray]:
    """Step 2. Data before `start` stays as it is: it only warms up the indicators."""
    jobs = [(seed, start, end, grid, cost_bps) for seed in range(n)]
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(truncate(data, end),)) as pool:
        best = np.array(list(pool.map(_best_in_sample, jobs)))
    return _p_value(best, real_best), best


def walk_forward_permutation_test(data, grid: Grid, oos_start, real_score: float, n: int, cost_bps: float,
                                  train_days: int, test_days: int, workers: int) -> tuple[float, np.ndarray]:
    """Step 4. Only bars after the first training window are shuffled."""
    jobs = [(seed, oos_start, grid, cost_bps, train_days, test_days) for seed in range(n)]
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(data,)) as pool:
        scores = np.array(list(pool.map(_walk_forward_score, jobs)))
    return _p_value(scores, real_score), scores


def _p_value(null: np.ndarray, real: float) -> float:
    """Share of shuffles at least as good as the real result, counting the real one (never exactly 0)."""
    return float((1 + (null >= real).sum()) / (1 + len(null)))


def truncate(data: dict[str, pd.DataFrame], end) -> dict[str, pd.DataFrame]:
    """Every coin's bars before `end`."""
    return {c: df[df.index < end] for c, df in data.items()}
