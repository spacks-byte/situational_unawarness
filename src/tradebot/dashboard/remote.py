"""Read-only Supabase trade ledger and Binance public market data adapters."""
from __future__ import annotations

import math
import os
import re
import threading
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from tradebot.core.symbols import to_coin
from tradebot.data.binance_vision import klines_path
from tradebot.live.market_data import klines_rows_to_frame


class DataError(RuntimeError):
    """Safe message that can be shown in the browser (never raw HTTP exceptions)."""


def utc(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError('A valid UTC timestamp is required')
    return stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp.tz_convert('UTC')


def number(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


def get_json(url, **kwargs):
    try:
        response = requests.get(url, timeout=(8, 30), **kwargs)
        if not response.ok:
            raise DataError(f'Data provider returned HTTP {response.status_code}. Check connectivity and configuration.')
        return response.json()
    except (requests.RequestException, ValueError):
        raise DataError('Data provider is unreachable or returned invalid data. Retry shortly.') from None


class SupabaseLedger:
    def __init__(self):
        load_dotenv(Path.cwd() / '.env')
        self.url = os.getenv('SUPABASE_URL', '').rstrip('/')
        self.key = os.getenv('SUPABASE_SERVICE_KEY', '')
        self._cache = None
        self._expires = 0
        self._lock = threading.Lock()

    def rows(self):
        with self._lock:
            if self._cache is not None and time.monotonic() < self._expires:
                return self._cache
            if not self.url or not self.key:
                raise DataError('Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env, then restart the dashboard.')
            headers = {'apikey': self.key}
            if not self.key.startswith('sb_secret_'):
                headers['Authorization'] = 'Bearer ' + self.key
            rows = []
            # Paginate until an empty page: projects can cap results below our requested limit.
            while True:
                batch = get_json(self.url + '/rest/v1/trade_transactions', headers=headers, params={
                    'select': '*', 'environment': 'eq.live', 'order': 'created_at.asc,id.asc',
                    'offset': len(rows), 'limit': 1000,
                })
                if not isinstance(batch, list):
                    raise DataError('Unexpected Supabase trade_transactions response.')
                if not batch:
                    break
                rows.extend(batch)
                if len(rows) > 200_000:
                    raise DataError('Trade history exceeds 200,000 records; add server-side account/date partitioning.')
            # Upserts and retries may represent the same exchange order. Latest cumulative fill wins.
            unique = {}
            for row in rows:
                key = (row.get('bot_id'), row.get('environment'), row.get('exchange_order_id') or row['id'])
                if key not in unique or str(row.get('updated_at', '')) >= str(unique[key].get('updated_at', '')):
                    unique[key] = row
            self._cache = list(unique.values())
            self._expires = time.monotonic() + 15
            return self._cache


def normalize_order(row):
    response = row.get('exchange_response') or {}
    status = str(row.get('exchange_status') or row.get('status') or 'UNKNOWN').upper()
    qty = number(row.get('filled_quantity'))
    price = number(row.get('average_fill_price'))
    if not price and qty:
        price = number(row.get('filled_value_usd')) / qty
    submitted = row.get('submitted_at') or row.get('created_at')
    # The ledger stores aggregate fills, so only the final fill timestamp is available.
    finished = number(response.get('FinishTimestamp'))
    fill_time = pd.Timestamp(finished, unit='ms', tz='UTC').isoformat() if finished > 0 else (row.get('resolved_at') or submitted)
    if status in {'CANCELED', 'CANCELLED', 'EXPIRED'}:
        state = 'cancelled'
    elif status == 'FILLED':
        state = 'filled'
    elif status in {'NEW', 'OPEN', 'PENDING', 'SENT', 'PENDING_SEND', 'PARTIALLY_FILLED'}:
        state = 'pending'
    elif status in {'REJECTED', 'REJECTED_RISK', 'FAILED'}:
        state = 'rejected'
    else:
        state = 'unknown'
    side = {'SHORT_OPEN': 'SHORT', 'SHORT_CLOSE': 'COVER'}.get(row.get('side'), row.get('side'))
    return dict(id=row['id'], bot=row.get('bot_id') or 'unknown', strategy=row.get('strategy') or 'unattributed',
                symbol=to_coin(row.get('symbol') or row.get('pair')), side=side,
                order_type=row.get('order_type') or 'UNKNOWN', status=state, raw_status=row.get('status'),
                exchange_status=status, quantity=number(row.get('requested_quantity')),
                price=number(row.get('requested_price'), None), filled_quantity=qty,
                fill_price=price or None, fee=number(row.get('fee_amount')),
                fee_currency=row.get('fee_currency') or 'USD', time=submitted, fill_time=fill_time,
                filled=qty > 0, exchange_order_id=row.get('exchange_order_id'))


def reconstruct_portfolio(orders, marks):
    """Rebuild positions and strategy P&L from the same weighted-average books."""
    books = {}
    warnings = []
    for order in sorted(orders, key=lambda o: (utc(o['fill_time']), o['id'])):
        # Dashboard policy: partial orders do not affect the live position book,
        # even when their exchange status is cancelled and a fill quantity exists.
        if any(str(order.get(field) or '').upper() == 'PARTIALLY_FILLED'
               for field in ('raw_status', 'exchange_status')):
            continue
        q, p = order['filled_quantity'], order['fill_price']
        if q <= 0:
            continue
        side = order['side']
        key = (order['bot'], order['strategy'], order['symbol'], 'SHORT' if side in {'SHORT', 'COVER'} else 'LONG')
        book = books.setdefault(key, dict(quantity=0.0, cost=0.0, realized=0.0, complete=True))
        if not p or side not in {'BUY', 'SELL', 'SHORT', 'COVER'}:
            book['complete'] = False
            warnings.append(f"{order['strategy']} / {order['symbol']}: fill price or side is missing.")
            continue
        fee = order['fee']
        base_fee = fee if order['fee_currency'].upper() == order['symbol'] else 0
        if fee and not base_fee and order['fee_currency'].upper() not in {'USD', 'USDT'}:
            warnings.append(f"{order['symbol']}: fee in {order['fee_currency']} cannot be valued in USDT.")
        if side in {'BUY', 'SHORT'}:
            net_q = q - base_fee if side == 'BUY' else q
            book['quantity'] += net_q
            book['cost'] += q * p
        else:
            close_q = q + base_fee if side == 'SELL' else q
            avg = book['cost'] / book['quantity'] if book['quantity'] > 0 else 0
            if close_q > book['quantity'] + max(1e-8, close_q * 1e-9):
                book['complete'] = False
                warnings.append(f"{order['strategy']} / {order['symbol']}: closing fill exceeds recorded holdings; position is incomplete.")
            matched = min(close_q, book['quantity'])
            book['realized'] += q * p - matched * avg if side == 'SELL' else matched * (avg - p)
            book['quantity'] = max(0, book['quantity'] - close_q)
            book['cost'] = book['quantity'] * avg
    positions = []
    # Keep closed books in strategy totals; realized P&L survives liquidation of a position.
    summaries = {(o['bot'], o['strategy']): dict(
        bot=o['bot'], strategy=o['strategy'], realized_pnl=0.0, unrealized_pnl=0.0,
        open_positions=0, complete=True) for o in orders}
    for (bot, strategy, symbol, side), book in books.items():
        summary = summaries[(bot, strategy)]
        if not book['complete']:
            summary.update(realized_pnl=None, unrealized_pnl=None, complete=False)
        elif summary['realized_pnl'] is not None:
            summary['realized_pnl'] += book['realized']
        q = book['quantity']
        if q <= 1e-12 and book['complete']:
            continue
        mark = marks.get(symbol)
        entry = book['cost'] / q if q and book['complete'] else None
        signed = q * (1 if side == 'LONG' else -1)
        unrealized = signed * (mark - entry) if mark is not None and entry is not None else None
        summary['open_positions'] += 1
        if unrealized is None:
            summary['unrealized_pnl'] = None
        elif summary['unrealized_pnl'] is not None:
            summary['unrealized_pnl'] += unrealized
        positions.append(dict(bot=bot, strategy=strategy, symbol=symbol, side=side,
                              quantity=signed if book['complete'] else None, entry=entry, price=mark,
                              pnl=unrealized,
                              complete=book['complete']))
    for summary in summaries.values():
        summary['total_pnl'] = (summary['realized_pnl'] + summary['unrealized_pnl']
                                if summary['realized_pnl'] is not None and summary['unrealized_pnl'] is not None else None)
    return positions, sorted(summaries.values(), key=lambda s: (s['strategy'], s['bot'])), list(dict.fromkeys(warnings))


def reconstruct_positions(orders, marks):
    """Position-only interface for existing callers."""
    positions, _, warnings = reconstruct_portfolio(orders, marks)
    return positions, warnings


class BinanceData:
    def __init__(self, data_dir='data/binance', cache_dir='var/dashboard/candles'):
        self.data_dir = data_dir
        self.cache_dir = Path(cache_dir)
        self.base = 'https://data-api.binance.vision'
        self._marks = None
        self._mark_expiry = 0
        self._mark_lock = threading.Lock()

    def marks(self, symbols):
        with self._mark_lock:
            if self._marks is None or time.monotonic() >= self._mark_expiry:
                rows = get_json(self.base + '/api/v3/ticker/price')
                self._marks = {r['symbol']: number(r['price'], None) for r in rows}
                self._mark_expiry = time.monotonic() + 10
                self.mark_time = pd.Timestamp.now(tz='UTC').isoformat()
            return {s: self._marks.get(s + 'USDT') for s in symbols}

    def candles(self, symbol, interval, start, end):
        if not re.fullmatch(r'[A-Z0-9]{2,25}', symbol) or interval not in {'1m', '5m', '15m', '1h'}:
            raise ValueError('Unsupported symbol or interval')
        step = pd.Timedelta(interval.replace('m', 'min'))
        start, end = utc(start).floor(step), utc(end).floor(step)
        if end <= start:
            return klines_rows_to_frame([])
        filename = f'{symbol}-{interval}-{start.value}-{end.value}.parquet'
        cached = self.cache_dir / filename
        if cached.exists():
            return pd.read_parquet(cached)
        local = klines_path(self.data_dir, symbol, interval)
        expected = pd.date_range(start, end, freq=step, inclusive='left')
        if local.exists():
            data = pd.read_parquet(local)
            if expected.isin(data.index).all():
                return data.reindex(expected)
        rows, cursor = [], int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        while cursor < end_ms:
            batch = get_json(self.base + '/api/v3/klines', params=dict(
                symbol=symbol + 'USDT', interval=interval, startTime=cursor, endTime=end_ms-1, limit=1000))
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][0]) + int(step.total_seconds()*1000)
            if next_cursor <= cursor:
                raise DataError('Binance returned non-advancing candles.')
            cursor = next_cursor
        data = klines_rows_to_frame(rows)
        data = data[~data.index.duplicated()].sort_index()
        data = data[(data.index >= start) & (data.index < end)]
        # Cache only immutable, completed windows with full coverage.
        if expected.isin(data.index).all():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            import uuid
            temp = cached.with_suffix('.' + uuid.uuid4().hex + '.tmp')
            data.to_parquet(temp)
            temp.replace(cached)
        return data


