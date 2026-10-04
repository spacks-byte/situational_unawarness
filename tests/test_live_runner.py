"""The unattended runner: recovery, backoff, kill switch, ops files, restarts and the guard.
Runs on the replay exchange with a simulated clock; nothing touches the network."""
import json
from datetime import UTC, datetime

import pytest

from tests.test_strategy_bridge import _frame_fetch, _universe
from tradebot.core.clock import SimClock
from tradebot.core.config import Settings
from tradebot.exchange.replay import ReplayExchangePort
from tradebot.live.runner import LiveRunner

START = datetime(2026, 2, 25, 0, 16, tzinfo=UTC)


class FlakyPort:
    """Wraps a port; the first `failures` balance reads raise like a dropped connection."""

    def __init__(self, port, failures):
        self._port, self.remaining = port, failures

    def __getattr__(self, name):
        return getattr(self._port, name)

    def get_balance(self):
        if self.remaining > 0:
            self.remaining -= 1
            raise ConnectionError("connection reset by peer")
        return self._port.get_balance()


def _settings(tmp_path, **live):
    settings = Settings.load("config/competition.yaml")
    values = {"state_dir": str(tmp_path / "state"), "kill_file": str(tmp_path / "KILL"), **live}
    return settings.model_copy(update={"live": settings.live.model_copy(update=values)})


def _runner(tmp_path, port=None, clock=None, **live):
    frames = _universe(n=10, days=70, end="2026-03-01")
    clock = clock or SimClock(START)
    sim = ReplayExchangePort(frames, clock, initial_usd=100_000)
    settings = _settings(tmp_path, universe=list(frames), **live)
    runner = LiveRunner(settings, mode="simulate", port=port(sim) if port else sim,
                        fetch=_frame_fetch(frames), clock=clock)
    return runner, sim, clock


def test_failed_loops_back_off_exponentially_then_recover(tmp_path):
    runner, sim, clock = _runner(tmp_path, port=lambda sim: FlakyPort(sim, failures=3))
    delays = []
    for _ in range(4):
        runner.run_once()
        delays.append(runner.next_delay())
    assert delays == [60.0, 120.0, 240.0, 60.0]           # three failures, then success resets the delay
    assert runner.failures == 0 and runner.last_error is None
    assert runner.last_result["status"] == "EXECUTED"
    assert sim.fills or sim.orders                         # it traded once the exchange came back


def test_backoff_is_capped(tmp_path):
    runner, _, _ = _runner(tmp_path, port=lambda sim: FlakyPort(sim, failures=50), max_backoff_seconds=300)
    for _ in range(8):
        runner.run_once()
    assert runner.failures == 8 and runner.next_delay() == 300.0


def test_kill_switch_pauses_everything_and_resumes(tmp_path):
    runner, sim, _ = _runner(tmp_path)
    kill = tmp_path / "KILL"
    kill.touch()
    assert runner.run_once() is None and runner.paused
    assert not sim.history and sim.calls.get("get_balance", 0) == 0     # no reads, no orders while paused
    kill.unlink()
    assert runner.run_once()["status"] == "EXECUTED" and not runner.paused


def test_run_writes_status_snapshot_and_history_and_stops_cleanly(tmp_path):
    runner, _, clock = _runner(tmp_path)
    assert runner.run(max_iterations=20) == 0               # 20 loops of 60 s on the simulated clock
    state = tmp_path / "state"
    status = json.loads((state / "status.json").read_text())
    assert status["state"] == "stopped" and status["loops"] == 20 and status["consecutive_failures"] == 0
    assert status["last_signal"].startswith("comp-20260225T0000")
    assert 90_000 < status["equity_usd"] < 110_000
    snap = json.loads((state / "latest_snapshot.json").read_text())
    assert "timestamp" in snap and snap["snapshot"]["equity_usd"] > 0
    history = (state / "snapshots.jsonl").read_text().splitlines()
    assert 1 <= len(history) <= 2                           # one line per 15m bar (20 minutes ran)
    assert (state / "engine_state.db").exists() and (state / "strategy_state.json").exists()


def test_stop_request_ends_the_loop(tmp_path):
    runner, _, _ = _runner(tmp_path)
    runner.stop()
    assert runner.run() == 0 and runner.iterations == 0


