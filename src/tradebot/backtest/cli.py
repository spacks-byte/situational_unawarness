"""
`python -m tradebot backtest ...`: run a strategy on downloaded klines.

Every option defaults to the `backtest` / `fees` sections of the config file, so flags only
override them. Examples:
    python -m tradebot backtest --strategy ma_crossover --params fast=80,slow=400
    python -m tradebot backtest --strategy ma_crossover --windows   # rolling 7-day competition windows
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from tradebot.backtest.simulator import buy_and_hold, run_backtest
from tradebot.backtest.windows import evaluate_windows, summarize_windows
from tradebot.core.config import Settings
from tradebot.core.metrics import compute_metrics
from tradebot.data.loader import load_universe
from tradebot.strategy.registry import STRATEGIES

PERCENT_KEYS = {"total_return", "annual_return", "annual_volatility", "max_drawdown",
                "avg_exposure", "avg_net_exposure", "fill_rate"}

# CLI flag -> (config section, field)
OVERRIDES = {
    "cash": ("backtest", "initial_cash"),
    "limit_offset_bps": ("backtest", "limit_offset_bps"),
    "limit_fill": ("backtest", "limit_fill"),
    "band": ("backtest", "rebalance_band"),
    "borrow_rate": ("backtest", "borrow_rate_annual"),
    "maintenance": ("backtest", "maintenance_margin"),
    "interval": ("backtest", "interval"),
    "window_days": ("backtest", "window_days"),
    "step_days": ("backtest", "step_days"),
    "warmup_days": ("backtest", "warmup_days"),
    "maker_fee": ("fees", "spot_maker"),
    "short_open_fee": ("fees", "short_open"),
    "short_close_fee": ("fees", "short_close"),
}


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


def add_parser(subparsers) -> None:
    p = subparsers.add_parser("backtest", help="Backtest a strategy on downloaded klines",
                              description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    p.add_argument("--params", default="", help="Strategy params, e.g. fast=20,slow=100")
    p.add_argument("--symbols", help="Comma-separated coins (default: config backtest.symbols)")
    p.add_argument("--interval", help="Kline interval, e.g. 5m or 15m")
    p.add_argument("--start", help="Inclusive, YYYY-MM-DD")
    p.add_argument("--end", help="Exclusive, YYYY-MM-DD")
    p.add_argument("--cash", type=float, help="Starting portfolio in USD")
    p.add_argument("--maker-fee", type=float, help="Spot limit order fee")
    p.add_argument("--short-open-fee", type=float, help="Fee on short collateral when opening")
    p.add_argument("--short-close-fee", type=float, help="Fee on short closes (market)")
    p.add_argument("--limit-offset-bps", type=float, help="Place buys below / sells above the last close")
    p.add_argument("--limit-fill", choices=["through", "touch"], help="through: price must trade past the limit")
    p.add_argument("--band", type=float, help="Rebalance band in weight units")
    p.add_argument("--borrow-rate", type=float, help="Annual borrow fee on short notional")
    p.add_argument("--maintenance", type=float, help="Short liquidation threshold as a fraction of notional")
    p.add_argument("--windows", action="store_true", help="Evaluate on rolling competition-length windows")
    p.add_argument("--window-days", type=int)
    p.add_argument("--step-days", type=int)
    p.add_argument("--warmup-days", type=int)
    p.add_argument("--no-save", action="store_true", help="Don't write results to disk")
    p.set_defaults(handler=run)


def _apply_overrides(settings: Settings, args) -> Settings:
    updates: dict[str, dict] = {"backtest": {}, "fees": {}}
    for flag, (section, key) in OVERRIDES.items():
        value = getattr(args, flag, None)
        if value is not None:
            updates[section][key] = value
    data = settings.model_dump()
    for section, values in updates.items():
        data[section].update(values)
    return Settings(**data)


def _fmt(key, value):
    if isinstance(value, float):
        return f"{value:.2%}" if key in PERCENT_KEYS else f"{value:,.2f}"
    return str(value)


def _print_table(strategy_metrics: dict, benchmark_metrics: dict, label: str) -> None:
    print(f"\n{'metric':<20}{label:>24}{'buy & hold':>16}")
    print("-" * 60)
    for key, value in strategy_metrics.items():
        if key not in ("start", "end"):
            print(f"{key:<20}{_fmt(key, value):>24}{_fmt(key, benchmark_metrics.get(key, '')):>16}")


def _print_windows(windows, window_days: int) -> None:
    summary = summarize_windows(windows)
    print(f"\n{len(windows)} windows of {window_days} days, each starting from cash")
    print(f"{'metric':<28}{'p10':>12}{'median':>12}{'p90':>12}")
    print("-" * 64)
    for label, row in summary.iterrows():
        key = label.split()[-1]
        print(f"{label:<28}" + "".join(f"{_fmt(key, float(row[c])):>12}" for c in ("p10", "median", "p90")))
    print(f"\nWindows with positive return: {(windows['total_return'] > 0).mean():.0%}")
    print(f"Windows beating buy & hold:   {(windows['total_return'] > windows['bh_total_return']).mean():.0%}")


def run(args, settings: Settings) -> int:
    settings = _apply_overrides(settings, args)
    config = settings.backtest
    strategy = STRATEGIES[args.strategy](**parse_params(args.params))
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else config.symbols

    data = load_universe(symbols, config.interval, args.start, args.end, data_dir=settings.data.dir)
    print(f"Strategy: {strategy} | {config.interval} | {', '.join(data)}")

    mode = "windows" if args.windows else "full"
    out = Path(config.results_dir) / f"{strategy.name}_{config.interval}_{mode}_{datetime.now():%Y%m%d-%H%M%S}"
    summary = {"strategy": strategy.name, "params": strategy.params, "symbols": list(data),
               "interval": config.interval, "config": config.model_dump()}

    if args.windows:
        windows = evaluate_windows(strategy, data, config.interval, config,
                                   config.window_days, config.step_days, config.warmup_days)
        if windows.empty:
            print("[ERROR] Not enough data for a single window")
            return 1
        _print_windows(windows, config.window_days)
        if not args.no_save:
            out.mkdir(parents=True, exist_ok=True)
            windows.to_csv(out / "windows.csv", index=False)
            summary["windows"] = summarize_windows(windows).to_dict()
    else:
        result = run_backtest(strategy, data, config.interval, config)
        benchmark = buy_and_hold(data, config)
        metrics = compute_metrics(result.equity, config.interval, result.trades,
                                  result.exposure, result.net_exposure, initial=config.initial_cash)
        bench_metrics = compute_metrics(benchmark, config.interval, initial=config.initial_cash)
        print(f"Period: {metrics['start']} -> {metrics['end']}")
        _print_table(metrics, bench_metrics, strategy.name)
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
