"""Independent MM execution, history integrity and dashboard/CLI contracts."""
from io import StringIO
import json

import numpy as np
import pandas as pd
import pytest

from tradebot.backtest.history import SecondHistory, instrument_rules, validate_seconds
from tradebot.backtest.simulator import run_backtest
from tradebot.core.clock import SimClock
from tradebot.core.config import BacktestConfig, BacktestMMConfig, Settings
from tradebot.core.metrics import daily_returns
from tradebot.dashboard.research import BacktestRequest, perform_backtest
from tradebot.exchange.replay import ReplayExchangePort, order_penetration, touches_limit
from tradebot.strategy.library.mm_fluctuation import MMFluctuation

SYMBOLS = ['PEPE','BONK','1000CHEEMS']
RULES = {s+'/USD':dict(CanTrade=True, PricePrecision=0, AmountPrecision=8, MiniOrder=1) for s in SYMBOLS}
START = pd.Timestamp('2025-01-01',tz='UTC')


def data(seconds=12):
    index = pd.date_range(START-pd.Timedelta(seconds=4),periods=seconds+4,freq='s')
    frame = pd.DataFrame(dict(open=100.,high=102.,low=98.,close=100.,volume=1.,trades=1),index=index)
    return {s:frame.copy() for s in SYMBOLS}


def simulate(frames=None, **kwargs):
    cfg = BacktestConfig(mm=BacktestMMConfig(warmup_seconds=4,refresh_seconds=3), **kwargs)
    return run_backtest(MMFluctuation(cfg.mm), frames or data(), '1s', cfg, START, rules=RULES)


def test_real_quote_parity_refresh_reservations_and_accounting():
    frames = data()
    result = simulate(frames)
    cfg = result.config
    batch = MMFluctuation(cfg.mm).generate_quotes(frames,now=START,
        books={s:dict(capital=cfg.initial_cash*w,cash=cfg.initial_cash*w,quantity=0) for s,w in cfg.mm.allocations.items()},
        features={},rules=RULES)
    first = result.quotes[result.quotes.posted_at==START].set_index('symbol')
    for quote in batch.quotes:
        assert first.loc[quote.symbol,'limit_price']==quote.price
        assert first.loc[quote.symbol,'quantity']==quote.quantity
    assert set(first.side)=={'BUY'}
    assert not result.quotes.id.duplicated().any()
    assert (result.quotes.fill_time > result.quotes.posted_at).all()
    assert (result.quotes.fill_time <= result.quotes.expires_at).all()
    assert (result.trades.price==result.trades.quote_price).all()
    assert result.equity.iloc[-1]==pytest.approx(sum(b['equity'] for b in result.metadata['ending_books'].values()))
    for s,w in cfg.mm.allocations.items():
        fills = result.trades[result.trades.symbol==s]
        cash = cfg.initial_cash*w - (fills.value*np.where(fills.side=='BUY',1,-1)+fills.fee).sum()
        qty = (fills.quantity*np.where(fills.side=='BUY',1,-1)).sum()
        assert cash >= 0 and qty >= 0
        assert qty*100 <= cfg.initial_cash*w*cfg.mm.inventory_fraction
        assert result.metadata['ending_books'][s]['cash']==pytest.approx(cash)
        assert result.metadata['ending_books'][s]['quantity']==pytest.approx(qty)


def test_chunks_and_future_prices_do_not_change_earlier_quotes():
    frames=data()
    whole=simulate(frames)
    chunks=[{s:f.iloc[:9] for s,f in frames.items()}, {s:f.iloc[9:] for s,f in frames.items()}]
    split=simulate(iter(chunks))
    pd.testing.assert_frame_equal(whole.quotes,split.quotes)
    pd.testing.assert_series_equal(whole.equity,split.equity)
    for frame in frames.values():
        frame.loc[START+pd.Timedelta(seconds=6):,['open','high','low','close']] *= 10
    changed=simulate(frames)
    pd.testing.assert_frame_equal(whole.quotes[whole.quotes.posted_at<START+pd.Timedelta(seconds=6)].reset_index(drop=True),
                                  changed.quotes[changed.quotes.posted_at<START+pd.Timedelta(seconds=6)].reset_index(drop=True))


