"""
Bar-by-bar portfolio simulator that mirrors the live engine's order policy:
limit orders at the previous close (maker fee), shorts at 1x with short-open/close fees,
short closes at market, one-bar order lifetime, and cash locked by open orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from tradebot.core.config import BacktestConfig
from tradebot.strategy.base import Strategy

TRADE_COLUMNS = ["time", "symbol", "side", "order_type", "filled", "quantity", "price", "value", "fee"]


@dataclass
class BacktestResult:
    equity: pd.Series        # portfolio value at each bar close
    exposure: pd.Series      # gross exposure: (long + short notional) / equity at each bar close
    net_exposure: pd.Series  # (long - short notional) / equity at each bar close
    trades: pd.DataFrame     # one row per order, filled or not (see TRADE_COLUMNS)
    config: BacktestConfig
    interval: str


def _normalize_targets(target: pd.DataFrame) -> pd.DataFrame:
    """Scale rows down so longs plus short collateral (1x) never need more than 100% of equity."""
    capital = target.abs().sum(axis=1)
    return target.div(capital.where(capital > 1.0, 1.0), axis=0)


def run_backtest(strategy: Strategy, data: dict[str, pd.DataFrame], interval: str,
                 config: Optional[BacktestConfig] = None,
                 trade_start: Optional[pd.Timestamp] = None) -> BacktestResult:
    """
    Simulate a portfolio following the strategy's target weights using limit orders.
    Positive weights are spot longs; negative weights are 1x collateralised shorts.

    Each bar t:
      1. Shorts that gapped through their liquidation price are closed at the open (market, short-close fee).
      2. Targets decided at the close of bar t-1 are sized against that close price P.
      3. Short reductions are covered at the open (market, short-close fee: Roostoo's close_short has no price).
      4. Everything else becomes a limit order at P (± offset), resting for this bar only
         (spot maker fee; short opens pay the short-open fee). With config.latency_bars > 0 the limit
         is priced off an older close (the order arrives late; crossed on arrival = taker fee):
         buys fill if the low trades through the limit, sells/short-opens if the high does.
         Buys and short-opens can only use cash that was free before this bar's sells fill.
      5. Borrow fees, then intrabar liquidation check against the high (market, short-close fee).

    Lock-in (config.lockin_return > 0): once equity at the end of a bar is up lockin_return since the
    start, every later target is scaled by lockin_scale (a competition overlay; see docs/STRATEGY_SPEC.md).

    trade_start: if given, nothing trades before it and the result starts there (data before it
    is only used as indicator warm-up), like a competition starting from cash.
    """
    config = config or BacktestConfig()
    symbols = list(data)
    n = len(symbols)

    opens = pd.DataFrame({s: data[s]["open"] for s in symbols}).sort_index()
    index = opens.index
    highs = pd.DataFrame({s: data[s]["high"] for s in symbols}).reindex(index)
    lows = pd.DataFrame({s: data[s]["low"] for s in symbols}).reindex(index)
    closes = pd.DataFrame({s: data[s]["close"] for s in symbols}).reindex(index)

    target = strategy.generate_weights(data)
    target = target.reindex(index=index, columns=symbols).fillna(0.0).clip(-1.0, 1.0)
    target = _normalize_targets(target)
    held = target.shift(1).fillna(0.0)  # no look-ahead: decided at close t-1, ordered during bar t
    if trade_start is not None:
        held.loc[index < trade_start] = 0.0

    O = opens.to_numpy()
    H = highs.to_numpy()
    L = lows.to_numpy()
    C = np.nan_to_num(closes.ffill().to_numpy())  # last known price for valuation
    P_prev = np.vstack([np.full(n, np.nan), closes.to_numpy()[:-1]])  # limit reference: previous close
    W = held.to_numpy()
    lat = max(0, int(config.latency_bars))
    # stale limit reference: the close lat bars before the previous one (latency stress only)
    P_stale = np.vstack([np.full((lat + 1, n), np.nan), closes.to_numpy()[:-(lat + 1)]]) if lat else None

    maker, taker = config.fees.spot_maker, config.fees.spot_taker
    short_open_fee, short_close_fee = config.fees.short_open, config.fees.short_close
    off = config.limit_offset_bps / 1e4
    strict = config.limit_fill == "through"
    band = config.rebalance_band
    mm = config.maintenance_margin
    borrow_per_bar = config.borrow_rate_annual / (pd.Timedelta(days=365) / (index[1] - index[0])) \
        if len(index) > 1 else 0.0

    cash = config.initial_cash
    qty = np.zeros(n)          # long quantity
    sqty = np.zeros(n)         # short quantity
    entry = np.zeros(n)        # average short entry price
    collat = np.zeros(n)       # collateral locked in each short
    last_px = np.zeros(n)
    equity = np.empty(len(index))
    gross = np.empty(len(index))
    net = np.empty(len(index))
    orders = []

    def record(ts, i, side, order_type, filled, q, price, fee):
        orders.append((ts, symbols[i], side, order_type, filled, q, price, q * price, fee))

    def close_short(i, q, price, ts, side="COVER"):
        nonlocal cash
        frac = q / sqty[i]
        released = collat[i] * frac + q * (entry[i] - price)
        f = q * price * short_close_fee
        if side == "LIQUIDATE":
            # Losses (fee included) are capped at the posted collateral
            f = min(f, max(released, 0.0))
            released = max(released, 0.0)
        cash += released - f
        collat[i] -= collat[i] * frac
        sqty[i] -= q
        if sqty[i] <= 1e-12:
            sqty[i] = entry[i] = collat[i] = 0.0
        record(ts, i, side, "MARKET", True, q, price, f)

    def through_low(low, limit):
        return low < limit if strict else low <= limit

    def through_high(high, limit):
        return high > limit if strict else high >= limit

    locked = False
    start_idx = 0 if trade_start is None else int(index.searchsorted(trade_start))

    for t in range(len(index)):
        ts = index[t]
        px = O[t]
        P = P_prev[t]
        tradable = ~np.isnan(px) & ~np.isnan(P)
        last_px = np.where(~np.isnan(px), px, last_px)

        # 1) Gap liquidations at the open
        for i in np.flatnonzero((sqty > 0) & ~np.isnan(px)):
            if collat[i] + sqty[i] * (entry[i] - px[i]) <= mm * sqty[i] * px[i]:
                close_short(i, sqty[i], px[i], ts, side="LIQUIDATE")

        # 1b) Lock-in check on equity known at the end of the previous bar (no look-ahead)
        if config.lockin_return > 0 and not locked and t > 0 and t - 1 >= start_idx                 and equity[t - 1] / config.initial_cash - 1 >= config.lockin_return:
            locked = True

        # 2) Size orders against the previous close
        ref = np.where(np.isnan(P), last_px, P)
        eq = cash + (qty * ref).sum() + (collat + sqty * (entry - ref)).sum()
        cur_long = qty * ref
        cur_short = sqty * ref
        cur_w = (cur_long - cur_short) / eq if eq > 0 else np.zeros(n)
        tgt_w = W[t] * (config.lockin_scale if locked else 1.0)
        want_long = np.clip(tgt_w, 0, None) * eq
        want_short = np.clip(-tgt_w, 0, None) * eq

        # Exiting a side ignores the band, but not the dust floor: float residue (e.g. 1e-20 coins left
        # after a sell) would otherwise be re-ordered every bar and inflate trade counts / fill rates.
        dust = config.min_trade_usd
        exit_side = ((want_long == 0) & (cur_long >= dust)) | ((want_short == 0) & (cur_short >= dust))
        need = tradable & ((np.abs(tgt_w - cur_w) > band) | exit_side)

        if need.any():
            # 3) Short covers: market at the open
            for i in np.flatnonzero(need & (sqty > 0) & (cur_short > want_short)):
                q = sqty[i] if want_short[i] == 0 else (cur_short[i] - want_short[i]) / ref[i]
                if want_short[i] == 0 or q * px[i] >= config.min_trade_usd:
                    close_short(i, min(q, sqty[i]), px[i], ts)

            # 4) Limit orders. Budget for buys/short-opens = cash free before this bar's sells fill.
            add_long = np.where(need, np.clip(want_long - cur_long, 0, None), 0.0)
            add_short = np.where(need, np.clip(want_short - sqty * ref, 0, None), 0.0)
            add_long[add_long < config.min_trade_usd] = 0.0
            add_short[add_short < config.min_trade_usd] = 0.0

            lim_ref = ref if not lat else np.where(np.isnan(P_stale[t]), ref, P_stale[t])
            sell_px = lim_ref * (1 + off)
            buy_px = lim_ref * (1 - off)
            # A resting limit that the open has already gapped past fills at the better open price
            sell_fill = np.fmax(sell_px, px) if config.gap_improvement else sell_px
            buy_fill = np.fmin(buy_px, px) if config.gap_improvement else buy_px
            sell_fee = np.full(n, maker)
            buy_fee = np.full(n, maker)
            if lat:
                # Late order: already marketable on arrival -> taker fee, filled at the (worse) limit price
                sell_fill, buy_fill = sell_px, buy_px
                sell_fee = np.where(px >= sell_px, taker, maker)
                buy_fee = np.where(px <= buy_px, taker, maker)

            cost = add_long.sum() * (1 + maker) + (add_long * (buy_fee - maker)).sum()                 + add_short.sum() * (1 + short_open_fee)
            if cost > 0 and cost > cash:
                scale = max(cash, 0.0) / cost
                add_long *= scale
                add_short *= scale
                add_long[add_long < config.min_trade_usd] = 0.0    # scaling to ~0 cash must not leave dust
                add_short[add_short < config.min_trade_usd] = 0.0

            for i in np.flatnonzero(need & (qty > 0) & (cur_long > want_long)):
                q = qty[i] if want_long[i] == 0 else (cur_long[i] - want_long[i]) / ref[i]
                if want_long[i] > 0 and q * ref[i] < config.min_trade_usd:
                    continue
                q = min(q, qty[i])
                if through_high(H[t][i], sell_px[i]):
                    value = q * sell_fill[i]
                    f = value * sell_fee[i]
                    cash += value - f
                    qty[i] -= q
                    record(ts, i, "SELL", "LIMIT", True, q, sell_fill[i], f)
                else:
                    record(ts, i, "SELL", "LIMIT", False, q, sell_px[i], 0.0)

            for i in np.flatnonzero(add_short > 0):
                if through_high(H[t][i], sell_px[i]):
                    value = add_short[i]
                    q = value / sell_fill[i]
                    f = value * short_open_fee
                    cash -= value + f
                    entry[i] = (entry[i] * sqty[i] + sell_fill[i] * q) / (sqty[i] + q)
                    sqty[i] += q
                    collat[i] += value
                    record(ts, i, "SHORT", "LIMIT", True, q, sell_fill[i], f)
                else:
                    record(ts, i, "SHORT", "LIMIT", False, add_short[i] / sell_px[i], sell_px[i], 0.0)

            for i in np.flatnonzero(add_long > 0):
                if through_low(L[t][i], buy_px[i]):
                    value = add_long[i]
                    q = value / buy_fill[i]
                    f = value * buy_fee[i]
                    cash -= value + f
                    qty[i] += q
                    record(ts, i, "BUY", "LIMIT", True, q, buy_fill[i], f)
                else:
                    record(ts, i, "BUY", "LIMIT", False, add_long[i] / buy_px[i], buy_px[i], 0.0)

        # 5) Borrow fees and intrabar liquidations
        for i in np.flatnonzero(sqty > 0):
            collat[i] -= sqty[i] * C[t][i] * borrow_per_bar
            high = H[t][i]
            if np.isnan(high):
                continue
            if collat[i] + sqty[i] * (entry[i] - high) <= mm * sqty[i] * high:
                # Price at which position equity hits the maintenance margin
                liq_px = (collat[i] + sqty[i] * entry[i]) / (sqty[i] * (1 + mm))
                close_short(i, sqty[i], liq_px, ts, side="LIQUIDATE")

        long_val = qty * C[t]
        short_notional = sqty * C[t]
        equity[t] = cash + long_val.sum() + (collat + sqty * (entry - C[t])).sum()
        gross[t] = (long_val.sum() + short_notional.sum()) / equity[t] if equity[t] > 0 else 0.0
        net[t] = (long_val.sum() - short_notional.sum()) / equity[t] if equity[t] > 0 else 0.0

    keep = slice(None) if trade_start is None else index >= trade_start
    trades = pd.DataFrame(orders, columns=TRADE_COLUMNS)
    return BacktestResult(
        equity=pd.Series(equity, index=index, name="equity")[keep],
        exposure=pd.Series(gross, index=index, name="gross_exposure")[keep],
        net_exposure=pd.Series(net, index=index, name="net_exposure")[keep],
        trades=trades,
        config=config,
        interval=interval,
    )


def buy_and_hold(data: dict[str, pd.DataFrame], config: Optional[BacktestConfig] = None) -> pd.Series:
    """Benchmark: split cash equally across symbols at the first bar where each has data, never rebalance.
    Assumes one limit buy per coin filled at that bar's open (maker fee)."""
    config = config or BacktestConfig()
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).sort_index()
    opens = pd.DataFrame({s: df["open"] for s, df in data.items()}).reindex(closes.index)

    slice_cash = config.initial_cash / len(data)
    equity = pd.Series(0.0, index=closes.index)
    for s in data:
        first = opens[s].first_valid_index()
        qty = slice_cash * (1 - config.fees.spot_maker) / opens.at[first, s]
        value = (closes[s].ffill() * qty).where(closes.index >= first)
        equity += value.fillna(slice_cash)  # uninvested slice stays in cash until listing
    return equity.rename("buy_and_hold")
