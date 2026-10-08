"""CLI backtests preserve archived outcomes and refuse incomplete evidence."""
import argparse
from datetime import timezone
import json
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from tradebot.backtest import cli, pairs
from tradebot.core.cointegration import CointegrationConfig
from tradebot.core.config import Settings
from tradebot.data.binance_vision import klines_path
from tradebot.data.market import klines_rows_to_frame
from tradebot.strategy.library.cointegration import common_closes
from tests.test_cointegration import fixture


def args(*flags):
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers())
    return parser.parse_args(['backtest', '--strategy', 'cointegration-pairs', *flags])


@pytest.mark.parametrize('case', ['recent_24h', 'fortnight_43'])
def test_cli_archived_report_and_costs(tmp_path, case):
    config, _, folder = fixture(case)
    output = tmp_path / case
    command = args('--start', config.cycle_start.isoformat(), '--end', config.cycle_end.isoformat(),
                   '--data-dir', str(folder / 'data'), '--no-download-missing', '--out', str(output))
    assert cli.run(command, Settings()) == 0
    summary = json.loads((output / 'summary.json').read_text())
    expected = pd.read_csv(folder / 'expected/portfolio_equity.csv')
    equity = pd.read_csv(output / 'equity.csv')
    for key in ['equity', 'net_pnl', 'fees', 'slippage']:
        np.testing.assert_allclose(equity[key], expected[key], rtol=1e-9, atol=1e-8)
    expected_trades = pd.read_csv(folder / 'expected/trades.csv')
    trades = pd.read_csv(output / 'trades.csv')
    assert len(trades) == len(expected_trades)
    assert trades.net_pnl.sum() == pytest.approx(equity.net_pnl.iloc[-1])
    weights = pd.read_csv(output / 'weights.csv')
    assert weights.budget.sum() == pytest.approx(config.reference_capital)
    assert summary['cycles'][0]['metrics']['final_equity'] == equity.equity.iloc[-1]
    assert summary['costs'] == dict(long_fee=.0005, short_open_fee=.001, short_close_fee=.001, slippage_bps=2.)
    assert summary['num_windows'] == 1
    assert len(pd.read_csv(output / 'signals.csv.gz')) == len(equity)*13
    assert '<svg' in (output / 'report.html').read_text()
    with pytest.raises(ValueError, match='new directory'):
        cli.run(command, Settings())


def test_market_costs_charge_extra_long_fee_without_changing_quantities():
    config, data, _ = fixture()
    reference = pairs.run_cycle(data, config)
    market = pairs.run_cycle(data, config, costs=dict(long_fee=.001))
    pd.testing.assert_series_equal(reference.trades.quantity_a, market.trades.quantity_a)
    pd.testing.assert_series_equal(reference.trades.quantity_b, market.trades.quantity_b)
    notional = sum(row[f'quantity_{leg}']*(row[f'entry_price_{leg}']+row[f'exit_price_{leg}'])
                   for _, row in market.trades.iterrows() for leg in ('a', 'b') if row[f'side_{leg}'] == 'long')
    assert market.metrics['fees']-reference.metrics['fees'] == pytest.approx(notional*.0005)
    assert reference.metrics['net_pnl']-market.metrics['net_pnl'] == pytest.approx(notional*.0005)


def test_independent_windows_reset_and_refit(tmp_path):
    config, data, folder = fixture('fortnight_43')
    out = tmp_path / 'windows'
    assert cli.run(args('--start', config.cycle_start.isoformat(), '--end', config.cycle_end.isoformat(),
        '--windows', '--window-days', '7', '--cash', '20000', '--data-dir', str(folder / 'data'),
        '--no-download-missing', '--out', str(out)), Settings()) == 0
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['num_windows'] == 2
    for i, cycle in enumerate(summary['cycles'], 1):
        assert cycle['config']['reference_capital'] == 20000
        standalone = pairs.run_cycle(data, CointegrationConfig.model_validate(cycle['config']))
        pd.testing.assert_frame_equal(pd.read_csv(out / f'window_{i:03d}' / 'weights.csv'), standalone.weights,
                                      check_exact=False, rtol=1e-10)
        assert cycle['metrics']['net_pnl'] == pytest.approx(standalone.metrics['net_pnl'])
    returns = [c['metrics']['total_return'] for c in summary['cycles']]
    assert summary['window_statistics']['total_return']['mean'] == pytest.approx(np.mean(returns))
    assert summary['window_statistics']['total_return']['var'] == pytest.approx(np.var(returns, ddof=1))