def test_empty_trade_candles_and_cancellations():
    frames=data()
    for frame in frames.values():
        frame['volume']=0
        frame['trades']=0
    result=simulate(frames)
    assert result.trades.empty
    assert (result.quotes.status=='cancelled').all()
    assert (result.quotes.finished_at==result.quotes.expires_at).all()
    assert result.equity.eq(result.config.initial_cash).all()


def test_final_candle_fills_before_refresh_cancel():
    frames=data(seconds=3)
    for frame in frames.values():
        frame.loc[START:,'low']=100
        frame.iloc[-1,frame.columns.get_loc('low')]=98
    result=simulate(frames)
    assert len(result.trades)==3
    assert (result.quotes.fill_time==result.quotes.expires_at).all()


def test_probability_extremes_reproducibility_and_lifetime_assignment():
    baseline=simulate()
    disabled=simulate(penetration_ticks=100,penetration_probability=0)
    pd.testing.assert_frame_equal(baseline.trades,disabled.trades)
    stressed=simulate(penetration_ticks=100,penetration_probability=1)
    assert stressed.trades.empty
    cfg=dict(penetration_ticks=2,penetration_probability=.4,random_seed=123)
    a,b=simulate(**cfg),simulate(dict(reversed(list(data().items()))),**cfg)
    pd.testing.assert_frame_equal(a.quotes,b.quotes)
    pd.testing.assert_frame_equal(a.trades,b.trades)
    assert set(a.quotes.assigned_penetration_ticks)=={0,2}
    for row in a.quotes.itertuples():
        assert row.assigned_penetration_ticks==order_penetration(123,int(row.posted_at.timestamp()),row.side=='BUY',2,.4)


def test_tolerance_and_direction():
    assert touches_limit(.00000999, .00001000, 1e-8,1,True)
    assert touches_limit(.00001001, .00001000, 1e-8,1,False)
    assert not touches_limit(.00001000,.00001000,1e-8,1,True)
    assert not touches_limit(.00001000,.00001000,1e-8,1,False)
    assert touches_limit(np.nextafter(.00001000,np.inf),.00001000,1e-8,0,True)


def test_liquidation_costs_replace_final_observation():
    held=simulate(data(seconds=3))
    closed=simulate(data(seconds=3),liquidate_mm=True,market_slippage_bps=20)
    assert closed.equity.index.equals(held.equity.index)
    assert len(daily_returns(closed.equity,closed.config.initial_cash))==1
    exits=closed.trades[closed.trades.terminal_exit]
    assert len(exits)==3 and (exits.price==99.8).all()
    cost=(exits.quantity*(100-exits.price)+exits.fee).sum()
    assert closed.equity.iloc[-1]==pytest.approx(held.equity.iloc[-1]-cost)
    assert closed.exposure.iloc[-1]==0
    assert all(b['quantity']==0 for b in closed.metadata['ending_books'].values())


def test_reservations_and_market_slippage():
    clock=SimClock(START.to_pydatetime())
    cfg=BacktestConfig(market_slippage_bps=100)
    port=ReplayExchangePort(data(),clock,initial_usd=100,intervals={s:'1s' for s in SYMBOLS},execution=cfg,rules=RULES)
    assert not port.place_order('PEPE','BUY',1,100)['Success']  # fee must be reserved
    assert port.place_order('PEPE','BUY',.5,99)['Success']
    locked=port.get_balance()['SpotWallet']['USD']['Lock']
    assert locked==pytest.approx(.5*99*1.0005)
    port.cancel_order()
    assert port.free_usd==100
    assert port.place_order('PEPE','BUY',.5,order_type='MARKET')['Success']
    assert port.fills[-1]['price']==101
    assert port.place_order('PEPE','SELL',.5,order_type='MARKET')['Success']
    assert port.fills[-1]['price']==99
    assert port.free_usd==pytest.approx(100-.5*101*1.001+.5*99*.999)
    opened=port.open_short('PEPE',10)
    assert opened['EntryPrice']==99
    closed=port.close_short('PEPE',close_pct=100)
    assert closed['ClosePrice']==101
    assert closed['CloseFee']==pytest.approx(10/99*101*.001)


