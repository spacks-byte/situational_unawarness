"""Backtesting: limit-order portfolio simulator, rolling competition windows and the backtest CLI."""
from tradebot.backtest.simulator import BacktestResult, buy_and_hold, run_backtest
from tradebot.backtest.windows import evaluate_windows, summarize_windows

__all__ = ["BacktestResult", "buy_and_hold", "evaluate_windows", "run_backtest", "summarize_windows"]
