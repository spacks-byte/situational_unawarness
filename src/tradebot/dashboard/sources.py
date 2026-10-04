"""
Data adapters. Both return the same plain-dict model that dashboard/build.py renders:

  load_backtest(...)  runs tradebot.backtest.run_backtest for a frozen RXM preset
  load_engine(dir)    reads a live/mock engine run: audit JSONL + intent SQLite + snapshot JSON

Nothing here talks to an exchange.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from tradebot.dashboard.analytics import (
    _short,
    equity_kpis,
    execution_stats,
    momentum_scores,
    reconstruct_book,
    series_payload,
    signal_payload,
    trade_map_payload,
    trades_payload,
)
from tradebot.core.config import Settings
from tradebot.core.intervals import interval_to_timedelta
from tradebot.data.binance_vision import klines_path
from tradebot.live.guard import Guard, GuardConfig, ProposedOrder
TRADE_COLUMNS = ["time", "symbol", "side", "order_type", "filled", "quantity", "price", "value", "fee"]
ORDER_CHECKS = ("price_band", "order_notional", "self_cross", "order_rate", "api_budget")


# ============================================================================ backtest
class _CachedStrategy:
    """Wraps a Strategy so its weights can be reused after run_backtest."""

    def __init__(self, inner):
        self.inner, self.weights = inner, None
        self.name, self.params = inner.name, inner.params

    def generate_weights(self, data):
        self.weights = self.inner.generate_weights(data)
        return self.weights


def _data_end(interval: str) -> pd.Timestamp:
    df = pd.read_parquet(klines_path(Settings.load().data.dir, "BTC", interval), columns=["close"])
    return (df.index[-1] + interval_to_timedelta(interval)).floor("D")


def load_backtest(days: int = 14, end: Optional[str] = None, variant: str = "comp",
                  warmup_days: int = 45, interval: str = "15m", kill_file: str = "KILL",
                  guard_config: Optional[GuardConfig] = None) -> Dict[str, Any]:
    from tradebot.backtest.simulator import run_backtest
    from tradebot.core.config import BacktestConfig
    from tradebot.data.loader import load_universe
    from tradebot.strategy.library.rxm import UNIVERSE, ResidualMomentum, preset

    end_ts = pd.Timestamp(end, tz="UTC") if end else _data_end(interval)
    start_ts = end_ts - pd.Timedelta(days=days)
    data = load_universe(list(UNIVERSE), interval, str((start_ts - pd.Timedelta(days=warmup_days)).date()),
                         str(end_ts.date()))
    params, engine = preset(variant)
    strat = _CachedStrategy(ResidualMomentum(**params))
    cfg = BacktestConfig(**engine)
    res = run_backtest(strat, data, interval, cfg, trade_start=start_ts)

    equity, gross, net, trades = res.equity, res.exposure, res.net_exposure, res.trades
    initial = cfg.initial_cash
    k = equity_kpis(equity, initial, interval, gross, net, cfg.lockin_return)
    closes = pd.DataFrame({s: d["close"] for s, d in data.items()}).sort_index().ffill()
    last_px = {s: float(v) for s, v in closes.iloc[-1].items() if pd.notna(v)}
    bar = interval_to_timedelta(interval)
    as_of = closes.index[-1] + bar  # close time of the last bar

    w = strat.weights.reindex(index=closes.index, columns=list(data)).fillna(0.0).clip(-1, 1)
    w = w.div(w.abs().sum(axis=1).clip(lower=1.0), axis=0)
    scale = cfg.lockin_scale if k.get("locked") else 1.0
    target = {_short(s): float(v) * scale for s, v in w.iloc[-1].items() if abs(v) > 1e-9}

    eq_now = float(equity.iloc[-1])
    positions = reconstruct_book(trades, last_px, eq_now, initial, target)

    # ---- signals at the last daily rebalance
    full = strat.inner.params      # preset overrides plus the strategy's own defaults (lookbacks, rebalance_h)
    score, vol = momentum_scores(data, [int(x) for x in str(full["lookbacks"]).split("/")])
    rebal = closes.index[(closes.index.hour % full["rebalance_h"] == 0) & (closes.index.minute == 0)]
    ts = rebal[-1]
    signals = signal_payload(score.loc[ts], vol.loc[ts], w.loc[ts], params.get("k", 3), ts.isoformat(), scale)

    # ---- guard: portfolio checks on the final book, order checks on the last order batch
    gcfg = guard_config or GuardConfig(initial_equity_usd=initial, kill_file=kill_file,
                                       lockin_return=cfg.lockin_return, lockin_scale=cfg.lockin_scale)
    now = (as_of - pd.Timedelta(seconds=1)).to_pydatetime(warn=False)  # still inside the last UTC day
    guard = Guard(gcfg, now=lambda: now)
    for t, v in equity.iloc[:-1].items():  # replay history: peak + lock-in latch (realised equity only)
        guard.observe_equity(v, t.to_pydatetime(warn=False))
    filled = trades[trades["filled"]] if len(trades) else trades
    for t in pd.to_datetime(filled["time"], utc=True):
        guard.record_fill(t.to_pydatetime(warn=False))
    snapshot = _snapshot_from_positions(positions, eq_now, last_px)
    price_times = {_short(s): min(data[s].index[-1] + bar, pd.Timestamp(now)) for s in data}
    poll = guard.poll(snapshot, target_weights=target, price_times=price_times, now=now)
    results = poll.to_dict()["results"]
    batch_note = None
    if len(trades):
        last_t = trades["time"].max()
        batch = trades[trades["time"] == last_t]
        ref = closes[closes.index < last_t].iloc[-1]  # the limit reference: previous close
        orders = [ProposedOrder(_short(r.symbol), "COVER" if r.side == "LIQUIDATE" else r.side, float(r.quantity),
                                float(r.price) if r.order_type == "LIMIT" else None, r.order_type)
                  for r in batch.itertuples(index=False)]
        bsnap = dict(snapshot, prices={_short(s): float(v) for s, v in ref.items() if pd.notna(v)})
        bguard = Guard(gcfg, now=lambda: last_t.to_pydatetime(warn=False))
        brep = bguard.evaluate(bsnap, orders, price_times={s: last_t for s in bsnap["prices"]},
                               now=last_t.to_pydatetime(warn=False), project=False)
        by_name = {r["name"]: r for r in brep.to_dict()["results"]}
        batch_note = f"order checks replay the last batch ({len(orders)} orders @ {last_t:%Y-%m-%d %H:%M} UTC)"
        results = [dict(by_name[r["name"]], message=f"[last batch] {by_name[r['name']]['message']}")
                   if r["name"] in ORDER_CHECKS else r for r in results]

    exec_stats = execution_stats(trades, float(equity.mean()))
    return {
        "source": "backtest",
        "title": f"Backtest · {variant} · {start_ts:%Y-%m-%d} → {end_ts:%Y-%m-%d}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "window": {"start": start_ts.isoformat(), "end": end_ts.isoformat(), "days": days},
        "strategy": {"variant": variant, "params": params,
                     "config": {"limit_offset_bps": cfg.limit_offset_bps, "lockin_return": cfg.lockin_return,
                                "lockin_scale": cfg.lockin_scale, "maker_fee": cfg.fees.spot_maker,
                                "taker_fee": cfg.fees.spot_taker, "universe": len(data)}},
        "kpis": k,
        "series": series_payload(equity, gross, net),
        "positions": positions,
        "trades": trades_payload(trades),
        "exec": exec_stats,
        "guard": {"status": _worst([r["status"] for r in results]), "results": results,
                  "note": batch_note, "config": gcfg.__dict__},
        "signals": signals,
        "trade_map": trade_map_payload(closes, score, w, trades, start_ts, params.get("k", 3)),
        "notes": ["Simulated: limit fills follow tradebot.backtest.simulator (5 bp passive, through-fill rule).",
                  "Positions/entries are rebuilt from simulated fills."],
    }


def _snapshot_from_positions(positions, equity, prices) -> Dict[str, Any]:
    longs = {p["symbol"]: p["value"] for p in positions if p["side"] == "LONG"}
    shorts = {p["symbol"]: p["qty"] * p["entry"] for p in positions if p["side"] == "SHORT"}
    short_eq = sum(p["qty"] * p["entry"] + p["upnl"] for p in positions if p["side"] == "SHORT")
    return {"cash_usd": equity - sum(longs.values()) - short_eq, "longs": longs, "shorts": shorts,
            "equity_usd": equity, "prices": {_short(s): v for s, v in prices.items()},
            "entry_prices": {}, "pending_orders": []}


def _worst(statuses) -> str:
    order = {"OK": 0, "WARN": 1, "BLOCK": 2}
    return max(statuses, key=lambda s: order.get(s, 0)) if statuses else "OK"


# ============================================================================== engine
def _latest(directory: Path, patterns) -> Optional[Path]:
    found = []
    for p in patterns:
        found += [Path(x) for x in glob.glob(str(directory / p))]
    return max(found, key=lambda x: x.stat().st_mtime) if found else None


def _ts(value: Any) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return pd.Timestamp(value, unit="ms" if value > 1e11 else "s", tz="UTC")
        t = pd.Timestamp(value)
        return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    except (ValueError, TypeError):
        return None


def _unwrap_snapshot(obj: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Accept a bare normalized snapshot or {"timestamp", "snapshot", ...extras}."""
    if isinstance(obj.get("snapshot"), dict):
        extras = {k: v for k, v in obj.items() if k != "snapshot"}
        return obj["snapshot"], extras
    return obj, obj


