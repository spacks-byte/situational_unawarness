from __future__ import annotations

import math
from typing import Any

from tradebot.core.symbols import to_coin, to_pair


SHORT_OPEN_FEE = 0.001   # Roostoo locks collateral + 0.1% fee while a limit short open rests


class SnapshotReadError(RuntimeError):
    """An account read came back as an error (Success: false) or without its payload.

    Treating such a response as "empty" would value the account without that part (e.g. no short
    positions = equity minus the whole short book) and the strategy would trade on it.
    """


def _remaining_qty(order: dict[str, Any]) -> float:
    """Unfilled quantity of a resting order. A row whose FilledQuantity is not strictly between 0 and
    Quantity is taken as unfilled (the API docs show PENDING rows with FilledQuantity == Quantity)."""
    qty = _number(order.get("Quantity"))
    filled = _number(order.get("FilledQuantity"))
    return qty - filled if 0.0 < filled < qty else qty


def pending_reserved_usd(orders: list[dict[str, Any]], short_fee: float = SHORT_OPEN_FEE) -> float:
    """USD that resting orders hold back: buys (remaining qty x price) and short opens (collateral + fee)."""
    total = 0.0
    for order in orders:
        if str(order.get("Status", "PENDING")).upper() != "PENDING":
            continue
        side = str(order.get("Side", "")).upper()
        qty, price = _number(order.get("Quantity")), _number(order.get("Price"))
        if side == "BUY":
            total += _remaining_qty(order) * price
        elif side == "SHORT_OPEN":
            collateral = _number(order.get("Collateral")) or qty * price
            total += collateral * (_remaining_qty(order) / qty if qty > 0 else 1.0) * (1 + short_fee)
    return total


