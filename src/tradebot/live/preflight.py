"""
Read-only checks before the shared account coordinator (MM + RXM) takes over a Roostoo account.

    tradebot --config config/market-making.yaml account preflight   # venue + local state, no orders
    tradebot --config config/market-making.yaml account explain     # local portfolio.db only, no network

Nothing here mutates anything: the venue is read through a port wrapper that refuses every order,
cancel and short call; portfolio.db and the legacy RXM journal are opened with SQLite `mode=ro`; the
account lock is probed without being taken. Findings are reported, never acted on: positions are not
sold, unknown orders are not adopted or cancelled, and portfolio.db is never deleted
(docs/MARKET_MAKING.md, "Migration").
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from tradebot.core.locking import lock_holder
from tradebot.engine.state.portfolio import MM, order_rows, total_quantity, wallet_of
from tradebot.engine.state.snapshot import normalize_exchange_snapshot, remaining_qty

MUTATING = {"place_order", "cancel_order", "open_short", "close_short"}


class ReadOnlyPort:
    """Pass reads through; refuse anything that could change the account."""

    def __init__(self, port: Any) -> None:
        self._port = port

    def __getattr__(self, name: str) -> Any:
        if name in MUTATING:
            raise PermissionError(f"preflight is read-only: {name} refused")
        return getattr(self._port, name)


def _read_only_db(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def stored_account(state_dir: str | Path) -> dict[str, Any] | None:
    """The coordinator's persisted state and journal, opened read-only (None if never started)."""
    db = _read_only_db(Path(state_dir) / "portfolio.db")
    if db is None:
        return None
    try:
        row = db.execute("SELECT payload FROM portfolio WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else None
        completed = [json.loads(r[0]) for r in db.execute("SELECT payload FROM completed_orders")]
        events = [{"id": i, "timestamp": t, "kind": k, "payload": json.loads(p)}
                  for i, t, k, p in db.execute("SELECT id, timestamp, kind, payload FROM events ORDER BY id DESC LIMIT 200")]
    finally:
        db.close()
    return {"state": state, "completed": completed, "events": events}


def legacy_order_ids(directory: str | Path) -> set[str]:
    db = _read_only_db(Path(directory) / "engine_state.db")
    if db is None:
        return set()
    try:
        return {str(r[0]) for r in db.execute("SELECT response_id FROM intents WHERE response_id IS NOT NULL")}
    finally:
        db.close()


def legacy_activity(directory: str | Path, now: datetime, active_within_seconds: float) -> dict[str, Any] | None:
    """The standalone RXM runner rewrites status.json every loop; a recent one means it is running."""
    path = Path(directory) / "status.json"
    if not path.exists():
        return None
    try:
        stamp = json.loads(path.read_text(encoding="utf-8")).get("timestamp")
        updated = datetime.fromisoformat(stamp) if stamp else None
    except (OSError, ValueError):
        updated = None
    if updated is None:
        updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    age = (now - updated).total_seconds()
    return {"status_file": str(path), "updated": updated.isoformat(), "age_seconds": round(age, 1),
            "active": age < active_within_seconds}


def preflight(settings: Any, port: Any, *, lock_path: Path, now: datetime | None = None) -> dict[str, Any]:
    """Collect what the coordinator would find at startup. `blockers` stop a takeover; `warnings` don't."""
    now = now or datetime.now(timezone.utc)
    mm = settings.market_making
    venue = ReadOnlyPort(port)
    blockers: list[str] = []
    warnings: list[str] = []
    report: dict[str, Any] = {"checked_at": now.isoformat(), "read_only": True}

    holder = lock_holder(lock_path)
    report["lock"] = {"path": str(lock_path), "holder": holder}
    if holder is not None:
        blockers.append(f"account lock is held ({holder}): another coordinator or RXM runner is using this account")
    legacy = legacy_activity(mm.rxm_state_dir, now, active_within_seconds=3 * settings.execution.strategy_poll_interval_seconds + 120)
    report["legacy_runner"] = legacy
    if legacy and legacy["active"]:
        blockers.append(f"standalone RXM runner looks active ({legacy['status_file']} updated "
                        f"{legacy['age_seconds']:.0f}s ago): stop it before the coordinator starts")

    balance = venue.get_balance()
    pending = order_rows(venue.list_open_orders())
    shorts = venue.get_short_positions()
    tickers = venue.get_ticker()
    if balance.get("Success") is False or not (balance.get("SpotWallet") or balance.get("Wallet")):
        raise RuntimeError(f"balance read failed: {balance.get('ErrMsg', balance)}")
    snapshot = normalize_exchange_snapshot(balance, shorts, tickers, pending)
    wallet = wallet_of(balance)
    prices = snapshot["prices"]

    positions = {}
    for coin in sorted(c for c in wallet if c != "USD"):
        quantity = total_quantity(wallet, coin)
        if quantity > 0:
            price = prices.get(coin)
            positions[coin] = {"quantity": quantity, "value_usd": quantity * price if price else None,
                               "owner_at_takeover": "rxm"}
    report["positions"] = positions
    report["shorts"] = {p["Pair"]: {"quantity": float(p["ShortQty"]), "entry_price": float(p.get("EntryPrice", 0))}
                        for p in (shorts or {}).get("Positions", []) if float(p.get("ShortQty", 0)) > 0}
    if snapshot["unpriced"]:
        blockers.append(f"held coins without a Roostoo price: {sorted(snapshot['unpriced'])}")
    if snapshot["lock_unexplained_usd"] > 0.01:
        blockers.append(f"USD Lock not explained by resting orders or short collateral: "
                        f"${snapshot['lock_unexplained_usd']:.2f}")

    stored = stored_account(settings.live.state_dir)
    state = (stored or {}).get("state")
    known = legacy_order_ids(mm.rxm_state_dir)
    if state:
        known |= {str(o.get("order_id")) for o in state.get("orders", {}).values() if o.get("order_id")}
        known |= {str(o.get("order_id")) for o in stored["completed"] if o.get("order_id")}
    unknown = [{"order_id": str(r["OrderID"]), "pair": r.get("Pair"), "side": r.get("Side"),
                "price": r.get("Price"), "remaining": remaining_qty(r)} for r in pending if str(r["OrderID"]) not in known]
    report["pending_orders"] = len(pending)
    report["unknown_orders"] = unknown
    if unknown:
        blockers.append(f"{len(unknown)} resting orders are in neither the RXM journal nor portfolio.db: "
                        "the coordinator never adopts or cancels them; resolve them by hand first")

    equity = snapshot["equity_usd"]
    pending_fees = sum(remaining_qty(r) * float(r["Price"]) * settings.fees.spot_maker for r in pending if r.get("Side") == "BUY")
    free = float(wallet["USD"]["Free"]) - pending_fees if "USD" in wallet else 0.0
    report["equity_usd"] = equity
    report["unreserved_cash_usd"] = free

    if state is None:
        required = equity * mm.capital.mm_fraction
        report["allocation"] = {"mm_fraction": mm.capital.mm_fraction, "mm_required_usd": required,
                                "rxm_capital_usd": equity - required, "shortfall_usd": max(required - free, 0.0)}
        if not math.isfinite(equity) or equity <= 0:
            blockers.append("account equity is not positive")
        elif required > free:
            blockers.append(f"MM funding shortfall: needs ${required:,.2f} unreserved cash, ${free:,.2f} available. "
                            "Nothing is sold automatically: free cash by hand (or lower mm_fraction) first")
    else:
        report["existing_ledger"] = {"run_mode": state.get("run_mode"), "capital_fraction": state.get("capital_fraction"),
                                     "rxm_capital": state.get("rxm_capital"), "restrictions": state.get("restrictions", {}),
                                     "active_orders": len(state.get("orders", {}))}
        if state.get("capital_fraction") != mm.capital.mm_fraction:
            blockers.append(f"portfolio.db was allocated at mm_fraction={state.get('capital_fraction')}, "
                            f"config says {mm.capital.mm_fraction}: an explicit migration is required")
        if state.get("restrictions"):
            warnings.append(f"existing restrictions will persist: {sorted(state['restrictions'])}")
    if report["shorts"]:
        warnings.append(f"open shorts {sorted(report['shorts'])} are attributed to RXM at takeover")
    if positions:
        warnings.append(f"spot holdings {sorted(positions)} are attributed to RXM at takeover")
    report["blockers"], report["warnings"] = blockers, warnings
    report["ready"] = not blockers
    return report


def explain(state_dir: str | Path, events: int = 20) -> dict[str, Any]:
    """Why the coordinator is refusing orders right now, from portfolio.db alone (no network)."""
    stored = stored_account(state_dir)
    if stored is None or stored["state"] is None:
        return {"state_dir": str(state_dir), "initialized": False}
    state = stored["state"]
    interesting = {"submission_uncertain", "short_uncertain", "cancel_uncertain", "cancel_unconfirmed", "cancel_pending",
                   "reconciled", "read_failed", "cash_rounding_adjustment", "pending_row_full_filled_quantity",
                   "submission_recovered", "submission_not_executed", "restricted_skip", "quote_restricted"}
    unresolved = [{k: o.get(k) for k in ("intent_id", "strategy", "coin", "side", "status", "order_id",
                                         "quantity", "price", "submitted_at", "cancel_requested_at")}
                  for o in state.get("orders", {}).values() if o.get("status") in {"SUBMITTING", "CANCELING"}]
    return {
        "state_dir": str(state_dir), "initialized": True, "run_mode": state.get("run_mode"),
        "restrictions": state.get("restrictions", {}),
        "adjustments": state.get("adjustments", {}),
        "quote_stats": state.get("quote_stats", {}),
        "awaiting_venue": unresolved,
        "active_orders": {MM: sum(o["strategy"] == MM for o in state.get("orders", {}).values()),
                          "rxm": sum(o["strategy"] == "rxm" for o in state.get("orders", {}).values())},
        "recent_events": [e for e in stored["events"] if e["kind"] in interesting][:events],
    }


def render(report: dict[str, Any]) -> str:
    """Human summary of a preflight report."""
    lines = [f"Preflight {'READY' if report['ready'] else 'NOT READY'} (read-only, {report['checked_at']})",
             f"  equity ${report['equity_usd']:,.2f}, unreserved cash ${report['unreserved_cash_usd']:,.2f}, "
             f"{report['pending_orders']} resting orders"]
    if "allocation" in report:
        a = report["allocation"]
        lines.append(f"  allocation: MM ${a['mm_required_usd']:,.2f} ({a['mm_fraction']:.0%}), RXM ${a['rxm_capital_usd']:,.2f}, "
                     f"shortfall ${a['shortfall_usd']:,.2f}")
    for name, items in (("BLOCKER", report["blockers"]), ("warning", report["warnings"])):
        lines += [f"  {name}: {item}" for item in items]
    for o in report["unknown_orders"]:
        lines.append(f"    unknown order {o['order_id']} {o['side']} {o['pair']} @ {o['price']} remaining {o['remaining']}")
    return "\n".join(lines)
