"""Offline pair cycles using the same decisions and ownership as observation mode."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html import escape
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import uuid

import numpy as np
import pandas as pd

from tradebot.backtest.period import relative_period
from tradebot.core.clock import SimClock
from tradebot.core.cointegration import CointegrationConfig
from tradebot.core.metrics import compute_metrics, daily_returns
from tradebot.data.binance_vision import klines_path
from tradebot.data.market import binance_public_fetch
from tradebot.live.pairs import PairRuntime
from tradebot.strategy.library.cointegration import STEP, common_closes, matches_candle_grid

WARMUP = pd.Timedelta(days=60) + STEP
ASSUMPTIONS = (
    'Completed 30m signals fill at the next candle open; remaining positions close at the final close. '
    'Every leg fills in full. Fees and slippage are additive cash costs. No limit-order fill probability, '
    'funding, borrow costs, margin liquidation, exchange size rounding or live account risk limits are modeled. '
    'Fixed pair budgets exclude fee headroom and are reused after losses; this is a research execution model. '
    'Alpha, beta and inverse-volatility weights are frozen within each cycle. '
    'Annualized statistics use UTC daily returns (partial first/last days included) and can be unstable on short tests.'
)


def utc(value):
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        raise ValueError('A valid date or timestamp is required.')
    return ts.tz_localize('UTC') if ts.tz is None else ts.tz_convert('UTC')


def cycle_ranges(start, end, *, windows=False, window_days=None, step_days=None):
    """Exact, non-overlapping reset cycles; never silently discard a short tail."""
    start, end = utc(start), utc(end)
    if start != start.floor(STEP) or end != end.floor(STEP) or end <= start:
        raise ValueError('Pairs need a positive period on UTC 30-minute boundaries.')
    if not windows:
        if window_days is not None or step_days is not None:
            raise ValueError('--window-days / --step-days require --windows.')
        return [(start, end)]
    days = 14 if window_days is None else window_days
    if days <= 0 or (step_days is not None and step_days != days):
        raise ValueError('Pairs windows must be non-overlapping: positive window days and step days equal to window days.')
    width = pd.Timedelta(days=days)
    if (end-start) % width:
        raise ValueError('For --windows the selected period must be an exact multiple of --window-days (default 14).')
    return [(a, a+width) for a in pd.date_range(start, end, freq=width, inclusive='left')]


def validate_candles(frame, asset, start, end):
    if not {'open', 'close'}.issubset(frame):
        raise ValueError(f'{asset}: history requires open and close columns.')
    if not matches_candle_grid(frame.index, pd.date_range(start, end, freq=STEP, inclusive='left')):
        raise ValueError(f'{asset}: incomplete, duplicated, unordered or off-grid 30m history for {start} to {end}.')
    common_closes({asset: frame}, [asset], start, end)
    if 'open' not in frame or not np.isfinite(frame['open'].to_numpy(float)).all() or (frame['open'] <= 0).any():
        raise ValueError(f'{asset}: missing or invalid open prices.')


def load_history(assets, start, end, data_dir, *, download_missing=True, progress=print, fetch=None):
    """Use local history, fetch only gaps, and reject gaps/corruption before trading."""
    data_dir = Path(data_dir).expanduser()
    data, provenance = {}, []
    expected = pd.date_range(start, end, freq=STEP, inclusive='left')
    remote = fetch or binance_public_fetch(interval='30m')
    for asset in assets:
        path = klines_path(data_dir, asset, '30m')
        csv_path = data_dir / f'{asset}USDT_30m.csv.gz'
        original = pd.DataFrame(index=pd.DatetimeIndex([], tz='UTC', name='open_time'))
        source = None
        if path.exists():
            original, source = pd.read_parquet(path), str(path.resolve())
        elif csv_path.exists():
            original = pd.read_csv(csv_path, index_col='open_time')
            original.index = pd.to_datetime(original.index, utc=True)
            source = str(csv_path.resolve())
        if (not isinstance(original.index, pd.DatetimeIndex) or original.index.tz is None
                or original.index.has_duplicates or not original.index.is_monotonic_increasing):
            raise ValueError(f'{asset}: local history has duplicate, unordered or timezone-naive timestamps.')
        frame = original.loc[(original.index >= start) & (original.index < end)].copy()
        missing = expected.difference(frame.index)
        downloads = []
        if len(missing):
            if not download_missing:
                raise ValueError(f'{asset}: missing {len(missing)} 30m candles, first {missing[0]}; enable --download-missing.')
            progress(f'{asset}: fetching {len(missing)} missing candles, including warm-up')
            # Group adjacent missing bars so scattered holes do not trigger a full redownload.
            groups = np.split(missing, np.flatnonzero((missing[1:]-missing[:-1]) != STEP) + 1)
            parts = [frame] if len(frame) else []
            for group in groups:
                a, b = pd.Timestamp(group[0]), pd.Timestamp(group[-1])+STEP
                part = remote(asset, a, b)
                validate_candles(part, asset, a, b)
                parts.append(part)
                downloads.append(dict(start=a.isoformat(), end=b.isoformat(), source='Binance public spot klines'))
            frame = pd.concat(parts).sort_index()
        validate_candles(frame, asset, start, end)
        if len(missing):
            # Preserve all other cached dates. Atomic replace leaves the old cache intact on failure.
            merged = pd.concat([original.loc[~original.index.isin(frame.index)], frame]).sort_index()
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=path.parent, suffix='.parquet', delete=False) as tmp:
                temporary = Path(tmp.name)
            try:
                merged.to_parquet(temporary)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        data[asset] = frame
        digest = hashlib.sha256(pd.util.hash_pandas_object(frame[['open', 'close']], index=True).values.tobytes()).hexdigest()
        provenance.append(dict(asset=asset, local_source=source, downloads=downloads, rows=len(frame),
                               open_close_sha256=digest, start=start.isoformat(), end=end.isoformat()))
    return data, provenance


@dataclass
class PairBacktestResult:
    config: CointegrationConfig
    equity: pd.DataFrame
    trades: pd.DataFrame
    weights: pd.DataFrame
    signals: pd.DataFrame
    exposures: pd.DataFrame
    pair_metrics: pd.DataFrame
    metrics: dict


def run_cycle(data, config, *, costs=None, progress=None):
    """One independent reset; no live state files or exchange credentials are used."""
    if config.cycle_start is None or config.cycle_end is None:
        raise ValueError('A backtest needs both cycle_start and cycle_end.')
    start, end = utc(config.cycle_start), utc(config.cycle_end)
    for asset in config.assets:
        if asset not in data:
            raise ValueError(f'Missing history: {asset}')
        frame = data[asset].loc[(data[asset].index >= start-WARMUP) & (data[asset].index < end)]
        validate_candles(frame, asset, start-WARMUP, end)
    costs = costs or {}
    clock = SimClock(start.to_pydatetime())
    runtime = PairRuntime(config, ':memory:', clock=clock)
    try:
        runtime.initialize(data)
        weights = pd.DataFrame([dict(pair=s.pair, entry_z=s.entry_z, exit_z=s.exit_z,
            **{k: v for k, v in runtime.state['models'][s.pair].items() if k != 'history'}) for s in config.pairs])
        equity, exposures = [], []
        for n, ts in enumerate(pd.date_range(start, end, freq=STEP, inclusive='left')):
            opens = {a: float(data[a].at[ts, 'open']) for a in config.assets}
            closes = {a: float(data[a].at[ts, 'close']) for a in config.assets}
            runtime.fill_reference(ts, opens, **costs)
            clock.advance(STEP.total_seconds())
            runtime.process_close(ts, closes)
            runtime.fill_reference(ts+STEP, closes, terminal=True, **costs)
            mark = runtime.ledger.mark(closes)
            exposure = mark.pop('exposure')
            mark.update(timestamp=ts+STEP,
                        gross_notional=sum(v['gross_notional'] for v in exposure.values()),
                        net_notional=sum(v['net_notional'] for v in exposure.values()))
            equity.append(mark)
            for asset in config.assets:
                exposures.append(dict(timestamp=ts+STEP, asset=asset, **exposure.get(asset,
                    dict(net_quantity=0., net_notional=0., gross_notional=0.))))
            if progress and (n+1) % 480 == 0:
                progress(f'  Replayed through {ts+STEP}')
        if runtime.pending() or runtime.ledger.positions():
            raise RuntimeError('Backtest ended with unfilled instructions or open positions.')
        trades = []
        for (payload,) in runtime.db.execute('SELECT payload FROM pair_trades ORDER BY exit_intent'):
            trade = json.loads(payload)
            legs = trade.pop('legs')
            for leg, values in legs.items():
                trade.update({f'{key}_{leg.lower()}': value for key, value in values.items()})
            trade['holding_hours'] = (utc(trade['exit_time'])-utc(trade['entry_time'])).total_seconds()/3600
            trades.append(trade)
        trade_frame = pd.DataFrame(trades) if trades else pd.DataFrame(columns=[
            'pair', 'entry_time', 'exit_time', 'exit_reason', 'net_pnl', 'fees', 'slippage', 'holding_hours'])
        signals = pd.DataFrame([json.loads(p) for (p,) in runtime.db.execute(
            'SELECT payload FROM pair_decisions ORDER BY candle,pair_id')])
        curve = pd.DataFrame(equity).set_index('timestamp')
        # Core metrics take candle-open timestamps; exported marks remain at candle close.
        metric_equity = curve.equity.copy()
        metric_equity.index -= STEP
        metrics = compute_metrics(metric_equity, '30m', initial=config.reference_capital)
        metrics.update(start=start.isoformat(), end=end.isoformat(), initial_equity=config.reference_capital,
            final_equity=float(curve.equity.iloc[-1]), net_pnl=float(curve.net_pnl.iloc[-1]),
            fees=float(curve.fees.iloc[-1]), slippage=float(curve.slippage.iloc[-1]),
            num_trades=len(trade_frame), win_rate=float((trade_frame.net_pnl > 0).mean()) if len(trade_frame) else None,
            forced_exits=int((trade_frame.exit_reason == 'end_of_data').sum()),
            daily_observations=len(daily_returns(metric_equity, config.reference_capital)))
        pair_metrics = []
        for account in runtime.ledger.accounts():
            pair = account['pair_id']
            selected = trade_frame.loc[trade_frame.pair == pair]
            pair_metrics.append(dict(pair=pair, budget=account['capital'], final_equity=account['cash'],
                net_pnl=account['cash']-account['capital'], total_return=account['cash']/account['capital']-1,
                fees=account['fees'], slippage=account['slippage'], num_trades=len(selected),
                win_rate=float((selected.net_pnl > 0).mean()) if len(selected) else None))
        return PairBacktestResult(config, curve, trade_frame, weights, signals,
                                  pd.DataFrame(exposures), pd.DataFrame(pair_metrics), metrics)
    finally:
        runtime.close()


def clean_json(value):
    if isinstance(value, dict):
        return {k: clean_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json(v) for v in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def equity_chart(result):
    values = np.r_[result.config.reference_capital, result.equity.equity.to_numpy()]
    lo, hi = float(values.min()), float(values.max())
    width = max(hi-lo, abs(lo)*.001, 1.)
    lo, hi = lo-width*.1, hi+width*.1
    points = ' '.join(f'{55+i/(len(values)-1)*820:.2f},{220-(v-lo)/(hi-lo)*195:.2f}' for i, v in enumerate(values))
    labels = ''.join(f'<text x="5" y="{y}">{v:,.2f}</text>' for y, v in [(30, hi), (220, lo)])
    return (f'<svg viewBox="0 0 910 260" role="img" aria-label="Portfolio equity in USDT">{labels}'
            f'<polyline points="{points}" fill="none" stroke="#1675c9" stroke-width="2"/>'
            f'<text x="55" y="250">{result.metrics["start"]}</text>'
            f'<text x="650" y="250">{result.metrics["end"]}</text></svg>')


def save_report(out, results, summary):
    """Standalone HTML plus inspectable machine-readable evidence for every cycle."""
    out.mkdir(parents=True, exist_ok=False)
    sections = []
    for i, result in enumerate(results, 1):
        folder = out if len(results) == 1 else out / f'window_{i:03d}'
        folder.mkdir(exist_ok=True)
        result.equity.to_csv(folder / 'equity.csv')
        for name in ('trades', 'weights', 'exposures', 'pair_metrics'):
            getattr(result, name).to_csv(folder / f'{name}.csv', index=False)
        result.signals.to_csv(folder / 'signals.csv.gz', index=False)
        metrics_html = pd.DataFrame([clean_json(result.metrics)]).T.to_html(header=False, na_rep='N/A', escape=True)
        sections.append(f'<h2>Cycle {i}</h2>{equity_chart(result)}{metrics_html}'
                        f'<h3>Pair results</h3>{result.pair_metrics.to_html(index=False, escape=True)}')
    pd.DataFrame([r.metrics for r in results]).to_csv(out / 'windows.csv', index=False)
    (out / 'summary.json').write_text(json.dumps(clean_json(summary), indent=2, default=str, allow_nan=False))
    aggregate = summary.get('window_statistics')
    overview = pd.DataFrame(aggregate).T.to_html(escape=True, na_rep='N/A') if aggregate else ''
    html = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Cointegration backtest</title>'
            '<style>body{font:15px system-ui;max-width:1200px;margin:40px auto;padding:0 20px;color:#172432}'
            'table{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px;display:block;overflow:auto}'
            'td,th{padding:7px;border:1px solid #dde4ea;text-align:right}svg{width:100%;max-width:1000px}'
            'svg text{font-size:11px}h2{margin-top:40px}</style><h1>Cointegration pairs backtest</h1>'
            f'<p>{escape(summary["mode"])}. {len(results)} cycle(s). Returns, volatility, drawdown and win rate '
            'are decimal fractions: 0.01 = 1%. Equity and costs are in USDT.</p>'
            f'<p>Costs: {escape(json.dumps(summary["costs"]))}</p><p>{escape(ASSUMPTIONS)}</p>'
            f'{overview}{"".join(sections)}</html>')
    (out / 'report.html').write_text(html)


def run_cli(args, settings):
    if args.params or args.symbols or args.allocations:
        raise ValueError('Configure the ordered universe and thresholds in cointegration.pairs; --params/--symbols/--allocations do not apply.')
    unsupported = ('limit_offset_bps', 'limit_fill', 'band', 'borrow_rate', 'maintenance', 'warmup_days',
                   'penetration_ticks', 'penetration_probability', 'random_seed', 'liquidate_mm',
                   'refresh_seconds', 'mm_warmup_seconds', 'feature_lag_seconds', 'lot_fraction',
                   'inventory_fraction', 'one_tick_distance', 'reference_source', 'instrument_rules_path',
                   'archive_cache_dirs', 'candle_store_dir')
    for flag in unsupported:
        if getattr(args, flag, None) is not None:
            raise ValueError(f'--{flag.replace("_", "-")} is not supported by the pairs backtester.')
    if args.interval and args.interval != '30m':
        raise ValueError('The frozen pairs strategy requires --interval 30m.')
    if args.out and args.no_save:
        raise ValueError('Use --out or --no-save, not both.')
    if args.out and Path(args.out).expanduser().exists():
        raise ValueError('--out must be a new directory; existing reports are never overwritten.')
    relative = args.last_hours is not None or args.last_minutes is not None
    if relative:
        if args.start or args.end or args.windows:
            raise ValueError('Use relative periods without --start, --end or --windows.')
        start, end = relative_period('30m', last_hours=args.last_hours, last_minutes=args.last_minutes)
    elif args.start and args.end:
        start, end = utc(args.start), utc(args.end)
    else:
        raise ValueError('Pairs require --start and --end, or --last-hours / --last-minutes.')
    if end > pd.Timestamp.now(tz='UTC').floor(STEP):
        raise ValueError('The period includes unfinished or future candles.')
    ranges = cycle_ranges(start, end, windows=args.windows, window_days=args.window_days, step_days=args.step_days)
    cfg = settings.cointegration
    cash = settings.backtest.initial_cash if args.cash is not None else cfg.reference_capital
    fees = settings.fees
    cost_model = args.pair_cost_model or settings.backtest.pair_cost_model
    if cost_model == 'market' and args.maker_fee is not None:
        raise ValueError('--maker-fee does not apply to market costs; set fees.spot_taker in YAML.')
    costs = dict(long_fee=fees.spot_maker if cost_model == 'reference' else fees.spot_taker,
                 short_open_fee=fees.short_open, short_close_fee=fees.short_close,
                 slippage_bps=settings.backtest.pair_slippage_bps if args.market_slippage_bps is None
                 else settings.backtest.market_slippage_bps)
    if any(not np.isfinite(v) or v < 0 for v in costs.values()):
        raise ValueError('Backtest costs must be finite and nonnegative.')
    print(f'cointegration-pairs | 30m | {len(cfg.pairs)} pairs | {cash:,.2f} USDT per cycle')
    print(f'{start} -> {end} | {len(ranges)} cycle(s) | {cost_model} costs: {costs}')
    data, provenance = load_history(cfg.assets, start-WARMUP, end, args.data_dir or settings.data.dir,
                                    download_missing=settings.backtest.download_missing)
    results = []
    for i, (a, b) in enumerate(ranges, 1):
        cycle = CointegrationConfig.model_validate(cfg.model_dump() | dict(
            cycle_start=a, cycle_end=b, reference_capital=cash))
        print(f'Cycle {i}/{len(ranges)}: {a} -> {b}')
        result = run_cycle(data, cycle, costs=costs, progress=print)
        results.append(result)
        m = result.metrics
        print(f'  Return {m["total_return"]:+.3%} | P&L {m["net_pnl"]:+.2f} | '
              f'Max drawdown {m["max_drawdown"]:.3%} | Trades {m["num_trades"]} | '
              f'Annual volatility {m["annual_volatility"]:.3%} | Sharpe {m["sharpe"]:.3f}')
    summary = dict(strategy='cointegration-pairs', interval='30m', start=start.isoformat(), end=end.isoformat(),
        mode='Independent windows, capital reset and refit each window' if args.windows else 'Single frozen-model cycle',
        initial_cash=cash, num_windows=len(results), cost_model=cost_model, costs=costs,
        assumptions=ASSUMPTIONS, data_provenance=provenance,
        cycles=[dict(config=r.config.model_dump(mode='json'), metrics=r.metrics) for r in results])
    if args.windows:
        metrics = pd.DataFrame([r.metrics for r in results])
        stats = metrics[['total_return', 'max_drawdown', 'annual_volatility', 'sharpe', 'num_trades']].agg(
            ['count', 'mean', 'median', 'min', 'max', 'std', 'var']).T
        stats['range'] = stats['max']-stats['min']
        summary['window_statistics'] = stats.to_dict(orient='index')
        print('\nAcross independent windows (sample variance/std; returns in decimal fractions):')
        print(stats.to_string())
    if not args.no_save:
        out = Path(args.out).expanduser() if args.out else Path(settings.backtest.results_dir) / (
            f'cointegration-pairs_30m_{pd.Timestamp.now(tz="UTC"):%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}')
        save_report(out, results, summary)
        print(f'Saved report: {(out / "report.html").resolve()}')
    return 0
