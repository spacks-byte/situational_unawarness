"""`tradebot account preflight` / `explain`: read-only by construction, findings reported not acted on."""
from datetime import timedelta
import json

import pytest

from tests.test_shared_portfolio import prepare, setup_account
from tradebot.core.config import Settings
from tradebot.core.locking import AccountLock, lock_holder
from tradebot.engine.state.portfolio import AccountBlocked
from tradebot.live.preflight import ReadOnlyPort, explain, preflight

MUTATING = {"place_order", "cancel_order", "open_short", "close_short"}


def _settings(tmp_path):
    settings = Settings.load("config/market-making.yaml")
    settings.live.state_dir = str(tmp_path / "shared")
    settings.market_making.rxm_state_dir = str(tmp_path / "legacy")
    return settings


def _venue(tmp_path):
    _, sim, clock = setup_account(tmp_path / "scratch")   # a ReplayExchangePort with priced coins
    sim.calls.clear()
    return sim, clock


def test_fresh_account_is_ready_and_nothing_is_written(tmp_path):
    settings = _settings(tmp_path)
    sim, clock = _venue(tmp_path)
    report = preflight(settings, sim, lock_path=tmp_path / "lock", now=clock.now())
    assert report["ready"], report["blockers"]
    assert report["allocation"]["mm_required_usd"] == pytest.approx(90_000)
    assert report["allocation"]["rxm_capital_usd"] == pytest.approx(10_000)
    assert not MUTATING & set(sim.calls)
    assert not (tmp_path / "shared").exists() and not (tmp_path / "lock").exists()


def test_shortfall_unknown_orders_and_holdings_are_reported_not_fixed(tmp_path):
    settings = _settings(tmp_path)
    sim, clock = _venue(tmp_path)
    sim.free_usd, sim.coins = 20_000, {"PEPE": 800}            # most of the account sits in a coin
    sim.place_order("BONK", "BUY", 10, price=90)                # resting, in no journal
    sim.calls.clear()
    report = preflight(settings, sim, lock_path=tmp_path / "lock", now=clock.now())
    assert not report["ready"]
    assert any("MM funding shortfall" in b for b in report["blockers"])
    assert any("resting orders are in neither" in b for b in report["blockers"])
    assert [o["pair"] for o in report["unknown_orders"]] == ["BONK/USD"]
    assert report["positions"]["PEPE"]["owner_at_takeover"] == "rxm"
    assert not MUTATING & set(sim.calls)
    assert sim.coins == {"PEPE": 800} and len(sim.orders) == 1  # nothing sold, nothing cancelled


def test_held_lock_and_active_legacy_runner_block_the_takeover(tmp_path):
    settings = _settings(tmp_path)
    sim, clock = _venue(tmp_path)
    lock = AccountLock(tmp_path / "lock", state_dir=tmp_path / "other")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "status.json").write_text(json.dumps({"timestamp": (clock.now()-timedelta(seconds=20)).isoformat()}))
    try:
        report = preflight(settings, sim, lock_path=tmp_path / "lock", now=clock.now())
        assert report["lock"]["holder"]["state_dir"] == str((tmp_path / "other").resolve())
        assert any("account lock is held" in b for b in report["blockers"])
        assert any("standalone RXM runner looks active" in b for b in report["blockers"])
    finally:
        lock.close()
    assert lock_holder(tmp_path / "lock") is None                # the probe never kept the lock


def test_existing_ledger_with_changed_allocation_needs_migration(tmp_path):
    settings = _settings(tmp_path)
    a, sim, clock = setup_account(tmp_path / "shared-src")
    a.sync()
    a.store.close()
    settings.live.state_dir = str(tmp_path / "shared-src")
    settings.market_making.capital.mm_fraction = 0.6
    report = preflight(settings, sim, lock_path=tmp_path / "lock", now=clock.now())
    assert report["existing_ledger"]["capital_fraction"] == 0.9
    assert any("explicit migration" in b for b in report["blockers"])


def test_read_only_port_refuses_every_mutation(tmp_path):
    sim, _ = _venue(tmp_path)
    port = ReadOnlyPort(sim)
    for name in MUTATING:
        with pytest.raises(PermissionError):
            getattr(port, name)
    assert port.get_balance()["Success"]


def test_explain_shows_restrictions_and_unresolved_intents_from_the_db_only(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    def timeout(*args, **kwargs):
        raise TimeoutError("response lost")
    sim.place_order = timeout
    o, = prepare(a, clock)
    with pytest.raises(AccountBlocked):
        a.submit(o)
    a.sync()
    out = explain(tmp_path)
    assert "coin:PEPE" in out["restrictions"]
    assert [x["status"] for x in out["awaiting_venue"]] == ["SUBMITTING"]
    assert any(e["kind"] == "submission_uncertain" for e in out["recent_events"])
    assert explain(tmp_path / "nowhere") == {"state_dir": str(tmp_path / "nowhere"), "initialized": False}
    assert not (tmp_path / "nowhere").exists()
