from __future__ import annotations

from typing import Any

from tradebot.core.symbols import to_coin
from tradebot.engine.schema.models import LongTarget, ShortTarget, TargetPortfolio
from tradebot.engine.state.snapshot import remaining_qty


def _as_float_map(raw: dict[str, Any] | None) -> dict[str, float]:
    if not raw:
        return {}
    return {str(symbol): float(value) for symbol, value in raw.items()}


def _desired_amount_for_long(target: LongTarget, total_equity_usd: float) -> float:
    if target.notional_usd is not None:
        return float(target.notional_usd)
    if target.weight is not None:
        return float(target.weight) * float(total_equity_usd)
    return 0.0


def _desired_amount_for_short(target: ShortTarget, total_equity_usd: float) -> float:
    if target.collateral_usd is not None:
        return float(target.collateral_usd)
    return 0.0


def compute_rebalance_plan(target: TargetPortfolio, actual: dict[str, Any], total_equity_usd: float) -> dict[str, Any]:
    """Build the minimal close/open plan to drift actual state toward the target.

    The function intentionally keeps the model small and deterministic: close
    reductions first, then add the missing exposures. The plan is represented as
    a dict of symbol -> amount for each action type.
    """

    actual_longs = _as_float_map(actual.get("longs"))
    actual_shorts = _as_float_map(actual.get("shorts"))
    pending_longs, pending_shorts = _pending_open_exposure(actual.get("pending_orders"))
    for symbol, amount in pending_longs.items():
        actual_longs[symbol] = actual_longs.get(symbol, 0.0) + amount
    for symbol, amount in pending_shorts.items():
        actual_shorts[symbol] = actual_shorts.get(symbol, 0.0) + amount

    desired_longs: dict[str, float] = {}
    for entry in target.longs:
        desired_longs[str(entry.symbol).upper()] = _desired_amount_for_long(entry, total_equity_usd)

    desired_shorts: dict[str, float] = {}
    for entry in target.shorts:
        desired_shorts[str(entry.symbol).upper()] = _desired_amount_for_short(entry, total_equity_usd)

    for symbol in (target.flatten or []):
        desired_longs[str(symbol).upper()] = 0.0
        desired_shorts[str(symbol).upper()] = 0.0

    close_longs: dict[str, float] = {}
    open_longs: dict[str, float] = {}
    close_shorts: dict[str, float] = {}
    open_shorts: dict[str, float] = {}

    for symbol in sorted(set(actual_longs) | set(desired_longs)):
        actual_value = actual_longs.get(symbol, 0.0)
        target_value = desired_longs.get(symbol, 0.0)
        delta = target_value - actual_value
        if delta < 0:
            close_longs[symbol] = abs(delta)
        elif delta > 0:
            open_longs[symbol] = delta

    for symbol in sorted(set(actual_shorts) | set(desired_shorts)):
        actual_value = actual_shorts.get(symbol, 0.0)
        target_value = desired_shorts.get(symbol, 0.0)
        delta = target_value - actual_value
        if delta < 0:
            close_shorts[symbol] = abs(delta)
        elif delta > 0:
            open_shorts[symbol] = delta

    return {
        "close_longs": close_longs,
        "open_longs": open_longs,
        "close_shorts": close_shorts,
        "open_shorts": open_shorts,
        "flatten": list(dict.fromkeys(str(x).upper() for x in (target.flatten or []))),
        "meta": {"total_equity_usd": float(total_equity_usd)},
    }


def _pending_open_exposure(orders: list[dict[str, Any]] | None) -> tuple[dict[str, float], dict[str, float]]:
    pending_longs: dict[str, float] = {}
    pending_shorts: dict[str, float] = {}
    for order in orders or []:
        if str(order.get("Status", "PENDING")).upper() != "PENDING":
            continue
        pair = str(order.get("Pair", ""))
        symbol = to_coin(pair) if pair else ""
        if not symbol:
            continue
        side = str(order.get("Side", "")).upper()
        qty = float(order.get("Quantity", 0.0) or 0.0)
        # Only the unfilled part is pending: a partial fill already sits in the wallet / short position
        remaining = remaining_qty(order)
        notional = remaining * float(order.get("Price", 0.0) or 0.0)
        if side == "SHORT_OPEN":                    # query_order rows carry no Collateral (Roostoo docs)
            collateral = float(order.get("Collateral", 0.0) or 0.0)
            amount = (collateral * (remaining / qty if qty > 0 else 1.0)) if collateral else notional
            pending_shorts[symbol] = pending_shorts.get(symbol, 0.0) + amount
        elif side in ("BUY", "SELL"):               # a resting sell already reduces the long
            pending_longs[symbol] = pending_longs.get(symbol, 0.0) + (notional if side == "BUY" else -notional)
    return pending_longs, pending_shorts


# Public name: the live bridge counts resting orders the same way the plan does
pending_open_exposure = _pending_open_exposure
