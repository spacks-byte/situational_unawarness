from __future__ import annotations

import math
from typing import Any


def normalize_exchange_snapshot(
    balance: dict[str, Any] | None,
    short_positions: dict[str, Any] | None,
    tickers: dict[str, Any] | None,
    pending_orders: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize live or simulated exchange responses into engine state."""

    balance = balance or {}
    wallet = balance.get("Wallet") or balance.get("SpotWallet") or {}   # the API has used both names
    ticker_data = (tickers or {}).get("Data", tickers or {})
    cash_usd = 0.0
    longs: dict[str, float] = {}
    long_value = 0.0
    prices = {
        pair.rsplit("/", 1)[0].upper(): _number(entry.get("LastPrice"))
        for pair, entry in ticker_data.items()
        if isinstance(pair, str) and "/" in pair and isinstance(entry, dict) and _number(entry.get("LastPrice")) > 0
    }

    for raw_symbol, raw_entry in wallet.items():
        if not isinstance(raw_entry, dict):
            continue
        symbol = str(raw_symbol).upper()
        quantity = _number(raw_entry.get("Free")) + _number(raw_entry.get("Lock", raw_entry.get("Locked")))
        if symbol == "USD":
            cash_usd = quantity
            continue
        price = _ticker_price(ticker_data, symbol)
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
        symbol = pair.split("/", 1)[0]
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
    return {
        "cash_usd": cash_usd,
        "longs": longs,
        "shorts": shorts,
        "equity_usd": cash_usd + long_value + short_collateral + short_pnl,
        "prices": prices,
        "entry_prices": entry_prices,
        "pending_orders": [order for order in raw_pending if isinstance(order, dict)],
    }


def read_exchange_snapshot(port: Any) -> dict[str, Any]:
    return normalize_exchange_snapshot(
        port.get_balance(),
        port.get_short_positions(),
        port.get_ticker(),
        port.list_open_orders(),
    )


def _ticker_price(tickers: dict[str, Any], symbol: str) -> float:
    pair = f"{symbol}/USD"
    entry = tickers.get(pair, {}) if isinstance(tickers, dict) else {}
    if not isinstance(entry, dict):
        return 0.0
    return _number(entry.get("LastPrice"))


def _number(value: Any) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0