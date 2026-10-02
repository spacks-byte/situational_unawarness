"""Source-agnostic analytics for the dashboard: KPIs, book reconstruction, execution stats, signals."""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Optional

import numpy as np
import pandas as pd

from backtest.metrics import compute_metrics

QUALIFY_CUTOFFS = (0.033, 0.052)  # estimated top-20 qualification band over 14 days
CALMAR_CAP = 50.0                  # same cap as backtest/experiments.composite


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def composite(sortino, sharpe, calmar) -> Optional[float]:
    """Judges' score 0.4*Sortino + 0.3*Sharpe + 0.3*Calmar (Calmar capped like the research code)."""
    so, sh, ca = _f(sortino), _f(sharpe), _f(calmar)
    if so is None and sh is None:
        return None
    ca = min(ca, CALMAR_CAP) if ca is not None else 0.0
    return 0.4 * (so or 0.0) + 0.3 * (sh or 0.0) + 0.3 * ca


# --------------------------------------------------------------------------- KPIs
def equity_kpis(equity: pd.Series, initial: float, interval: str = "15m",
                gross: Optional[pd.Series] = None, net: Optional[pd.Series] = None,
                lockin_return: float = 0.06) -> Dict[str, Any]:
    equity = equity.dropna()
    k: Dict[str, Any] = {"initial": initial, "lockin_return": lockin_return, "cutoffs": list(QUALIFY_CUTOFFS)}
    if equity.empty:
        return k
    last = float(equity.iloc[-1])
    peak = max(float(equity.max()), initial)
    k.update(equity=last, pnl_usd=last - initial, pnl_pct=last / initial - 1, peak=peak,
             drawdown_now=last / peak - 1,
             max_drawdown=min(float((equity / equity.cummax()).min() - 1), float(equity.min()) / initial - 1, 0.0))
    day_start = equity.index[-1].floor("D")
    before = equity[equity.index < day_start]
    ref = float(before.iloc[-1]) if len(before) else initial
    k.update(today_pnl_usd=last - ref, today_pnl_pct=last / ref - 1 if ref else None, today=str(day_start.date()))
    if len(equity) >= 2:
        m = compute_metrics(equity, interval, initial=initial)
        k.update(sharpe=_f(m["sharpe"]), sortino=_f(m["sortino"]), calmar=_f(m["calmar"]),
                 ann_vol=_f(m["annual_volatility"]))
        k["composite"] = composite(m["sortino"], m["sharpe"], m["calmar"])
        k["days"] = (equity.index[-1] - equity.index[0]) / pd.Timedelta(days=1)
    if gross is not None and len(gross):
        k["gross"] = _f(gross.iloc[-1])
        k["avg_gross"] = _f(gross.mean())
    if net is not None and len(net):
        k["net"] = _f(net.iloc[-1])
    # lock-in on equity known at the previous bar close (same rule as the backtester)
    hit = equity[equity / initial - 1 >= lockin_return] if lockin_return > 0 else equity.iloc[0:0]
    k["locked"] = bool(len(hit)) and hit.index[0] < equity.index[-1]
    k["lock_time"] = str(hit.index[0]) if len(hit) else None
    k["to_lockin_pct"] = lockin_return - k["pnl_pct"]
    return k


def series_payload(equity: pd.Series, gross: Optional[pd.Series] = None, net: Optional[pd.Series] = None,
                   max_points: int = 2500) -> Dict[str, Any]:
    eq = equity.dropna()
    if len(eq) > max_points:
        step = int(math.ceil(len(eq) / max_points))
        eq = eq.iloc[::step]
    dd = eq / eq.cummax() - 1
    out = {"t": [ts.isoformat() for ts in eq.index], "equity": [round(float(v), 2) for v in eq],
           "drawdown": [round(float(v), 6) for v in dd]}
    for name, s in (("gross", gross), ("net", net)):
        if s is not None and len(s):
            out[name] = [round(float(v), 4) for v in s.reindex(eq.index).ffill().fillna(0.0)]
    return out


