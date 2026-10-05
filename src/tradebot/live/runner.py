"""
Unattended live runner: `python -m tradebot live` (see docs/LIVE_RUNBOOK.md).

    strategy (LiveStrategy / CompetitionStrategy, fed by BarBuffer candles)
      -> Engine.run_once: cancel stale orders -> snapshot -> plan -> risk -> orders
      (plan_guard: guard portfolio checks on the whole plan, before anything is sent)
      -> RepegPort (fresh price per limit) -> GuardedPort (per-order guard checks)
      -> ThrottledPort (<= max_http_per_minute) -> RoostooExchangePort | ReplayExchangePort (simulation)

Built to run for two weeks without anyone watching:
  * One failed loop never stops the bot. Errors are logged with a traceback, and the next attempt
    waits poll * 2**(failures-1), capped at live.max_backoff_seconds. A success resets the delay.
  * Every loop rebuilds state from the exchange (snapshot) and the on-disk journal, so restarts are
    safe: signal ids are idempotent and the strategy's start equity and lock-in are persisted.
  * Kill switch: while the file live.kill_file exists, no loop runs and nothing is sent. Delete it to resume.
  * The guard (tradebot.live.guard) checks the whole plan, then every order, and the account every loop.
  * Ops files in live.state_dir: status.json (rewritten every loop: for a watchdog), latest_snapshot.json
    (for the dashboard), bot.log, engine_state.db, engine_audit.jsonl, strategy_state.json.
  * SIGINT / SIGTERM stop it cleanly between loops.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import traceback
from dataclasses import fields
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Literal

import pandas as pd

from tradebot.core.clock import Clock, RealClock
from tradebot.core.config import Settings
from tradebot.core.symbols import to_coin
from tradebot.engine import Engine
from tradebot.live.bridge import CompetitionStrategy, LiveStrategy, SnapshotRejected, rxm_spec
from tradebot.engine.state.snapshot import remaining_qty
from tradebot.live.guard import Guard, GuardConfig, ProposedOrder, Status, check_kill_switch, project_snapshot
from tradebot.live.market_data import BarBuffer, FetchFn, binance_public_fetch
from tradebot.live.repeg import RepegPort
from tradebot.live.throttle import ThrottledPort
from tradebot.strategy.library.rxm import UNIVERSE as RXM_UNIVERSE
from tradebot.strategy.registry import STRATEGIES

log = logging.getLogger(__name__)

RunMode = Literal["dry-run", "live", "simulate"]


# Guard checks that judge one order on its own; the portfolio checks run on the whole plan instead
ORDER_CHECKS = {"kill_switch", "price_band", "order_notional", "self_cross", "order_rate", "api_budget"}


class GuardedPort:
    """Checks every order-changing call against the guard's per-order checks before it is sent.

    The guard runs at two levels (see LiveRunner.plan_guard):
      * the whole plan, once, before anything is sent: gross, net, per-symbol, short collateral, cash,
        lock-in. These describe the portfolio after the batch; judging them order by order fails a
        normal rebalance halfway (e.g. after the buys and before the shorts, net looks too long);
      * each order at send time (here): kill switch, fat-finger price, order size, self-cross,
        order/API rate. These depend only on the order, with the price it is actually sent at.
    A blocked order is not sent; the engine sees Success: False and marks it REJECTED. The rest of
    the plan still runs, and each later order is checked on its own. Cancels are never blocked; short closes only by the kill switch (they reduce risk).
    """

    def __init__(self, port: Any, guard: Guard, snapshot: Callable[[], dict[str, Any] | None],
                 price_times: Callable[[], dict[str, Any] | None] = lambda: None) -> None:
        self._port = port
        self._guard = guard
        self._snapshot = snapshot
        self._price_times = price_times
        self.is_live = bool(getattr(port, "is_live", False))
        self.blocked: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._port, name)

    def _check(self, order: ProposedOrder) -> dict[str, Any] | None:
        report = self._guard.pre_trade(self._snapshot() or {}, [order], price_times=self._price_times())
        blocks = [r for r in report.blocks if r.name in ORDER_CHECKS]
        if not blocks:
            self._guard.record_orders_sent(1)
            return None
        reason = "; ".join(f"{r.name}: {r.message}" for r in blocks)
        self.blocked.append(reason)
        log.error("guard blocked %s %s %.8g @ %s: %s", order.side, order.sym, order.quantity, order.price, reason)
        return {"Success": False, "ErrMsg": f"guard: {reason}"}

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        blocked = self._check(ProposedOrder(to_coin(pair_or_coin), str(side).upper(), float(quantity),
                                            None if price is None else float(price), order_type))
        return blocked or self._port.place_order(pair_or_coin, side, quantity, price=price, order_type=order_type)

    def open_short(self, pair_or_coin, collateral, price=None):
        coin = to_coin(pair_or_coin)
        px = float(price) if price is not None else float((self._snapshot() or {}).get("prices", {}).get(coin, 0.0))
        qty = float(collateral) / px if px > 0 else 0.0
        blocked = self._check(ProposedOrder(coin, "SHORT", qty, None if price is None else float(price)))
        return blocked or self._port.open_short(pair_or_coin, collateral, price=price)

    def close_short(self, pair_or_coin, close_qty=None, close_pct=None):
        kill = check_kill_switch(self._guard.config.kill_file, self._guard.killed)
        if kill.status is Status.BLOCK:
            log.error("guard blocked short close on %s: kill switch", to_coin(pair_or_coin))
            return {"Success": False, "ErrMsg": "guard: kill switch"}
        self._guard.record_orders_sent(1)
        return self._port.close_short(pair_or_coin, close_qty=close_qty, close_pct=close_pct)


def plan_orders(plan: dict[str, Any], prices: dict[str, float]) -> list[ProposedOrder]:
    """The engine's rebalance plan (USD per symbol) as guard orders, valued at the snapshot price."""
    sides = (("close_longs", "SELL"), ("close_shorts", "COVER"), ("open_longs", "BUY"), ("open_shorts", "SHORT"))
    orders = []
    for key, side in sides:
        for symbol, amount in (plan.get(key) or {}).items():
            price = float(prices.get(symbol, 0.0))
            if price > 0 and float(amount) > 0:
                orders.append(ProposedOrder(symbol, side, float(amount) / price, price))
    return orders