def live_payload(ledger, market):
    orders = [normalize_order(row) for row in ledger.rows()]
    symbols = sorted({o['symbol'] for o in orders})
    warnings = ['Positions and strategy P&L are reconstructed from recorded fills, excluding PARTIALLY_FILLED orders, not an exchange balance snapshot. P&L excludes cash trading fees; coin-denominated fees adjust holdings. USD entries are treated as USDT at 1:1.']
    try:
        marks = market.marks(symbols) if symbols else {}
        mark_time = getattr(market, 'mark_time', None)
    except DataError as exc:
        marks, mark_time = {}, None
        warnings.append(str(exc) + ' Binance marks and unrealized P&L are unavailable.')
    positions, strategy_pnl, issues = reconstruct_portfolio(orders, marks)
    warnings.extend(issues)
    missing = [s for s in symbols if marks.get(s) is None]
    if missing:
        warnings.append('No Binance spot USDT mark: ' + ', '.join(missing))
    return dict(orders=sorted(orders, key=lambda o: o['time'], reverse=True), positions=positions, strategy_pnl=strategy_pnl,
                strategies=sorted({o['strategy'] for o in orders}), bots=sorted({o['bot'] for o in orders}),
                as_of=pd.Timestamp.now(tz='UTC').isoformat(), mark_time=mark_time, warnings=warnings)


def execution_window(orders, strategy, bot=None):
    selected = [o for o in orders if o['strategy'] == strategy and (not bot or o['bot'] == bot)]
    if not selected:
        raise ValueError('No recorded live activity for this strategy and account.')
    start = min(utc(o['time']) for o in selected)
    end = max(utc(o['fill_time']) for o in selected)
    # TODO: use explicit strategy_runs activation/deactivation intervals once available.
    # No heartbeat or deployment table exists: never claim last observation means still active.
    return start, max(start, end), selected