@pytest.mark.parametrize('change',[lambda f:f.iloc[1:],lambda f:f.iloc[::-1],
    lambda f:pd.concat([f,f.iloc[-1:]]),lambda f:f.assign(high=99),lambda f:f.assign(volume=-1),
    lambda f:f.assign(trades=.5),lambda f:f.assign(trades=0),lambda f:f.assign(close=np.nan)])
def test_history_validation(change):
    frame=data()['PEPE']
    with pytest.raises(ValueError):validate_seconds(change(frame),'PEPE',frame.index[0],frame.index[-1]+pd.Timedelta(seconds=1))


def test_absent_cache_and_missing_rules_are_actionable(tmp_path):
    cfg=BacktestConfig(archive_cache_dirs=[str(tmp_path/'absent')],candle_store_dir=str(tmp_path/'store'),
                       instrument_rules_path=str(tmp_path/'rules.json'),download_missing=False)
    loader=SecondHistory(cfg,tmp_path/'data')
    with pytest.raises(ValueError,match='PEPE.*2025-01-01.*missing'):
        loader.candles('PEPE',START,START+pd.Timedelta(seconds=1))
    with pytest.raises(ValueError,match='PEPE.*instrument_rules'):
        instrument_rules(cfg,SYMBOLS)
    cfg.instrument_rules=RULES
    assert instrument_rules(cfg,SYMBOLS)[0]==RULES


@pytest.mark.parametrize('relative', [False, True])
def test_dashboard_mm_exports_and_effective_configuration(tmp_path, monkeypatch, relative):
    settings=Settings()
    settings.backtest.instrument_rules=RULES
    settings.backtest.instrument_rules_path=str(tmp_path/'rules.json')
    settings.backtest.download_missing=False
    settings.backtest.archive_cache_dirs=[]
    settings.backtest.candle_store_dir=str(tmp_path/'store')
    frames=data()
    for s,f in frames.items():
        for day,part in f.groupby(f.index.date):
            path=tmp_path/'store'/'1s'/s/f'{day}.parquet'
            path.parent.mkdir(parents=True,exist_ok=True)
            part.to_parquet(path)
    period = dict(start=START.isoformat(), end=(START+pd.Timedelta(seconds=12)).isoformat())
    if relative:
        from tradebot.backtest.period import relative_period
        monkeypatch.setattr('tradebot.dashboard.research.relative_period',
                            lambda interval, **kwargs: relative_period(interval, now=START+pd.Timedelta(seconds=12), **kwargs))
        period = dict(last_minutes=.2)
    req=BacktestRequest(strategy='mm-10m-fluctuation',**period,mm=dict(warmup_seconds=4,refresh_seconds=3))
    payload,exports=perform_backtest(req,object(),settings=settings)
    quotes=pd.read_csv(StringIO(exports['quotes']))
    trades=pd.read_csv(StringIO(exports['trades']))
    assert payload['quote_count']==len(quotes)
    assert len(trades)==payload['metrics']['num_trades']
    assert payload['metadata']['data_provenance']
    assert payload['metadata']['instrument_rules']==RULES
    assert payload['effective_config']['fees']['spot_maker']==.0005
    assert payload['metrics']['final_equity']==round(float(simulate().equity.iloc[-1]),2)
    json.dumps(payload,allow_nan=False)


def test_mm_request_rejects_incompatible_settings():
    base=dict(strategy='mm-10m-fluctuation',start='2025-01-01',end='2025-01-02')
    for change in [dict(interval='15m'),dict(maker_fee_bps=6),dict(mm={'reference_source':'midpoint'}),
                   dict(mm={'allocations':{'PEPE':.8,'BONK':.1,'1000CHEEMS':.2}}),dict(mm={'warmup_seconds':4,'feature_lag_seconds':4}),
                   dict(penetration_probability=2),dict(random_seed=-1),dict(market_slippage_bps=10000)]:
        with pytest.raises(ValueError):BacktestRequest(**(base|change))