def resting_orders(pending: list[dict[str, Any]] | None, prices: dict[str, float]) -> list[ProposedOrder]:
    """Resting opens/sells (unfilled part) as guard orders, so portfolio checks see the book they make."""
    sides = {"BUY": "BUY", "SELL": "SELL", "SHORT_OPEN": "SHORT"}
    out = []
    for order in pending or []:
        side = sides.get(str(order.get("Side", "")).upper())
        if side is None or str(order.get("Status", "PENDING")).upper() != "PENDING":
            continue
        coin = to_coin(str(order.get("Pair", "")))
        price = float(order.get("Price") or prices.get(coin, 0.0) or 0.0)
        qty = remaining_qty(order)
        if coin and price > 0 and qty > 0:
            out.append(ProposedOrder(coin, side, qty, price, "LIMIT"))
    return out


def build_live_strategy(settings: Settings, buffer: BarBuffer, state_path: Path, clock: Clock) -> LiveStrategy:
    cfg = settings.live
    common = dict(state_path=state_path, clock=clock, band=cfg.band, gross_cap=cfg.gross_cap,
                  stale_after=pd.Timedelta(hours=cfg.stale_after_hours), lock_confirmations=cfg.lock_confirmations,
                  min_trade_usd=cfg.min_trade_usd, max_equity_jump=cfg.max_equity_jump,
                  bar_grace=pd.Timedelta(minutes=cfg.bar_grace_minutes), escalate=cfg.escalate,
                  ladder_bps=cfg.ladder_bps, cross_bps=cfg.cross_bps)
    if cfg.strategy == "rxm":
        return CompetitionStrategy(buffer, mode=cfg.mode, **common)
    if cfg.strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {cfg.strategy!r}; registered: {sorted(STRATEGIES)}")
    strategy = STRATEGIES[cfg.strategy](**cfg.params)
    return LiveStrategy(strategy, buffer, signal_prefix=cfg.mode or strategy.name,
                        limit_offset_bps=settings.execution.limit_offset_bps, **common)


