from copy import deepcopy
import json
import sqlite3

import pandas as pd
import pytest

from tests.test_shared_portfolio import setup_account
from tradebot.engine.state.ownership import OwnershipLedger
from tradebot.engine.state.ownership_migration import migration_plan, migrate, main
from tradebot.engine.state.portfolio import MM, PortfolioStore


def test_shared_and_opposite_legs_do_not_net_away_ownership():
    db = sqlite3.connect(":memory:")
    ledger = OwnershipLedger(db)
    with ledger.atomic():
        for pair in ("HEMI-POL", "CAKE-POL", "POL-TAO"):
            ledger.allocate("pairs", pair, 1000.)
        for n, (pair, side) in enumerate((("HEMI-POL", "short"), ("CAKE-POL", "short"), ("POL-TAO", "long"))):
            ledger.apply_fill(str(n), strategy="pairs", pair_id=pair, leg="POL", symbol="POL", side=side,
                              action="open", quantity=10., price=10., fee=1.)
    report = ledger.mark({"POL": 12.})
    assert report["exposure"]["POL"] == dict(net_quantity=-10., net_notional=-120., gross_notional=360.)
    with pytest.raises(ValueError, match="another pair"):
        ledger.apply_fill("too-much", strategy="pairs", pair_id="HEMI-POL", leg="POL", symbol="POL", side="short",
                          action="close", quantity=11., price=12.)
    args = dict(strategy="pairs", pair_id="HEMI-POL", leg="POL", symbol="POL", side="short", action="close",
                quantity=10., price=12., fee=1.)
    assert ledger.apply_fill("cover", **args)
    assert not ledger.apply_fill("cover", **args)
    with pytest.raises(ValueError, match="different contents"):
        ledger.apply_fill("cover", **dict(args, price=13.))
    assert len(ledger.positions()) == 2
    assert ledger.mark({"POL": 12.})["equity"] == pytest.approx(2976.)


def test_reservations_and_fills_rollback_together():
    db = sqlite3.connect(":memory:")
    ledger = OwnershipLedger(db)
    with ledger.atomic():
        ledger.allocate("pairs", "A-B", 100.)
    with pytest.raises(ValueError, match="insufficient"):
        with ledger.atomic():
            ledger.reserve("first", "pairs", "A-B", 80., {})
            ledger.reserve("second", "pairs", "A-B", 30., {})
    assert db.execute("SELECT COUNT(*) FROM owner_reservations").fetchone()[0] == 0
    with pytest.raises(RuntimeError):
        with ledger.atomic():
            ledger.apply_fill("fill", strategy="pairs", pair_id="A-B", leg="A", symbol="A", side="long",
                              action="open", quantity=1., price=50.)
            raise RuntimeError("crash before decision commit")
    assert not ledger.positions() and ledger.account("pairs", "A-B")["cash"] == 100.


def test_explicit_legacy_migration_preserves_owners_orders_and_restart(tmp_path):
    account, sim, clock = setup_account(tmp_path)
    account.scoped("rxm").open_short("PEPE", 1000.)
    account.sync()
    account.scoped("rxm").place_order("BONK", "BUY", 1., price=90.)
    state = deepcopy(account.state)
    state["version"] = 1
    state.pop("short_collateral")
    state.pop("short_settlement_adjustment")
    old = PortfolioStore(tmp_path / "legacy.db", clock)
    old.state = state
    old.save("legacy_fixture")
    with pytest.raises(ValueError, match="entry-value"):
        migration_plan(old.state, {"PEPE/USD": 1000.})
    plan = migration_plan(old.state, {"PEPE/USD": 1000.}, {"PEPE/USD": 1000.})
    original_orders = deepcopy(state["orders"])
    assert migrate(old, plan)
    assert not migrate(old, plan)
    assert old.state["orders"] == original_orders
    assert old.state["version"] == 2
    assert old.ownership.positions("rxm")[0]["quantity"] == 10.
    assert old.db.execute("SELECT COUNT(*) FROM owner_reservations").fetchone()[0] == 1
    positions = old.ownership.positions()
    old.close()
    reopened = PortfolioStore(tmp_path / "legacy.db", clock)
    assert reopened.ownership.positions() == positions
    assert reopened.state["orders"] == original_orders
    changed = deepcopy(plan)
    changed["accounts"][0]["cash"] += 1
    with pytest.raises(ValueError, match="different migration"):
        migrate(reopened, changed)
    reopened.close()
    account.store.close()