def test_weight_limits_unchanged_and_short_market_losses_are_capped():
    from tests.test_backtest import Weights, bars, filled, IDX
    frames=bars([100]*4,[101]*4,[99]*4,[100]*4)
    plain=run_backtest(Weights(.5),frames,'15m',BacktestConfig())
    stress=run_backtest(Weights(.5),frames,'15m',BacktestConfig(market_slippage_bps=500))
    pd.testing.assert_frame_equal(plain.trades,stress.trades)
    weights=pd.DataFrame({'X':[-.5,-.5,0,0]},index=IDX[:4])
    covered=run_backtest(Weights(weights),frames,'15m',BacktestConfig(market_slippage_bps=100,rebalance_band=.2))
    cover=filled(covered).query("side=='COVER'").iloc[0]
    assert cover.price==101 and cover.fee==pytest.approx(500*101*.001)
    assert covered.equity.iloc[-1]==pytest.approx(100000-50-500-cover.fee)
    gap=bars([100,100,110,210],[100,101,111,215],[99,99,109,209],[100,100,110,210])
    liquidated=run_backtest(Weights(-.5),gap,'15m',BacktestConfig(market_slippage_bps=100,rebalance_band=.3))
    assert filled(liquidated).query("side=='LIQUIDATE'").iloc[0].price==212.1
    assert 49900<=liquidated.equity.iloc[-1]<=50000


def test_corrupt_verified_binary_is_not_hidden_by_another_source(tmp_path):
    root=tmp_path/'archive'
    path=root/'binance'/'spot'/'PEPEUSDT'/'1s'/'2025-01-01.bin'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'corrupt')
    path.with_suffix('.json').write_text(json.dumps(dict(schema_version=1,validation_version=2,binary_sha256='wrong')))
    cfg=BacktestConfig(archive_cache_dirs=[str(root)],download_missing=False)
    with pytest.raises(ValueError,match='PEPE.*2025-01-01.*corrupt'):
        SecondHistory(cfg,tmp_path/'data').candles('PEPE',START,START+pd.Timedelta(seconds=1))


def test_archive_then_recent_rest_tail_without_interpolation(tmp_path,monkeypatch):
    import tradebot.backtest.history as history
    start=pd.Timestamp.now(tz='UTC').floor('D')-pd.Timedelta(days=1)
    calls=[]
    monkeypatch.setattr(history,'download',lambda remote,root:calls.append('archive') or None)
    class Response:
        def raise_for_status(self):pass
        def json(self):
            return [[int(start.timestamp()*1000),100,101,99,100,1,0,100,1,.5,50,0],
                    [int((start+pd.Timedelta(seconds=1)).timestamp()*1000),100,101,99,100,1,0,100,1,.5,50,0]]
    monkeypatch.setattr(history.requests,'get',lambda *a,**k:calls.append('rest') or Response())
    cfg=BacktestConfig(archive_cache_dirs=[],candle_store_dir=str(tmp_path/'store'))
    result=SecondHistory(cfg,tmp_path/'data').candles('PEPE',start,start+pd.Timedelta(seconds=2))
    assert len(result)==2 and calls==['archive','rest']
    class Missing(Response):
        def json(self):return super().json()[1:]
    monkeypatch.setattr(history.requests,'get',lambda *a,**k:Missing())
    with pytest.raises(ValueError,match='first missing'):
        SecondHistory(cfg,tmp_path/'other').candles('PEPE',start,start+pd.Timedelta(seconds=2))


def test_native_splitmix64_golden_vectors():
    from pathlib import Path
    cases=json.loads((Path(__file__).parent/'fixtures/mm_execution_native.json').read_text())
    for case in cases:
        assert order_penetration(case['seed'],case['second'],case['buy'],3,.5)==case['assigned']


def test_refresh_longer_than_warmup_retains_unconsumed_features_across_chunks():
    frames=data(seconds=24)
    cfg=BacktestConfig(mm=BacktestMMConfig(warmup_seconds=4,refresh_seconds=10))
    def run(source):return run_backtest(MMFluctuation(cfg.mm),source,'1s',cfg,START,rules=RULES)
    chunks=[{s:f.iloc[a:b] for s,f in frames.items()} for a,b in [(0,9),(9,15),(15,21),(21,28)]]
    whole,split=run(frames),run(iter(chunks))
    pd.testing.assert_frame_equal(whole.quotes,split.quotes)
    pd.testing.assert_series_equal(whole.equity,split.equity)


