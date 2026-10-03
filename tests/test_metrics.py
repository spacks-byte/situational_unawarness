import numpy as np
import pandas as pd

from tradebot.core.metrics import compute_metrics, daily_returns, equity_summary


def test_equity_summary_reports_return_and_drawdown():
    metrics = equity_summary([10000.0, 11000.0, 9000.0, 12000.0])

    assert metrics["start_equity_usd"] == 10000.0
    assert metrics["end_equity_usd"] == 12000.0
    assert round(metrics["total_return"], 10) == 0.2
    assert metrics["max_drawdown_usd"] == 2000.0
    assert round(metrics["max_drawdown"], 4) == -0.1818


def test_daily_sharpe_matches_hand_calculation():
    index = pd.date_range("2026-01-01", periods=4 * 96, freq="15min", tz="UTC")
    equity = pd.Series(100_000 * (1 + 0.001 * np.sin(np.arange(len(index)) / 7)).cumprod(), index=index)

    metrics = compute_metrics(equity, "15m", initial=100_000)
    daily = pd.concat([pd.Series([100_000.0]), equity.resample("1D").last().reset_index(drop=True)]).pct_change().dropna()

    assert np.isclose(metrics["sharpe"], daily.mean() / daily.std() * np.sqrt(365))
    assert len(daily_returns(equity, 100_000)) == 4
