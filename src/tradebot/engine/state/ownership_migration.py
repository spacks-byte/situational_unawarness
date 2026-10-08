"""Explicit version-1 ownership import and MM/RXM compatibility projection.

Legacy spot cost includes acquisition fees; preserve that basis rather than
inventing a historical fill price. Short collateral must come from a reconciled
snapshot, because legacy short_basis is not necessarily posted collateral.
"""
from copy import deepcopy
import hashlib
import math

from tradebot.engine.state.ownership import encoded

MM = "mm-10m-fluctuation"
LEGACY = {MM, "rxm"}


def legacy_records(state, *, other_cash=0., other_collateral=0.):
    collateral = state.get("short_collateral", {})
    missing = [p for p, q in state["short_quantity"].items() if q and p not in collateral]
    if missing:
        raise ValueError(f"explicit short collateral snapshot required for {missing}")
    accounts, positions, reservations = [], [], []
    for coin, book in state["mm"].items():
        accounts.append(dict(strategy=MM, pair_id=coin, capital=book["capital"], cash=book["cash"],
                             fees=book["fees"], slippage=0., realized_gross=None))
        if book["quantity"]:
            positions.append(dict(strategy=MM, pair_id=coin, leg="spot", symbol=coin, side="long",
                                  quantity=book["quantity"], basis=book["cost"], collateral=0.))
    cash = state["expected_cash_assets"] - sum(b["cash"] for b in state["mm"].values())
    cash -= sum(collateral.values()) + other_cash + other_collateral + state.get("short_settlement_adjustment", 0.)
    accounts.append(dict(strategy="rxm", pair_id="portfolio", capital=state["rxm_capital"], cash=cash,
                         fees=state["rxm_fees"], slippage=0., realized_gross=None))
    for coin, qty in state["rxm_quantity"].items():
        if qty:
            positions.append(dict(strategy="rxm", pair_id="portfolio", leg=f"spot:{coin}", symbol=coin,
                                  side="long", quantity=qty, basis=state["rxm_cost"].get(coin, 0.), collateral=0.))
    for pair, qty in state["short_quantity"].items():
        if qty:
            coin = pair.removesuffix("/USD")
            positions.append(dict(strategy="rxm", pair_id="portfolio", leg=f"short:{coin}", symbol=coin,
                side="short", quantity=qty, basis=state["short_basis"].get(pair, 0.), collateral=collateral[pair]))
    for order in state["orders"].values():
        if order["strategy"] not in LEGACY or order["status"] not in {"READY", "SUBMITTING", "PENDING", "CANCELING"}:
            continue
        cash = 0.
        if order["side"] in {"BUY", "SHORT_OPEN"}:
            cash = order["quantity"]*order["price"]
            # Short open fees have already left cash once an accepted limit is pending.
            cash *= 1 + (.0005 if order["side"] == "BUY" else (0 if order.get("open_fee") else .001))
        reservations.append(dict(intent_id=order["intent_id"], strategy=order["strategy"],
            pair_id=order["coin"] if order["strategy"] == MM else "portfolio", cash=cash, payload=deepcopy(order)))
    for record in accounts + positions:
        if any(isinstance(v, (float, int)) and not math.isfinite(v) for v in record.values()):
            raise ValueError("nonfinite legacy ownership")
    if any(p["quantity"] < 0 or p["collateral"] < 0 for p in positions):
        raise ValueError("negative legacy position; repair explicitly before migrating")
    return dict(accounts=accounts, positions=positions, reservations=reservations)


def replace_legacy_records(ledger, state):
    """Legacy algorithms mutate their views; publish only their owners atomically.

    Arbitrary strategy/pair records are never rebuilt or attributed to RXM.
    Call inside the transaction saving the legacy state/order journal.
    """
    other_accounts = [a for a in ledger.accounts() if a["strategy"] not in LEGACY]
    other_positions = [p for p in ledger.positions() if p["strategy"] not in LEGACY]
    records = legacy_records(state, other_cash=sum(a["cash"] for a in other_accounts),
                             other_collateral=sum(p["collateral"] for p in other_positions))
    for table in ("owner_accounts", "owner_positions", "owner_reservations"):
        ledger.db.execute(f"DELETE FROM {table} WHERE strategy IN (?,?)", (MM, "rxm"))
    for a in records["accounts"]:
        ledger.db.execute("INSERT INTO owner_accounts VALUES(?,?,?,?,?,?,?)", tuple(a.values()))
    for p in records["positions"]:
        ledger.db.execute("INSERT INTO owner_positions VALUES(?,?,?,?,?,?,?,?)", tuple(p.values()))
    for r in records["reservations"]:
        ledger.db.execute("INSERT INTO owner_reservations VALUES(?,?,?,?,?)",
                          (r["intent_id"], r["strategy"], r["pair_id"], r["cash"], encoded(r["payload"])))
    return records