def read_audit(path: Optional[Path]) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def read_intents(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not Path(path).exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM intents ORDER BY created_at DESC")]
        signals = con.execute("SELECT COUNT(*) FROM signal_receipts").fetchone()[0]
        con.close()
    except sqlite3.Error:
        return {}
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"counts": counts, "signals": signals, "total": len(rows),
            "uncertain": [{k: r[k] for k in ("intent_id", "signal_id", "symbol", "kind", "updated_at")}
                          for r in rows if r["status"] == "UNCERTAIN"][:20]}


_KIND_SIDE = {"open_long": "BUY", "close_long": "SELL", "open_short": "SHORT", "close_short": "COVER"}


def trades_from_audit(events: list[dict], cfg: GuardConfig) -> pd.DataFrame:
    rows = []
    for e in events:
        p = e.get("payload") or {}
        if e.get("event") == "operation":
            resp = p.get("response") or {}
            d = resp.get("OrderDetail") if isinstance(resp.get("OrderDetail"), dict) else resp
            amt = float(p.get("amount_usd") or 0.0)
            px = float(d.get("FilledAverPrice") or d.get("Price") or d.get("EntryPrice") or resp.get("EntryPrice") or 0.0)
            qty = float(d.get("FilledQuantity") or d.get("Quantity") or d.get("ShortQty") or d.get("ClosedQty") or 0.0)
            if not qty and px:
                qty = amt / px
            if not px and qty:
                px = amt / qty
            dry = bool(resp.get("DryRun"))
            otype = "DRY" if dry else str(d.get("Type") or resp.get("OrderType") or "MARKET").upper()
            ex_status = str(d.get("Status") or resp.get("Status") or "").upper()
            filled = (not dry) and p.get("status") in ("RESOLVED", "SENT") and ex_status in ("FILLED", "OPEN", "")
            if ex_status == "PENDING":
                filled = False
            fee = d.get("CommissionChargeValue") or d.get("OpenFee") or d.get("CloseFee")
            side = _KIND_SIDE.get(p.get("kind"), str(p.get("kind", "")).upper())
            if fee is None:
                rate = cfg.short_fee if side == "SHORT" else (cfg.limit_fee if otype == "LIMIT" else cfg.market_fee)
                fee = amt * rate if filled else 0.0
            rows.append(dict(time=_ts(e.get("timestamp")), symbol=str(p.get("symbol", "")).upper(), side=side,
                             order_type=otype, filled=bool(filled), quantity=qty, price=px,
                             value=amt or qty * px, fee=float(fee),
                             status=f"{p.get('status', '')}{'/' + ex_status if ex_status else ''}"))
        elif e.get("event") == "risk_rejection":
            rows.append(dict(time=_ts(e.get("timestamp")), symbol="—", side="REJECT", order_type="RISK",
                             filled=False, quantity=0.0, price=0.0, value=0.0, fee=0.0,
                             status="REJECTED: " + ",".join(p.get("reasons", []))))
    df = pd.DataFrame(rows, columns=TRADE_COLUMNS + ["status"])
    return df.dropna(subset=["time"]) if len(df) else df


