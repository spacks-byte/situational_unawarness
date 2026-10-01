from typing import Optional

import numpy as np
import pandas as pd

DAYS_PER_YEAR = 365  # crypto trades every day


def max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1).min())


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
    bar = pd.Timedelta(interval[:-1] + "min" if interval.endswith("m") else interval)
    days = (equity.index[-1] - equity.index[0] + bar) / pd.Timedelta(days=1)

    total_return = equity.iloc[-1] / start_value - 1
    ann_return = (1 + total_return) ** (DAYS_PER_YEAR / days) - 1 if days > 0 else np.nan
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
            "maker_fees": round(float(filled.loc[filled["order_type"] == "LIMIT", "fee"].sum()), 2)
            if len(filled) else 0.0,
            "taker_fees": round(float(market["fee"].sum()), 2) if len(market) else 0.0,
            "turnover": float(filled["value"].sum() / equity.mean()) if len(filled) else 0.0,
        })
    if exposure is not None:
        metrics["avg_exposure"] = float(exposure.mean())
    if net_exposure is not None:
        metrics["avg_net_exposure"] = float(net_exposure.mean())
    return metrics
