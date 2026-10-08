"""Independent long-only MM books using the real quote generator and replay venue."""
from __future__ import annotations

import numpy as np
import pandas as pd

from tradebot.backtest.history import SECOND, instrument_rules, utc, validate_seconds, validate_instrument_rules
from tradebot.backtest.simulator import BacktestResult, TRADE_COLUMNS
from tradebot.core.clock import SimClock
from tradebot.core.config import BacktestMMConfig
from tradebot.exchange.replay import ReplayExchangePort

QUOTE_COLUMNS = ['id', 'symbol', 'side', 'time', 'posted_at', 'expires_at', 'finished_at', 'fill_time',
                 'limit_price', 'execution_price', 'quantity', 'filled', 'status', 'posting_tick_size',
                 'assigned_penetration_ticks']


def run_quote_backtest(strategy, data, config, *, trade_start=None, rules=None, progress=lambda _: None):
    cfg = BacktestMMConfig.model_validate(strategy.config.model_dump())
    config = config.model_copy(deep=True)
    config.mm = cfg
    config.interval = '1s'
    config.fees.spot_maker = .0005  # frozen policy: sizing and spread checks use the same fee
    config.symbols = list(cfg.allocations)
    symbols = cfg.active_symbols
    rule_info = {'assumption': 'Explicit rules applied throughout history; not historical rule changes.'}
    if rules is None:
        rules, rule_info = instrument_rules(config, symbols)
    else:
        # Apply the same validation to programmatic callers, without network access.
        rules = validate_instrument_rules(rules, symbols)
    chunks = iter([data] if isinstance(data, dict) else data)
    features, ports, tail, equity_parts, exposure_parts = {}, {}, {}, [], []
    price_parts = {s: [] for s in symbols}
    clock = None
    next_post = None
    end = None
    for chunk in chunks:
        if set(chunk) - set(cfg.allocations) or not set(symbols).issubset(chunk):
            raise ValueError(f'MM requires history for the symbols with positive allocations: {symbols}')
        # Disabled symbols must not create zero-size lots in the quote generator.
        chunk = {s: chunk[s] for s in symbols}
        empty = [s for s, frame in chunk.items() if frame.empty]
        if empty:
            raise ValueError(f'Missing MM history for {empty}')
        first = min(df.index[0] for df in chunk.values())
        stop = max(df.index[-1]+SECOND for df in chunk.values())
        for symbol, frame in chunk.items():
            validate_seconds(frame, symbol, first, stop)
        if clock is None:
            start = utc(trade_start) if trade_start is not None else first+pd.Timedelta(seconds=cfg.warmup_seconds)
            if first > start-pd.Timedelta(seconds=cfg.warmup_seconds) or start >= stop:
                raise ValueError('MM requires complete warm-up history and at least one trading second')
            clock = SimClock(start.to_pydatetime())
            next_post = start
            for symbol in symbols:
                ports[symbol] = ReplayExchangePort({}, clock, config.initial_cash*cfg.allocations[symbol],
                    config.fees, {symbol:'1s'}, execution=config, rules=rules)
        elif first != end:
            raise ValueError(f'MM chunks must be contiguous: expected {end}, got {first}')
        combined = {s: pd.concat([tail[s], chunk[s]]) if s in tail else chunk[s] for s in symbols}
        for symbol, port in ports.items():
            port.bars = {symbol+'/USD': combined[symbol]}
            port._px_cache.clear()
        while pd.Timestamp(clock.now()) < stop:
            now = pd.Timestamp(clock.now())
            if now == next_post:
                # Sync consumes all earlier candles before cancel releases reservations.
                for port in ports.values():
                    port.cancel_order()
                books = {s: dict(capital=config.initial_cash*cfg.allocations[s], cash=p.free_usd,
                                 quantity=p.coins.get(s, 0.0)) for s,p in ports.items()}
                history = {}
                for symbol, frame in combined.items():
                    cursor = features.get(symbol, {}).get('cursor')
                    begin = (pd.Timestamp(cursor+1, unit='s', tz='UTC') if cursor is not None
                             else now-pd.Timedelta(seconds=cfg.warmup_seconds))
                    history[symbol] = frame.loc[begin:now-SECOND]
                batch = strategy.generate_quotes(history, now=now, books=books, features=features, rules=rules)
                for q in batch.quotes:
                    reply = ports[q.symbol].place_order(q.symbol, q.side, q.quantity, q.price, 'LIMIT')
                    if not reply['Success']:
                        raise ValueError(f'MM reservation failed for {q.symbol} {q.side}: {reply.get("ErrMsg")}')
                    ports[q.symbol].history[-1]['ExpiresAt'] = now+pd.Timedelta(seconds=cfg.refresh_seconds)
                next_post = now+pd.Timedelta(seconds=cfg.refresh_seconds)
            boundary = min(next_post, stop)
            index = pd.date_range(now, boundary, freq='s', inclusive='left')
            total, inventory_value = np.zeros(len(index)), np.zeros(len(index))
            # Valuation includes reserved cash/coins. Fill deltas take effect on the
            # candle that touched the quote; no per-second Python venue polling needed.
            before = {}
            for s,p in ports.items():
                cash = p.free_usd + sum(o['Quantity']*o['Price']+o.get('ReservedFee',0) for o in p.orders if o['Side']=='BUY')
                qty = p.coins.get(s,0) + sum(o['Quantity'] for o in p.orders if o['Side']=='SELL')
                before[s] = (cash, qty, len(p.fills))
            clock.advance((boundary-now).total_seconds())
            for s,p in ports.items():
                p._sync()
                cash, qty, fill_cursor = before[s]
                cash_curve, qty_curve = np.full(len(index), cash, dtype=float), np.full(len(index), qty, dtype=float)
                for fill in p.fills[fill_cursor:]:
                    offset = int((fill['time']-SECOND-now).total_seconds())
                    sign = 1 if fill['side']=='BUY' else -1
                    qty_curve[offset:] += sign*fill['qty']
                    cash_curve[offset:] -= sign*fill['value']+fill['value']*config.fees.spot_maker
                values = qty_curve*combined[s].loc[index,'close'].to_numpy()
                total += cash_curve+values
                inventory_value += values
            equity_parts.append(pd.Series(total,index=index))
            exposure_parts.append(pd.Series(np.divide(inventory_value,total,out=np.zeros_like(total),where=total>0),index=index))
        end = stop
        for s,df in combined.items():
            # A refresh can be longer than warm-up. Retain every unconsumed
            # observation across chunk boundaries until the feature cursor advances.
            tail[s] = df.iloc[-max(cfg.warmup_seconds, cfg.refresh_seconds):].copy()
            traded = chunk[s].loc[start:,'close']
            # Keep price display data small; metrics always use every equity observation.
            if len(traded):
                price_parts[s].append(traded.iloc[np.unique(np.linspace(0,len(traded)-1,min(240,len(traded)),dtype=int))])
        progress(f'Simulated through {end.isoformat()} ({sum(len(p.fills) for p in ports.values())} fills)')
    if not equity_parts:
        raise ValueError('No MM candles in the requested period')
    equity = pd.concat(equity_parts).rename('equity')
    exposure = pd.concat(exposure_parts).rename('gross_exposure')
    for symbol, port in ports.items():
        port.cancel_order()
        if config.liquidate_mm and port.coins.get(symbol,0) > 0:
            reply = port.place_order(symbol,'SELL',port.coins[symbol],order_type='MARKET')
            if not reply['Success']:
                raise ValueError(f'Terminal MM liquidation failed: {symbol}')
    if config.liquidate_mm:
        equity.iloc[-1] = sum(p.equity() for p in ports.values())
        exposure.iloc[-1] = 0
    quotes, trades = [], []
    for symbol, port in ports.items():
        for order in port.history:
            posted = pd.Timestamp(order['CreateTimestamp'],unit='ms',tz='UTC')
            finished = pd.Timestamp(order['FinishTimestamp'],unit='ms',tz='UTC')
            filled = order['Status']=='FILLED'
            is_limit = order['Type']=='LIMIT'
            if is_limit:
                quotes.append(dict(id=f'{symbol}:{order["OrderID"]}',symbol=symbol,side=order['Side'],time=posted,
                    posted_at=posted,expires_at=order['ExpiresAt'],finished_at=finished,fill_time=finished if filled else None,
                    limit_price=order['Price'],execution_price=order.get('FilledAverPrice'),quantity=order['Quantity'],
                    filled=filled,status='filled' if filled else 'cancelled',posting_tick_size=order['PostingTick'],
                    assigned_penetration_ticks=order['PenetrationTicks']))
            if filled:
                px, qty = order['FilledAverPrice'], order['Quantity']
                fee = qty*px*(config.fees.spot_maker if is_limit else config.fees.spot_taker)
                trades.append(dict(time=finished,symbol=symbol,side=order['Side'],order_type=order['Type'],filled=True,
                    quantity=qty,price=px,value=qty*px,fee=fee,quote_price=order['Price'] if is_limit else None,
                    order_id=f'{symbol}:{order["OrderID"]}',terminal_exit=not is_limit))
    return BacktestResult(equity, exposure, exposure.rename('net_exposure'),
        pd.DataFrame(trades,columns=TRADE_COLUMNS+['order_id','terminal_exit']).sort_values(['time','symbol']),config,'1s',
        quotes=pd.DataFrame(quotes,columns=QUOTE_COLUMNS).sort_values(['posted_at','symbol','side']),
        prices={s: pd.concat(parts) for s,parts in price_parts.items()},
        metadata=dict(instrument_rules=rules,rule_provenance=rule_info,effective_config=config.model_dump(),
            ending_books={s:dict(cash=p.free_usd,quantity=p.coins.get(s,0),equity=p.equity()) for s,p in ports.items()},
            assumptions=['All starting capital allocated to independent long-only MM symbol books.',
                'Binance USD/USDT 1:1 candle-close proxy; no historical Roostoo bid/ask observations.',
                'Full fills once per posted side at quote price; no queue, partial fills, impact or price improvement.',
                'SplitMix64(seed, absolute posting second, side), fixed for order lifetime and shared across coins.',
                'Maker fee frozen at 5 bps; terminal sells use adverse market slippage and taker fees.',
                'Equity timestamps label candle opens; fill timestamps label candle closes. Terminal costs replace final equity observation.']))