def normalize_exchange_snapshot(
    balance: dict[str, Any] | None,
    short_positions: dict[str, Any] | None,
    tickers: dict[str, Any] | None,
    pending_orders: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize live or simulated exchange responses into engine state.

    USD cash = Free + the part of Lock that belongs to resting orders. Roostoo keeps the collateral of
    OPEN shorts locked too ("the remaining collateral stays locked until the position is fully
    closed"), and that collateral is already counted once as the short position, so adding the whole
    Lock double-counts it (equity read $134k on a $100k account on 2026-10-04 and tripped the +6%
    lock-in). The split is inferred, so it is right whichever way Roostoo reports it:
        P = USD the resting orders hold (from the order rows)        C = open-short collateral
        in_lock_collateral = C if Lock - P >= C - tolerance else 0    (a position's collateral is
                                                                       locked whole or not at all)
        cash_usd = Free + max(Lock - in_lock_collateral, P)           (P also if Lock omits orders)
    Lock that neither explains is kept as cash and reported as lock_unexplained_usd.
    """

    # Roostoo returns the wallet under "SpotWallet"; older/mock responses use "Wallet"
    wallet = (balance or {}).get("SpotWallet") or (balance or {}).get("Wallet") or {}
    ticker_data = (tickers or {}).get("Data", tickers or {})
    cash_usd = 0.0
    cash_free_usd = 0.0
    usd_lock = 0.0
    longs: dict[str, float] = {}
    unpriced: dict[str, float] = {}
    long_value = 0.0
    prices = {
        to_coin(pair): _number(entry.get("LastPrice"))
        for pair, entry in ticker_data.items()
        if isinstance(pair, str) and "/" in pair and isinstance(entry, dict) and _number(entry.get("LastPrice")) > 0
    }

    for raw_symbol, raw_entry in wallet.items():
        if not isinstance(raw_entry, dict):
            continue
        symbol = str(raw_symbol).upper()
        quantity = _number(raw_entry.get("Free")) + _number(raw_entry.get("Lock", raw_entry.get("Locked")))
        if symbol == "USD":
            cash_free_usd = _number(raw_entry.get("Free"))
            usd_lock = quantity - cash_free_usd
            continue
        price = _ticker_price(ticker_data, symbol)
        if quantity > 0 and price <= 0:
            unpriced[symbol] = quantity           # held but not valued: equity is understated
        if quantity <= 0 or price <= 0:
            continue
        notional = quantity * price
        longs[symbol] = notional
        long_value += notional

    shorts: dict[str, float] = {}
    short_collateral = 0.0
    short_pnl = 0.0
    entry_prices: dict[str, float] = {}
    for position in (short_positions or {}).get("Positions", []) or []:
        if not isinstance(position, dict):
            continue
        if str(position.get("PositionStatus", "OPEN")).upper() not in {"OPEN", ""}:
            continue
        pair = str(position.get("Pair", "")).upper()
        symbol = to_coin(pair) if pair else ""
        if not symbol:
            continue
        entry_price = _number(position.get("EntryPrice"))
        if entry_price > 0:
            entry_prices[symbol] = entry_price
        collateral = _number(position.get("Collateral", position.get("Margin")))
        if collateral <= 0:
            collateral = _number(position.get("ShortQty")) * _number(
                position.get("CurrentPrice", _ticker_price(ticker_data, symbol))
            )
        shorts[symbol] = shorts.get(symbol, 0.0) + collateral
        short_collateral += collateral
        short_pnl += _number(position.get("UnrealizedPNL"))

    raw_pending = pending_orders.get("OrderMatched", []) if isinstance(pending_orders, dict) else (pending_orders or [])
    # history rows (FILLED / CANCELED) are never resting orders
    pending = [order for order in raw_pending if isinstance(order, dict)
               and str(order.get("Status", "PENDING")).upper() == "PENDING"]
    reserved = pending_reserved_usd(pending)
    residual = usd_lock - reserved
    # All of the open-short collateral is in Lock or none of it is (fees/rounding tolerance)
    tolerance = 1.0 + 0.01 * short_collateral + 0.002 * reserved
    lock_collateral = short_collateral if short_collateral > 0 and residual >= short_collateral - tolerance else 0.0
    cash_usd = cash_free_usd + max(usd_lock - lock_collateral, reserved)
    return {
        "cash_usd": cash_usd,
        "cash_free_usd": cash_free_usd,
        "longs": longs,
        "shorts": shorts,
        "equity_usd": cash_usd + long_value + short_collateral + short_pnl,
        "prices": prices,
        "entry_prices": entry_prices,
        "pending_orders": pending,
        "unpriced": unpriced,
        # how USD Lock was read (ops / dashboards; the bridge refuses a lock-in on unexplained Lock)
        "usd_lock": usd_lock,
        "lock_pending_usd": reserved,
        "lock_short_collateral_usd": lock_collateral,
        "lock_unexplained_usd": max(usd_lock - reserved - lock_collateral, 0.0),
    }


def _check_read(name: str, response: Any, payload_key: str | None) -> None:
    if not isinstance(response, dict):
        raise SnapshotReadError(f"{name}: unexpected response {response!r:.200}")
    if response.get("Success") is False or (payload_key and payload_key not in response):
        raise SnapshotReadError(f"{name}: {response.get('ErrMsg') or 'missing ' + str(payload_key)}")


def read_exchange_snapshot(port: Any) -> dict[str, Any]:
    balance = port.get_balance()
    _check_read("balance", balance, None)
    if not (balance.get("SpotWallet") or balance.get("Wallet")):
        raise SnapshotReadError("balance: no wallet in the response")
    short_positions = port.get_short_positions()
    _check_read("short_positions", short_positions, "Positions")
    tickers = port.get_ticker()
    _check_read("ticker", tickers, "Data")
    pending = port.list_open_orders()
    if isinstance(pending, dict) and pending.get("Success") is False:
        # Roostoo answers "no order matched" (Success: false) when nothing is pending
        if "no order" not in str(pending.get("ErrMsg", "")).lower():
            raise SnapshotReadError(f"pending orders: {pending.get('ErrMsg')}")
        pending = []
    return normalize_exchange_snapshot(balance, short_positions, tickers, pending)


def _ticker_price(tickers: dict[str, Any], symbol: str) -> float:
    entry = tickers.get(to_pair(symbol), {}) if isinstance(tickers, dict) else {}
    if not isinstance(entry, dict):
        return 0.0
    return _number(entry.get("LastPrice"))


def _number(value: Any) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0