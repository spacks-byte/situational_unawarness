"""One account engine hosting independently selected strategy runtimes."""
from __future__ import annotations

import json
import math
import logging
from pathlib import Path
import signal
import sqlite3
import threading

import pandas as pd

from tradebot.core.clock import RealClock
from tradebot.engine import Engine
from tradebot.engine.execution.quotes import QuoteExecutor
from tradebot.engine.state.portfolio import AccountCoordinator, AccountLock, PortfolioStore, MM
from tradebot.data.engine import MarketDataEngine
from tradebot.data.store import MarketDataStore
from tradebot.data.market import binance_public_fetch
from tradebot.live.runner import LiveRunner, _universe, _write_json, account_lock_path
from tradebot.live.throttle import ThrottledPort
from tradebot.strategy.library.mm_fluctuation import MMFluctuation
from tradebot.strategy.registry import STRATEGIES

log = logging.getLogger(__name__)


class AccountReadDegraded(RuntimeError):
    """A venue read failed this loop; the ledger and its restrictions are intact."""


class QuoteBridge:
    output_kind = "quotes"

    def __init__(self, account, fetch, clock, config):
        self.account, self.fetch, self.clock, self.config = account, fetch, clock, config
        self.strategy = MMFluctuation(config)

    def __call__(self, snapshot):
        now = self.clock.now()
        end = pd.Timestamp(now).floor("s")
        data = {}
        for coin in self.config.allocations:
            state = self.account.state["features"].get(coin, {})
            cursor = state.get("cursor")
            # Cold start: the warmup window ends at the decision second, up to max_data_delay
            # before the wall clock; rows older than the warmup are ignored by the strategy.
            start = pd.Timestamp(cursor+1, unit="s", tz="UTC") if cursor is not None else end-pd.Timedelta(
                seconds=self.config.warmup_seconds + math.ceil(self.config.max_data_delay_seconds) + 1)
            data[coin] = self.fetch(coin, start, end)
        return self.strategy.generate(data, now=self.decision_time(now, data), books=self.account.state["mm"],
                                      features=self.account.state["features"], rules=self.account.rules)

    def decision_time(self, now, data):
        """Decide at the latest complete second + 1 s when live candles arrive late.

        The policy needs the candle of second t-1 at decision second t. Live one-second
        candles arrive 2-3 s after their open time, so deciding at the wall clock would
        almost never find it and MM would post empty batches. Decide instead at the
        earliest "last complete candle + 1 s" over the coins whose data is at most
        `max_data_delay_seconds` behind. Data older than that stays stale and does not
        quote. With fresh data (always in replay) `now` is returned unchanged.
        """
        second = pd.Timestamp(now).floor("s")
        max_delay = pd.Timedelta(seconds=self.config.max_data_delay_seconds)
        ready = []
        for coin, frame in data.items():
            cursor = self.account.state["features"].get(coin, {}).get("cursor")
            last = frame.index[-1] if len(frame) else (pd.Timestamp(cursor, unit="s", tz="UTC") if cursor is not None else None)
            if last is not None and second - (last + pd.Timedelta(seconds=1)) <= max_delay:
                ready.append(last + pd.Timedelta(seconds=1))
        decision = min(ready, default=second)
        return now if decision >= second else decision.to_pydatetime()


def legacy_ids(directory):
    path = Path(directory) / "engine_state.db"
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return [r[0] for r in db.execute("SELECT response_id FROM intents WHERE response_id IS NOT NULL")]


def selected_strategies(settings):
    names = list(settings.live.strategies or [settings.live.strategy])
    if not names or len(names) != len(set(names)) or any(n not in {"rxm", MM} for n in names):
        raise ValueError("account strategies must be unique selections of rxm and mm-10m-fluctuation")
    return names


