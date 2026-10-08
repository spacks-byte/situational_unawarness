from copy import deepcopy
import json

import pytest

from tests.test_shared_portfolio import setup_account, prepare, fill_next
from tradebot.engine.state.portfolio import MM
from tradebot.live.fill_repair import audit, apply_manifest, read_snapshot


def corrupt_cancel(tmp_path, side='BUY'):
    a,sim,clock=setup_account(tmp_path)
    if side=='SELL':
        o,=prepare(a,clock)
        a.submit(o)
        fill_next(a,sim,clock,98)
    before=deepcopy(a.owner('PEPE'))
    expected_cash=a.state['expected_cash_assets']
    o,=prepare(a,clock,side=side,price=99 if side=='BUY' else 101)
    a.submit(o)
    sim.cancel_order(order_id=o['order_id'])
    raw=sim.history[-1].copy()
    raw.update(FilledQuantity=10,FilledAverPrice=0,CoinChange=0,UnitChange=0,CommissionChargeValue=0)
    sim.history[-1].update(raw)
    value=o['quantity']*o['price']
    book=a.owner('PEPE')
    sign=1 if side=='BUY' else -1
    if side=='BUY':
        book['cost']+=value
    else:
        cost=book['cost']
        book['cost']=0
        book['realized_pnl']+=value-cost
    book['quantity']+=sign*10
    book['cash']-=sign*value
    a.state['expected_cash_assets']-=sign*value
    o.update(status='CANCELED',filled=10,value=value,row=raw)
    a.store.save('fill',dict(strategy=MM,coin='PEPE',side=side,quantity=10,value=value,
                             fee_delta=0,order_id=o['order_id']))
    a.store.close()
    return sim,before,expected_cash


@pytest.mark.parametrize('side',['BUY','SELL'])
def test_dry_run_and_idempotent_repair_restore_cash_inventory_and_cost(tmp_path,side):
    sim,before,cash=corrupt_cancel(tmp_path,side)
    path=tmp_path/'portfolio.db'
    original=read_snapshot(path)
    manifest=audit(path,sim,bot_id='test')
    assert read_snapshot(path)==original
    assert manifest['applicable'],manifest['errors']
    assert manifest['proposed_state']['mm']['PEPE']==pytest.approx(before)
    assert manifest['proposed_state']['expected_cash_assets']==pytest.approx(cash)
    result=apply_manifest(path,manifest,sim,bot_id='test')
    assert result['applied'] and result['uploads_queued']==1
    fixed=read_snapshot(path)
    assert fixed['state']['mm']['PEPE']==pytest.approx(before)
    assert fixed['uploads'][0]['payload']['status']=='CANCELED'
    assert fixed['uploads'][0]['payload']['filled_quantity']==0
    corrected=[o for o in fixed['completed'] if 'execution_correction' in o]
    assert corrected[0]['execution_correction']['original']['filled']==10
    assert apply_manifest(path,manifest,sim,bot_id='test')['already_applied']


def test_stale_manifest_never_applies(tmp_path):
    sim,_,_=corrupt_cancel(tmp_path)
    path=tmp_path/'portfolio.db'
    manifest=audit(path,sim,bot_id='test')
    sim.history[-1]['Status']='FILLED'
    sim.history[-1]['FilledAverPrice']=99
    with pytest.raises(ValueError,match='changed'):
        apply_manifest(path,manifest,sim,bot_id='test')
    assert not list(tmp_path.glob('*.before-fill-repair*'))


def test_missing_journal_does_not_guess_a_repair(tmp_path):
    import sqlite3
    sim,_,_=corrupt_cancel(tmp_path)
    path=tmp_path/'portfolio.db'
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM events WHERE kind='fill'")
    report=audit(path,sim,bot_id='test')
    assert not report['applicable']
    assert any('incomplete accounting history' in e for e in report['errors'])


def test_interrupted_repair_rolls_back_and_can_retry(tmp_path,monkeypatch):
    from tradebot.engine.state.portfolio import PortfolioStore
    sim,_,_=corrupt_cancel(tmp_path)
    path=tmp_path/'portfolio.db'
    original=read_snapshot(path)
    manifest=audit(path,sim,bot_id='test')
    queue=PortfolioStore.queue_upload
    def fail(*args):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(PortfolioStore,'queue_upload',fail)
    with pytest.raises(OSError):
        apply_manifest(path,manifest,sim,bot_id='test')
    assert read_snapshot(path)==original
    monkeypatch.setattr(PortfolioStore,'queue_upload',queue)
    assert apply_manifest(path,manifest,sim,bot_id='test')['applied']


def test_false_sell_repair_replays_later_real_transactions(tmp_path):
    import sqlite3
    sim,before,_=corrupt_cancel(tmp_path,'SELL')
    path=tmp_path/'portfolio.db'
    # A later real BUY should retain its own cost while the fictitious SELL is removed.
    with sqlite3.connect(path) as db:
        state=json.loads(db.execute('SELECT payload FROM portfolio WHERE id=1').fetchone()[0])
        book=state['mm']['PEPE']
        book['quantity']+=2
        book['cost']+=240
        book['cash']-=240
        state['expected_cash_assets']-=240
        db.execute('UPDATE portfolio SET payload=?',(json.dumps(state),))
        db.execute('INSERT INTO events(timestamp,kind,payload) VALUES (?,?,?)',
                   ('2026-09-21T02:00:00+00:00','fill',json.dumps(dict(strategy=MM,coin='PEPE',side='BUY',quantity=2,value=240,fee_delta=0,order_id='later'))))
    report=audit(path,sim,bot_id='test')
    assert report['applicable'],report['errors']
    book=report['proposed_state']['mm']['PEPE']
    assert book['quantity']==12
    assert book['cost']==pytest.approx(before['cost']+240)
    assert book['realized_pnl']==before['realized_pnl']