def load_engine(engine_dir: str | os.PathLike = "var/live", *, audit: Optional[str] = None,
                db: Optional[str] = None, snapshot: Optional[str] = None, initial: float = 100_000.0,
                kill_file: str = "KILL", now: Optional[datetime] = None,
                guard_config: Optional[GuardConfig] = None) -> Dict[str, Any]:
    d = Path(engine_dir)
    audit_p = Path(audit) if audit else _latest(d, ["*audit*.jsonl", "*.audit.jsonl"])
    db_p = Path(db) if db else _latest(d, ["*.db", "*.sqlite", "*.sqlite3"])
    snap_p = Path(snapshot) if snapshot else _latest(d, ["latest_snapshot.json", "*snapshot*.json"])
    hist_p = _latest(d, ["snapshots.jsonl", "*snapshot*.jsonl", "equity*.jsonl"])
    now = now or datetime.now(timezone.utc)
    notes = [f"audit: {audit_p or '—'}", f"intents: {db_p or '—'}", f"snapshot: {snap_p or '—'}"]
    gcfg = guard_config or GuardConfig(initial_equity_usd=initial, kill_file=kill_file)
    initial = gcfg.initial_equity_usd

    events = read_audit(audit_p)
    intents = read_intents(db_p)
    trades = trades_from_audit(events, gcfg)

    snap, extras = ({}, {})
    if snap_p and snap_p.exists():
        snap, extras = _unwrap_snapshot(json.loads(snap_p.read_text()))
    snap_time = _ts(extras.get("timestamp")) or (_ts(snap_p.stat().st_mtime) if snap_p and snap_p.exists() else None)

    # ---- equity history: snapshot JSONL, audit events carrying equity, then the latest snapshot
    pts = []
    if hist_p and hist_p.exists():
        for e in read_audit(hist_p):
            s, ex = _unwrap_snapshot(e)
            t = _ts(ex.get("timestamp"))
            if t is not None and s.get("equity_usd") is not None:
                g = _gross_net(s)
                pts.append((t, float(s["equity_usd"]), *g))
    for e in events:
        p = e.get("payload") or {}
        if "equity_usd" in p and _ts(e.get("timestamp")) is not None:
            pts.append((_ts(e["timestamp"]), float(p["equity_usd"]), *_gross_net(p)))
    if snap and snap.get("equity_usd") is not None:
        pts.append((snap_time or pd.Timestamp(now), float(snap["equity_usd"]), *_gross_net(snap)))
    if pts:
        hist = pd.DataFrame(pts, columns=["t", "equity", "gross", "net"]).drop_duplicates("t", keep="last") \
            .set_index("t").sort_index()
    else:
        hist = pd.DataFrame({"equity": [initial], "gross": [0.0], "net": [0.0]}, index=[pd.Timestamp(now)])
        notes.append("no snapshot found: showing an empty book at initial equity")
    equity = hist["equity"]
    k = equity_kpis(equity, initial, "15m", hist["gross"], hist["net"], gcfg.lockin_return)
    if extras.get("locked") is not None:
        k["bot_locked"] = bool(extras["locked"])

    # ---- positions from the snapshot, entries/P&L from fills where available
    prices = {str(s).upper(): float(v) for s, v in (snap.get("prices") or {}).items()}
    eq_now = float(snap.get("equity_usd") or initial)
    target = {str(s).upper(): float(v) for s, v in (extras.get("target_weights") or {}).items()}
    fills = trades[trades["side"].isin(["BUY", "SELL", "SHORT", "COVER"])] if len(trades) else trades
    book = {r["symbol"]: r for r in reconstruct_book(fills, prices, eq_now, initial)} if len(fills) else {}
    positions = []
    for sym, val in (snap.get("longs") or {}).items():
        s, last = sym.upper(), prices.get(sym.upper(), 0.0)
        b = book.get(s, {})
        qty = val / last if last else 0.0
        entry = b.get("entry") or last
        upnl = qty * (last - entry)
        positions.append(_pos_row(s, "LONG", qty, entry, last, val, eq_now, target, upnl, b, initial))
    for sym, coll in (snap.get("shorts") or {}).items():
        s, last = sym.upper(), prices.get(sym.upper(), 0.0)
        entry = float((snap.get("entry_prices") or {}).get(s) or book.get(s, {}).get("entry") or last)
        qty = coll / entry if entry else 0.0
        upnl = qty * (entry - last)
        positions.append(_pos_row(s, "SHORT", qty, entry, last, -qty * last, eq_now, target, upnl, book.get(s, {}), initial))
    held = {p["symbol"] for p in positions}
    for s, t in target.items():
        if s not in held and abs(t) > 1e-9:
            positions.append(_pos_row(s, "FLAT", 0.0, 0.0, prices.get(s, 0.0), 0.0, eq_now, target, 0.0, {}, initial))
    positions.sort(key=lambda r: -r["weight"])

    # ---- signals (optional extras written by the strategy bridge)
    scores = extras.get("scores") or extras.get("signals") or {}
    signals = {"as_of": str(snap_time) if snap_time is not None else None, "rows": [], "longs": [], "shorts": [],
               "consistent": None, "k": extras.get("k")}
    if isinstance(scores, dict) and scores:
        ranked = sorted(((str(s).upper(), float(v)) for s, v in scores.items() if v is not None), key=lambda x: -x[1])
        for i, (s, v) in enumerate(ranked, 1):
            t = target.get(s, 0.0)
            signals["rows"].append(dict(rank=i, symbol=s, score=v, vol_ann=None,
                                        side="LONG" if t > 0 else "SHORT" if t < 0 else "", target_weight=t))
        signals["longs"] = [s for s, t in target.items() if t > 0]
        signals["shorts"] = [s for s, t in target.items() if t < 0]

    # ---- guard on the live state
    guard = Guard(gcfg, now=lambda: now)
    for t, v in equity.iloc[:-1].items():
        guard.observe_equity(v, t.to_pydatetime(warn=False))
    for t in (trades[trades["filled"]]["time"] if len(trades) else []):
        guard.record_fill(t.to_pydatetime(warn=False))
        if now - t.to_pydatetime(warn=False) <= timedelta(seconds=60):
            guard.record_orders_sent(1, t.to_pydatetime(warn=False))
    ptimes = extras.get("price_times") or ({s: snap_time.to_pydatetime(warn=False) for s in prices} if snap_time is not None else None)
    report = guard.poll(snap or {"equity_usd": initial, "cash_usd": initial}, target_weights=target or None,
                        price_times=ptimes, bot_locked=extras.get("locked"), now=now)

    tdf = trades[trades["side"] != "REJECT"] if len(trades) else trades
    ex = execution_stats(tdf[TRADE_COLUMNS] if len(tdf) else pd.DataFrame(columns=TRADE_COLUMNS), float(equity.mean()))
    ex["intents"] = intents
    ex["risk_rejections"] = int((trades["side"] == "REJECT").sum()) if len(trades) else 0
    as_of = snap_time.isoformat() if snap_time is not None else now.isoformat()
    return {
        "source": "engine",
        "title": f"Engine · {d.resolve().name}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of,
        "window": {"start": equity.index[0].isoformat(), "end": as_of, "days": None},
        "strategy": {"variant": extras.get("strategy_id", "engine"), "params": extras.get("params", {}), "config": {}},
        "kpis": k,
        "series": series_payload(equity, hist["gross"], hist["net"]),
        "positions": positions,
        "trades": trades_payload(trades),
        "exec": ex,
        "guard": {"status": report.status.value, "results": report.to_dict()["results"], "note": None,
                  "config": gcfg.__dict__},
        "signals": signals,
        "notes": notes,
    }


