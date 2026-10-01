from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.strategy import Strategy
from data_pipeline.binance_vision import to_binance_symbol
from data_pipeline.loader import load_klines

TRADE_COLUMNS = ["time", "symbol", "side", "order_type", "filled", "quantity", "price", "value", "fee"]


@dataclass
class BacktestConfig:
    initial_cash: float = 100_000.0  # competition starting portfolio
    maker_fee: float = 0.0005        # limit orders (0.05%)
    taker_fee: float = 0.001         # market orders (0.1%): short covers and liquidations only
    limit_offset_bps: float = 0.0    # buys rest this far below the last close, sells above
    limit_fill: str = "through"      # "through": price must trade past the limit; "touch": reaching it is enough
    rebalance_band: float = 0.01     # skip orders that move a weight by less than this
    min_trade_usd: float = 1.0       # skip dust orders
    # Shorts are 1x (collateral = short notional). Roostoo hasn't published these, so they're assumptions:
    borrow_rate_annual: float = 0.0  # borrow fee on short notional, charged against collateral each bar
    maintenance_margin: float = 0.0  # liquidate when position equity <= this fraction of short notional


@dataclass
class BacktestResult:
    equity: pd.Series        # portfolio value at each bar close
    exposure: pd.Series      # gross exposure: (long + short notional) / equity at each bar close
    net_exposure: pd.Series  # (long - short notional) / equity at each bar close
    trades: pd.DataFrame     # one row per order, filled or not (see TRADE_COLUMNS)
    config: BacktestConfig
    interval: str


def load_universe(symbols: List[str], interval: str,
                  start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, pd.DataFrame]:
    """Load klines for several symbols, keyed by Binance symbol (e.g. BTCUSDT). Skips empty ones."""
    data = {}
    for s in symbols:
        df = load_klines(s, interval, start, end)
        if not df.empty:
            data[to_binance_symbol(s)] = df
    if not data:
        raise ValueError("No data loaded for any symbol in the given range")
    return data


def _normalize_targets(target: pd.DataFrame) -> pd.DataFrame:
    """Scale rows down so longs plus short collateral (1x) never need more than 100% of equity."""
    capital = target.abs().sum(axis=1)
    return target.div(capital.where(capital > 1.0, 1.0), axis=0)


def run_backtest(strategy: Strategy, data: Dict[str, pd.DataFrame], interval: str,
                 config: Optional[BacktestConfig] = None,
                 trade_start: Optional[pd.Timestamp] = None) -> BacktestResult:
    """
    Simulate a portfolio following the strategy's target weights using limit orders.
    Positive weights are spot longs; negative weights are 1x collateralised shorts.

    Each bar t:
      1. Shorts that gapped through their liquidation price are closed at the open (market, taker fee).
      2. Targets decided at the close of bar t-1 are sized against that close price P.
      3. Short reductions are covered at the open (market, taker fee: Roostoo's close_short has no price).
      4. Everything else becomes a limit order at P (± offset), resting for this bar only:
         buys fill if the low trades through the limit, sells/short-opens if the high does.
         Buys and short-opens can only use cash that was free before this bar's sells fill.
      5. Borrow fees, then intrabar liquidation check against the high (market, taker fee).

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

    maker, taker = config.maker_fee, config.taker_fee
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
        f = q * price * taker
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

        # 2) Size orders against the previous close
        ref = np.where(np.isnan(P), last_px, P)
        eq = cash + (qty * ref).sum() + (collat + sqty * (entry - ref)).sum()
        cur_long = qty * ref
        cur_short = sqty * ref
        cur_w = (cur_long - cur_short) / eq if eq > 0 else np.zeros(n)
        tgt_w = W[t]
        want_long = np.clip(tgt_w, 0, None) * eq
        want_short = np.clip(-tgt_w, 0, None) * eq

        exit_side = ((want_long == 0) & (qty > 0)) | ((want_short == 0) & (sqty > 0))
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
            cost = (add_long.sum() + add_short.sum()) * (1 + maker)
            if cost > 0 and cost > cash:
                scale = max(cash, 0.0) / cost
                add_long *= scale
                add_short *= scale

            sell_px = ref * (1 + off)
            buy_px = ref * (1 - off)
            # A resting limit that the open has already gapped past fills at the better open price
            sell_fill = np.fmax(sell_px, px)
            buy_fill = np.fmin(buy_px, px)

            for i in np.flatnonzero(need & (qty > 0) & (cur_long > want_long)):
                q = qty[i] if want_long[i] == 0 else (cur_long[i] - want_long[i]) / ref[i]
                if want_long[i] > 0 and q * ref[i] < config.min_trade_usd:
                    continue
                q = min(q, qty[i])
                if through_high(H[t][i], sell_px[i]):
                    value = q * sell_fill[i]
                    f = value * maker
                    cash += value - f
                    qty[i] -= q
                    record(ts, i, "SELL", "LIMIT", True, q, sell_fill[i], f)
                else:
                    record(ts, i, "SELL", "LIMIT", False, q, sell_px[i], 0.0)

            for i in np.flatnonzero(add_short > 0):
                if through_high(H[t][i], sell_px[i]):
                    value = add_short[i]
                    q = value / sell_fill[i]
                    f = value * maker
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
                    f = value * maker
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


def buy_and_hold(data: Dict[str, pd.DataFrame], config: Optional[BacktestConfig] = None) -> pd.Series:
    """Benchmark: split cash equally across symbols at the first bar where each has data, never rebalance.
    Assumes one limit buy per coin filled at that bar's open (maker fee)."""
    config = config or BacktestConfig()
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).sort_index()
    opens = pd.DataFrame({s: df["open"] for s, df in data.items()}).reindex(closes.index)

    slice_cash = config.initial_cash / len(data)
    equity = pd.Series(0.0, index=closes.index)
    for s in data:
        first = opens[s].first_valid_index()
        qty = slice_cash * (1 - config.maker_fee) / opens.at[first, s]
        value = (closes[s].ffill() * qty).where(closes.index >= first)
        equity += value.fillna(slice_cash)  # uninvested slice stays in cash until listing
    return equity.rename("buy_and_hold")
