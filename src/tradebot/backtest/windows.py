"""
Evaluate a strategy the way the competition runs: many short windows (default 7 days),
each starting from cash, with earlier data available only as indicator warm-up.
"""


import numpy as np
import pandas as pd

from tradebot.backtest.simulator import buy_and_hold, run_backtest
from tradebot.core.config import BacktestConfig
from tradebot.core.metrics import compute_metrics
from tradebot.strategy.base import Strategy

SUMMARY_METRICS = ["total_return", "sharpe", "sortino", "calmar", "max_drawdown"]


def _slice(data: dict[str, pd.DataFrame], start, end) -> dict[str, pd.DataFrame]:
    out = {}
    for symbol, df in data.items():
        part = df[(df.index >= start) & (df.index < end)]
        if not part.empty:
            out[symbol] = part
    return out


def evaluate_windows(strategy: Strategy, data: dict[str, pd.DataFrame], interval: str,
                     config: BacktestConfig, window_days: int = 7, step_days: int = 1,
                     warmup_days: int = 30) -> pd.DataFrame:
    """Run one backtest per window and return one row of metrics per window."""
    first = min(df.index[0] for df in data.values())
    last = max(df.index[-1] for df in data.values())
    window = pd.Timedelta(days=window_days)
    warmup = pd.Timedelta(days=warmup_days)

    starts = pd.date_range((first + warmup).ceil("D"), last - window, freq=f"{step_days}D")
    rows = []
    for start in starts:
        end = start + window
        window_data = _slice(data, start - warmup, end)
        traded = _slice(window_data, start, end)
        if not traded:
            continue
        window_data = {s: window_data[s] for s in traded}  # only symbols with prices in the window

        result = run_backtest(strategy, window_data, interval, config, trade_start=start)
        m = compute_metrics(result.equity, interval, result.trades, result.exposure,
                            result.net_exposure, initial=config.initial_cash)
        b = compute_metrics(buy_and_hold(traded, config), interval, initial=config.initial_cash)

        row = {"start": start, "end": end}
        row.update({k: m[k] for k in SUMMARY_METRICS + ["num_trades", "fill_rate", "avg_exposure"]})
        row.update({f"bh_{k}": b[k] for k in SUMMARY_METRICS})
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_windows(windows: pd.DataFrame) -> pd.DataFrame:
    """Median and 10th/90th percentiles per metric, for the strategy and buy & hold."""
    rows = {}
    for metric in SUMMARY_METRICS:
        for label, col in ((metric, metric), (f"buy&hold {metric}", f"bh_{metric}")):
            values = windows[col].replace([np.inf, -np.inf], np.nan).dropna()
            rows[label] = {
                "p10": values.quantile(0.10),
                "median": values.median(),
                "p90": values.quantile(0.90),
            }
    return pd.DataFrame(rows).T
