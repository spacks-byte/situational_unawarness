"""End-to-end MOCK run: frozen strategy -> CompetitionStrategy -> Engine -> simulated exchange.

Replays real Binance 15m bars from data/binance/klines/15m on a SimClock, polling like the live
bot (default every 60 s), with dry_run=False on a simulated port only. Never touches the
network or the Roostoo API; live_mode is never set.

    python3 scripts/run_comp_mock.py                         # 2026-09-01 .. 09-08, comp mode
    python3 scripts/run_comp_mock.py --mode neutral --days 3
    python3 scripts/run_comp_mock.py --port mock             # engine's MockExchangePort (shows its gaps)

Outputs under results/live_demo/: engine_audit.jsonl, engine_state.db, strategy_state.json,
fills.csv, equity.csv.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from backtest.experiments import UNIVERSE  # noqa: E402
from backtest.strategies.rxm import ResidualMomentum  # noqa: E402
from src.engine.app import Engine  # noqa: E402
from src.engine.clock import SimClock  # noqa: E402
from src.engine.ports.mock_port import MockExchangePort  # noqa: E402
from src.strategy_bridge import load_competition_config  # noqa: E402
from src.strategy_bridge.live_strategy import CompetitionStrategy, mode_spec  # noqa: E402
from src.strategy_bridge.market_data import (BarBuffer, DEFAULT_KLINES_DIR, binance_to_roostoo,  # noqa: E402
                                             parquet_fetch, roostoo_to_binance)
from src.strategy_bridge.repeg import RepegPort  # noqa: E402
from src.strategy_bridge.replay_port import ReplayExchangePort  # noqa: E402
from src.strategy_bridge.throttle import ThrottledPort  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="comp", choices=["comp", "neutral"])
    p.add_argument("--start", default="2026-09-01T00:16")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--poll", type=int, default=None, help="seconds between engine loops (default: config, 60)")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--port", default="replay", choices=["replay", "mock"])
    p.add_argument("--out", default=str(ROOT / "results" / "live_demo"))
    p.add_argument("--keep-state", action="store_true", help="don't wipe previous state (restart test)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    files = {k: out / v for k, v in dict(state="engine_state.db", audit="engine_audit.jsonl",
                                          strat="strategy_state.json").items()}
    if not a.keep_state:
        for f in files.values():
            f.unlink(missing_ok=True)

    start = pd.Timestamp(a.start, tz="UTC")
    end = start + pd.Timedelta(days=a.days)
    clock = SimClock(start.to_pydatetime())
    symbols = [roostoo_to_binance(s) for s in UNIVERSE]
    fetch = parquet_fetch(DEFAULT_KLINES_DIR)          # stands in for Binance public klines
    bars = {s: fetch(s, start - pd.Timedelta(days=60), end + pd.Timedelta(hours=1)) for s in symbols}
    bars = {s: df for s, df in bars.items() if len(df)}

    cfg = load_competition_config(dry_run=False)        # live_mode stays False
    if a.poll:
        cfg = load_competition_config(dry_run=False, strategy_poll_interval_seconds=a.poll)
    assert cfg.live_mode is False

    if a.port == "replay":
        sim = ReplayExchangePort(bars, clock, initial_usd=a.cash)
    else:
        sim = MockExchangePort(initial_wallet={"USD": a.cash, "BTC": 0.0, "ETH": 0.0}, tickers={}, clock=clock)
    port = ThrottledPort(sim, clock, max_per_minute=25)
    port = RepegPort(port, mode_spec(a.mode)["limit_offset_bps"])   # same latency guard as scripts/run_live.py
    engine = Engine(port, config=cfg, state_path=files["state"], audit_path=files["audit"], clock=clock)
    buffer = BarBuffer(symbols, fetch=lambda s, a_, b_: bars.get(s, pd.DataFrame()).loc[a_: b_ - pd.Timedelta(seconds=1)],
                       window_days=50)
    strategy = CompetitionStrategy(buffer, mode=a.mode, state_path=files["strat"], clock=clock)

    print(f"MOCK run | mode={a.mode} ({mode_spec(a.mode)['variant']}) port={a.port} {start} -> {end} "
          f"poll={cfg.strategy_poll_interval_seconds}s cash={a.cash:,.0f} dry_run={cfg.dry_run} live_mode={cfg.live_mode}")
    t0 = time.time()
    statuses: Counter = Counter()
    ops: Counter = Counter()
    equity_rows = []
    n_events = 0
    last_day = None
    while pd.Timestamp(clock.now()) < end:
        if a.port == "mock":       # the engine's mock has static tickers: feed it the replayed closes
            last_closed = pd.Timestamp(clock.now()).floor("15min") - pd.Timedelta(minutes=15)
            for s, df in bars.items():
                upto = df.loc[:last_closed, "close"]
                if len(upto):
                    sim.tickers[binance_to_roostoo(s)] = float(upto.iloc[-1])
        result = engine.run_once(strategy)
        statuses[result["status"]] += 1
        for op in result.get("operations", []):
            ops[(op["kind"], op["status"])] += 1
        for ev in strategy.events[n_events:]:
            w = ev["weights"]
            longs = ", ".join(f"{k} {v:+.3f}" for k, v in sorted(w.items(), key=lambda kv: -kv[1]) if v > 0)
            shorts = ", ".join(f"{k} {v:+.3f}" for k, v in sorted(w.items(), key=lambda kv: kv[1]) if v < 0)
            if "-r" not in ev["signal_id"]:
                print(f"\n[{ev['time']:%Y-%m-%d %H:%M}] {ev['signal_id']} ({ev['reason']}, locked={ev['locked']})"
                      f"\n    long : {longs}\n    short: {shorts}")
            elif a.verbose:
                print(f"[{ev['time']:%Y-%m-%d %H:%M}] {ev['signal_id']} {ev['reason']}")
        n_events = len(strategy.events)
        if result.get("operations"):
            kinds = Counter(f"{o['kind']}:{o['status']}" for o in result["operations"])
            if "-r" not in result["signal_id"] or a.verbose:
                print(f"    -> {result['status']} {dict(kinds)}")
        elif result["status"] == "REJECTED_RISK":
            print(f"[{clock.now():%Y-%m-%d %H:%M}] {result['signal_id']} REJECTED_RISK {result.get('reasons')}")
        now = pd.Timestamp(clock.now())
        if hasattr(sim, "equity") and (not equity_rows or now - equity_rows[-1][0] >= pd.Timedelta(minutes=15)):
            equity_rows.append((now, sim.equity()))
        if last_day != now.date() and now.hour == 23 and now.minute >= 59 and equity_rows:
            last_day = now.date()
            print(f"  [{now:%Y-%m-%d} end of day] equity {equity_rows[-1][1]:,.2f}")
        clock.sleep(cfg.strategy_poll_interval_seconds)

    wall = time.time() - t0
    engine.close()
    eq_final = sim.equity() if hasattr(sim, "equity") else None
    fills = pd.DataFrame(getattr(sim, "fills", []))
    if len(fills):
        fills.to_csv(out / "fills.csv", index=False)
    pd.DataFrame(equity_rows, columns=["time", "equity"]).to_csv(out / "equity.csv", index=False)
    signals = [e["signal_id"] for e in strategy.events]
    daily = [s for s in signals if "-r" not in s and "-hold" not in s]
    requotes = [s for s in signals if "-r" in s]
    calls = getattr(sim, "calls", {})
    placed = sum(calls.get(k, 0) for k in ("place_order", "open_short", "close_short"))
    minutes = a.days * 24 * 60
    print("\n================ SUMMARY ================")
    print(f"wall time {wall:.1f}s, engine loops {sum(statuses.values())}, results {dict(statuses)}")
    print(f"daily rebalance signals {len(daily)}: {daily}")
    print(f"re-quote signals {len(requotes)}, lock-in: {strategy.locked} {strategy.state.get('lock_time', '')}")
    print("operations:", {f"{k[0]}:{k[1]}": v for k, v in sorted(ops.items())})
    if len(fills):
        print(f"fills {len(fills)} (limit {int((fills['type'] == 'LIMIT').sum())}, market {int((fills['type'] == 'MARKET').sum())}), "
              f"traded value {fills['value'].sum():,.0f}; orders placed {placed}; "
              f"limit orders cancelled unfilled {sum(1 for o in sim.history if o.get('Status') == 'CANCELED')}")
    if eq_final is not None:
        print(f"final equity {eq_final:,.2f} ({eq_final / a.cash - 1:+.2%}); start {a.cash:,.0f}")
    print(f"API: {port.total_http} HTTP requests (weighted) over {minutes:.0f} sim-min = {port.total_http / minutes:.1f}/min avg, "
          f"peak {port.peak}/min (cap 25), throttle sleeps {port.total_sleep:.0f}s; port calls {getattr(sim, 'calls', {})}")
    if a.port == "replay":
        ref = _backtest_reference(bars, a.mode, start, end, a.cash)
        print(f"backtest engine reference, same period: {ref:+.2%} (00:00-start windows; differences: market exits, "
              f"start at {start:%H:%M}, re-quote timing)")
    print(f"audit: {files['audit']}  state: {files['state']}  strategy state: {files['strat']}")
    return 0


def _backtest_reference(bars, mode, start, end, cash) -> float:
    spec = mode_spec(mode)
    data = {s: df[df.index < end.floor("15min")] for s, df in bars.items()}
    cfg = BacktestConfig(initial_cash=cash, limit_offset_bps=spec["limit_offset_bps"], gap_improvement=False,
                         lockin_return=spec["lockin_return"], lockin_scale=spec["lockin_scale"] if spec["lockin_return"] else 0.3)
    res = run_backtest(ResidualMomentum(**spec["params"]), data, "15m", cfg, trade_start=start.floor("15min"))
    return float(res.equity.iloc[-1] / cash - 1)


if __name__ == "__main__":
    raise SystemExit(main())