# --------------------------------------------------------------------- book from fills
def reconstruct_book(trades: pd.DataFrame, last_prices: Dict[str, float], equity: float, initial: float,
                     target_weights: Optional[Dict[str, float]] = None) -> list[dict]:
    """Rebuild open positions, entry prices and per-symbol P&L from filled trades (backtest columns)."""
    book: Dict[str, Dict[str, float]] = {}
    filled = trades[trades["filled"]] if len(trades) else trades
    for row in filled.itertuples(index=False):
        b = book.setdefault(row.symbol, dict(q=0.0, avg=0.0, sq=0.0, entry=0.0, realized=0.0, fees=0.0))
        q, px = float(row.quantity), float(row.price)
        b["fees"] += float(row.fee)
        if row.side == "BUY":
            b["avg"] = (b["avg"] * b["q"] + px * q) / (b["q"] + q) if b["q"] + q > 0 else 0.0
            b["q"] += q
        elif row.side == "SELL":
            q = min(q, b["q"])
            b["realized"] += q * (px - b["avg"])
            b["q"] -= q
            if b["q"] <= 1e-12:
                b["q"] = b["avg"] = 0.0
        elif row.side == "SHORT":
            b["entry"] = (b["entry"] * b["sq"] + px * q) / (b["sq"] + q) if b["sq"] + q > 0 else 0.0
            b["sq"] += q
        elif row.side in ("COVER", "LIQUIDATE"):
            q = min(q, b["sq"])
            b["realized"] += q * (b["entry"] - px)
            b["sq"] -= q
            if b["sq"] <= 1e-12:
                b["sq"] = b["entry"] = 0.0
    tw = {_short(k): v for k, v in (target_weights or {}).items()}
    rows = []
    for sym, b in book.items():
        s = _short(sym)
        last = float(last_prices.get(sym, last_prices.get(s, 0.0)) or 0.0)
        if b["q"] > 0:
            qty, entry, side, upnl = b["q"], b["avg"], "LONG", b["q"] * (last - b["avg"])
            value = b["q"] * last
        elif b["sq"] > 0:
            qty, entry, side, upnl = b["sq"], b["entry"], "SHORT", b["sq"] * (b["entry"] - last)
            value = -b["sq"] * last
        else:
            qty, entry, side, upnl, value = 0.0, 0.0, "FLAT", 0.0, 0.0
        w = value / equity if equity else 0.0
        t = float(tw.get(s, 0.0))
        rows.append(dict(symbol=s, side=side, qty=qty, entry=entry, last=last, value=value, weight=w,
                         target_weight=t, drift=w - t, upnl=upnl,
                         upnl_pct=(upnl / (qty * entry)) if qty and entry else 0.0,
                         realized=b["realized"], fees=b["fees"],
                         contribution=(b["realized"] + upnl - b["fees"]) / initial))
    for s, t in tw.items():  # targets we do not hold yet
        if abs(t) > 1e-9 and not any(r["symbol"] == s for r in rows):
            rows.append(dict(symbol=s, side="FLAT", qty=0.0, entry=0.0, last=float(last_prices.get(s, 0.0) or 0.0),
                             value=0.0, weight=0.0, target_weight=t, drift=-t, upnl=0.0, upnl_pct=0.0,
                             realized=0.0, fees=0.0, contribution=0.0))
    open_rows = [r for r in rows if r["side"] != "FLAT" or abs(r["target_weight"]) > 1e-9]
    closed = [r for r in rows if r not in open_rows]
    open_rows.sort(key=lambda r: -r["weight"])
    closed.sort(key=lambda r: -r["contribution"])
    return open_rows + closed