def migration_plan(state, short_collateral=None, short_entry_value=None):
    if state.get("version") != 1:
        raise ValueError("migration expects a version-1 account snapshot")
    migrated = deepcopy(state)
    migrated["short_collateral"] = dict(short_collateral or {})
    entries = dict(short_entry_value or {})
    if any(q and p not in entries for p, q in state["short_quantity"].items()):
        raise ValueError("explicit venue short entry-value snapshot required")
    active = {p for p, q in state["short_quantity"].items() if q}
    for label, values in (("collateral", migrated["short_collateral"]), ("entry value", entries)):
        if set(values) != active or any(not math.isfinite(v) or v <= 0 for v in values.values()):
            raise ValueError(f"{label} snapshot must have positive finite values for exactly the owned shorts")
    migrated["short_settlement_adjustment"] = sum(state["short_basis"].values()) - sum(entries.values())
    records = legacy_records(migrated)
    return dict(source_sha256=hashlib.sha256(encoded(state).encode()).hexdigest(),
                short_collateral=migrated["short_collateral"], short_entry_value=entries,
                short_settlement_adjustment=migrated["short_settlement_adjustment"], **records,
                basis_note="Legacy spot basis includes acquisition fees; original short basis is preserved.")


def migrate(store, plan):
    """Apply a reviewed plan to this local store. No exchange operations."""
    fingerprint = hashlib.sha256(encoded(plan).encode()).hexdigest()
    prior = store.db.execute("SELECT fingerprint FROM owner_migrations WHERE migration_id='legacy-v1'").fetchone()
    if prior:
        if prior[0] != fingerprint:
            raise ValueError("different migration has already been applied")
        return False
    if plan != migration_plan(store.state, plan["short_collateral"], plan["short_entry_value"]):
        raise ValueError("migration plan is stale or altered")
    if store.ownership.accounts():
        raise ValueError("cannot import over existing owners")
    old = deepcopy(store.state)
    try:
        with store.ownership.atomic():
            store.state = dict(store.state, version=2, short_collateral=plan["short_collateral"],
                               short_settlement_adjustment=plan["short_settlement_adjustment"])
            store.db.execute("INSERT INTO owner_migrations VALUES('legacy-v1',?,?)", (fingerprint, encoded(plan)))
            store.save("ownership_migration", {"source_sha256": plan["source_sha256"]})
    except BaseException:
        store.state = old
        raise
    return True


def main(argv=None):
    """Plan read-only, or build a separate migrated database from a reviewed plan."""
    import argparse
    import json
    from pathlib import Path
    import sqlite3

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="existing portfolio.db (always opened read-only)")
    parser.add_argument("--shorts", type=Path, help="reconciled get_short_positions JSON, required for open shorts")
    parser.add_argument("--apply-plan", type=Path, help="exact previously reviewed plan JSON")
    parser.add_argument("--output", type=Path, help="new database path; existing paths are refused")
    args = parser.parse_args(argv)
    if bool(args.apply_plan) != bool(args.output):
        parser.error("--apply-plan and --output are required together")
    with sqlite3.connect(args.source.resolve().as_uri()+"?mode=ro", uri=True) as source:
        state = json.loads(source.execute("SELECT payload FROM portfolio WHERE id=1").fetchone()[0])
        if not args.apply_plan:
            rows = json.loads(args.shorts.read_text())["Positions"] if args.shorts else []
            actual = {p["Pair"]: float(p["ShortQty"]) for p in rows}
            expected = {p: q for p, q in state["short_quantity"].items() if q}
            if set(actual) != set(expected) or any(not math.isclose(actual[p], q, rel_tol=1e-10) for p, q in expected.items()):
                raise ValueError("short snapshot does not match the source ownership; reconcile before migration")
            plan = migration_plan(state, {p["Pair"]: float(p["Collateral"]) for p in rows},
                                  {p["Pair"]: float(p["ShortQty"])*float(p["EntryPrice"]) for p in rows})
            print(json.dumps(plan, indent=2, allow_nan=False))
            return 0
        plan = json.loads(args.apply_plan.read_text())
        if plan != migration_plan(state, plan["short_collateral"], plan["short_entry_value"]):
            raise ValueError("source changed or plan was edited; generate and review a new plan")
        # Exclusive creation prevents overwriting either the source or an existing deployment.
        with args.output.open("xb"):
            pass
        try:
            with sqlite3.connect(args.output) as copy:
                source.backup(copy)
            from tradebot.core.clock import RealClock
            from tradebot.engine.state.portfolio import PortfolioStore
            store = PortfolioStore(args.output, RealClock())
            try:
                migrate(store, plan)
            finally:
                store.close()
        except BaseException:
            args.output.unlink()
            raise
    print(f"Created migrated copy: {args.output}; source unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
