"""
Interactive menu for manually testing the Roostoo API: `python -m tradebot api`.
Options 1-6 are read-only; 7-10 change the account and ask for confirmation first.
"""
from __future__ import annotations

import json
from typing import Callable

from tradebot.core.config import Settings
from tradebot.exchange.client import RoostooClient, RoostooError


def _ask(prompt: str, cast: Callable = str, default=None):
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            return cast(raw)
        except ValueError:
            print(f"Invalid value, expected {cast.__name__}")


def _confirm(action: str) -> bool:
    return input(f"About to {action}. Type 'yes' to send: ").strip().lower() == "yes"


def _show(result) -> None:
    print(json.dumps(result, indent=2, default=str))


def _wallet(client: RoostooClient) -> dict:
    balance = client.balance()
    wallet = balance.get("SpotWallet") or balance.get("Wallet") or {}
    return {coin: v for coin, v in wallet.items() if float(v.get("Free", 0)) or float(v.get("Lock", 0))}


def _portfolio_worth(client: RoostooClient) -> None:
    wallet = _wallet(client)
    prices = client.ticker().get("Data", {})
    total = 0.0
    for coin, entry in sorted(wallet.items()):
        qty = float(entry.get("Free", 0)) + float(entry.get("Lock", 0))
        price = 1.0 if coin == "USD" else float(prices.get(f"{coin}/USD", {}).get("LastPrice", 0) or 0)
        total += qty * price
        print(f"{coin:<8} qty={qty:<16g} value=${qty * price:,.2f}")
    print(f"Total spot value: ${total:,.2f} (excludes short collateral)")


MENU = [
    ("Server time", lambda c: _show(c.server_time())),
    ("Exchange info (summary)", lambda c: _show({k: v for k, v in c.exchange_info().items() if k != "TradePairs"}
                                               | {"pairs": len(c.exchange_info().get("TradePairs", {}))})),
    ("Ticker for one coin", lambda c: _show(c.ticker(_ask("Coin (e.g. BTC): ").upper()))),
    ("Balance (non-zero)", lambda c: _show(_wallet(c))),
    ("Portfolio worth", _portfolio_worth),
    ("Pending orders + short positions", lambda c: (_show(c.query_order(pending_only=True)), _show(c.short_positions()))),
]


def _place_limit(c: RoostooClient) -> None:
    coin = _ask("Coin: ").upper()
    side = _ask("BUY or SELL: ").upper()
    qty = _ask("Quantity: ", float)
    price = _ask("Limit price: ", float)
    if _confirm(f"place LIMIT {side} {qty} {coin} @ {price}"):
        _show(c.place_order(coin, side, qty, price=price))


def _cancel(c: RoostooClient) -> None:
    order_id = _ask("Order ID: ")
    if _confirm(f"cancel order {order_id}"):
        _show(c.cancel_order(order_id=order_id))


def _open_short(c: RoostooClient) -> None:
    coin = _ask("Coin: ").upper()
    collateral = _ask("USD collateral: ", float)
    price = _ask("Limit price (Enter for market): ", float, default="")
    if _confirm(f"open short {coin} collateral ${collateral} @ {price or 'market'}"):
        _show(c.open_short(coin, collateral, price=price or None))


def _close_short(c: RoostooClient) -> None:
    coin = _ask("Coin: ").upper()
    pct = _ask("Percent to close (1-100): ", float)
    if _confirm(f"close {pct}% of the {coin} short at market"):
        _show(c.close_short(coin, close_pct=pct))


MENU += [
    ("Place LIMIT order", _place_limit),
    ("Cancel order", _cancel),
    ("Open short", _open_short),
    ("Close short (market)", _close_short),
]


def run_menu(settings: Settings | None = None) -> None:
    settings = settings or Settings.load()
    client = RoostooClient(settings=settings.exchange)
    print(f"Roostoo API test menu ({client.base_url})")
    while True:
        print()
        for i, (label, _) in enumerate(MENU, 1):
            print(f"{i:>2}. {label}")
        print(" 0. Exit")
        choice = _ask("Choice: ", int, default=0)
        if choice == 0:
            return
        if not 1 <= choice <= len(MENU):
            continue
        try:
            MENU[choice - 1][1](client)
        except (RoostooError, ValueError) as e:
            print(f"[ERROR] {e}")
            if isinstance(e, RoostooError) and e.body:
                print(e.body[:500])