def _short(symbol: str) -> str:
    s = str(symbol).upper()
    for q in ("USDT", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s.split("/")[0]


# ------------------------------------------------------------------ execution stats
def execution_stats(trades: pd.DataFrame, mean_equity: float) -> Dict[str, Any]:
    if trades is None or not len(trades):
        return {"orders": 0, "fills": 0, "fill_rate": None, "fills_by_day": {}, "by_side": {}}
    filled = trades[trades["filled"]]
    limit = trades[trades["order_type"] == "LIMIT"]
    market = filled[filled["order_type"] == "MARKET"]
    maker = float(filled.loc[filled["order_type"] == "LIMIT", "fee"].sum())
    taker = float(market["fee"].sum())
    by_day = filled.groupby(pd.to_datetime(filled["time"], utc=True).dt.floor("D")).size() if len(filled) else pd.Series(dtype=int)
    all_days = pd.to_datetime(trades["time"], utc=True).dt.floor("D")
    days = pd.date_range(all_days.min(), all_days.max(), freq="D") if len(all_days) else []
    by_side = {}
    for side, g in trades.groupby("side"):
        f = g[g["filled"]]
        by_side[side] = {"orders": int(len(g)), "fills": int(len(f)), "notional": float(f["value"].sum()),
                         "fill_rate": float(g["filled"].mean()) if len(g) else None}
    return {
        "orders": int(len(trades)), "fills": int(len(filled)),
        "fill_rate": float(limit["filled"].mean()) if len(limit) else None,
        "limit_orders": int(len(limit)), "market_orders": int(len(market)),
        "liquidations": int((filled["side"] == "LIQUIDATE").sum()),
        "maker_fees": maker, "taker_fees": taker, "total_fees": maker + taker,
        "notional": float(filled["value"].sum()),
        "turnover": float(filled["value"].sum() / mean_equity) if mean_equity else None,
        "fee_bps_of_notional": (maker + taker) / float(filled["value"].sum()) * 1e4 if len(filled) and filled["value"].sum() else None,
        "fills_by_day": {str(d.date()): int(by_day.get(d, 0)) for d in days},
        "by_side": by_side,
    }


def trades_payload(trades: pd.DataFrame, limit: int = 400) -> list[dict]:
    if trades is None or not len(trades):
        return []
    t = trades.sort_values("time", ascending=False, kind="stable").head(limit)
    out = []
    for r in t.itertuples(index=False):
        out.append(dict(time=pd.Timestamp(r.time).isoformat(), symbol=_short(r.symbol), side=r.side,
                        type=r.order_type, qty=float(r.quantity), price=float(r.price), notional=float(r.value),
                        fee=float(r.fee), filled=bool(r.filled), status=getattr(r, "status", None) or
                        ("FILLED" if r.filled else "UNFILLED")))
    return out


# ------------------------------------------------------------------- trade map (research)
def trade_map_payload(closes: pd.DataFrame, score: pd.DataFrame, weights: pd.DataFrame, trades: pd.DataFrame,
                      start: pd.Timestamp, k: int) -> Dict[str, Any]:
    """Per-coin price, signal score and held target weight over the window, plus every order.

    Answers "where did the strategy buy and sell, and what did the signal look like there". The
    long/short thresholds are the k-th best / k-th worst score across coins at each bar: a coin is
    in the long book while its score is at or above the upper one.
    """
    px = closes[closes.index >= start]
    idx = px.index
    sc = score.reindex(idx)
    w = weights.shift(1).reindex(idx).fillna(0.0)          # the engine trades a decision one bar later
    ranked = np.sort(sc.to_numpy(), axis=1)                # NaN sorts last
    n = sc.notna().sum(axis=1).to_numpy()
    kk = np.minimum(k, n // 2)
    rows = np.arange(len(idx))
    ok = kk > 0
    hi = np.where(ok, ranked[rows, np.clip(n - kk, 0, None)], np.nan)
    lo = np.where(ok, ranked[rows, np.clip(kk - 1, 0, None)], np.nan)

    def series(values, digits):
        return [None if not np.isfinite(v) else round(float(v), digits) for v in values]

    orders: Dict[str, list] = {}
    if trades is not None and len(trades):
        for r in trades.itertuples(index=False):
            orders.setdefault(r.symbol, []).append(dict(t=pd.Timestamp(r.time).isoformat(), side=r.side,
                                                         type=r.order_type, price=float(r.price),
                                                         notional=float(r.value), filled=bool(r.filled)))
    symbols = sorted(set(orders) | {s for s in w.columns if (w[s] != 0).any()},
                     key=lambda s: -sum(o["notional"] for o in orders.get(s, []) if o["filled"]))
    coins = []
    for s in symbols:
        if s not in px.columns:
            continue
        coins.append(dict(symbol=_short(s), price=[None if not np.isfinite(v) else float(f"{v:.6g}") for v in px[s]],
                          score=series(sc[s], 3) if s in sc.columns else [],
                          weight=series(w[s], 4) if s in w.columns else [], orders=orders.get(s, [])))
    return {"t": [ts.isoformat() for ts in idx], "k": int(k), "long_threshold": series(hi, 3),
            "short_threshold": series(lo, 3), "coins": coins}


# ------------------------------------------------------------------------- signals
def momentum_scores(data: Dict[str, pd.DataFrame], lookbacks: Iterable[int] = (72, 168, 336),
                    resid: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Layer-1 ensemble residual-momentum score, mirroring backtest/strategies/rxm.py.

    Returns (score, per-bar vol). The dashboard cross-checks the resulting top/bottom-k against the
    strategy's own target weights and flags any mismatch, so a drift between the two copies shows up.
    """
    raw_close = pd.DataFrame({s: df["close"] for s, df in data.items()}).sort_index()
    close = raw_close.ffill()
    vol = pd.DataFrame({s: df["volume"] for s, df in data.items()}).reindex(close.index)
    idx = close.index
    bph = max(1, int(round(pd.Timedelta(hours=1) / (idx[1] - idx[0]))))
    lp = np.log(close)
    r = lp.diff()
    sig = r.rolling(168 * bph, min_periods=48 * bph).std()
    flat = close.diff().abs().rolling(24 * bph, min_periods=24 * bph).sum() <= 0
    stale = raw_close.isna() | (vol.rolling(24 * bph, min_periods=1).sum() <= 0) | flat
    sig = sig.clip(lower=0.05 * sig.median(axis=1), axis=0)
    btc = next((c for c in close.columns if c.startswith("BTC")), None)
    beta = None
    if resid and btc is not None:
        win = 720 * bph
        beta = r.rolling(win, min_periods=win // 3).cov(r[btc]).div(
            r[btc].rolling(win, min_periods=win // 3).var(), axis=0)
    hs = list(lookbacks)
    parts = []
    for h in hs:
        L = h * bph
        mom = lp - lp.shift(L)
        if beta is not None:
            mom = mom - beta.mul(lp[btc] - lp[btc].shift(L), axis=0)
        s_h = mom / (sig * np.sqrt(L))
        if len(hs) > 1:
            s_h = s_h.sub(s_h.mean(axis=1), axis=0).div(s_h.std(axis=1), axis=0)
        parts.append(s_h)
    score = (sum(parts) / len(parts)).where(sig.notna() & ~stale)
    return score, sig


def signal_payload(score_row: pd.Series, vol_row: pd.Series, weights_row: pd.Series, k: int,
                   as_of: str, scale: float = 1.0) -> Dict[str, Any]:
    s = score_row.dropna().sort_values(ascending=False)
    n = len(s)
    kk = min(k, n // 2)
    longs = [_short(x) for x in s.index[:kk]]
    shorts = [_short(x) for x in s.index[n - kk:]] if kk else []
    w = {_short(c): float(v) * scale for c, v in weights_row.items() if abs(v) > 1e-9}
    strat_longs = sorted(c for c, v in w.items() if v > 0)
    strat_shorts = sorted(c for c, v in w.items() if v < 0)
    rows = []
    for i, (sym, val) in enumerate(s.items(), 1):
        ss = _short(sym)
        rows.append(dict(rank=i, symbol=ss, score=float(val), vol_ann=float(vol_row.get(sym, np.nan) * np.sqrt(365 * 96))
                         if pd.notna(vol_row.get(sym, np.nan)) else None,
                         side="LONG" if ss in longs else "SHORT" if ss in shorts else "",
                         target_weight=w.get(ss, 0.0)))
    return {"as_of": as_of, "k": k, "rows": rows, "longs": longs, "shorts": shorts,
            "strategy_longs": strat_longs, "strategy_shorts": strat_shorts,
            "consistent": sorted(longs) == strat_longs and sorted(shorts) == strat_shorts}
