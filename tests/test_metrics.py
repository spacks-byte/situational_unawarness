from src.engine.analytics.metrics import compute_equity_metrics


def test_compute_equity_metrics_reports_return_and_drawdown():
    metrics = compute_equity_metrics([10000.0, 11000.0, 9000.0, 12000.0])

    assert metrics["start_equity_usd"] == 10000.0
    assert metrics["end_equity_usd"] == 12000.0
    assert metrics["total_return_pct"] == 20.0
    assert metrics["max_drawdown_usd"] == 2000.0
    assert round(metrics["max_drawdown_pct"], 2) == 18.18