def test_restart_keeps_start_equity_and_does_not_resend_orders(tmp_path):
    clock = SimClock(START)
    first, sim, _ = _runner(tmp_path, clock=clock)
    first.run(max_iterations=2)
    orders_before = len(sim.history)
    start_equity = first.strategy.state.get("start_equity")

    second = LiveRunner(first.settings, mode="simulate", port=sim, clock=clock,
                        fetch=first.buffer.fetch)                       # a new process, same state dir
    result = second.run_once()
    assert second.strategy.state.get("start_equity") == start_equity
    assert result["status"] == "DUPLICATE"                  # same day's signal: already executed
    assert len(sim.history) == orders_before


def test_default_guard_caps_would_block_rxm_rebalances(tmp_path):
    """Why config/competition.yaml raises the guard caps: RXM holds up to ~0.55 of the book in one coin."""
    runner, sim, _ = _runner(tmp_path, guard={"max_symbol_weight": 0.10, "warn_symbol_weight": 0.05})
    result = runner.run_once()                               # the loop itself must survive a block
    assert runner.failures == 0 and not sim.history
    assert result["status"] == "REJECTED_RISK" and "guard:symbol_weight" in result["reasons"]


def test_any_backtested_strategy_runs_live_through_the_bridge(tmp_path):
    runner, sim, _ = _runner(tmp_path, strategy="ma_crossover", mode="ma",
                             params={"fast": 8, "slow": 96})   # long-only: stays inside the guard caps
    results = [runner.run_once() for _ in range(3)]
    assert runner.failures == 0
    assert results[0]["signal_id"].startswith("ma-")        # recomputed every bar (no rebalance_h)
    assert results[0]["status"] == "EXECUTED" and sim.history


def test_live_mode_refuses_a_simulated_port(tmp_path):
    frames = _universe(n=4, days=50, end="2026-03-01")
    sim = ReplayExchangePort(frames, SimClock(START))
    with pytest.raises(ValueError, match="live mode needs the real exchange port"):
        LiveRunner(_settings(tmp_path), mode="live", port=sim, clock=SimClock(START))


def test_a_locked_ops_file_never_stops_the_bot(tmp_path, monkeypatch):
    """Windows: an indexer or reader holding status.json made os.replace fail and killed the loop."""
    import os

    import tradebot.live.runner as runner_module

    real_replace = os.replace

    def flaky_replace(src, dst):
        if str(dst).endswith(("status.json", "latest_snapshot.json")):
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(runner_module.os, "replace", flaky_replace)
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: None)
    runner, sim, _ = _runner(tmp_path)
    assert runner.run(max_iterations=5) == 0
    assert runner.iterations == 5 and runner.failures == 0 and sim.history   # it kept trading


def test_portfolio_checks_judge_the_whole_plan_not_each_order(tmp_path):
    """A normal rebalance (buys, then shorts) must pass, though net looks too long after the buys alone."""
    runner, sim, _ = _runner(tmp_path)
    result = runner.run_once()
    assert result["status"] == "EXECUTED" and not runner.guarded.blocked
    kinds = [op["kind"] for op in result["operations"]]
    assert kinds.count("open_long") == 3 and kinds.count("open_short") == 3


def test_a_plan_that_breaks_a_portfolio_limit_is_rejected_before_any_order(tmp_path):
    runner, sim, _ = _runner(tmp_path, guard={"net_max": 0.10, "net_warn_max": 0.05})   # comp is net +0.3
    result = runner.run_once()
    assert result["status"] == "REJECTED_RISK" and "guard:net_exposure" in result["reasons"]
    assert not sim.history and runner.failures == 0           # nothing sent; the loop is healthy


def test_a_fat_finger_order_is_blocked_at_send_time(tmp_path):
    from tradebot.live.guard import Guard, GuardConfig
    from tradebot.live.runner import GuardedPort

    class Port:
        is_live = False
        sent = []

        def place_order(self, *a, **k):
            self.sent.append(a)
            return {"Success": True}

    snap = {"cash_usd": 1e5, "longs": {}, "shorts": {}, "equity_usd": 1e5, "prices": {"AAA": 100.0},
            "pending_orders": []}
    port = GuardedPort(Port(), Guard(GuardConfig(kill_file=str(tmp_path / "KILL")), now=lambda: START), lambda: snap)
    assert port.place_order("AAA", "BUY", 1.0, price=100.01)["Success"]
    out = port.place_order("AAA", "BUY", 1.0, price=110.0)                     # 10% through the market
    assert out["Success"] is False and "price_band" in out["ErrMsg"] and len(Port.sent) == 1