def test_partial_rule_overrides_and_all_missing_symbols(tmp_path):
    path=tmp_path/'rules.json'
    path.write_text(json.dumps({'TradePairs':RULES}))
    cfg=BacktestConfig(instrument_rules_path=str(path),instrument_rules={'PEPE':{'PricePrecision':2}},
        archive_cache_dirs=[],candle_store_dir=str(tmp_path/'missing'),download_missing=False)
    effective,provenance=instrument_rules(cfg,SYMBOLS)
    assert effective['PEPE/USD']['PricePrecision']==2
    assert effective['PEPE/USD']['MiniOrder']==1
    assert len(provenance['source']['sha256'])==64
    with pytest.raises(ValueError) as error:
        next(SecondHistory(cfg,tmp_path/'data').chunks(SYMBOLS,START,START+pd.Timedelta(seconds=1),4))
    assert all(s in str(error.value) for s in SYMBOLS)


def test_custom_universe_and_zero_allocations_preserve_active_books():
    frames = {'BTC': data()['PEPE'], 'ETH': data()['BONK']}
    rules = {s+'/USD': RULES['PEPE/USD'] for s in frames}
    def run(allocations, candles):
        mm = BacktestMMConfig(allocations=allocations, warmup_seconds=4, refresh_seconds=3)
        return run_backtest(MMFluctuation(mm), candles, '1s', BacktestConfig(mm=mm), START, rules=rules)
    baseline = run({'BTC':.7, 'ETH':.3}, frames)
    disabled = run({'BTC':.7, 'ETH':.3, 'UNAVAILABLE':0}, frames)
    supplied_empty = run({'BTC':.7, 'ETH':.3, 'UNAVAILABLE':0}, frames | {'UNAVAILABLE':pd.DataFrame()})
    assert set(disabled.quotes.symbol) == {'BTC', 'ETH'}
    assert disabled.config.mm.allocations['UNAVAILABLE'] == 0
    assert set(disabled.metadata['instrument_rules']) == {'BTC/USD', 'ETH/USD'}
    pd.testing.assert_frame_equal(baseline.trades, disabled.trades)
    pd.testing.assert_series_equal(baseline.equity, disabled.equity)
    pd.testing.assert_series_equal(baseline.equity, supplied_empty.equity)
    assert sum(b['equity'] for b in disabled.metadata['ending_books'].values()) == pytest.approx(disabled.equity.iloc[-1])


@pytest.mark.parametrize('allocations', [
    {'BTC':1.1,'ETH':-.1}, {'BTC':0,'ETH':0}, {'BTC':.5}, {'BTC':float('nan')},
    {'../BTC':1}, {'btc':.5,'BTC/USD':.5}, {}, {f'COIN{i}':1/51 for i in range(51)},
])
def test_custom_allocations_reject_invalid_tickers_and_weights(allocations):
    with pytest.raises(ValueError):
        BacktestMMConfig(allocations=allocations)


def test_custom_request_normalization_and_live_allocation_policy():
    from tradebot.core.config import MarketMakingConfig
    req = BacktestRequest(strategy='mm-10m-fluctuation', start='2025-01-01', end='2025-01-02',
                          mm={'allocations':{' btc/usd ':1,'eth':0}})
    assert req.symbols == ['BTC','ETH']
    assert req.mm.allocations == {'BTC':1,'ETH':0}
    assert req.mm.active_symbols == ['BTC']
    single = BacktestRequest(strategy='mm-10m-fluctuation', start=req.start, end=req.end,
                             mm={'allocations':{'S':1}})
    assert single.mm.active_symbols == ['S']
    with pytest.raises(ValueError, match='allocation keys'):
        BacktestRequest.model_validate(req.model_dump() | {'symbols':['BTC']})
    assert MarketMakingConfig(allocations={'PEPE': 1}).allocations == {'PEPE': 1}
    live = MarketMakingConfig(allocations={'PEPE': 1}, refresh_seconds=180,
                              enforce_one_tick_distance=False)
    assert live.refresh_seconds == 180
    assert not live.enforce_one_tick_distance
    with pytest.raises(ValueError, match='positive fractions'):
        MarketMakingConfig(allocations={'PEPE': 0, 'BONK': 1})


