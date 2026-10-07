from datetime import UTC, datetime
from pathlib import Path
import json

import pandas as pd
import pytest

from tests.test_strategy_bridge import _frame_fetch, _universe
from tests.test_mm_fluctuation import frames
from tradebot.core.clock import SimClock
from tradebot.core.config import Settings
from tradebot.exchange.replay import ReplayExchangePort
from tradebot.live.account import AccountRunner
from tradebot.engine.state.portfolio import MM


def shared(tmp_path, dry=False, strategies=None):
    clock = SimClock(datetime(2026, 2, 25, 0, 16, tzinfo=UTC))
    rxm = _universe(n=10, days=70, end="2026-03-01")
    _, mm = frames(pd.Timestamp(clock.now()), future=8000)
    bars = {**rxm, **mm}
    sim = ReplayExchangePort(bars, clock, intervals={c: "1s" for c in mm})
    settings = Settings.load("config/market-making.yaml")
    settings.live.state_dir = str(tmp_path / "shared")
    settings.live.kill_file = str(tmp_path / "KILL")
    settings.live.universe = list(rxm)
    if strategies is not None:
        settings.live.strategies = strategies
    settings.market_making.rxm_state_dir = str(tmp_path / "legacy")
    settings.market_making.account_lock = str(tmp_path / "account.lock")
    runner = AccountRunner(settings, mode="dry-run" if dry else "simulate", port=sim, clock=clock,
                          fetch=_frame_fetch(rxm), mm_fetch=_frame_fetch(mm))
    return runner, sim, clock


def test_shared_coordinator_rxm_and_mm_through_actual_engine(tmp_path):
    runner, sim, clock = shared(tmp_path)
    try:
        for _ in range(40):
            result = runner.run_once()
            assert result["status"] == "OK", result
            clock.advance(60)
        assert sim.history
        assert any(r["Side"] == "SHORT_OPEN" for r in sim.history)  # RXM policy remains intact
        assert runner.throttled.peak <= 25
        runner.account.sync()
        report = runner.account.report()
        assert sum(b["equity"] for b in report["mm"].values())+report["rxm"]["equity_usd"] == pytest.approx(sim.equity(), abs=.02)
        assert all(b["quantity"] >= 0 for b in report["mm"].values())
        assert report["rxm_capital"] == 10_000
        assert all(o["side"] in {"BUY", "SELL"} for o in runner.store.completed(MM))
    finally:
        runner.close()


def test_engine_keeps_quotes_fixed_midinterval_and_restart_preserves_anchor(tmp_path):
    runner, sim, clock = shared(tmp_path)
    result = runner.run_once()
    assert result["status"] == "OK", result
    features = json.loads(json.dumps(runner.account.state["features"]))
    batch = runner.account.state["batch"]
    clock.advance(30)
    assert runner.run_once()["strategies"][MM]["status"] == "HOLD"
    assert runner.account.state["batch"] == batch
    settings, fetch, mm_fetch = runner.settings, runner.runtimes["rxm"].buffer.fetch, runner.bridges[MM].fetch
    runner.close()
    restart = AccountRunner(settings, mode="simulate", port=sim, clock=clock, fetch=fetch, mm_fetch=mm_fetch)
    try:
        result = restart.run_once()
        assert result["status"] == "OK", result
        assert result["strategies"][MM]["status"] == "HOLD"
        assert restart.account.state["features"] == features
        assert restart.account.state["rxm_capital"] == 10_000
    finally:
        restart.close()


def test_shared_dry_run_sends_zero_mutations(tmp_path):
    runner, sim, clock = shared(tmp_path, dry=True)
    try:
        result = runner.run_once()
        assert result["status"] == "OK", result
        assert not {"place_order", "open_short", "close_short", "cancel_order"} & set(sim.calls)
    finally:
        runner.close()


