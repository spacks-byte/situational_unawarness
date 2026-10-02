"""
Run a backtest from the command line.

Usage:
    python -m backtest.run --strategy ma_crossover --params fast=20,slow=100 \
        --symbols BTC,ETH,SOL --interval 15m --start 2025-10-01
    python -m backtest.run --strategy ma_crossover --windows      # rolling 7-day competition windows
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from backtest.engine import BacktestConfig, buy_and_hold, load_universe, run_backtest
from backtest.metrics import compute_metrics
from backtest.strategies import STRATEGIES
from backtest.windows import evaluate_windows, summarize_windows

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "results"
DEFAULT_SYMBOLS = "BTC,ETH,SOL,BNB,XRP"

PERCENT_KEYS = {"total_return", "annual_return", "annual_volatility", "max_drawdown",
                "avg_exposure", "avg_net_exposure", "fill_rate"}


def parse_params(text: str) -> dict:
    """'fast=20,slow=100' -> {'fast': 20, 'slow': 100}"""
    params = {}
    for item in filter(None, (text or "").split(",")):
        key, value = item.split("=", 1)
        for cast in (int, float):
            try:
                value = cast(value)
                break
            except ValueError:
                pass
        params[key.strip()] = value
    return params


def format_value(key, value):
    if isinstance(value, float):
        return f"{value:.2%}" if key in PERCENT_KEYS else f"{value:,.2f}"
    return str(value)


def print_table(strategy_metrics: dict, benchmark_metrics: dict, strategy_label: str):
    print(f"\n{'metric':<20}{strategy_label:>24}{'buy & hold':>16}")
    print("-" * 60)
    for key, value in strategy_metrics.items():
        if key in ("start", "end"):
            continue
        bench = benchmark_metrics.get(key, "")
        print(f"{key:<20}{format_value(key, value):>24}{format_value(key, bench):>16}")


def print_window_summary(windows, window_days: int):
    summary = summarize_windows(windows)
    print(f"\n{len(windows)} windows of {window_days} days, each starting from cash")
    print(f"{'metric':<28}{'p10':>12}{'median':>12}{'p90':>12}")
    print("-" * 64)
    for label, row in summary.iterrows():
        key = label.split()[-1]
        cells = "".join(f"{format_value(key, float(row[c])):>12}" for c in ("p10", "median", "p90"))
        print(f"{label:<28}{cells}")
    print(f"\nWindows with positive return: {(windows['total_return'] > 0).mean():.0%}")
    print(f"Windows beating buy & hold:   {(windows['total_return'] > windows['bh_total_return']).mean():.0%}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Backtest a strategy on Binance Vision klines.")
    p.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    p.add_argument("--params", default="", help="Strategy params, e.g. fast=20,slow=100")
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--interval", default="15m")
    p.add_argument("--start", help="Inclusive, YYYY-MM-DD")
    p.add_argument("--end", help="Exclusive, YYYY-MM-DD")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--maker-fee", type=float, default=0.0005, help="Limit order fee (default 0.05%%)")
    p.add_argument("--taker-fee", type=float, default=0.001,
                   help="Market order fee, used for short covers and liquidations (default 0.1%%)")
    p.add_argument("--limit-offset-bps", type=float, default=0.0,
                   help="Place buys this far below / sells above the last close")
    p.add_argument("--limit-fill", choices=["through", "touch"], default="through",
                   help="through: price must trade past the limit (default); touch: reaching it fills")
    p.add_argument("--band", type=float, default=0.01, help="Rebalance band in weight units")
    p.add_argument("--short-open-fee", type=float, default=0.001,
                   help="Fee on opening a short (Roostoo /v6/short_open: 0.1%% flat)")
    p.add_argument("--borrow-rate", type=float, default=0.0,
                   help="Annual borrow fee on short notional, e.g. 0.1 = 10%%/yr")
    p.add_argument("--maintenance", type=float, default=0.0,
                   help="Liquidate a short when its equity falls to this fraction of notional")
    p.add_argument("--windows", action="store_true", help="Evaluate on rolling competition-length windows")
    p.add_argument("--window-days", type=int, default=14, help="Competition length (Oct 4-17 = 14 days)")
    p.add_argument("--step-days", type=int, default=1)
    p.add_argument("--warmup-days", type=int, default=30)
    p.add_argument("--no-save", action="store_true", help="Don't write results to disk")
    args = p.parse_args(argv)

    strategy = STRATEGIES[args.strategy](**parse_params(args.params))
    config = BacktestConfig(initial_cash=args.cash, maker_fee=args.maker_fee, taker_fee=args.taker_fee,
                            limit_offset_bps=args.limit_offset_bps, limit_fill=args.limit_fill,
                            rebalance_band=args.band, short_open_fee=args.short_open_fee,
                            borrow_rate_annual=args.borrow_rate,
                            maintenance_margin=args.maintenance)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    data = load_universe(symbols, args.interval, args.start, args.end)
    print(f"Strategy: {strategy} | {args.interval} | {', '.join(data)}")

    mode = "windows" if args.windows else "full"
    out = RESULTS_DIR / f"{strategy.name}_{args.interval}_{mode}_{datetime.now():%Y%m%d-%H%M%S}"
    summary = {"strategy": strategy.name, "params": strategy.params, "symbols": list(data),
               "interval": args.interval, "config": vars(config)}

    if args.windows:
        windows = evaluate_windows(strategy, data, args.interval, config,
                                   args.window_days, args.step_days, args.warmup_days)
        if windows.empty:
            print("[ERROR] Not enough data for a single window")
            return 1
        print_window_summary(windows, args.window_days)
        if not args.no_save:
            out.mkdir(parents=True, exist_ok=True)
            windows.to_csv(out / "windows.csv", index=False)
            summary["windows"] = summarize_windows(windows).to_dict()
    else:
        result = run_backtest(strategy, data, args.interval, config)
        benchmark = buy_and_hold(data, config)
        metrics = compute_metrics(result.equity, args.interval, result.trades,
                                  result.exposure, result.net_exposure, initial=config.initial_cash)
        bench_metrics = compute_metrics(benchmark, args.interval, initial=config.initial_cash)
        print(f"Period: {metrics['start']} -> {metrics['end']}")
        print_table(metrics, bench_metrics, strategy.name)
        if not args.no_save:
            out.mkdir(parents=True, exist_ok=True)
            curves = result.equity.to_frame().join(benchmark).join(result.exposure).join(result.net_exposure)
            curves.to_csv(out / "equity.csv")
            result.trades.to_csv(out / "trades.csv", index=False)
            summary.update(metrics=metrics, benchmark=bench_metrics)

    if not args.no_save:
        (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        print(f"\nSaved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
