"""Validated backtest requests, daily analytics and downloadable simulation artifacts."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradebot.backtest.simulator import run_backtest
from tradebot.core.config import BacktestConfig, FeeSchedule, BacktestMMConfig, Settings
from tradebot.core.metrics import compute_metrics, daily_returns
from tradebot.dashboard.remote import DataError, utc, execution_window
from tradebot.strategy.registry import STRATEGIES
from tradebot.strategy.library.rxm import UNIVERSE, preset


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    strategy: Literal['rxm', 'ma_crossover', 'mm-10m-fluctuation'] = 'rxm'
    preset: Literal['comp', 'neutral'] = 'comp'
    initial_capital: float = Field(default=100000, gt=0, le=1e10)
    start: str
    end: str
    interval: Literal['1s', '5m', '15m', '1h'] = '15m'
    symbols: list[str] = Field(default_factory=lambda: list(UNIVERSE), min_length=1, max_length=50)
    limit_offset_bps: float = Field(default=5, ge=0, le=1000)
    maker_fee_bps: float = Field(default=5, ge=0, le=100)
    taker_fee_bps: float = Field(default=10, ge=0, le=100)
    short_fee_bps: float = Field(default=10, ge=0, le=100)
    rebalance_band: float = Field(default=.01, ge=0, le=1)
    lockin_return: float = Field(default=.06, ge=0, le=1)
    lockin_scale: float = Field(default=.3, ge=0, le=1)
    k: int = Field(default=3, ge=1, le=20)
    tilt: float = Field(default=.3, ge=-1, le=1)
    gross: float = Field(default=1, gt=0, le=1)
    buffer: int = Field(default=2, ge=0, le=20)
    fast: int = Field(default=20, ge=1, le=1000)
    slow: int = Field(default=100, ge=2, le=5000)
    allow_short: bool = False
    mm: BacktestMMConfig = Field(default_factory=BacktestMMConfig)
    penetration_ticks: int = Field(default=0, ge=0)
    penetration_probability: float = Field(default=1, ge=0, le=1)
    random_seed: int = Field(default=0, ge=0, le=2**63-1)
    market_slippage_bps: float = Field(default=0, ge=0, lt=10000)
    liquidate_mm: bool = False
    instrument_rules: dict[str, dict] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def preset_defaults(cls, payload):
        if not isinstance(payload, dict):
            return payload
        if payload.get('strategy', 'rxm') == 'rxm' and payload.get('preset') == 'neutral':
            return dict(k=5, tilt=0, gross=.9, buffer=0, lockin_return=0) | payload
        if payload.get('strategy') == 'mm-10m-fluctuation':
            mm = payload.get('mm', {})
            allocations = mm.allocations if isinstance(mm, BacktestMMConfig) else mm.get('allocations', {}) if isinstance(mm, dict) else {}
            return dict(interval='1s', symbols=list(allocations) or ['PEPE','BONK','1000CHEEMS'], maker_fee_bps=5, lockin_return=0) | payload
        if payload.get('strategy') == 'ma_crossover':
            return {'lockin_return': 0} | payload
        return payload

    @model_validator(mode='after')
    def check_request(self):
        import re
        if self.strategy == 'mm-10m-fluctuation':
            if self.interval != '1s' or self.maker_fee_bps != 5:
                raise ValueError('MM uses a fixed 1s interval and frozen 5-bps maker fee.')
        elif self.interval == '1s':
            raise ValueError('The 1s interval is reserved for MM backtests.')
        start, end = utc(self.start), utc(self.end)
        step = pd.Timedelta(self.interval.replace('m', 'min'))
        if end <= start or end - start > pd.Timedelta(days=90):
            raise ValueError('Choose a positive backtest period of at most 90 days.')
        if start != start.floor(step) or end != end.floor(step):
            raise ValueError('Start and end must align with the selected candle interval.')
        if end > pd.Timestamp.now(tz='UTC').floor(step):
            raise ValueError('End must be at or before the latest completed candle.')
        self.symbols = list(dict.fromkeys(s.strip().upper().removesuffix('/USDT').removesuffix('/USD') for s in self.symbols))
        ticker_pattern = r'[A-Z0-9]{1,25}' if self.strategy == 'mm-10m-fluctuation' else r'[A-Z0-9]{2,25}'
        if any(not re.fullmatch(ticker_pattern, s) for s in self.symbols):
            raise ValueError('Symbols must be coin tickers, e.g. BTC, ETH, SOL.')
        if self.strategy == 'mm-10m-fluctuation' and set(self.symbols) != set(self.mm.allocations):
            raise ValueError('MM symbols must match the allocation keys, including any zero-weight symbols.')
        if self.strategy == 'rxm' and ('BTC' not in self.symbols or len(self.symbols) < 3):
            raise ValueError('RXM requires BTC and at least two other symbols.')
        if self.strategy == 'ma_crossover' and self.fast >= self.slow:
            raise ValueError('Fast MA must be smaller than slow MA.')
        return self


def records(frame):
    return json.loads(frame.to_json(orient='records', date_format='iso'))


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    return value


def points(series, limit=1800):
    if len(series) > limit:
        # Preserve extrema in each bucket so drawdown spikes remain visible.
        chosen = {0, len(series)-1}
        for group in np.array_split(np.arange(len(series)), limit // 2):
            values = series.iloc[group].to_numpy()
            chosen.update([int(group[np.argmin(values)]), int(group[np.argmax(values)])])
        series = series.iloc[sorted(chosen)]
    return [[t.isoformat(), float(v)] for t, v in series.items() if pd.notna(v)]


def perform_backtest(request, market, progress=lambda _: None, settings=None):
    start, end = utc(request.start), utc(request.end)
    step = pd.Timedelta(request.interval.replace('m', 'min'))
    if request.strategy == 'mm-10m-fluctuation':
        from tradebot.backtest.history import SecondHistory, instrument_rules
        settings = settings or Settings.load()
        config = settings.backtest.model_copy(deep=True)
        config.initial_cash = request.initial_capital
        config.mm = request.mm
        config.fees.spot_maker = .0005
        config.fees.spot_taker = request.taker_fee_bps/10000
        for key in ['penetration_ticks','penetration_probability','random_seed','market_slippage_bps','liquidate_mm']:
            setattr(config, key, getattr(request,key))
        config.instrument_rules = {**config.instrument_rules, **request.instrument_rules}
        symbols = request.mm.active_symbols
        rules, rule_info = instrument_rules(config, symbols, settings.exchange.base_url)
        history = SecondHistory(config, getattr(market, 'data_dir', settings.data.dir))
        chunks = history.chunks(symbols, start, end, request.mm.warmup_seconds, progress)
        strategy = STRATEGIES[request.strategy](request.mm)
        result = run_backtest(strategy, chunks, '1s', config, trade_start=start, rules=rules, progress=progress)
        result.metadata.update(data_provenance=history.provenance, rule_provenance=rule_info)
        data = {s: values.to_frame('close') for s,values in result.prices.items()}
    else:
        warmup = pd.Timedelta(days=45) if request.strategy == 'rxm' else step * (request.slow + 2)
        expected = pd.date_range(start - warmup, end, freq=step, inclusive='left')
        data = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(market.candles, s, request.interval, start-warmup, end): s for s in request.symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                frame = future.result()
                if not expected.isin(frame.index).all():
                    missing = len(expected.difference(frame.index))
                    raise DataError(f'{symbol}: {missing} missing candles in the requested period or warm-up. Choose another period or symbol universe.')
                if not np.isfinite(frame[['open', 'high', 'low', 'close', 'volume']].to_numpy()).all() or (frame[['open','high','low','close']] <= 0).any().any():
                    raise DataError(f'{symbol}: invalid candle prices.')
                data[symbol] = frame.reindex(expected)
                progress(f'Loaded {len(data)} / {len(request.symbols)} symbols (including warm-up)')
        data = {s: data[s] for s in request.symbols}
        if request.strategy == 'rxm':
            params, _ = preset(request.preset)
            params.update(k=request.k, tilt=request.tilt, gross=request.gross, buffer=request.buffer)
            strategy = STRATEGIES['rxm'](**params)
        else:
            strategy = STRATEGIES['ma_crossover'](fast=request.fast, slow=request.slow, allow_short=int(request.allow_short))
        config = BacktestConfig(initial_cash=request.initial_capital, limit_offset_bps=request.limit_offset_bps,
                                rebalance_band=request.rebalance_band, lockin_return=request.lockin_return,
                                lockin_scale=request.lockin_scale, market_slippage_bps=request.market_slippage_bps, fees=FeeSchedule(
                                    spot_maker=request.maker_fee_bps/10000, spot_taker=request.taker_fee_bps/10000,
                                    short_open=request.short_fee_bps/10000, short_close=request.short_fee_bps/10000))
        progress('Simulating orders and calculating daily metrics')
        result = run_backtest(strategy, data, request.interval, config, trade_start=start)
    metrics = compute_metrics(result.equity, request.interval, result.trades, initial=request.initial_capital)
    metrics['pnl'] = float(result.equity.iloc[-1] - request.initial_capital)
    returns = daily_returns(result.equity, request.initial_capital)
    metrics['daily_observations'] = len(returns)
    drawdown = result.equity / result.equity.cummax().clip(lower=request.initial_capital) - 1
    # An empty ledger has object-typed columns; use an explicit boolean mask so
    # pandas keeps its export columns instead of treating [] as column selection.
    trades = result.trades.loc[result.trades.filled.eq(True)].copy()
    if result.quotes is not None:
        quotes = result.quotes.copy()
        metrics['num_orders'] = len(quotes) + int((trades.order_type == 'MARKET').sum())
        metrics['fill_rate'] = float(quotes.filled.mean()) if len(quotes) else np.nan
    else:
        quotes = result.trades[result.trades.order_type == 'LIMIT'].copy()
        quotes = quotes.rename(columns={'price': 'execution_price', 'quote_price': 'limit_price'})
        quotes.loc[~quotes.filled, 'execution_price'] = np.nan
        quotes['expires_at'] = quotes['time'] + step
        quotes['status'] = np.where(quotes.filled, 'filled', 'expired')
    for frame in [trades, quotes]:
        frame.insert(0, 'strategy', request.strategy)
    payload = dict(config=request.model_dump(), effective_config=result.config.model_dump(), metadata=result.metadata, metrics=metrics,
                   equity=points(result.equity), drawdown=points(drawdown),
                   daily_returns=[[t.isoformat(), float(v)] for t,v in returns.items()],
                   prices={s: points(frame.loc[frame.index >= start, 'close']) for s,frame in data.items()},
                   trades=records(trades), quote_count=len(quotes),
                   notes=['Ratios use UTC daily returns (365-day annualization, zero risk-free rate). First and final partial days are included.',
                          'Drawdown includes every simulation bar and initial capital. Undefined ratios are shown as —.',
                          'Trade times indicate the simulation bar, not a known intrabar execution time. Quotes expire after one bar.'] if result.quotes is None else result.metadata['assumptions'] + ['Ratios use UTC daily returns (365-day annualization). Drawdown uses all one-second observations and initial capital.'])
    return clean(payload), {'quotes': quotes.to_csv(index=False), 'trades': trades.to_csv(index=False)}


def live_executions(orders, market, strategy, symbol, bot=None):
    start, end, selected = execution_window(orders, strategy, bot)
    fills = [dict(o, time=o['fill_time'], price=o['fill_price'], quantity=o['filled_quantity'])
             for o in selected if o['symbol'] == symbol and o['filled_quantity'] > 0 and o['fill_price']]
    # TODO: replace inferred first/last transaction bounds with a strategy_runs table
    # (strategy, bot_id, activated_at, deactivated_at, heartbeat_at), including restarts.
    # Recorded transactions cannot establish idle periods or whether a strategy is still active.
    interval = '1m' if end-start <= pd.Timedelta(days=2) else '15m'
    step = pd.Timedelta(interval.replace('m', 'min'))
    warning = None
    try:
        candles = market.candles(symbol, interval, start.floor(step), end.ceil(step))
        # Close timestamps must be within the recorded window (never beyond its final event).
        close = candles['close'].copy()
        close.index = close.index + step
        close = close[(close.index >= start) & (close.index <= end)]
        price_points = points(close)
        if not price_points:
            warning = 'No completed Binance candles within this recorded execution window.'
    except DataError as exc:
        price_points = []
        warning = str(exc) + ' Recorded executions remain available.'
    return dict(strategy=strategy, symbol=symbol, start=start.isoformat(), end=end.isoformat(),
                prices=price_points, trades=fills, warning=warning,
                window_basis='First submitted order to last recorded resolution; inferred activity, not deployment timestamps.',
                timestamp_basis='Markers use the recorded final fill time; partial fills are aggregated per order.')
