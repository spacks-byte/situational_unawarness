from copy import deepcopy
import json

import pytest

from tradebot.exchange.order_state import normalize_order, OrderEvidenceError, pending_quantity
from tradebot.engine.state.portfolio import MM, AccountCoordinator, PortfolioStore
from tests.test_shared_portfolio import setup_account, prepare, fill_next
from tests.venue_shaped import venue_account


def row(**changes):
    return dict(dict(OrderID=1, Pair='PEPE/USD', Side='BUY', Status='CANCELED',
        Quantity=10, FilledQuantity=10, Price=99, FilledAverPrice=0,
        CoinChange=0, UnitChange=0, CommissionChargeValue=0, CommissionCoin='PEPE'), **changes)


@pytest.mark.parametrize('status', ['PENDING','CANCELED','CANCELLED','REJECTED'])
@pytest.mark.parametrize('filled', [0,10])
def test_nonexecution_placeholder_is_not_a_trade(status, filled):
    execution = normalize_order(row(Status=status,FilledQuantity=filled))
    assert execution.filled == execution.price == execution.fee == 0
    assert execution.status == ('CANCELED' if status=='CANCELLED' else status)
    assert pending_quantity(row(Status=status)) == (10 if status=='PENDING' else 0)


@pytest.mark.parametrize('changes', [dict(Status='PARTIALLY_FILLED'), dict(FilledQuantity=4),
    dict(FilledAverPrice=99), dict(CoinChange=1), dict(CommissionChargeValue=.5),
    dict(Quantity=float('nan')), dict(CommissionChargeValue=-1), dict(Status='FILLED'),
    dict(Status='FILLED',FilledAverPrice=99,CommissionCoin='USD')])
def test_contradictory_or_partial_evidence_is_rejected(changes):
    with pytest.raises(OrderEvidenceError):
        normalize_order(row(**changes))


def test_full_execution_requires_matching_identity_and_positive_evidence():
    good = row(Status='FILLED',FilledAverPrice=99,CoinChange=10,UnitChange=990,CommissionCoin='USD')
    expected = dict(order_id='1',coin='PEPE',side='BUY',quantity=10)
    assert normalize_order(good,expected).filled == 10
    for field,value in [('OrderID',2),('Pair','BONK/USD'),('Side','SELL'),('Quantity',11)]:
        with pytest.raises(OrderEvidenceError):
            normalize_order(dict(good,**{field:value}),expected)


@pytest.mark.parametrize('coin,side', [('PEPE','BUY'),('BONK','BUY'),('FIL','BUY'),('1000CHEEMS','SELL')])
def test_live_incident_shapes_do_not_change_accounting(tmp_path, coin, side):
    a, venue, clock, _ = venue_account(tmp_path)
    strategy = 'rxm' if coin=='FIL' else MM
    if coin=='FIL':
        venue.sim.bars['FIL/USD'] = venue.sim.bars['PEPE/USD'].copy()
        venue.sim.intervals['FIL/USD'] = '1s'
        a.rules['FIL/USD'] = a.rules['PEPE/USD'].copy()
    if side=='SELL':
        buy,=prepare(a,clock,coin=coin)
        a.submit(buy)
        clock.advance(1)
        venue.sim.bars[coin+'/USD'].loc[:,'low'] = 98
        a.sync()
        venue.sim.bars[coin+'/USD'].loc[:,'low'] = 100
    a.sync()
    before=deepcopy({k:a.state[k] for k in ['mm','rxm_quantity','rxm_cost','rxm_fees','expected_cash_assets']})
    if strategy==MM:
        order,=prepare(a,clock,coin=coin,side=side,price=101 if side=='SELL' else 99)
        a.submit(order)
    else:
        a.scoped('rxm').place_order(coin,'BUY',10,price=99)
        order=a.active('rxm')[0]
    a.cancel(strategy,order['order_id'])
    a.sync()
    for key,value in before.items():
        assert a.state[key] == value
    assert a.reservations(strategy) == (0.,{})
    assert not a.report()['issues']