class AccountRunner:
    def __init__(self, settings, *, mode="dry-run", port=None, fetch=None, mm_fetch=None, clock=None):
        if settings.fees.spot_maker != 0.0005:
            raise ValueError("MM preset requires the 5 bps spot maker fee")
        self.strategy_names = selected_strategies(settings)
        self.settings, self.mode = settings, mode
        self.clock = clock or RealClock()
        self.state_dir = Path(settings.live.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_dir.resolve() == Path(settings.market_making.rxm_state_dir).resolve():
            raise ValueError("shared state must be separate from standalone RXM state")
        if port is None:
            if mode == "simulate":
                raise ValueError("simulate requires an exchange adapter")
            from tradebot.exchange import RoostooClient, RoostooExchangePort
            client = RoostooClient(settings=settings.exchange)      # no network until first use
            if not client.api_key or not client.api_secret:
                raise ValueError("Roostoo credentials missing")
            port = RoostooExchangePort(client)
        # One process per account on this host, keyed to the account (not the working directory)
        lockpath = account_lock_path(settings, port) if mode != "simulate" else self.state_dir / "account.lock"
        self.lock = AccountLock(lockpath, state_dir=self.state_dir)
        self.store = None
        self.market_data = None
        try:
            if mode == "live" and not getattr(port, "is_live", False):
                raise ValueError("live requires real exchange transport")
            self.throttled = ThrottledPort(port, self.clock, max_per_minute=settings.live.max_http_per_minute)
            self.store = PortfolioStore(self.state_dir / "portfolio.db", self.clock)
            self.account = AccountCoordinator(self.throttled, self.store, settings.market_making, settings.fees,
                                              self.clock, dry_run=mode == "dry-run",
                                              import_order_ids=legacy_ids(settings.market_making.rxm_state_dir), paused=self.risk_paused)
            self.runtimes = {}
            self.engines = {}
            self.bridges = {}
            self.quotes = QuoteExecutor(self.account, self.clock)
            # One strategy-neutral producer owns connections, repair and retention.
            # Strategy adapters only receive read views of completed candles.
            windows = {}
            sources = {}
            if MM in self.strategy_names:
                windows.update({(coin, "1s"): settings.market_making.warmup_seconds + 600
                                for coin in settings.market_making.allocations})
                sources["1s"] = mm_fetch or binance_public_fetch(settings.live.klines_url, interval="1s")
            if "rxm" in self.strategy_names:
                rxm_settings = settings.model_copy(deep=True)
                rxm_settings.live.strategy = "rxm"
                windows.update({(coin, "15m"): settings.live.buffer_days*86400 for coin in _universe(rxm_settings)})
                sources["15m"] = fetch or binance_public_fetch(settings.live.klines_url)
            streams = [settings.live.market_stream_url, *settings.live.market_stream_fallback_urls]
            live_data = isinstance(self.clock, RealClock)
            store = None
            if live_data and settings.live.market_store_enabled:
                store = MarketDataStore(settings.live.market_store_dir,
                                        retention_days=settings.live.market_store_retention_days,
                                        min_free_bytes=int(settings.live.market_store_min_free_gb * 1e9))
            self.market_data = MarketDataEngine(self.clock, sources, windows,
                stream_url=streams if live_data else None, store=store)
            self.market_data.start()
            for name in self.strategy_names:
                strategy_class = STRATEGIES[name]
                if strategy_class.output_kind == "weights":
                    self._import_rxm_state()
                    weight_settings = settings.model_copy(deep=True)
                    weight_settings.live.strategy = name
                    weight_settings.live.strategies = []
                    weight_settings.live.state_dir = str(self.state_dir / name)
                    weight_settings.backtest.initial_cash = self.account.state["rxm_capital"]
                    runtime = LiveRunner(weight_settings, mode=mode, port=self.account.scoped(name),
                                         fetch=self.market_data.fetch("15m"), clock=self.clock, shared_account=True)
                    self.runtimes[name] = runtime
                    self.engines[name] = runtime.engine
                elif strategy_class.output_kind == "quotes":
                    self.bridges[name] = QuoteBridge(self.account, self.market_data.fetch("1s"),
                                                      self.clock, settings.market_making)
                    self.engines[name] = Engine(self.account.scoped(name), config=settings.execution.model_copy(
                        update={"dry_run": mode == "dry-run", "live_mode": mode == "live"}), clock=self.clock,
                        quote_executor=self.quotes, state_path=self.state_dir / name / "engine.db",
                        audit_path=self.state_dir / name / "audit.jsonl")
                else:
                    raise ValueError(f"unsupported strategy output: {strategy_class.output_kind}")

        except Exception:
            if self.market_data:
                self.market_data.close()
            for engine in getattr(self, "engines", {}).values():
                engine.close()
            if self.store:
                self.store.close()
            self.lock.close()
            raise
        self.iterations, self.failures = 0, 0
        self.poll_seconds = float(settings.execution.strategy_poll_interval_seconds)
        self.last_error = None
        self.strategy_failures = {name: 0 for name in self.strategy_names}
        self.retry_after = {name: 0.0 for name in self.strategy_names}
        self._stop = threading.Event()

    def _import_rxm_state(self):
        if self.account.state["rxm_transferred"]:
            return
        source = Path(self.settings.market_making.rxm_state_dir) / "strategy_state.json"
        target = self.state_dir / "rxm" / "strategy_state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            state = json.loads(source.read_text())
            # Preserve return and lock-in history across the explicit capital transfer.
            factor = self.account.state["rxm_capital"] / self.account.state["initial_equity"]
            for key in ("start_equity", "peak_equity", "lock_equity"):
                if state.get(key) is not None:
                    state[key] *= factor
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(state, allow_nan=False))
            temp.replace(target)
        self.account.state["rxm_transferred"] = True
        self.store.save("rxm_capital_transfer")

    def risk_paused(self, strategy):
        return (Path(self.settings.live.kill_file).exists() or (self.state_dir / "PAUSE").exists()
                or (self.state_dir / ("PAUSE_MM" if strategy == MM else "PAUSE_RXM")).exists()
                or strategy == MM and (self.state_dir / "STOP_MM").exists())

    def run_once(self):
        self.iterations += 1
        try:
            if not self.account.sync():
                # Venue read failed: no strategy step on stale balances this loop (new orders are refused
                # anyway). Market data keeps running; the loop backs off and re-reads.
                raise AccountReadDegraded(f"account read failed: {self.account.read_error}")
            if Path(self.settings.live.kill_file).exists() or (self.state_dir / "PAUSE").exists():
                result = {"status": "PAUSED"}
            else:
                strategy_results = {}
                # RXM first: its rebalance must not queue behind a 600 s MM refresh for request budget
                for name in sorted(self.strategy_names, key=lambda n: n != "rxm"):
                    if self.risk_paused(name):
                        if name == MM and (self.state_dir / "STOP_MM").exists():
                            self.quotes.cancel_owned()
                            strategy_results[name] = {"status": "STOPPED"}
                        else:
                            strategy_results[name] = {"status": "PAUSED"}
                        continue
                    if self.clock.monotonic() < self.retry_after[name]:
                        strategy_results[name] = {"status": "BACKOFF"}
                        continue
                    self.throttled.tag = name
                    try:
                        if name in self.bridges:
                            strategy_results[name] = self.engines[name].run_once(self.bridges[name])
                        else:
                            runtime = self.runtimes[name]
                            strategy_results[name] = runtime.run_once() or {"status": "PAUSED"}
                            if runtime.failures:
                                raise RuntimeError(runtime.last_error)
                        self.strategy_failures[name] = 0
                        self.retry_after[name] = 0.0
                    except Exception as exc:
                        self.strategy_failures[name] += 1
                        delay = min(self.poll_seconds*2**min(self.strategy_failures[name]-1, 8),
                                    self.settings.live.max_backoff_seconds)
                        self.retry_after[name] = self.clock.monotonic()+delay
                        strategy_results[name] = {"status": "ERROR", "reason": str(exc)}
                        log.exception("strategy %s failed", name)
                        if self.account.blocked:
                            raise
                    finally:
                        self.throttled.tag = None
                if self.account.dirty:          # only re-read the venue if something was sent/cancelled
                    self.account.sync()
                status = "PARTIAL" if any(r.get("status") in {"ERROR", "BACKOFF"} for r in strategy_results.values()) else "OK"
                result = {"status": status, "strategies": strategy_results}
            self.failures, self.last_error = 0, None
        except AccountReadDegraded as exc:
            self.failures += 1
            self.last_error = str(exc)
            log.warning("%s", exc)
            result = {"status": "DEGRADED", "reason": self.last_error}
        except Exception as exc:
            self.failures += 1
            self.last_error = str(exc)
            log.exception("shared account loop paused")
            result = {"status": "BLOCKED", "reason": self.last_error}
        try:
            account_report = self.account.report()
        except Exception as exc:
            account_report = {"report_unavailable": str(exc), "blocked": self.last_error}
        _write_json(self.state_dir / "status.json", {
            "timestamp": self.clock.now().isoformat(), "mode": self.mode, "result": result,
            "active_strategies": self.strategy_names, "iterations": self.iterations, "failures": self.failures,
            "http_peak": self.throttled.peak, "http_by_strategy": dict(self.throttled.by_tag),
            "http_last_minute": self.throttled.requests_last_minute(), "account": account_report,
            "market_data": self.market_data.status(),
        })
        return result

    def stop(self, *_):
        self._stop.set()

    def close(self):
        self.market_data.close()
        for engine in self.engines.values():
            engine.close()
        self.store.close()
        self.lock.close()

    def run(self, max_iterations=None):
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)
        try:
            while not self._stop.is_set() and (max_iterations is None or self.iterations < max_iterations):
                started = self.clock.monotonic()
                self.run_once()
                delay = min(self.poll_seconds*2**min(self.failures, 8), self.settings.live.max_backoff_seconds)
                wait = max(1.0, delay-(self.clock.monotonic()-started))
                if isinstance(self.clock, RealClock):
                    self._stop.wait(wait)
                else:
                    self.clock.sleep(wait)
        finally:
            self.close()
        return 0
