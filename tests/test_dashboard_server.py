"""Dashboard behavior: partial fills, daily metrics, exports, windows and API boundaries."""
import json
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import numpy as np
import pandas as pd
import pytest

from tradebot.dashboard.remote import (
    BinanceData, SupabaseLedger, normalize_order, reconstruct_positions, reconstruct_portfolio, execution_window, DataError,
)
from tradebot.dashboard.research import BacktestRequest, perform_backtest, live_executions
from tradebot.dashboard.server import Dashboard, make_server


def row(id='1', **changes):
    if 'filled_quantity' in changes and 'requested_quantity' not in changes:
        changes['requested_quantity'] = changes['filled_quantity']
    return dict(dict(id=id, bot_id='a', environment='live', strategy='rxm', symbol='BTC',
                     side='BUY', order_type='LIMIT', status='FILLED', exchange_status='FILLED',
                     filled=True, requested_quantity=2, requested_price=100, filled_quantity=2,
                     average_fill_price=100, filled_value_usd=200, fee_amount=0, fee_currency='USD',
                     submitted_at='2025-01-01T00:00:00Z', resolved_at='2025-01-01T00:05:00Z',
                     created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:05:00Z', exchange_response={}), **changes)


def test_filled_orders_contribute_to_long_and_short_positions():
    a=normalize_order(row(filled_quantity=1))
    b=normalize_order(row('2',side='SELL',filled_quantity=.4,average_fill_price=120,resolved_at='2025-01-02T00:00:00Z'))
    c=normalize_order(row('3',side='SHORT_OPEN',symbol='ETH',filled_quantity=3,average_fill_price=200))
    d=normalize_order(row('4',side='SHORT_CLOSE',symbol='ETH',filled_quantity=1,average_fill_price=190,resolved_at='2025-01-02T00:00:00Z'))
    positions,warnings=reconstruct_positions([a,b,c,d],{'BTC':110,'ETH':180})
    assert not warnings
    btc=next(p for p in positions if p['symbol']=='BTC')
    eth=next(p for p in positions if p['symbol']=='ETH')
    assert btc['quantity']==pytest.approx(.6) and btc['pnl']==pytest.approx(6)
    assert eth['quantity']==-2 and eth['pnl']==40


@pytest.mark.parametrize('status,exchange_status', [
    ('PARTIALLY_FILLED', 'CANCELED'),
    ('PARTIALLY_FILLED', 'PARTIALLY_FILLED'),
    ('PARTIALLY_FILLED', 'FILLED'),
    ('FILLED', 'PARTIALLY_FILLED'),
    ('partially_filled', 'CANCELED'),
])
@pytest.mark.parametrize('side', ['BUY', 'SELL', 'SHORT_OPEN', 'SHORT_CLOSE'])
def test_partial_orders_do_not_create_or_modify_live_positions(status, exchange_status, side):
    partial = normalize_order(row('partial', status=status, exchange_status=exchange_status,
                                  side=side, filled_quantity=1, average_fill_price=120))
    # Retain the legacy order for audit, without drawing an execution.
    assert partial['filled_quantity'] == 0 and not partial['filled']
    assert reconstruct_positions([partial], {'BTC': 110}) == ([], [])
    opening_side = 'SHORT_OPEN' if side.startswith('SHORT') else 'BUY'
    complete = normalize_order(row(side=opening_side))
    expected = reconstruct_positions([complete], {'BTC': 110})
    assert reconstruct_positions([complete, partial], {'BTC': 110}) == expected


def test_account_strategy_isolation_and_missing_cost_basis():
    orders=[normalize_order(row()),normalize_order(row('2',strategy='other',average_fill_price=200)),
            normalize_order(row('3',bot_id='b',side='SELL',filled_quantity=5))]
    positions,warnings=reconstruct_positions(orders,{'BTC':110})
    assert len(positions)==3 and warnings
    assert next(p for p in positions if p['strategy']=='other')['pnl']==-180
    missing=next(p for p in positions if p['bot']=='b')
    assert missing['quantity'] is None and missing['pnl'] is None


def test_base_fee_and_unavailable_mark():
    positions,_=reconstruct_positions([normalize_order(row(fee_amount=.01,fee_currency='BTC'))],{})
    assert positions[0]['quantity']==1.99
    assert positions[0]['entry']==pytest.approx(200/1.99)
    assert positions[0]['pnl'] is None


def test_strategy_pnl_keeps_closed_books_and_separates_accounts():
    orders = [
        row(filled_quantity=2),
        row('2', filled_quantity=2, average_fill_price=120),
        row('3', side='SELL', filled_quantity=1, average_fill_price=130, resolved_at='2025-01-02T00:00:00Z'),
        row('4', symbol='ETH', side='SHORT_OPEN', filled_quantity=3, average_fill_price=200),
        row('5', symbol='ETH', side='SHORT_CLOSE', filled_quantity=3, average_fill_price=180, resolved_at='2025-01-02T00:00:00Z'),
        row('6', bot_id='b', average_fill_price=150),
        row('7', strategy='other', filled_quantity=1, average_fill_price=90),
        row('8', strategy='other', side='SELL', filled_quantity=1, average_fill_price=95, resolved_at='2025-01-02T00:00:00Z'),
        row('9', status='PARTIALLY_FILLED', exchange_status='CANCELED', side='SELL', filled_quantity=3, average_fill_price=999),
    ]
    positions, summaries, warnings = reconstruct_portfolio([normalize_order(o) for o in orders], {'BTC': 125})
    assert not warnings
    by_key = {(s['bot'], s['strategy']): s for s in summaries}
    a = by_key[('a', 'rxm')]
    assert a['realized_pnl'] == 80  # 20 on the long reduction + 60 on the closed short
    assert a['unrealized_pnl'] == 45  # 3 * (125 - 110)
    assert a['total_pnl'] == 125 and a['open_positions'] == 1
    assert by_key[('b', 'rxm')]['unrealized_pnl'] == -50
    assert by_key[('a', 'other')]['realized_pnl'] == 5
    assert by_key[('a', 'other')]['unrealized_pnl'] == 0
    assert by_key[('a', 'other')]['open_positions'] == 0
    assert len(positions) == 2


def test_strategy_pnl_missing_marks_preserves_realized_but_not_total():
    orders = [normalize_order(row()), normalize_order(row('2', side='SELL', filled_quantity=1,
              average_fill_price=120, resolved_at='2025-01-02T00:00:00Z'))]
    _, summaries, _ = reconstruct_portfolio(orders, {})
    assert summaries[0]['realized_pnl'] == 20
    assert summaries[0]['unrealized_pnl'] is None and summaries[0]['total_pnl'] is None
    _, incomplete, _ = reconstruct_portfolio(orders[1:], {'BTC': 120})
    assert incomplete[0]['realized_pnl'] is None and incomplete[0]['total_pnl'] is None


def test_strategy_pnl_with_only_partial_orders_is_zero():
    _, summaries, _ = reconstruct_portfolio([normalize_order(row(status='PARTIALLY_FILLED'))], {})
    assert summaries[0]['realized_pnl'] == summaries[0]['unrealized_pnl'] == summaries[0]['total_pnl'] == 0
    assert summaries[0]['open_positions'] == 0


def test_supabase_pagination_and_cumulative_dedup(monkeypatch):
    calls=[]
    def fake(url,headers,params):
        calls.append(params['offset'])
        if len(calls)==1:
            return [row(exchange_order_id='123',filled_quantity=.5)]
        if len(calls)==2:
            return [row('2',exchange_order_id='123',filled_quantity=1,updated_at='2025-01-02T00:00:00Z')]
        return []
    monkeypatch.setattr('tradebot.dashboard.remote.get_json',fake)
    ledger=SupabaseLedger();ledger.url='https://example.invalid';ledger.key='server-secret'
    result=ledger.rows()
    assert calls==[0,1,2] and len(result)==1 and result[0]['filled_quantity']==1
    assert ledger.rows()==result and calls==[0,1,2]


class FakeMarket:
    def candles(self,symbol,interval,start,end):
        index=pd.date_range(start,end,freq=pd.Timedelta(interval.replace('m','min')),inclusive='left')
        t=np.arange(len(index))
        close=100 + t*.01 + np.sin(t/14)*3
        return pd.DataFrame(dict(open=close,high=close+1,low=close-1,close=close,volume=np.ones(len(index))),index=index)
    def marks(self,symbols):
        return {s:110.0 for s in symbols}


def test_backtest_daily_metrics_csv_and_real_quotes():
    req=BacktestRequest(strategy='ma_crossover',symbols=['BTC'],start='2025-01-01',end='2025-01-05',
                        initial_capital=10000,fast=3,slow=10,lockin_return=0)
    result,exports=perform_backtest(req,FakeMarket())
    assert len(result['daily_returns'])==4
    returns=np.array([v for _,v in result['daily_returns']])
    assert result['metrics']['sharpe']==pytest.approx(returns.mean()/returns.std(ddof=1)*np.sqrt(365))
    assert result['metrics']['pnl']==pytest.approx(result['equity'][-1][1]-10000)
    assert len(result['trades'])>0
    assert 'limit_price' in exports['quotes'].splitlines()[0]
    assert 'expires_at' in exports['quotes'].splitlines()[0]
    assert all(t['filled'] for t in result['trades'])
    assert 'strategy' in exports['trades'].splitlines()[0]
    json.dumps(result,allow_nan=False)


def test_backtest_rejects_gaps_and_bad_configs():
    class Gapped(FakeMarket):
        def candles(self,*args):return super().candles(*args).iloc[1:]
    req=BacktestRequest(strategy='ma_crossover',symbols=['BTC'],start='2025-01-01',end='2025-01-02')
    with pytest.raises(DataError,match='missing candles'):
        perform_backtest(req,Gapped())
    for update in [{'initial_capital':-1},{'strategy':'unknown'},{'start':'2025-01-03'}, {'slow':2,'fast':20},{'end':'2100-01-01'}, {'symbols':['../../secret']}, {'maker_fee_bps':float('nan')}]:
        with pytest.raises(ValueError):BacktestRequest.model_validate(req.model_dump()|update)


def test_preset_defaults_allow_explicit_overrides():
    neutral=BacktestRequest(preset='neutral',start='2025-01-01',end='2025-01-02')
    assert (neutral.k,neutral.tilt,neutral.gross,neutral.buffer,neutral.lockin_return)==(5,0,.9,0,0)
    custom=BacktestRequest(preset='neutral',k=2,start='2025-01-01',end='2025-01-02')
    assert custom.k==2


def test_live_window_filters_strategy_account_and_has_no_outside_points():
    orders=[normalize_order(row()),normalize_order(row('2',strategy='other',resolved_at='2025-01-04T00:00:00Z')),
            normalize_order(row('3',bot_id='b',resolved_at='2025-01-05T00:00:00Z'))]
    start,end,selected=execution_window(orders,'rxm','a')
    assert len(selected)==1 and end==pd.Timestamp('2025-01-01T00:05:00Z')
    chart=live_executions(orders,FakeMarket(),'rxm','BTC','a')
    assert len(chart['trades'])==1
    assert all(start<=pd.Timestamp(t)<=end for t,_ in chart['prices'])
    assert chart['trades'][0]['time']==orders[0]['fill_time']
    assert 'not deployment' in chart['window_basis']


def test_simulator_preserves_quote_price_on_gap_improvement():
    from tradebot.backtest.simulator import run_backtest
    from tradebot.core.config import BacktestConfig
    class AlwaysLong:
        def generate_weights(self,data):return pd.DataFrame({'BTC':1.0},index=data['BTC'].index)
    frame=pd.DataFrame({'open':[100,90,90],'high':[101,95,95],'low':[99,89,89], 'close':[100,90,90], 'volume':[1,1,1]},index=pd.date_range('2025-01-01',periods=3,freq='15min',tz='UTC'))
    result=run_backtest(AlwaysLong(),{'BTC':frame},'15m',BacktestConfig(limit_offset_bps=5))
    fill=result.trades.iloc[0]
    assert fill.quote_price==pytest.approx(99.95)
    assert fill.price==90 and fill.filled


def test_http_routes_do_not_serve_secrets_and_require_same_origin():
    class Ledger:
        def rows(self):return [row()]
    app=Dashboard(ledger=Ledger(),market=FakeMarket())
    server=make_server(0,app)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    root=f'http://127.0.0.1:{server.server_port}'
    def fetch(path,**kwargs):
        with urlopen(Request(root+path,**kwargs),timeout=5) as response:return response.read()
    try:
        assert b'Trading desk' in fetch('/')
        config=json.loads(fetch('/api/config'))
        assert 'SUPABASE' not in json.dumps(config)
        assert json.loads(fetch('/api/live'))['positions'][0]['pnl']==20
        for path in ['/.env','/../.env','/api/backtests/missing']:
            with pytest.raises(HTTPError) as e:fetch(path)
            assert e.value.code==404
        with pytest.raises(HTTPError) as e:fetch('/api/live',headers={'Host':'evil.invalid'})
        assert e.value.code==403
        with pytest.raises(HTTPError) as e:fetch('/api/backtests',data=b'{}',headers={'Content-Type':'application/json'})
        assert e.value.code==403
        with pytest.raises(HTTPError) as e:fetch('/api/backtests',data=b'{}',headers={'Content-Type':'application/json','X-Dashboard-Token':config['csrf']})
        assert e.value.code==400
    finally:
        server.shutdown();server.server_close();app.executor.shutdown(wait=True)