def test_issues_never_escalate_to_trading_gates_and_survive_restart(tmp_path):
    a,sim,clock=setup_account(tmp_path)
    sim.free_usd+=100
    sim.coins['PEPE']=2
    for _ in range(8):
        a.sync()
    assert a.report()['issues']['coin:PEPE']['blocking'] is False
    assert a.refusal(MM,'PEPE','BUY') is None
    o,=prepare(a,clock)
    assert a.submit(o)['Success']
    a.cancel(MM,o['order_id'])
    a.store.close()
    store=PortfolioStore(tmp_path/'portfolio.db',clock)
    restarted=AccountCoordinator(sim,store,a.config,a.fees,clock)
    assert restarted.report()['issues']
    assert restarted.scoped('rxm').place_order('PEPE','BUY',1,price=99)['Success']


def test_legacy_restrictions_migrate_without_modifying_books(tmp_path):
    a,sim,clock=setup_account(tmp_path)
    a.state['restrictions']={'coin:PEPE':dict(reason='old mismatch',seen=20)}
    before=deepcopy(a.state['mm'])
    a.store.save('legacy')
    a.store.close()
    store=PortfolioStore(tmp_path/'portfolio.db',clock)
    assert 'restrictions' not in store.state and store.state['issues']['coin:PEPE']['blocking'] is False
    restarted=AccountCoordinator(sim,store,a.config,a.fees,clock)
    assert restarted.state['mm']==before
    assert restarted.refusal(MM,'PEPE','BUY') is None


def test_issue_does_not_allow_spending_phantom_resources(tmp_path):
    a,sim,clock=setup_account(tmp_path)
    a.owner('PEPE')['quantity']=1000
    a.owner('PEPE')['cash']=1e9
    sim.free_usd=5
    a.sync()
    assert a.refusal(MM,'PEPE','BUY') is None
    assert prepare(a,clock)==[]
    assert prepare(a,clock,side='SELL',price=101)==[]


def test_invalid_order_evidence_is_serializable_and_other_orders_continue(tmp_path):
    a,sim,clock=setup_account(tmp_path)
    original=sim.place_order
    def malformed(*args,**kwargs):
        result=original(*args,**kwargs)
        result['OrderDetail']['FilledQuantity']=float('nan')
        return result
    sim.place_order=malformed
    order,=prepare(a,clock)
    assert a.submit(order)['Success']
    assert a.owner('PEPE')['quantity']==0
    assert a.report()['issues']
    sim.place_order=original
    # A malformed extra pending row must not halt reconciliation of good rows.
    original_pending=sim.list_open_orders
    sim.list_open_orders=lambda: {'OrderMatched': original_pending()['OrderMatched']+[{'Quantity':float('nan')}]}
    assert a.sync()
    assert a.refusal(MM,'BONK','BUY') is None
    other,=prepare(a,clock,coin='BONK')
    assert a.submit(other)['Success']


def test_other_strategies_cannot_spend_fees_reserved_for_pending_orders(tmp_path):
    a,sim,clock=setup_account(tmp_path)
    order,=prepare(a,clock)
    a.submit(order)
    sim.free_usd=1.2
    a.sync()
    # 0.495 is still owed when the existing order fills; this small new order
    # would overcommit cash even though its principal alone fits venue Free.
    assert prepare(a,clock,coin='BONK',quantity=.01)==[]


def test_retrying_uncertain_intent_preserves_reservation_and_does_not_resubmit(tmp_path):
    from tradebot.engine.state.portfolio import AccountBlocked
    a,sim,clock=setup_account(tmp_path)
    original=sim.place_order
    def timeout(*args,**kwargs):
        original(*args,**kwargs)
        raise TimeoutError('lost response')
    sim.place_order=timeout
    order,=prepare(a,clock)
    with pytest.raises(AccountBlocked):
        a.submit(order)
    assert a.submit(order)['Pending']
    assert order['status']=='SUBMITTING' and len(sim.history)==1
    assert a.reservations(MM)[0]>0
    a.sync()
    assert order['status']=='PENDING' and len(sim.history)==1
