from __future__ import annotations

from collections.abc import Iterable


def compute_equity_metrics(equity_curve: Iterable[float]) -> dict[str, float]:
    values = [float(value) for value in equity_curve]
    if not values:
        return {
            "start_equity_usd": 0.0,
            "end_equity_usd": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_usd": 0.0,
            "max_drawdown_pct": 0.0,
        }

    start_equity = values[0]
    end_equity = values[-1]
    peak_equity = values[0]
    max_drawdown_usd = 0.0
    max_drawdown_pct = 0.0
    for equity in values:
        peak_equity = max(peak_equity, equity)
        drawdown_usd = peak_equity - equity
        drawdown_pct = drawdown_usd / peak_equity if peak_equity > 0 else 0.0
        max_drawdown_usd = max(max_drawdown_usd, drawdown_usd)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

    total_return_pct = ((end_equity / start_equity) - 1.0) * 100.0 if start_equity else 0.0
    return {
        "start_equity_usd": start_equity,
        "end_equity_usd": end_equity,
        "total_return_pct": round(total_return_pct, 10),
        "max_drawdown_usd": round(max_drawdown_usd, 10),
        "max_drawdown_pct": round(max_drawdown_pct * 100.0, 10),
    }