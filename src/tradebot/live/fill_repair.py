"""Auditable correction of phantom spot fills. Default operations are read-only.

Rebuild affected MM books from their opening allocation and the full fill journal.
RXM imports require an opening_books baseline unless a false BUY can be reversed
without subsequent cost-basis dependencies. Never guess a strategy's ownership
from today's wallet. Applying a manifest only queues telemetry; it sends no orders.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from tradebot.core.clock import RealClock
from tradebot.engine.state.portfolio import PortfolioStore, MM, order_rows
from tradebot.engine.state.issues import ReconciliationIssues
from tradebot.exchange.order_state import normalize_order, OrderEvidenceError
from tradebot.live.preflight import ReadOnlyPort
from tradebot.telemetry.supabase import SupabaseTradeUploader


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def read_snapshot(path):
    with sqlite3.connect(f'file:{Path(path).resolve().as_posix()}?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        state = json.loads(db.execute('SELECT payload FROM portfolio WHERE id=1').fetchone()[0])
        completed = [json.loads(r[0]) for r in db.execute('SELECT payload FROM completed_orders ORDER BY intent_id')]
        events = [dict(id=i, timestamp=t, kind=k, payload=json.loads(p)) for i,t,k,p in
                  db.execute('SELECT id,timestamp,kind,payload FROM events ORDER BY id')]
        uploads = [dict(intent_id=i, payload=json.loads(p), uploaded=u) for i,p,u in
                   db.execute('SELECT intent_id,payload,uploaded FROM trade_uploads ORDER BY intent_id')]
    return dict(state=state, completed=completed, events=events, uploads=uploads)


def _replay(book, events, strategy, coin, corrected_ids):
    book = deepcopy(book)
    since_sync = []
    for event in events:
        p = event['payload']
        if event['kind']=='reconciled':
            since_sync=[]
        if event['kind']=='fill' and abs(p.get('value',0)):
            since_sync.append(p)
        if event['kind'] == 'cash_rounding_adjustment':
            shares=p.get('shares')
            if shares is None and strategy in p.get('owners', []) and p.get('diff'):
                total=sum(abs(f['value']) for f in since_sync)
                if not total or abs(total-p.get('fill_notional',0)) > max(1e-7,total*1e-9):
                    raise ValueError('cash rounding adjustment needs an attributable per-book baseline')
                shares=[dict(strategy=f['strategy'],coin=f['coin'],amount=p['diff']*abs(f['value'])/total) for f in since_sync]
            for share in shares or []:
                if (share['strategy'],share['coin']) == (strategy,coin):
                    book['cash']+=share['amount']
                    book['fees']-=share['amount']
        if event['kind'] != 'fill' or p.get('strategy') != strategy or p.get('coin') != coin:
            continue
        if str(p.get('order_id')) in corrected_ids:
            continue
        qty, value, fee = float(p['quantity']), float(p['value']), float(p.get('fee_delta', 0))
        if p['side'] == 'BUY':
            book['cash'] -= value+fee
            book['quantity'] += qty
            book['cost'] += value+fee
        elif p['side'] == 'SELL':
            if qty > book['quantity']+1e-8:
                raise ValueError('corrected history sells more inventory than the strategy owned')
            cost = book['cost']*qty/book['quantity'] if book['quantity'] else 0
            book['quantity'] -= qty
            book['cost'] -= cost
            book['cash'] += value-fee
            book['realized_pnl'] += value-fee-cost
        else:
            raise ValueError('spot repair cannot replay short executions')
        book['fees'] += fee
    return book


def audit(path, port, *, bot_id, remote_rows=()):
    snapshot = read_snapshot(path)
    state = snapshot['state']
    venue = ReadOnlyPort(port)
    orders = snapshot['completed'] + list(state.get('orders', {}).values())
    by_intent = {o['intent_id']: o for o in orders}
    remote = [r for r in remote_rows if r.get('bot_id') == bot_id and r.get('environment') == 'live']
    report = dict(version=1, source_digest=digest(snapshot), bot_id=bot_id,
                  corrections=[], changes=[], errors=[], telemetry=[], venue_evidence=[])
    corrected = set()
    for order in orders:
        if order['side'] not in {'BUY', 'SELL'} or not order.get('filled'):
            continue
        raw = order.get('row', {})
        # Also audit legacy labels, without treating every such label as proof.
        if raw.get('Status', '').upper() not in {'CANCELED','CANCELLED','REJECTED','PARTIALLY_FILLED'}:
            continue
        try:
            rows = order_rows(venue.query_order(order_id=order['order_id']))
            if len(rows) != 1:
                raise ValueError('authoritative order history is unavailable or ambiguous')
            rows[0] = {k:v for k,v in rows[0].items() if k != 'ServerTimeUsage'}
            evidence = normalize_order(rows[0], order)
            if evidence.status not in {'CANCELED', 'REJECTED'} or evidence.filled:
                raise ValueError('venue does not confirm a terminal, unexecuted order')
            fixed = dict(order, status=evidence.status, filled=0., value=0., fee=0., row=rows[0])
            fixed['execution_correction'] = dict(original=deepcopy(order), model='roostoo-all-or-nothing-v1')
            report['corrections'].append(fixed)
            corrected.add(str(order['order_id']))
            report['venue_evidence'].append(rows[0])
        except (ValueError, KeyError) as exc:
            report['errors'].append(f"{order.get('order_id')}: {exc}")
    # A remote-only false fill cannot be assigned to a local book by guessing.
    for row in remote:
        if row.get('status', '').upper() == 'PARTIALLY_FILLED' and row.get('intent_id') not in by_intent:
            report['errors'].append(f"remote order {row.get('exchange_order_id')} has no local ownership record")
    revised = deepcopy(state)
    affected = {(o['strategy'], o['coin']) for o in report['corrections']}
    for strategy, coin in sorted(affected):
        relevant = [o for o in report['corrections'] if (o['strategy'],o['coin']) == (strategy,coin)]
        try:
            if strategy == MM:
                current = state['mm'][coin]
                baseline = state.get('opening_books', {}).get('mm', {}).get(coin)
                if baseline is None:
                    if not any(e['kind']=='capital_allocated' for e in snapshot['events']):
                        raise ValueError('missing MM opening allocation journal')
                    baseline = dict(current, cash=current['capital'], quantity=0., cost=0., fees=0., realized_pnl=0.)
                # First prove the journal reproduces the existing book, then remove bad fills.
                old = _replay(baseline, snapshot['events'], strategy, coin, set())
                for key in ('cash','quantity','cost','fees','realized_pnl'):
                    if abs(old[key]-current[key]) > max(1e-7, abs(current[key])*1e-9):
                        raise ValueError(f'incomplete accounting history for {key}')
                new = _replay(baseline, snapshot['events'], strategy, coin, corrected)
                revised['mm'][coin] = new
                revised['expected_cash_assets'] += new['cash']-current['cash']
                report['changes'].append(dict(strategy=strategy, coin=coin, before=current, after=new))
            elif 'opening_books' in state:
                opening = state['opening_books']
                baseline = dict(cash=0., quantity=opening['rxm_quantity'].get(coin, 0.),
                    cost=opening['rxm_cost'].get(coin, 0.), fees=0., realized_pnl=0.)
                spot_events = [e for e in snapshot['events'] if e['kind']!='fill' or e['payload'].get('side') in {'BUY','SELL'}]
                old = _replay(baseline, spot_events, strategy, coin, set())
                for key, field in [('quantity','rxm_quantity'),('cost','rxm_cost')]:
                    if abs(old[key]-state[field].get(coin,0)) > max(1e-7, abs(old[key])*1e-9):
                        raise ValueError(f'incomplete RXM accounting history for {key}')
                new = _replay(baseline, spot_events, strategy, coin, corrected)
                revised['rxm_quantity'][coin], revised['rxm_cost'][coin] = new['quantity'],new['cost']
                revised['rxm_fees'] += new['fees']-old['fees']
                revised['expected_cash_assets'] += new['cash']-old['cash']
                report['changes'].append(dict(strategy=strategy,coin=coin,before=old,after=new))
            else:
                # A false BUY with no later accounting dependency has an exact inverse,
                # including imported holdings whose original cost baseline is unavailable.
                events = [e for e in snapshot['events'] if e['kind']=='fill' and
                          e['payload'].get('strategy')==strategy and e['payload'].get('coin')==coin]
                ids = {str(o['order_id']) for o in relevant}
                bad_events = [e for e in events if str(e['payload'].get('order_id')) in ids]
                if len(bad_events) != len(relevant) or any(o['side']!='BUY' for o in relevant):
                    raise ValueError('RXM repair requires a trustworthy opening baseline and complete book replay')
                if any(e['id']>min(b['id'] for b in bad_events) and str(e['payload'].get('order_id')) not in ids for e in events):
                    raise ValueError('later RXM transactions depend on the corrupted cost basis; baseline required')
                before = dict(quantity=state['rxm_quantity'].get(coin,0), cost=state['rxm_cost'].get(coin,0))
                for order in relevant:
                    original = order['execution_correction']['original']
                    revised['rxm_quantity'][coin] -= original['filled']
                    revised['rxm_cost'][coin] -= original['value']+original['fee']
                    revised['rxm_fees'] -= original['fee']
                    revised['expected_cash_assets'] += original['value']+original['fee']
                after = dict(quantity=revised['rxm_quantity'][coin], cost=revised['rxm_cost'][coin])
                if min(after.values()) < -1e-7:
                    raise ValueError('reversal produces a negative book')
                report['changes'].append(dict(strategy=strategy, coin=coin, before=before, after=after))
        except ValueError as exc:
            report['errors'].append(f'{strategy}/{coin}: {exc}')
    for order in report['corrections']:
        original = order['execution_correction']['original']
        for row in remote:
            if row.get('intent_id') == order['intent_id'] and (str(row.get('exchange_order_id')) != str(order['order_id']) or row.get('strategy') != order['strategy']):
                report['errors'].append(f"remote identity mismatch: {order['intent_id']}")
        payload = SupabaseTradeUploader.transaction(order, environment='live', bot_id=bot_id, correction=True)
        payload['resolved_at'] = next((r.get('resolved_at') for r in remote if r.get('intent_id')==order['intent_id']), None) or datetime.fromtimestamp(original['submitted_at'], timezone.utc).isoformat()
        payload['metadata'].update(original_filled=original['filled'], original_value=original['value'])
        report['telemetry'].append(payload)
    ReconciliationIssues(revised)
    report['proposed_state'] = revised
    report['applicable'] = bool(report['corrections']) and not report['errors']
    report['manifest_id'] = digest(report)
    return report


def apply_manifest(path, manifest, port, *, bot_id, remote_rows=()):
    """Caller holds the account lock; revalidate all evidence before opening for write."""
    previous = read_snapshot(path)
    if any(e['kind']=='fill_repair' and e['payload'].get('manifest_id')==manifest.get('manifest_id') for e in previous['events']):
        return dict(applied=False, already_applied=True, manifest_id=manifest['manifest_id'])
    fresh = audit(path, port, bot_id=bot_id, remote_rows=remote_rows)
    if fresh['manifest_id'] != manifest.get('manifest_id'):
        raise ValueError('state or execution evidence changed; produce and review a new dry-run')
    if not fresh['applicable']:
        raise ValueError('repair has unresolved evidence errors or no changes')
    backup = Path(str(path)+'.before-fill-repair-'+fresh['manifest_id'][:12]+'.db')
    if backup.exists():
        if digest(read_snapshot(backup)) != fresh['source_digest']:
            raise ValueError('existing backup does not match the reviewed source state')
    else:
        with sqlite3.connect(f'file:{Path(path).resolve().as_posix()}?mode=ro', uri=True) as source:
            with sqlite3.connect(backup) as target:
                source.backup(target)
    store = PortfolioStore(path, RealClock(), bot_id=bot_id)
    try:
        with store.db:
            store.state = fresh['proposed_state']
            for order, payload in zip(fresh['corrections'], fresh['telemetry']):
                store.state['orders'].pop(order['intent_id'], None)
                store.db.execute('INSERT OR REPLACE INTO completed_orders VALUES (?,?,?,?)',
                    (order['intent_id'],order['order_id'],order['strategy'],encoded(order)))
                store.queue_upload(order['intent_id'],payload)
            store.db.execute('UPDATE portfolio SET payload=? WHERE id=1', (encoded(store.state),))
            store.db.execute('INSERT INTO events(timestamp,kind,payload) VALUES (?,?,?)',
                (store.clock.now().isoformat(), 'fill_repair', encoded(dict(manifest_id=fresh['manifest_id'], changes=fresh['changes']))))
    finally:
        store.close()
    return dict(applied=True, manifest_id=fresh['manifest_id'], backup=str(backup), uploads_queued=len(fresh['telemetry']))
