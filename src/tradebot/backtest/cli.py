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
from tradebot.backtest.period import relative_period
from tradebot.backtest.windows import evaluate_windows, summarize_windows
from tradebot.core.config import Settings, BacktestMMConfig
from tradebot.core.metrics import compute_metrics
from tradebot.data.loader import load_universe
from tradebot.strategy.registry import STRATEGIES

PERCENT_KEYS = {"total_return", "annual_return", "annual_volatility", "max_drawdown",
                "avg_exposure", "avg_net_exposure", "fill_rate"}

# CLI flag -> (config section, field)
OVERRIDES = {
    "market_slippage_bps": ("backtest", "market_slippage_bps"),
    "penetration_ticks": ("backtest", "penetration_ticks"),
    "penetration_probability": ("backtest", "penetration_probability"),
    "random_seed": ("backtest", "random_seed"),
    "liquidate_mm": ("backtest", "liquidate_mm"),
    "instrument_rules_path": ("backtest", "instrument_rules_path"),
    "archive_cache_dirs": ("backtest", "archive_cache_dirs"),
    "candle_store_dir": ("backtest", "candle_store_dir"),
    "download_missing": ("backtest", "download_missing"),
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
    p.add_argument("--start", help="Inclusive UTC date or ISO timestamp")
    p.add_argument("--end", help="Exclusive UTC date or ISO timestamp")
    period = p.add_mutually_exclusive_group()
    period.add_argument('--last-hours', type=float, help='Run the last X hours of completed candles')
    period.add_argument('--last-minutes', type=float, help='Run the last X minutes of completed candles')
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
    p.add_argument('--market-slippage-bps', type=float)
    p.add_argument('--penetration-ticks', type=int)
    p.add_argument('--penetration-probability', type=float)
    p.add_argument('--random-seed', type=int)
    p.add_argument('--liquidate-mm', action=argparse.BooleanOptionalAction, default=None)
    p.add_argument('--allocations', help='Symbol fractions summing to 1; zero disables a symbol, e.g. BTC=.6,ETH=.4,PEPE=0')
    p.add_argument('--refresh-seconds', type=int)
    p.add_argument('--mm-warmup-seconds', type=int)
    p.add_argument('--feature-lag-seconds', type=int)
    p.add_argument('--lot-fraction', type=float)
    p.add_argument('--inventory-fraction', type=float)
    p.add_argument('--one-tick-distance', action=argparse.BooleanOptionalAction, default=None)
    p.add_argument('--reference-source', choices=['candle_close','midpoint'])
    p.add_argument('--instrument-rules-path', help='Saved Roostoo exchangeInfo JSON; config instrument_rules overrides individual pairs')
    p.add_argument('--archive-cache', dest='archive_cache_dirs', action='append', help='Verified archive root; repeat for fallback roots')
    p.add_argument('--candle-store-dir')
    p.add_argument('--download-missing', action=argparse.BooleanOptionalAction, default=None)
    p.add_argument('--data-dir', help='Pairs: local Binance Parquet root or flat ASSETUSDT_30m.csv.gz directory')
    p.add_argument('--out', help='Pairs: new output directory (default: timestamped results directory)')
    p.add_argument('--pair-cost-model', choices=['reference', 'market'],
                   help='Pairs: reference uses spot maker fees; market uses spot taker fees. Both assume next-open fills.')
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
    if STRATEGIES[args.strategy].output_kind == "pairs":
        from tradebot.backtest.pairs import run_cli
        return run_cli(args, _apply_overrides(settings, args))
    settings = _apply_overrides(settings, args)
    config = settings.backtest
    relative = getattr(args, 'last_hours', None) is not None or getattr(args, 'last_minutes', None) is not None
    trade_start = None
    if relative:
        if args.start or args.end or args.windows:
            raise ValueError('Use --last-hours / --last-minutes without --start, --end or --windows.')
        interval = '1s' if STRATEGIES[args.strategy].output_kind == 'quotes' else config.interval
        trade_start, end = relative_period(interval, last_hours=args.last_hours, last_minutes=args.last_minutes)
        args.start, args.end = trade_start.isoformat(), end.isoformat()
    if STRATEGIES[args.strategy].output_kind == 'quotes':
        return run_mm(args, settings)
    strategy = STRATEGIES[args.strategy](**parse_params(args.params))
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else config.symbols

    load_start = args.start
    if relative:
        import pandas as pd
        step = pd.Timedelta(config.interval.replace('m', 'min'))
        warmup = pd.Timedelta(days=45) if args.strategy == 'rxm' else step * (strategy.params['slow'] + 2)
        load_start = (trade_start - warmup).isoformat()
    data = load_universe(symbols, config.interval, load_start, args.end, data_dir=settings.data.dir)
    if relative:
        from tradebot.core.symbols import to_coin
        expected = pd.date_range(load_start, args.end, freq=step, inclusive='left')
        for symbol in symbols:
            frame = data.get(to_coin(symbol))
            if frame is None or not expected.isin(frame.index).all():
                raise ValueError(f'{symbol}: missing candles for {load_start} to {args.end}, including warm-up. Download the requested history first.')
    print(f"Strategy: {strategy} | {config.interval} | {', '.join(data)}")

    mode = "windows" if args.windows else "full"
    out = Path(config.results_dir) / f"{strategy.name}_{config.interval}_{mode}_{datetime.now():%Y%m%d-%H%M%S}"
    summary = {"strategy": strategy.name, "params": strategy.params, "symbols": list(data),
               "interval": config.interval, "config": config.model_dump(), "start": args.start, "end": args.end,
               "last_hours": getattr(args, 'last_hours', None), "last_minutes": getattr(args, 'last_minutes', None)}

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
        result = run_backtest(strategy, data, config.interval, config, trade_start=trade_start)
        benchmark_data = data if trade_start is None else {s: frame.loc[trade_start:] for s, frame in data.items()}
        benchmark = buy_and_hold(benchmark_data, config)
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


def run_mm(args, settings):
    from tradebot.backtest.history import SecondHistory, instrument_rules, utc
    from tradebot.core.metrics import daily_returns
    config = settings.backtest.model_copy(deep=True)
    if args.windows:
        raise ValueError('Independent MM currently takes an explicit --start/--end period; shared-replay remains available separately.')
    if not args.start or not args.end:
        raise ValueError('MM requires --start and --end (UTC, end exclusive).')
    if args.interval and args.interval != '1s':
        raise ValueError('MM requires --interval 1s.')
    start, end = utc(args.start), utc(args.end)
    import pandas as pd
    if end <= start or start != start.floor('s') or end != end.floor('s') or end > pd.Timestamp.now(tz='UTC').floor('s'):
        raise ValueError('MM needs integral UTC seconds and a positive period ending before the latest completed candle.')
    params = config.mm.model_dump() | parse_params(args.params)
    for flag, key in [('refresh_seconds','refresh_seconds'),('mm_warmup_seconds','warmup_seconds'),
                      ('feature_lag_seconds','feature_lag_seconds'),('lot_fraction','lot_fraction'),
                      ('inventory_fraction','inventory_fraction'),('one_tick_distance','enforce_one_tick_distance'),
                      ('reference_source','reference_source')]:
        if getattr(args,flag,None) is not None:
            params[key] = getattr(args,flag)
    if args.allocations:
        params['allocations'] = parse_params(args.allocations)
    config.mm = BacktestMMConfig.model_validate(params)
    symbols = config.mm.active_symbols
    if args.symbols and set(s.strip().upper().removesuffix('/USDT').removesuffix('/USD') for s in args.symbols.split(',')) != set(config.mm.allocations):
        raise ValueError('MM symbols must match the allocation keys; use --allocations to set the universe and weights.')
    if args.maker_fee is not None and args.maker_fee != .0005:
        raise ValueError('MM maker fee is frozen at .0005 (5 bps).')
    rules, rule_info = instrument_rules(config,symbols,settings.exchange.base_url)
    history = SecondHistory(config,settings.data.dir)
    chunks = history.chunks(symbols,start,end,config.mm.warmup_seconds,print)
    result = run_backtest(STRATEGIES[args.strategy](config.mm),chunks,'1s',config,trade_start=start,rules=rules,progress=print)
    result.metadata.update(data_provenance=history.provenance,rule_provenance=rule_info)
    metrics = compute_metrics(result.equity,'1s',result.trades,result.exposure,result.net_exposure,initial=config.initial_cash)
    metrics.update(num_orders=len(result.quotes)+int((result.trades.order_type=='MARKET').sum()),
                   fill_rate=float(result.quotes.filled.mean()) if len(result.quotes) else None,
                   daily_observations=len(daily_returns(result.equity, config.initial_cash)))
    for key,value in metrics.items():
        print(f'{key}: {_fmt(key,value)}')
    if not args.no_save:
        import uuid
        out = Path(config.results_dir) / f'{args.strategy}_1s_{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}'
        out.mkdir(parents=True,exist_ok=False)
        result.equity.to_frame().join(result.exposure).join(result.net_exposure).to_csv(out/'equity.csv')
        result.quotes.to_csv(out/'quotes.csv',index=False)
        result.trades.to_csv(out/'trades.csv',index=False)
        import math
        json_metrics = {k: None if isinstance(v, float) and not math.isfinite(v) else v for k,v in metrics.items()}
        summary = dict(strategy=args.strategy,start=start.isoformat(),end=end.isoformat(),
                       last_hours=getattr(args, 'last_hours', None), last_minutes=getattr(args, 'last_minutes', None),
                       metrics=json_metrics,**result.metadata)
        (out/'summary.json').write_text(json.dumps(summary,indent=2,default=str,allow_nan=False))
        print(f'Saved to {out}')
    return 0