def test_history_downloads_only_gaps_and_keeps_other_dates(tmp_path):
    start, end = pd.Timestamp('2025-01-01T00:00Z'), pd.Timestamp('2025-01-01T02:00Z')
    index = pd.date_range(start-pairs.STEP, end, freq=pairs.STEP)
    complete = pd.DataFrame(dict(open=10., close=11.), index=index)
    path = klines_path(tmp_path, 'FIL', '30m')
    path.parent.mkdir(parents=True)
    complete.drop(index[2:4]).to_parquet(path)
    calls = []
    def fetch(asset, a, b):
        calls.append((asset, a, b))
        return complete.loc[(complete.index >= a) & (complete.index < b)]
    data, provenance = pairs.load_history(['FIL'], start, end, tmp_path, fetch=fetch)
    assert calls == [('FIL', index[2], index[4])]
    assert len(data['FIL']) == 4
    pd.testing.assert_frame_equal(pd.read_parquet(path), complete, check_freq=False)
    assert provenance[0]['downloads']
    calls.clear()
    pairs.load_history(['FIL'], start, end, tmp_path / 'fresh', fetch=fetch)
    assert calls == [('FIL', start, end)]
    pairs.load_history(['FIL'], start, end, tmp_path, download_missing=False,
                       fetch=lambda *a: pytest.fail('Unexpected network access'))
    damaged = complete.copy()
    damaged.iloc[2, 0] = -1
    damaged.to_parquet(path)
    with pytest.raises(ValueError, match='invalid open'):
        pairs.load_history(['FIL'], start, end, tmp_path, fetch=fetch)


@pytest.mark.parametrize('cached_unit', ['ms', 'us', 'ns'])
def test_real_rest_format_merges_with_parquet_and_trains_on_exact_timestamps(tmp_path, cached_unit):
    start, end = pd.Timestamp('2025-01-01T00:00Z'), pd.Timestamp('2025-01-01T02:00Z')
    index = pd.date_range(start, end, freq=pairs.STEP, inclusive='left')
    cached = pd.DataFrame(dict(open=10., close=11.),
                          index=index[2:].as_unit(cached_unit).tz_convert(ZoneInfo('UTC')))
    path = klines_path(tmp_path, 'FIL', '30m')
    path.parent.mkdir(parents=True)
    cached.to_parquet(path)
    def fetch(asset, a, b):
        rows = [[int(ts.timestamp()*1000), 10, 12, 9, 11, 1, 0, 1, 1, 1, 1, 0]
                for ts in pd.date_range(a, b, freq=pairs.STEP, inclusive='left')]
        result = klines_rows_to_frame(rows)
        result.index = result.index.tz_convert(timezone.utc)
        return result
    data, _ = pairs.load_history(['FIL'], start, end, tmp_path, fetch=fetch)
    assert (data['FIL'].index == index).all()
    # Training and observations have their own grid check; both must accept this merge.
    closes = common_closes(data, ['FIL'], start, end)
    np.testing.assert_array_equal(closes.FIL, [11.]*4)
    reloaded, _ = pairs.load_history(['FIL'], start, end, tmp_path, download_missing=False)
    assert (reloaded['FIL'].index == index).all()


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'reversed', 'off_grid', 'naive'])
def test_timestamp_comparison_still_rejects_invalid_grids(damage):
    start, end = pd.Timestamp('2025-01-01T00:00Z'), pd.Timestamp('2025-01-01T02:00Z')
    index = pd.date_range(start, end, freq=pairs.STEP, inclusive='left').as_unit('ns')
    if damage == 'missing':
        index = index.delete(1)
    elif damage == 'duplicate':
        index = index.insert(1, index[0])
    elif damage == 'reversed':
        index = index[::-1]
    elif damage == 'off_grid':
        index += pd.Timedelta(nanoseconds=1)
    else:
        index = index.tz_localize(None)
    frame = pd.DataFrame(dict(open=10., close=11.), index=index)
    with pytest.raises(ValueError):
        pairs.validate_candles(frame, 'FIL', start, end)
    with pytest.raises(ValueError):
        common_closes({'FIL': frame}, ['FIL'], start, end)


