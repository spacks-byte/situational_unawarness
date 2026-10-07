"""Strict, bounded one-second history and explicit historical instrument assumptions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
import requests

from tradebot.data.binance_vision import KLINE_COLUMNS, download, klines_path
from tradebot.live.shared_replay import cached_seconds

SECOND = pd.Timedelta(seconds=1)
COLUMNS = ['open', 'high', 'low', 'close', 'volume', 'trades']


def file_source(path, provider):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return dict(path=str(path.resolve()), sha256=digest, provider=provider)


def utc(value):
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        raise ValueError('A valid UTC timestamp is required')
    return ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')


def validate_seconds(frame, symbol, start, end):
    expected = pd.date_range(start, end, freq='s', inclusive='left')
    if not isinstance(frame.index, pd.DatetimeIndex) or not frame.index.equals(expected):
        missing = expected.difference(frame.index)
        detail = f'first missing {missing[0].isoformat()}' if len(missing) else 'duplicate, unordered or non-second timestamps'
        raise ValueError(f'{symbol}: incomplete 1s history {start.isoformat()} to {end.isoformat()} ({detail}). Configure an archive cache or enable downloads.')
    if not set(COLUMNS).issubset(frame):
        raise ValueError(f'{symbol}: history needs OHLC, volume and trade counts for {start} to {end}')
    v = frame[COLUMNS]
    bad = (~np.isfinite(v.to_numpy()).all(axis=1) | (v[['open','high','low','close']] <= 0).any(axis=1)
           | (v.high < v[['open','close','low']].max(axis=1))
           | (v.low > v[['open','close','high']].min(axis=1))
           | (v.volume < 0) | (v.trades < 0) | (v.trades % 1 != 0)
           | ((v.trades == 0) != (v.volume == 0)))
    if 'taker_buy_base' in frame:
        t = frame.taker_buy_base
        bad |= ~np.isfinite(t) | (t < 0) | (t > v.volume + np.maximum(1e-12, v.volume * 1e-10))
    if bad.any():
        raise ValueError(f'{symbol}: invalid OHLC/volume/trades at {frame.index[np.flatnonzero(bad)[0]].isoformat()}')
    return frame


def _parquet_window(path, start, end):
    # Even a multi-year local parquet is read with bounded memory.
    import pyarrow.parquet as pq
    parts = []
    for batch in pq.ParquetFile(path).iter_batches(batch_size=86400):
        frame = batch.to_pandas()
        selected = frame[(frame.index >= start) & (frame.index < end)]
        if len(selected):
            parts.append(selected)
    return pd.concat(parts) if parts else pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz='UTC'))


def _zip_window(path, start, end):
    parts = []
    with zipfile.ZipFile(path) as archive:
        with archive.open(archive.namelist()[0]) as stream:
            for raw in pd.read_csv(stream, header=None, names=KLINE_COLUMNS, chunksize=86400):
                if str(raw.iloc[0]['open_time']) == 'open_time':
                    raw = raw.iloc[1:]
                ts = pd.to_numeric(raw.pop('open_time'), errors='raise')
                if (ts % 1 != 0).any():
                    raise ValueError(f'Non-integral archive timestamp in {path}')
                ts = ts.astype('int64')
                # Preserve fractional seconds for strict timestamp validation.
                raw.index = pd.to_datetime(ts, unit='us' if len(ts) and ts.iloc[0] >= 10**14 else 'ms', utc=True)
                frame = raw.loc[(raw.index >= start) & (raw.index < end), COLUMNS + ['taker_buy_base']]
                parts.append(frame.apply(pd.to_numeric, errors='raise'))
    return pd.concat(parts)


class SecondHistory:
    """Archives first, then REST only for recent unpublished archive tails.

    TODO — Superday: connect an additional archive provider through this normalized
    candle interface. Credentials, schemas and source precedence are deferred.
    """
    def __init__(self, config, data_dir='data/binance', base='https://data-api.binance.vision'):
        self.config, self.data_dir, self.base = config, Path(data_dir), base
        self.provenance = []

    def candles(self, symbol, start, end):
        start, end = utc(start), utc(end)
        parts = []
        for day in pd.date_range(start.floor('D'), (end-SECOND).floor('D'), freq='D'):
            a, b = max(day, start), min(day+pd.Timedelta(days=1), end)
            try:
                frame, sources = self._day(symbol, a, b)
                validate_seconds(frame, symbol, a, b)
            except Exception as exc:
                raise ValueError(f'{symbol} {a.isoformat()} to {b.isoformat()}: {exc}') from exc
            parts.append(frame)
            self.provenance.append(dict(symbol=symbol, start=a.isoformat(), end=b.isoformat(), sources=sources))
        return pd.concat(parts)

    def _day(self, symbol, start, end):
        expected = pd.date_range(start, end, freq='s', inclusive='left')
        frames, sources = [], []
        def add(frame, source):
            if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
                raise ValueError(f'duplicate/unordered candles in {source}')
            if len(frame):
                # Validate observations before merging; another source must not hide corruption.
                _validate_sparse(frame, symbol)
                frames.append(frame)
                sources.append(source)
        def merged():
            if not frames:
                return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz='UTC'))
            out = pd.concat(frames)
            return out[~out.index.duplicated(keep='first')].sort_index()
        def complete():
            return expected.isin(merged().index).all()
        for root in self.config.archive_cache_dirs:
            path = Path(root).expanduser() / 'binance' / 'spot' / f'{symbol}USDT' / '1s' / f'{start.date()}.bin'
            if path.exists():
                frame = cached_seconds(Path(root).expanduser(), symbol, start, end)
                add(frame, file_source(path, 'verified binary archive'))
                if complete():
                    return merged(), sources
        store = Path(self.config.candle_store_dir) / '1s' / symbol
        for suffix in ['.parquet', '.csv']:
            path = store / f'{start.date()}{suffix}'
            if path.exists():
                if suffix == '.parquet':
                    frame = _parquet_window(path, start, end)
                else:
                    frame = pd.read_csv(path)
                    frame.index = pd.to_datetime(frame.pop('open_time_ms'), unit='ms', utc=True)
                    frame = frame[(frame.index >= start) & (frame.index < end)]
                add(frame, file_source(path, 'local candle store'))
        local = klines_path(self.data_dir, symbol, '1s')
        if not complete() and local.exists():
            add(_parquet_window(local, start, end), file_source(local, 'local klines'))
        cached = self.data_dir / 'seconds' / symbol / f'{start.date()}.parquet'
        if not complete() and cached.exists():
            add(_parquet_window(cached, start, end), file_source(cached, 'normalized Binance cache'))
        if complete():
            return merged(), sources
        raw_dir = self.data_dir / 'raw'
        pair = symbol + 'USDT'
        monthly = f'data/spot/monthly/klines/{pair}/1s/{pair}-1s-{start:%Y-%m}.zip'
        daily = f'data/spot/daily/klines/{pair}/1s/{pair}-1s-{start:%Y-%m-%d}.zip'
        today = pd.Timestamp.now(tz='UTC').floor('D')
        for remote in ([monthly, daily] if start < today.replace(day=1) else [daily]):
            path = raw_dir / remote
            if not path.exists():
                path = download(remote, raw_dir) if self.config.download_missing else None
            if path:
                add(_zip_window(path, start, end), file_source(path, 'Binance archive'))
                if complete():
                    break
        if not complete() and self.config.download_missing and start >= today-pd.Timedelta(days=3):
            missing = expected.difference(merged().index)
            rows, cursor = [], int(missing[0].timestamp()*1000)
            while cursor < int(end.timestamp()*1000):
                response = requests.get(self.base+'/api/v3/klines', params=dict(symbol=pair, interval='1s', startTime=cursor, endTime=int(end.timestamp()*1000)-1, limit=1000), timeout=(8, 30))
                response.raise_for_status()
                batch = response.json()
                if not batch:
                    break
                following = int(batch[-1][0])+1000
                if following <= cursor:
                    raise ValueError('non-advancing Binance REST candles')
                rows.extend(batch)
                cursor = following
            if rows:
                raw = pd.DataFrame(rows, columns=KLINE_COLUMNS)
                raw.index = pd.to_datetime(raw.pop('open_time'), unit='ms', utc=True)
                add(raw[COLUMNS+['taker_buy_base']].apply(pd.to_numeric), dict(provider='Binance REST', retrieved_at=pd.Timestamp.now(tz='UTC').isoformat()))
        result = merged()
        if complete():
            # Persist only full UTC days, never replace a full cache with a partial request.
            if start == start.floor('D') and end == start+pd.Timedelta(days=1):
                cached.parent.mkdir(parents=True, exist_ok=True)
                result.to_parquet(cached)
        return result, sources

    def chunks(self, symbols, start, end, warmup, progress=lambda _: None):
        cursor = start
        while cursor < end:
            stop = min(cursor.floor('D')+pd.Timedelta(days=1), end)
            begin = cursor-pd.Timedelta(seconds=warmup) if cursor == start else cursor
            data, errors = {}, []
            for symbol in symbols:
                progress(f'Loading {symbol}: {begin.isoformat()} to {stop.isoformat()}')
                try:
                    data[symbol] = self.candles(symbol, begin, stop)
                except ValueError as exc:
                    errors.append(str(exc))
            if errors:
                raise ValueError('MM history unavailable: ' + '; '.join(errors))
            yield data
            cursor = stop


def _validate_sparse(frame, symbol):
    # Gaps may be repaired by later providers; malformed observations may not.
    for _, group in frame.groupby((frame.index.to_series().diff() != SECOND).cumsum()):
        validate_seconds(group, symbol, group.index[0], group.index[-1]+SECOND)


def instrument_rules(config, symbols, base_url='https://mock-api.roostoo.com'):
    path = Path(config.instrument_rules_path).expanduser()
    try:
        saved = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(saved, dict):
            raise ValueError('expected an object')
    except (OSError, ValueError) as exc:
        raise ValueError(f'Invalid Roostoo metadata at {path}; set instrument_rules_path to saved exchangeInfo JSON') from exc
    rules = saved.get('TradePairs', saved.get('rules', saved))
    overrides = {s if '/' in s else s+'/USD': r for s, r in config.instrument_rules.items()}
    effective = {**rules, **{s: rules.get(s, {}) | r for s, r in overrides.items()}}
    required = {'CanTrade', 'PricePrecision', 'AmountPrecision', 'MiniOrder'}
    if any(not required.issubset(effective.get(s+'/USD', {})) for s in symbols) and config.download_missing:
        try:
            response = requests.get(base_url.rstrip('/')+'/v3/exchangeInfo', timeout=(8, 30))
            response.raise_for_status()
            saved = dict(response.json(), retrieved_at=pd.Timestamp.now(tz='UTC').isoformat())
            rules = saved['TradePairs']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(saved, indent=2))
            effective = {**rules, **{s: rules.get(s, {}) | r for s, r in overrides.items()}}
        except (requests.RequestException, ValueError, KeyError):
            raise ValueError('Cannot obtain Roostoo instrument rules. Set backtest.instrument_rules_path to saved exchangeInfo JSON or supply backtest.instrument_rules overrides.') from None
    source = file_source(path, 'Roostoo metadata') if path.is_file() else {'path': str(path)}
    return validate_instrument_rules(effective, symbols), dict(source=source, retrieved_at=saved.get('retrieved_at'), overrides=overrides,
                        assumption='Current-rule snapshot applied throughout history; not historical rule changes.')


def validate_instrument_rules(effective, symbols):
    chosen = {}
    for symbol in symbols:
        rule = effective.get(symbol+'/USD', {})
        try:
            valid = (rule['CanTrade'] is True and all(isinstance(rule[k], int) and not isinstance(rule[k], bool) and 0 <= rule[k] <= 18 for k in ['PricePrecision','AmountPrecision'])
                     and np.isfinite(float(rule['MiniOrder'])) and float(rule['MiniOrder']) >= 0)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError(f'{symbol}: missing/invalid tradable Roostoo rules. Supply PricePrecision, AmountPrecision, MiniOrder and CanTrade in backtest.instrument_rules or instrument_rules_path.')
        chosen[symbol+'/USD'] = rule
    return chosen