def _universe(settings: Settings) -> list[str]:
    if settings.live.universe:
        return list(settings.live.universe)
    return list(RXM_UNIVERSE) if settings.live.strategy == "rxm" else list(settings.backtest.symbols)


def _guard_config(settings: Settings, strategy: LiveStrategy) -> GuardConfig:
    base: dict[str, Any] = dict(
        initial_equity_usd=float(strategy.state.get("start_equity") or settings.backtest.initial_cash),
        kill_file=settings.live.kill_file,
        limit_fee=settings.fees.spot_maker, market_fee=settings.fees.spot_taker, short_fee=settings.fees.short_open,
        lockin_return=strategy.lockin_return, lockin_scale=strategy.lockin_scale,
    )
    known = {f.name for f in fields(GuardConfig)}
    unknown = set(settings.live.guard) - known
    if unknown:
        raise ValueError(f"unknown live.guard settings: {sorted(unknown)}")
    return GuardConfig(**{**base, **settings.live.guard})


_ops_write_failures: dict[str, int] = {}


def _write_json(path: Path, payload: dict[str, Any]) -> bool:
    """Atomically replace an ops file. Never raises: a status or snapshot file must not stop trading.

    On Windows another process (indexer, antivirus, a reader) can briefly hold the file, so the
    replace is retried a few times before giving up for this loop.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    error: OSError | None = None
    for attempt in range(3):
        try:
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
            os.replace(tmp, path)
            _ops_write_failures.pop(str(path), None)
            return True
        except OSError as exc:
            error = exc
            time.sleep(0.05 * (attempt + 1))
    count = _ops_write_failures[str(path)] = _ops_write_failures.get(str(path), 0) + 1
    if count == 1 or count % 100 == 0:
        log.warning("could not write %s (%d times in a row): %s", path, count, error)
    return False


def _append_line(path: Path, record: dict[str, Any]) -> None:
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        log.warning("could not append to %s: %s", path, exc)


class LiveRunner:
    """Builds the live stack from Settings and runs it until stopped."""

    def __init__(self, settings: Settings, *, mode: RunMode = "dry-run", port: Any = None,
                 fetch: FetchFn | None = None, clock: Clock | None = None) -> None:
        self.settings = settings
        self.mode = mode
        cfg = settings.live
        self.clock = clock or RealClock()
        self.state_dir = Path(cfg.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        execution = settings.execution.model_copy(update={"dry_run": mode == "dry-run", "live_mode": mode == "live"})
        if port is None:
            if mode == "simulate":
                raise ValueError("simulate mode needs a simulated port (e.g. ReplayExchangePort)")
            from tradebot.exchange import RoostooClient, RoostooExchangePort
            client = RoostooClient(settings=settings.exchange)
            if not (client.api_key and client.api_secret):
                raise RuntimeError("ROOSTOO_API_KEY / ROOSTOO_API_SECRET are not set: copy .env.example to .env")
            port = RoostooExchangePort(client)
        if mode == "live" and not getattr(port, "is_live", False):
            raise ValueError("live mode needs the real exchange port")

        self.buffer = BarBuffer(_universe(settings), fetch or binance_public_fetch(cfg.klines_url),
                                window_days=cfg.buffer_days)
        self.strategy = build_live_strategy(settings, self.buffer, self.state_dir / "strategy_state.json", self.clock)
        self.guard = Guard(_guard_config(settings, self.strategy), now=lambda: self.clock.now().astimezone(timezone.utc))

        self.throttled = ThrottledPort(port, self.clock, max_per_minute=cfg.max_http_per_minute)
        self.guarded = GuardedPort(self.throttled, self.guard, lambda: self.last_snapshot, lambda: self._price_times)
        engine_port = RepegPort(self.guarded, self.strategy.offset * 1e4, cfg.repeg_max_move,
                                reference=lambda: (self.last_snapshot or {}).get("prices")) if cfg.repeg \
            else self.guarded
        self.engine = Engine(engine_port, config=execution, clock=self.clock,
                             state_path=self.state_dir / "engine_state.db",
                             audit_path=self.state_dir / "engine_audit.jsonl")
        self.engine.runner.plan_check = self.plan_guard
        self.poll_seconds = float(execution.strategy_poll_interval_seconds)

        self.last_snapshot: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_guard: str | None = None
        self.failures = 0
        self.iterations = 0
        self.paused = False
        self._stop = threading.Event()
        self._last_heartbeat: float | None = None
        self._last_history_bar: pd.Timestamp | None = None
        self._price_times: dict[str, Any] | None = None

    # ------------------------------------------------------------------ control
    def stop(self, *_: Any) -> None:
        if not self._stop.is_set():
            log.warning("stop requested: finishing after the current loop")
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is not None:
                signal.signal(sig, self.stop)

    def _sleep(self, seconds: float) -> None:
        if not isinstance(self.clock, RealClock):
            self.clock.sleep(seconds)           # simulated time: advance instantly
            return
        self._stop.wait(max(0.0, seconds))      # real time: wake up early on stop()

    # ------------------------------------------------------------------ loop
    def _strategy_call(self, snapshot: dict[str, Any]):
        self.last_snapshot = snapshot
        now = self.clock.now()
        record = {"timestamp": now.isoformat(), "snapshot": snapshot}
        _write_json(self.state_dir / "latest_snapshot.json", record)
        bar = pd.Timestamp(now).floor("15min")
        if bar != self._last_history_bar:   # performance history: one snapshot per 15m bar
            self._last_history_bar = bar
            _append_line(self.state_dir / "snapshots.jsonl", record)
        if not self.strategy.state.get("start_equity") and float(snapshot.get("equity_usd") or 0) > 0:
            # First loop of an event: anchor the guard (drawdown, lock-in) to the real starting equity
            self.guard.config.initial_equity_usd = self.guard.peak_equity = float(snapshot["equity_usd"])
        # Roostoo's ticker is read fresh in every snapshot (candle staleness is the bridge's job)
        self._price_times = {coin: now for coin in (snapshot.get("prices") or {})}
        report = self.guard.poll(snapshot, target_weights=self.strategy.scaled_weights() or None,
                                 price_times=self._price_times, bot_locked=self.strategy.locked)
        summary = report.summary()
        if summary != self.last_guard:
            (log.info if report.status is Status.OK else log.warning)(summary)
            self.last_guard = summary
        # Poll results are alerts; individual orders are checked (and blocked) by GuardedPort.
        return self.strategy(snapshot)

    def plan_guard(self, plan: dict[str, Any], actual: dict[str, Any], equity: float) -> list[str]:
        """Portfolio-level guard checks on the whole plan before anything is sent ([] = allowed)."""
        prices = actual.get("prices") or {}
        orders = plan_orders(plan, prices)
        if not orders:
            return []
        # The plan already nets out resting orders, so judge it from the book they will make: a long
        # top-up while the short opens still rest is not a +0.64 net book (guard net_max 0.60)
        resting = resting_orders(actual.get("pending_orders"), prices)
        base = project_snapshot(actual, resting, self.guard.config) if resting else actual
        report = self.guard.pre_trade(base, orders, price_times=self._price_times)
        blocks = [r for r in report.blocks if r.name not in ORDER_CHECKS or r.name == "kill_switch"]
        if blocks:
            log.error("guard rejected the plan: %s", "; ".join(f"{r.name}: {r.message}" for r in blocks))
        return [f"guard:{r.name}" for r in blocks]

    def run_once(self) -> dict[str, Any] | None:
        """One loop. Returns the engine result, or None when paused by the kill switch. Never raises."""
        self.iterations += 1
        if Path(self.settings.live.kill_file).exists():
            if not self.paused:
                log.warning("kill switch %s present: trading paused (delete the file to resume)",
                            self.settings.live.kill_file)
            self.paused = True
            return None
        if self.paused:
            log.warning("kill switch removed: trading resumed")
            self.paused = False
        try:
            result = self.engine.run_once(self._strategy_call)
        except SnapshotRejected as exc:
            self._failed(f"snapshot rejected: {exc}", trace=False)
            return None
        except Exception as exc:  # the loop must survive anything: log it and back off
            self._failed(f"{type(exc).__name__}: {exc}", trace=True)
            return None
        if self.failures:
            log.warning("recovered after %d failed loop(s)", self.failures)
        self.failures, self.last_error, self.last_result = 0, None, result
        if result.get("status") != "DUPLICATE":
            ops = [(o["kind"], o["symbol"], round(o["amount_usd"], 2), o["status"]) for o in result.get("operations", [])]
            log.info("%s %s ops=%s reasons=%s", result.get("signal_id"), result.get("status"), ops, result.get("reasons"))
        return result

    def _failed(self, message: str, trace: bool) -> None:
        self.failures += 1
        self.last_error = message
        if trace:
            log.error("loop %d failed (%d in a row): %s\n%s", self.iterations, self.failures, message,
                      traceback.format_exc())
        else:
            log.warning("loop %d skipped (%d in a row): %s", self.iterations, self.failures, message)

    def next_delay(self) -> float:
        if self.failures == 0:
            return self.poll_seconds
        return float(min(self.poll_seconds * 2 ** (self.failures - 1), self.settings.live.max_backoff_seconds))

    def run(self, max_iterations: int | None = None) -> int:
        self._install_signal_handlers()
        log.info("starting: strategy=%s mode=%s run=%s dry_run=%s live_mode=%s poll=%ss state=%s",
                 self.settings.live.strategy, self.strategy.mode, self.mode, self.engine.config.dry_run,
                 self.engine.config.live_mode, self.poll_seconds, self.state_dir)
        try:
            while not self._stop.is_set() and (max_iterations is None or self.iterations < max_iterations):
                started = time.monotonic()
                self.run_once()
                delay = self.next_delay()
                try:   # bookkeeping must never stop trading
                    self._heartbeat()
                    self._write_status("backoff" if self.failures else "paused" if self.paused else "running", delay)
                except Exception:
                    log.exception("status/heartbeat update failed (trading continues)")
                self._sleep(max(1.0, delay - (time.monotonic() - started)) if isinstance(self.clock, RealClock)
                            else delay)
        finally:
            self.engine.close()
            self._write_status("stopped", None)
            log.info("stopped after %d loops", self.iterations)
        return 0

    # ------------------------------------------------------------------ reporting
    def _equity(self) -> float | None:
        return float(self.last_snapshot["equity_usd"]) if self.last_snapshot else None

    def _write_status(self, state: str, next_delay: float | None) -> None:
        start = self.strategy.state.get("start_equity")
        equity = self._equity()
        _write_json(self.state_dir / "status.json", {
            "updated": self.clock.now().isoformat(),
            "state": state,
            "run_mode": self.mode,
            "strategy": self.settings.live.strategy,
            "mode": self.strategy.mode,
            "loops": self.iterations,
            "consecutive_failures": self.failures,
            "last_error": self.last_error,
            "last_signal": (self.last_result or {}).get("signal_id"),
            "last_status": (self.last_result or {}).get("status"),
            "weights_bar": self.strategy.weights_bar,
            "equity_usd": equity,
            "return_since_start": (equity / float(start) - 1) if equity and start else None,
            "locked": self.strategy.locked,
            "guard": self.last_guard,
            "orders_blocked_by_guard": len(self.guarded.blocked),
            "http_requests_last_minute": self.throttled.requests_last_minute(),
            "next_loop_in_seconds": next_delay,
        })

    def _heartbeat(self) -> None:
        now = self.clock.monotonic()
        if self._last_heartbeat is not None and now - self._last_heartbeat < self.settings.live.heartbeat_minutes * 60:
            return
        self._last_heartbeat = now
        snap = self.last_snapshot or {}
        start = self.strategy.state.get("start_equity")
        equity = self._equity()
        ret = f"{equity / float(start) - 1:+.2%}" if equity and start else "n/a"
        log.info("heartbeat: equity %s (%s since start) longs=%d shorts=%d last signal=%s locked=%s "
                 "failures=%d http/min=%d", f"{equity:,.2f}" if equity else "n/a", ret,
                 len(snap.get("longs") or {}), len(snap.get("shorts") or {}),
                 (self.last_result or {}).get("signal_id"), self.strategy.locked, self.failures,
                 self.throttled.requests_last_minute())