def test_missing_history_aborts_and_future_or_invalid_periods_are_rejected(tmp_path):
    with pytest.raises(ValueError, match='missing .* candles'):
        pairs.load_history(['FIL'], pd.Timestamp('2025-01-01T00:00Z'), pd.Timestamp('2025-01-02T00:00Z'),
                           tmp_path, download_missing=False)
    for flags in [('--start', '2025-01-01', '--end', '2025-01-01T00:31Z'),
                  ('--start', '2099-01-01', '--end', '2099-01-02'),
                  ('--last-hours', '1', '--start', '2025-01-01'),
                  ('--last-minutes', '15'),
                  ('--start', '2025-01-01', '--end', '2025-01-16', '--windows'),
                  ('--start', '2025-01-01', '--end', '2025-01-29', '--windows', '--step-days', '1'),
                  ('--last-hours', '24', '--interval', '1h'),
                  ('--last-hours', '24', '--borrow-rate', '.1')]:
        with pytest.raises(ValueError):
            cli.run(args(*flags), Settings())


def test_relative_cli_resolves_to_completed_candles(monkeypatch, tmp_path):
    config, _, folder = fixture()
    def resolve(interval, **kw):
        assert interval == '30m' and kw['last_hours'] == 24
        return pd.Timestamp(config.cycle_start), pd.Timestamp(config.cycle_end)
    monkeypatch.setattr(pairs, 'relative_period', resolve)
    out = tmp_path / 'relative'
    assert cli.run(args('--last-hours', '24', '--data-dir', str(folder / 'data'),
        '--no-download-missing', '--pair-cost-model', 'market', '--out', str(out)), Settings()) == 0
    assert json.loads((out / 'summary.json').read_text())['costs']['long_fee'] == .001


def test_no_trade_cycle_has_readable_empty_export(tmp_path):
    config, data, _ = fixture()
    cfg = CointegrationConfig.model_validate(config.model_dump() | dict(
        cycle_end=config.cycle_start+pairs.STEP, pairs=[dict(pair='FIL-MIRA', entry_z=100., exit_z=.1)]))
    result = pairs.run_cycle(data, cfg)
    assert result.metrics['num_trades'] == 0 and result.metrics['total_return'] == 0
    out = tmp_path / 'empty'
    pairs.save_report(out, [result], dict(mode='test', costs={}, cycles=[result.metrics]))
    assert pd.read_csv(out / 'trades.csv').empty
    assert json.loads((out / 'summary.json').read_text())['cycles'][0]['sharpe'] is None


def test_bad_test_open_or_missing_bar_rejected_before_runtime(monkeypatch):
    config, data, _ = fixture()
    original = data['FIL'].copy()
    data['FIL'].loc[config.cycle_start, 'open'] = np.nan
    monkeypatch.setattr(pairs, 'PairRuntime', lambda *a, **kw: pytest.fail('Validation must precede initialization'))
    with pytest.raises(ValueError, match='invalid open'):
        pairs.run_cycle(data, config)
    data['FIL'] = original.drop(config.cycle_start)
    with pytest.raises(ValueError, match='incomplete'):
        pairs.run_cycle(data, config)