def move_price(sim, clock, price):
    ts = pd.Timestamp(clock.now())
    sim.bars["PEPE/USD"].loc[ts, ["open", "high", "low", "close"]] = price
    clock.advance(1)


def test_shared_account_unequal_short_entries_and_owner_scoped_exit(tmp_path):
    account, sim, clock = setup_account(tmp_path)
    account.scoped("rxm").open_short("PEPE", 1000.)  # 10 @100
    account.sync()
    for pair in ("FIRST-PEPE", "SECOND-PEPE"):
        account.allocate_owner("pairs", pair, 1000.)
    for n, (pair, price) in enumerate((("FIRST-PEPE", 100.), ("SECOND-PEPE", 150.))):
        move_price(sim, clock, price)
        result = sim.open_short("PEPE", price*2)
        account.record_owned_fill(f"entry{n}", strategy="pairs", pair_id=pair, leg="B", symbol="PEPE",
            side="short", action="open", quantity=2., price=price, fee=result["OpenFee"])
        account.sync()
    response = sim.close_short("PEPE", close_qty=2.)
    account.record_owned_fill("cover1", strategy="pairs", pair_id="FIRST-PEPE", leg="B", symbol="PEPE",
        side="short", action="close", quantity=2., price=150., fee=response["CloseFee"],
        venue_realized_pnl=response["RealizedPNL"])
    account.sync()
    assert not account.owned_positions("pairs", "FIRST-PEPE")
    assert account.owned_positions("pairs", "SECOND-PEPE")[0]["quantity"] == 2.
    assert account.scoped("pairs", "SECOND-PEPE").get_short_positions()["Positions"][0]["ShortQty"] == 2.
    assert account.scoped("pairs", "FIRST-PEPE").get_short_positions()["Positions"] == []
    assert account.scoped("rxm").get_short_positions()["Positions"][0]["ShortQty"] == 10.
    assert account.store.ownership.mark({"PEPE": 150.})["equity"] == pytest.approx(sim.equity())
    assert not account._reconcile_cash()
    # RXM's percentage applies only to RXM, never the two remaining pair units.
    result = account.scoped("rxm").close_short("PEPE", close_pct=100)
    assert result["ClosedQty"] == 10.
    account.sync()
    assert sim.shorts["PEPE/USD"]["ShortQty"] == 2.
    assert account.store.ownership.mark({"PEPE": 150.})["equity"] == pytest.approx(sim.equity())
    assert not account._reconcile_cash()
    response = sim.close_short("PEPE", close_qty=2.)
    account.record_owned_fill("cover2", strategy="pairs", pair_id="SECOND-PEPE", leg="B", symbol="PEPE",
        side="short", action="close", quantity=2., price=150., fee=response["CloseFee"],
        venue_realized_pnl=response["RealizedPNL"])
    account.sync()
    assert account.state["short_settlement_adjustment"] == pytest.approx(0., abs=1e-8)
    assert not account.store.ownership.positions()
    assert account.store.ownership.mark({})["equity"] == pytest.approx(sim.equity())
    account.store.close()


def test_migration_command_keeps_source_unchanged(tmp_path, capsys):
    account, _, clock = setup_account(tmp_path)
    source = tmp_path / "v1.db"
    store = PortfolioStore(source, clock)
    store.state = deepcopy(account.state)
    store.state["version"] = 1
    store.state.pop("short_collateral")
    store.state.pop("short_settlement_adjustment")
    store.save("legacy")
    store.close()
    before = source.read_bytes()
    assert main([str(source)]) == 0
    plan = tmp_path / "plan.json"
    plan.write_text(capsys.readouterr().out)
    output = tmp_path / "v2.db"
    assert main([str(source), "--apply-plan", str(plan), "--output", str(output)]) == 0
    assert source.read_bytes() == before
    migrated = PortfolioStore(output, clock)
    assert migrated.state["version"] == 2 and migrated.ownership.accounts()
    migrated.close()
    with pytest.raises(FileExistsError):
        main([str(source), "--apply-plan", str(plan), "--output", str(output)])
    account.store.close()
