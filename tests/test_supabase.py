import json

from tradebot.engine.state.portfolio import PortfolioStore
from tradebot.core.clock import SimClock
from tradebot.telemetry.supabase import SupabaseTradeUploader


def canceled():
    return dict(intent_id='x',order_id='1',strategy='rxm',coin='PEPE',side='BUY',
        status='CANCELED',quantity=10,price=99,filled=10,value=990,fee=0,submitted_at=1,
        row=dict(OrderID=1,Pair='PEPE/USD',Side='BUY',Status='CANCELED',Quantity=10,
                 FilledQuantity=10,FilledAverPrice=0,Price=99,CommissionChargeValue=0))


def test_telemetry_independently_rejects_phantom_fills():
    assert SupabaseTradeUploader.transaction(canceled(),environment='live',bot_id='b') is None
    correction=SupabaseTradeUploader.transaction(canceled(),environment='live',bot_id='b',correction=True)
    assert correction['status']=='CANCELED' and correction['filled_quantity']==0
    assert not correction['filled'] and correction['average_fill_price'] is None


def test_revision_aware_upload_does_not_acknowledge_a_newer_correction(tmp_path):
    store=PortfolioStore(tmp_path/'portfolio.db',SimClock())
    class Uploader:
        def upload(self,payload):
            with store.db:
                store.queue_upload('x',{'corrected':True})
    store.trade_uploader=Uploader()
    with store.db:
        store.queue_upload('x',{'wrong':True})
    store.flush_trade_uploads()
    payload,uploaded,revision=store.db.execute('SELECT payload,uploaded,revision FROM trade_uploads').fetchone()
    assert json.loads(payload)=={'corrected':True} and uploaded==0 and revision==2