def test_dashboard_and_cli_skip_zero_weight_history_and_rules(tmp_path, capsys):
    import argparse
    from tradebot.backtest.cli import add_parser
    settings = Settings()
    settings.backtest.instrument_rules = {'BTC/USD':RULES['PEPE/USD']}
    settings.backtest.instrument_rules_path = str(tmp_path/'missing-rules.json')
    settings.backtest.download_missing = False
    settings.backtest.archive_cache_dirs = []
    settings.backtest.candle_store_dir = str(tmp_path/'store')
    settings.data.dir = str(tmp_path/'data')
    for day, part in data()['PEPE'].groupby(data()['PEPE'].index.date):
        path = tmp_path/'store'/'1s'/'BTC'/f'{day}.parquet'
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)
    req = BacktestRequest(strategy='mm-10m-fluctuation', start=START.isoformat(),
        end=(START+pd.Timedelta(seconds=12)).isoformat(),
        mm={'allocations':{'BTC':1,'UNAVAILABLE':0},'warmup_seconds':4,'refresh_seconds':3})
    payload, exports = perform_backtest(req, object(), settings=settings)
    assert set(payload['prices']) == {'BTC'}
    assert {p['symbol'] for p in payload['metadata']['data_provenance']} == {'BTC'}
    assert payload['effective_config']['mm']['allocations'] == {'BTC':1,'UNAVAILABLE':0}
    assert set(pd.read_csv(StringIO(exports['quotes'])).symbol) == {'BTC'}
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    args = parser.parse_args(['backtest', '--strategy', 'mm-10m-fluctuation',
        '--start', req.start, '--end', req.end, '--allocations', 'BTC=1,UNAVAILABLE=0',
        '--mm-warmup-seconds','4','--refresh-seconds','3','--no-save'])
    assert args.handler(args, settings) == 0
    assert 'Loading UNAVAILABLE' not in capsys.readouterr().out


@pytest.mark.parametrize('minimum_order', [1, 1e9])
def test_pepe_only_dashboard_completes_with_no_fills(tmp_path, monkeypatch, minimum_order):
    from tradebot.backtest.history import SecondHistory
    settings = Settings()
    settings.backtest.download_missing = False
    settings.backtest.instrument_rules_path = str(tmp_path/'rules.json')
    settings.backtest.instrument_rules = {'PEPE/USD': RULES['PEPE/USD'] | {'MiniOrder':minimum_order}}
    loaded = []
    def candles(self, symbol, start, end):
        loaded.append(symbol)
        index = pd.date_range(start, end, freq='s', inclusive='left')
        return pd.DataFrame(dict(open=100.,high=102.,low=98.,close=100.,volume=0.,trades=0),index=index)
    monkeypatch.setattr(SecondHistory, 'candles', candles)
    req = BacktestRequest(strategy='mm-10m-fluctuation',symbols=['PEPE'],start=START.isoformat(),
        end=(START+pd.Timedelta(seconds=12)).isoformat(),
        mm={'allocations':{'PEPE':1},'warmup_seconds':4,'refresh_seconds':3})
    payload, exports = perform_backtest(req, object(), settings=settings)
    assert loaded == ['PEPE']
    assert payload['effective_config']['mm']['allocations'] == {'PEPE':1}
    assert payload['metrics']['final_equity'] == req.initial_capital
    assert payload['metrics']['num_trades'] == 0
    assert payload['quote_count'] == (4 if minimum_order == 1 else 0)
    trades = pd.read_csv(StringIO(exports['trades']))
    assert trades.empty and {'order_type','quantity','fee','symbol'}.issubset(trades.columns)
    assert pd.read_csv(StringIO(exports['quotes'])).shape[0] == payload['quote_count']
