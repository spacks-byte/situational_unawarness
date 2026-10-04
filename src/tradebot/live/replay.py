"""
Replay simulation of the live bot: `python -m tradebot replay` (docs/LIVE_RUNBOOK.md section 1).

Runs the real `LiveRunner` (same strategy bridge, guard, re-pegging, throttle, engine and recovery
loop) against `ReplayExchangePort`, which replays downloaded 15m candles, on a simulated clock.
Never touches the network or the Roostoo API, and live mode is never set.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path

import pandas as pd

from tradebot.backtest.simulator import run_backtest
from tradebot.core.clock import SimClock
from tradebot.core.config import BacktestConfig, Settings
from tradebot.exchange.replay import ReplayExchangePort
from tradebot.live.bridge import rxm_spec
from tradebot.live.market_data import parquet_fetch
from tradebot.live.runner import LiveRunner, _universe
from tradebot.strategy.library.rxm import ResidualMomentum

log = logging.getLogger(__name__)


def run_replay(settings: Settings, start: str, days: float, cash: float, out_dir: str | Path,
               keep_state: bool = False) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if not keep_state:
        for name in ("engine_state.db", "engine_audit.jsonl", "strategy_state.json", "status.json",
                     "latest_snapshot.json", "snapshots.jsonl", "fills.csv"):
            (out / name).unlink(missing_ok=True)
    settings = settings.model_copy(update={"live": settings.live.model_copy(update={"state_dir": str(out)})})

    begin = pd.Timestamp(start, tz="UTC")
    end = begin + pd.Timedelta(days=days)
    clock = SimClock(begin.to_pydatetime())
    fetch_disk = parquet_fetch(settings.data.dir)
    bars = {c: fetch_disk(c, begin - pd.Timedelta(days=60), end + pd.Timedelta(hours=1)) for c in _universe(settings)}
    bars = {c: df for c, df in bars.items() if len(df)}
    if not bars:
        raise FileNotFoundError(f"no candles in {settings.data.dir} for {start}: run `python -m tradebot data` first")

    sim = ReplayExchangePort(bars, clock, initial_usd=cash, fees=settings.fees)
    runner = LiveRunner(settings, mode="simulate", port=sim, clock=clock,
                        fetch=lambda c, a, b: bars.get(c, pd.DataFrame()).loc[a: b - pd.Timedelta(seconds=1)])
    loops = int(days * 86_400 / runner.poll_seconds)
    print(f"REPLAY {runner.strategy.mode} {begin} -> {end} | poll {runner.poll_seconds:.0f}s, {loops} loops, "
          f"${cash:,.0f}, {len(bars)} coins (no network, no orders to Roostoo)")

    statuses: Counter = Counter()
    original = runner.run_once

    def counted():
        result = original()
        statuses[(result or {}).get("status", "PAUSED/FAILED")] += 1
        return result

    runner.run_once = counted
    t0 = time.time()
    runner.run(max_iterations=loops)
    wall = time.time() - t0

    strategy = runner.strategy
    fills = pd.DataFrame(sim.fills)
    if len(fills):
        fills.to_csv(out / "fills.csv", index=False)
    equity = sim.equity()
    signals = [e["signal_id"] for e in strategy.events]
    calls = sim.calls
    summary = {
        "final_equity": equity,
        "return": equity / cash - 1,
        "loops": sum(statuses.values()),
        "statuses": dict(statuses),
        "rebalances": [s for s in signals if "-r" not in s and "-hold" not in s],
        "requotes": sum("-r" in s for s in signals),
        "locked": strategy.locked,
        "fills": len(fills),
        "limit_fills": int((fills["type"] == "LIMIT").sum()) if len(fills) else 0,
        "market_fills": int((fills["type"] == "MARKET").sum()) if len(fills) else 0,
        "orders": sum(calls.get(k, 0) for k in ("place_order", "open_short", "close_short")),
        "guard_blocked": len(runner.guarded.blocked),
        "http_per_min": runner.throttled.total_http / (days * 1440),
        "http_peak": runner.throttled.peak,
    }
    print("\n================ REPLAY SUMMARY ================")
    print(f"wall time {wall:.0f}s | loops {summary['loops']} {summary['statuses']}")
    print(f"rebalances {len(summary['rebalances'])}: {summary['rebalances']}")
    print(f"re-quotes {summary['requotes']} | lock-in {summary['locked']} {strategy.state.get('lock_time', '')}")
    print(f"orders {summary['orders']} | fills {summary['fills']} (limit {summary['limit_fills']}, "
          f"market {summary['market_fills']}) | blocked by guard {summary['guard_blocked']}")
    print(f"API: {summary['http_per_min']:.1f} requests/min average, peak {summary['http_peak']}/min "
          f"(cap {settings.live.max_http_per_minute})")
    print(f"final equity {equity:,.2f} ({summary['return']:+.2%})")
    if settings.live.strategy == "rxm":
        ref = _backtest_reference(bars, strategy.mode, begin, end, cash, settings)
        summary["backtest_return"] = ref
        print(f"backtest simulator, same period: {ref:+.2%} (differences: start time, re-quote timing)")
    print(f"files: {out}")
    return summary


def _backtest_reference(bars, mode, start, end, cash, settings: Settings) -> float:
    spec = rxm_spec(mode)
    data = {c: df[df.index < end.floor("15min")] for c, df in bars.items()}
    cfg = BacktestConfig(initial_cash=cash, limit_offset_bps=spec["limit_offset_bps"], gap_improvement=False,
                         lockin_return=spec["lockin_return"], lockin_scale=spec["lockin_scale"] or 0.3,
                         fees=settings.fees)
    res = run_backtest(ResidualMomentum(**spec["params"]), data, "15m", cfg, trade_start=start.floor("15min"))
    return float(res.equity.iloc[-1] / cash - 1)