def test_pause_and_scoped_mm_stop(tmp_path):
    runner, sim, clock = shared(tmp_path)
    try:
        assert runner.run_once()["status"] == "OK"
        (runner.state_dir / "PAUSE").touch()
        count = len(sim.history)
        assert runner.run_once()["status"] == "PAUSED"
        assert len(sim.history) == count
        (runner.state_dir / "PAUSE").unlink()
        (runner.state_dir / "STOP_MM").touch()
        (runner.state_dir / "PAUSE_RXM").touch()
        before = {o["order_id"] for o in runner.account.active("rxm")}
        assert runner.run_once()["strategies"][MM]["status"] == "STOPPED"
        assert not runner.account.active(MM)
        assert {o["order_id"] for o in runner.account.active("rxm")} == before
    finally:
        runner.close()


def test_bootstrap_transfer_preserves_rxm_return_and_lock_state(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "strategy_state.json").write_text(json.dumps({"mode": "comp", "start_equity": 95_000,
                                                            "locked": True, "lock_equity": 101_000}))
    runner, sim, clock = shared(tmp_path)
    try:
        assert runner.runtimes["rxm"].strategy.state.get("start_equity") == pytest.approx(9_500)
        assert runner.runtimes["rxm"].strategy.state.get("lock_equity") == pytest.approx(10_100)
        assert runner.runtimes["rxm"].strategy.locked
        assert (10_000/9_500-1) == pytest.approx(100_000/95_000-1)
    finally:
        runner.close()


def test_failed_balance_read_reports_degraded_then_recovers(tmp_path):
    runner, sim, clock = shared(tmp_path)
    try:
        original = sim.get_balance
        sim.get_balance = lambda: {"Success": False, "ErrMsg": "temporary exchange failure"}
        sent = len(sim.history)
        assert runner.run_once()["status"] == "DEGRADED"
        assert runner.failures == 1
        assert len(sim.history) == sent             # no strategy step on a stale wallet
        status = json.loads((runner.state_dir / "status.json").read_text())
        assert status["result"]["status"] == "DEGRADED"
        assert "reads" in status["account"]["restrictions"]
        assert status["account"]["blocked"] is None  # the ledger itself is intact
        sim.get_balance = original
        assert runner.run_once()["status"] == "OK"
        assert runner.failures == 0
        assert "reads" not in runner.account.report()["restrictions"]
    finally:
        runner.close()


@pytest.mark.parametrize("name", [MM, "rxm"])
def test_each_strategy_can_run_alone_with_its_own_budget(tmp_path, name):
    runner, sim, clock = shared(tmp_path, strategies=[name])
    try:
        assert set(runner.engines) == {name}
        if name == MM:
            assert not runner.runtimes  # no RXM instance or 15m feature requests
        else:
            assert not runner.bridges  # no MM instance or one-second feature requests
        for _ in range(12):
            result = runner.run_once()
            assert result["status"] == "OK", result
            assert set(result["strategies"]) == {name}
            clock.advance(60)
        assert runner.account.state["rxm_capital"] == 10_000
        assert sum(b["capital"] for b in runner.account.state["mm"].values()) == 90_000
        other = "rxm" if name == MM else MM
        assert not runner.account.active(other)
        assert not runner.store.completed(other)
        if name == MM:
            assert not sim.shorts
            assert runner.account.state["rxm_quantity"] == {}
        else:
            assert not runner.account.state["features"]
            assert all(b["quantity"] == 0 and b["cash"] == b["capital"] for b in runner.account.state["mm"].values())
    finally:
        runner.close()


def test_strategy_data_failure_does_not_prevent_other_strategy_running(tmp_path):
    runner, sim, clock = shared(tmp_path)
    try:
        def unavailable(*args):
            raise ConnectionError("MM candle source unavailable")
        runner.bridges[MM].fetch = unavailable
        result = runner.run_once()
        assert result["status"] == "PARTIAL"
        assert result["strategies"][MM]["status"] == "ERROR"
        assert result["strategies"]["rxm"]["status"] == "EXECUTED"
        assert runner.account.active("rxm") or runner.store.completed("rxm")
        assert not runner.account.active(MM)
    finally:
        runner.close()
