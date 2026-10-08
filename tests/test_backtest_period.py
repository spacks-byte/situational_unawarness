"""Relative ranges resolve once, include warm-up, and never use unfinished bars."""
import argparse
import json

import pandas as pd
import pytest

from tradebot.backtest import cli
from tradebot.backtest.period import relative_period
from tradebot.core.config import Settings
from tradebot.dashboard.research import BacktestRequest, perform_backtest
from tradebot.data.binance_vision import klines_path
from tradebot.data.loader import load_klines


NOW = '2025-01-02T00:07:42.987Z'


@pytest.mark.parametrize('interval,kwargs,start,end', [
    ('1s', {'last_minutes': 2}, '2025-01-02T00:05:42Z', '2025-01-02T00:07:42Z'),
    ('15m', {'last_hours': 1.5}, '2025-01-01T22:30:00Z', '2025-01-02T00:00:00Z'),
    ('5m', {'last_minutes': 10}, '2025-01-01T23:55:00Z', '2025-01-02T00:05:00Z'),
])
def test_relative_period(interval, kwargs, start, end):
    assert relative_period(interval, now=NOW, **kwargs) == (pd.Timestamp(start), pd.Timestamp(end))


@pytest.mark.parametrize('kwargs', [
    {}, {'last_hours': 1, 'last_minutes': 60}, {'last_hours': 0},
    {'last_minutes': -1}, {'last_hours': float('nan')}, {'last_hours': float('inf')},
    {'last_hours': 2161}, {'last_minutes': 2}, {'last_minutes': 16},
])
def test_invalid_durations(kwargs):
    with pytest.raises(ValueError):
        relative_period('15m', now=NOW, **kwargs)


def freeze(monkeypatch, module):
    target = module if isinstance(module, str) else module.__name__
    monkeypatch.setattr(f'{target}.relative_period', lambda interval, **kwargs: relative_period(interval, now=NOW, **kwargs))


def test_dashboard_freezes_relative_range_and_exports_effective_times(monkeypatch):
    freeze(monkeypatch, 'tradebot.dashboard.research')
    request = BacktestRequest(strategy='ma_crossover', symbols=['BTC'], interval='5m', last_minutes=30, fast=2, slow=3)
    start, end = request.resolved_period
    monkeypatch.setattr('tradebot.dashboard.research.relative_period', lambda *a, **kw: pytest.fail('Range was resolved again'))

    class Market:
        def candles(self, symbol, interval, begin, finish):
            assert begin == start - pd.Timedelta(minutes=25)
            assert finish == end
            index = pd.date_range(begin, finish, freq='5min', inclusive='left')
            return pd.DataFrame(dict(open=100., high=102., low=98., close=100., volume=1.), index=index)

    result, exports = perform_backtest(request, Market())
    assert result['config']['start'] == start.isoformat()
    assert result['config']['end'] == end.isoformat()
    assert result['config']['last_minutes'] == 30
    assert len(result['equity']) == 6
    assert pd.Timestamp(result['equity'][0][0]) == start
    assert pd.Timestamp(result['equity'][-1][0]) == end - pd.Timedelta(minutes=5)
    assert 'quotes' in exports and 'trades' in exports
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('kwargs', [
    {}, {'last_minutes': 1, 'last_hours': 1},
    {'start': '2025-01-01', 'last_hours': 1},
    {'end': '2025-01-02', 'last_hours': 1},
    {'last_minutes': 2}, {'last_hours': float('nan')},
])
def test_dashboard_rejects_ambiguous_or_invalid_ranges(kwargs):
    with pytest.raises(ValueError):
        BacktestRequest(**kwargs)


def parse(*flags):
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers())
    return parser.parse_args(['backtest', *flags])


def test_cli_mm_resolves_one_second_range(monkeypatch):
    freeze(monkeypatch, cli)
    def run_mm(args, settings):
        assert args.start == '2025-01-02T00:05:42+00:00'
        assert args.end == '2025-01-02T00:07:42+00:00'
        return 0
    monkeypatch.setattr(cli, 'run_mm', run_mm)
    assert cli.run(parse('--strategy', 'mm-10m-fluctuation', '--last-minutes', '2'), Settings()) == 0


def test_cli_relative_run_preserves_warmup_and_benchmark_period(monkeypatch, tmp_path):
    freeze(monkeypatch, cli)
    def load(symbols, interval, start, end, **kwargs):
        assert pd.Timestamp(start) == pd.Timestamp('2025-01-01T23:00Z')
        index = pd.date_range(start, end, freq='5min', inclusive='left')
        return {'BTC': pd.DataFrame(dict(open=100., high=102., low=98., close=100., volume=1.), index=index)}
    monkeypatch.setattr(cli, 'load_universe', load)
    settings = Settings(backtest={'results_dir': str(tmp_path)})
    args = parse('--strategy', 'ma_crossover', '--symbols', 'BTC', '--interval', '5m',
                 '--params', 'fast=2,slow=5', '--last-minutes', '30')
    assert cli.run(args, settings) == 0
    summary = json.loads(next(tmp_path.glob('*/summary.json')).read_text())
    equity = pd.read_csv(next(tmp_path.glob('*/equity.csv')), index_col=0)
    assert len(equity) == 6
    assert not equity.buy_and_hold.isna().any()
    assert summary['start'] == '2025-01-01T23:35:00+00:00'
    assert summary['end'] == '2025-01-02T00:05:00+00:00'
    assert summary['last_minutes'] == 30


def test_cli_relative_run_rejects_missing_tail(monkeypatch):
    freeze(monkeypatch, cli)
    index = pd.date_range('2025-01-01', periods=3, freq='5min', tz='UTC')
    monkeypatch.setattr(cli, 'load_universe', lambda *a, **kw: {'BTC': pd.DataFrame({'close': 100.}, index=index)})
    with pytest.raises(ValueError, match='BTC: missing candles'):
        cli.run(parse('--strategy', 'ma_crossover', '--symbols', 'BTC', '--last-hours', '1'), Settings())


@pytest.mark.parametrize('flags', [('--start', '2025-01-01'), ('--end', '2025-01-02'), ('--windows',)])
def test_cli_rejects_mixed_period_options(flags):
    with pytest.raises(ValueError, match='without --start'):
        cli.run(parse('--strategy', 'rxm', '--last-hours', '1', *flags), Settings())


def test_loader_accepts_utc_offsets(tmp_path):
    path = klines_path(tmp_path, 'BTC', '5m')
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({'close': [1, 2, 3]}, index=pd.date_range('2025-01-01', periods=3, freq='5min', tz='UTC'))
    frame.to_parquet(path)
    loaded = load_klines('BTC', '5m', '2025-01-01T08:05:00+08:00', '2025-01-01T00:10:00Z', tmp_path)
    assert loaded.close.tolist() == [2]