def _gross_net(s: Dict[str, Any]) -> tuple[float, float]:
    eq = float(s.get("equity_usd") or 0.0)
    if eq <= 0:
        return 0.0, 0.0
    lv = sum(float(v) for v in (s.get("longs") or {}).values())
    sv = sum(float(v) for v in (s.get("shorts") or {}).values())
    return (lv + sv) / eq, (lv - sv) / eq


def _pos_row(sym, side, qty, entry, last, value, equity, target, upnl, b, initial) -> Dict[str, Any]:
    w = value / equity if equity else 0.0
    t = float(target.get(sym, 0.0))
    realized, fees = float(b.get("realized", 0.0)), float(b.get("fees", 0.0))
    return dict(symbol=sym, side=side, qty=qty, entry=entry, last=last, value=value, weight=w, target_weight=t,
                drift=w - t, upnl=upnl, upnl_pct=upnl / (qty * entry) if qty and entry else 0.0,
                realized=realized, fees=fees, contribution=(realized + upnl - fees) / initial if initial else 0.0)


def to_json(model: Dict[str, Any]) -> str:
    def default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if not np.isfinite(o) else float(o)
        if isinstance(o, (pd.Timestamp, datetime)):
            return o.isoformat()
        return str(o)

    def clean(o):
        if isinstance(o, float):
            return o if np.isfinite(o) else None
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        return o

    return json.dumps(clean(model), default=default, separators=(",", ":"))
