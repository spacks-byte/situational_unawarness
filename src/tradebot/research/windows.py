"""
Competition-window evaluation of an RXM preset through the full simulator: 14-day windows, each
starting from cash, with lock-in, limit-order fills and fees.

Score = the judges' composite 0.4·Sortino + 0.3·Sharpe + 0.3·Calmar. Calmar is capped at 50 per
window, so one window with a tiny drawdown can't dominate. P>5.2% is the estimated top-20 cut-off
for qualifying.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tradebot.backtest.windows import evaluate_windows
from tradebot.core.config import BacktestConfig
from tradebot.strategy.library.rxm import PRESETS, ResidualMomentum

WINDOW_DAYS, STEP_DAYS, WARMUP_DAYS = 14, 7, 45
QUALIFY = 0.052
SPLIT = pd.Timestamp("2025-06-01", tz="UTC")       # train before, validate after (spec §6)


def composite(row) -> float:
    calmar = min(row["calmar"], 50) if np.isfinite(row["calmar"]) else 0.0
    return 0.4 * np.nan_to_num(row["sortino"]) + 0.3 * np.nan_to_num(row["sharpe"]) + 0.3 * calmar


def preset_config(name: str, base: BacktestConfig, **overrides) -> BacktestConfig:
    return base.model_copy(update={**PRESETS[name]["backtest"], **overrides})


def evaluate(strategy: ResidualMomentum, data: dict[str, pd.DataFrame], config: BacktestConfig,
             step_days: int = STEP_DAYS) -> pd.DataFrame:
    windows = evaluate_windows(strategy, data, "15m", config, WINDOW_DAYS, step_days, WARMUP_DAYS)
    windows["composite"] = windows.apply(composite, axis=1)
    return windows


def summarize(windows: pd.DataFrame) -> pd.Series:
    r = windows["total_return"]
    return pd.Series({
        "windows": len(windows), "median": r.median(), "mean": r.mean(), "p10": r.quantile(0.1),
        "P>0": (r > 0).mean(), f"P>{QUALIFY:.1%}": (r > QUALIFY).mean(),
        "median_composite": windows["composite"].median(), "median_sharpe": windows["sharpe"].median(),
        "fill_rate": windows["fill_rate"].median(), "trades": windows["num_trades"].median(),
    })


def by_split(windows: pd.DataFrame) -> pd.DataFrame:
    """Summaries for all windows, train (start < SPLIT) and validate (start >= SPLIT + one window)."""
    train = windows[windows["start"] < SPLIT]
    validate = windows[windows["start"] >= SPLIT + pd.Timedelta(days=WINDOW_DAYS)]
    return pd.DataFrame({"all": summarize(windows), "train": summarize(train), "validate": summarize(validate)})
