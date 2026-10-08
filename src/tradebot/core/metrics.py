"""
Performance metrics, the single implementation used by the backtester and the live engine.

Conventions: returns and drawdowns are decimal fractions (0.05 = 5%); drawdowns are <= 0.
Sharpe, Sortino and Calmar use daily returns annualized over 365 days (crypto trades daily),
matching how the competition judges portfolios.
"""
from collections.abc import Iterable
from typing import Optional

import numpy as np
import pandas as pd

from tradebot.core.intervals import interval_to_timedelta

DAYS_PER_YEAR = 365


def max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1).min())


def equity_summary(equity_curve: Iterable[float]) -> dict:
    """Return and drawdown for a plain sequence of equity values (no timestamps needed)."""
    values = pd.Series([float(v) for v in equity_curve], dtype=float)
    if values.empty:
        return {"start_equity_usd": 0.0, "end_equity_usd": 0.0, "total_return": 0.0,
                "max_drawdown_usd": 0.0, "max_drawdown": 0.0}
    start, end = float(values.iloc[0]), float(values.iloc[-1])
    peak = values.cummax()
    return {
        "start_equity_usd": start,
        "end_equity_usd": end,
        "total_return": end / start - 1 if start else 0.0,
        "max_drawdown_usd": float((peak - values).max()),
        "max_drawdown": max_drawdown(values),
    }


def daily_returns(equity: pd.Series, initial: Optional[float] = None) -> pd.Series:
    """Daily (UTC) close-to-close returns, with the first day measured from the starting value."""
    daily = equity.resample("1D").last().dropna()
    start_value = equity.iloc[0] if initial is None else initial
    start = pd.Series([start_value], index=[daily.index[0] - pd.Timedelta(days=1)])
    return pd.concat([start, daily]).pct_change().dropna()


def compute_metrics(equity: pd.Series, interval: str,
                    trades: Optional[pd.DataFrame] = None,
                    exposure: Optional[pd.Series] = None,
                    net_exposure: Optional[pd.Series] = None,
                    initial: Optional[float] = None) -> dict:
    """
    Competition metrics. Sharpe, Sortino and Calmar use daily returns, annualized over 365 days.
    Max drawdown uses the full bar-level equity curve so intraday dips count.
    """
    start_value = equity.iloc[0] if initial is None else initial
    returns = daily_returns(equity, start_value)
    bar = interval_to_timedelta(interval)
    days = (equity.index[-1] - equity.index[0] + bar) / pd.Timedelta(days=1)

    total_return = equity.iloc[-1] / start_value - 1
    # Very short second-level experiments can exceed the representable annualized
    # return. Report that ratio as undefined while retaining the observed return.
    with np.errstate(over='ignore', invalid='ignore'):
        ann_return = (1 + total_return) ** (DAYS_PER_YEAR / days) - 1 if days > 0 else np.nan
    if not np.isfinite(ann_return):
        ann_return = np.nan
    std = returns.std()
    downside = np.sqrt((returns.clip(upper=0) ** 2).mean())
    sharpe = returns.mean() / std * np.sqrt(DAYS_PER_YEAR) if std > 0 else np.nan
    sortino = returns.mean() / downside * np.sqrt(DAYS_PER_YEAR) if downside > 0 else np.nan
    mdd = min(max_drawdown(equity), equity.min() / start_value - 1, 0.0)

    metrics = {
        "start": str(equity.index[0]),
        "end": str(equity.index[-1]),
        "final_equity": round(float(equity.iloc[-1]), 2),
        "total_return": float(total_return),
        "annual_return": float(ann_return),
        "annual_volatility": float(std * np.sqrt(DAYS_PER_YEAR)),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": float(mdd),
        "calmar": float(ann_return / abs(mdd)) if mdd < 0 else np.nan,
    }

    if trades is not None:
        filled = trades[trades["filled"]] if len(trades) else trades
        limit_orders = trades[trades["order_type"] == "LIMIT"] if len(trades) else trades
        market = filled[filled["order_type"] == "MARKET"] if len(filled) else filled
        metrics.update({
            "num_trades": int(len(filled)),
            "num_orders": int(len(trades)),
            "fill_rate": float(limit_orders["filled"].mean()) if len(limit_orders) else np.nan,
            "num_market_orders": int(len(market)),
            "num_shorts": int((filled["side"] == "SHORT").sum()) if len(filled) else 0,
            "num_liquidations": int((filled["side"] == "LIQUIDATE").sum()) if len(filled) else 0,
            "limit_order_fees": round(float(filled.loc[filled["order_type"] == "LIMIT", "fee"].sum()), 2)
            if len(filled) else 0.0,
            "market_order_fees": round(float(market["fee"].sum()), 2) if len(market) else 0.0,
            "turnover": float(filled["value"].sum() / equity.mean()) if len(filled) else 0.0,
        })
    if exposure is not None:
        metrics["avg_exposure"] = float(exposure.mean())
    if net_exposure is not None:
        metrics["avg_net_exposure"] = float(net_exposure.mean())
    return metrics
