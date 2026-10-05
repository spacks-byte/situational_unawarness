"""Replay the actual shared coordinator with verified PoC one-second archives.

Binance candle touch prices are an execution proxy, never an exact Roostoo replay.
RXM uses its existing local 15m data; no network/downloads happen in this command.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot.core.clock import SimClock
from tradebot.exchange.replay import ReplayExchangePort
from tradebot.live.market_data import parquet_fetch
from tradebot.live.runner import _universe
from tradebot.live.account import AccountRunner, selected_strategies
from tradebot.engine.state.portfolio import MM

DTYPE = np.dtype([("timestamp", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
                  ("close", "<f8"), ("volume", "<f8"), ("taker_buy_base", "<f8"), ("trades", "<i8")])


def cached_seconds(root, coin, start, end):
    parts = []
    for day in pd.date_range(start.floor("D"), (end-pd.Timedelta(seconds=1)).floor("D"), freq="D"):
        path = Path(root) / "binance" / "spot" / f"{coin}USDT" / "1s" / f"{day.date()}.bin"
        metadata = json.loads(path.with_suffix(".json").read_text())
        raw = path.read_bytes()
        if metadata.get("schema_version") != 1 or metadata.get("validation_version", 0) < 2 or \
                hashlib.sha256(raw).hexdigest() != metadata.get("binary_sha256"):
            raise ValueError(f"unverified/corrupt one-second cache: {path}")
        records = np.frombuffer(raw, dtype=DTYPE)
        df = pd.DataFrame(records)
        df.index = pd.to_datetime(df.pop("timestamp"), unit="s", utc=True)
        parts.append(df[(df.index >= start) & (df.index < end)])
    result = pd.concat(parts)
    expected = pd.date_range(start, end, freq="s", inclusive="left")
    if not result.index.equals(expected):
        raise ValueError(f"{coin}: missing/duplicate/out-of-order seconds")
    if not np.isfinite(result[["open", "high", "low", "close"]].to_numpy()).all():
        raise ValueError(f"{coin}: nonfinite candle prices")
    return result


def run_shared_replay(settings, start, days, cash, out_dir, keep_state=False):
    out = Path(out_dir)
    if keep_state:
        raise ValueError("shared replay cannot reuse a ledger with a reset exchange; restart tests retain exchange state")
    if (out / "portfolio.db").exists():
        raise ValueError("use a new replay output folder; existing portfolio state is preserved")
    begin = pd.Timestamp(start)
    begin = begin.tz_localize("UTC") if begin.tzinfo is None else begin.tz_convert("UTC")
    if begin != begin.floor("s") or days <= 0 or cash <= 0:
        raise ValueError("replay needs an integral start second, positive days and cash")
    end = begin+pd.Timedelta(days=days)
    warmup = begin-pd.Timedelta(seconds=settings.market_making.warmup_seconds)
    active = selected_strategies(settings)
    seconds = {c: cached_seconds(settings.market_making.replay_cache_dir, c, warmup, end)
               for c in settings.market_making.allocations} if MM in active else {}
    disk = parquet_fetch(settings.data.dir)
    rxm = {c: disk(c, begin-pd.Timedelta(days=settings.live.buffer_days), end) for c in _universe(settings)} if "rxm" in active else {}
    missing = [c for c, df in rxm.items() if len(df) < 45*96]
    if missing:
        raise FileNotFoundError(f"RXM needs cached 15m warmup for {missing}; run tradebot data first")
    bars = {**rxm, **seconds}
    clock = SimClock(begin.to_pydatetime())
    sim = ReplayExchangePort(bars, clock, initial_usd=cash, fees=settings.fees,
                             intervals={c: "1s" for c in seconds})
    settings = settings.model_copy(deep=True)
    settings.live.state_dir = str(out)
    settings.market_making.rxm_state_dir = str(out / "no-legacy-state")
    runner = AccountRunner(settings, mode="simulate", port=sim, clock=clock,
                          fetch=lambda c, a, b: rxm[c].loc[a:b-pd.Timedelta(nanoseconds=1)],
                          mm_fetch=lambda c, a, b: seconds[c].loc[a:b-pd.Timedelta(nanoseconds=1)])
    statuses = Counter()
    try:
        while clock.now() < end:
            result = runner.run_once()
            statuses[result["status"]] += 1
            if result["status"] in {"BLOCKED", "PARTIAL"}:
                raise RuntimeError(f"account replay failed: {result}")
            clock.sleep(min(runner.poll_seconds, (end-clock.now()).total_seconds()))
        runner.account.sync()
        report = runner.account.report()
        summary = {"start": begin.isoformat(), "end": end.isoformat(), "statuses": dict(statuses),
                   "finished_at": clock.now().isoformat(), "active_strategies": active,
                   "http_peak": runner.throttled.peak, "http_limit": settings.live.max_http_per_minute,
                   "fills": len(sim.fills), "account": report,
                   "execution_assumption": "Binance trade-containing 1s candle touches for MM; 15m through fills for other RXM symbols; historical Roostoo best asks/bids unavailable. No terminal liquidation."}
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        pd.DataFrame(sim.fills).to_csv(out / "fills.csv", index=False)
        events = pd.read_sql_query("SELECT * FROM events ORDER BY id", runner.store.db)
        events.to_csv(out / "ledger-events.csv", index=False)
        fills = [{"observed_at": e["timestamp"], **json.loads(e["payload"])}
                 for e in events.to_dict("records") if e["kind"] == "fill"]
        pd.DataFrame(fills).to_csv(out / "ledger-fills.csv", index=False)
        orders = runner.store.completed("mm-10m-fluctuation") + runner.store.completed("rxm")
        pd.DataFrame([{k: v for k, v in o.items() if k != "row"} for o in orders]).to_csv(out / "orders.csv", index=False)
        print(json.dumps({k: v for k, v in summary.items() if k != "account"}, indent=2))
        return summary
    finally:
        runner.close()
