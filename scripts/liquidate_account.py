"""Liquidate all spot and short holdings before a strategy migration.

This is intentionally an execution-only operator tool. It cancels resting orders,
closes every short, sells every non-USD spot balance at market, and verifies that
the account is flat before returning success. It never starts a replacement bot.

Run with the old bot stopped:
    ROOSTOO_CONFIRM_LIVE=YES python scripts/liquidate_account.py \
        --config config/market-making.yaml --state-dir var/live_comp
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradebot.core.config import Settings
from tradebot.exchange import RoostooClient, RoostooExchangePort
from tradebot.live.runner import account_lock_path
from tradebot.live.account import AccountLock

log = logging.getLogger("liquidate_account")


def _rows(response: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return response
    if response.get("Success") is False:
        raise RuntimeError(response.get("ErrMsg", "exchange request failed"))
    rows = response.get("OrderMatched")
    if rows is None and isinstance(response.get("OrderDetail"), dict):
        rows = [response["OrderDetail"]]
    return rows if isinstance(rows, list) else []


def _wallet(balance: dict[str, Any]) -> dict[str, Any]:
    wallet = balance.get("SpotWallet", balance.get("Wallet"))
    if balance.get("Success") is False or not isinstance(wallet, dict):
        raise RuntimeError("exchange returned no usable wallet")
    return wallet


def _pending(port: RoostooExchangePort) -> list[dict[str, Any]]:
    return _rows(port.list_open_orders())


def _shorts(port: RoostooExchangePort) -> list[dict[str, Any]]:
    response = port.get_short_positions()
    if response.get("Success") is False:
        raise RuntimeError(response.get("ErrMsg", "short position query failed"))
    return [p for p in response.get("Positions", []) if float(p.get("ShortQty", 0) or 0) > 0]


def _spot_holdings(port: RoostooExchangePort) -> list[tuple[str, float]]:
    wallet = _wallet(port.get_balance())
    holdings = []
    for coin, values in wallet.items():
        if coin == "USD" or not isinstance(values, dict):
            continue
        quantity = float(values.get("Free", 0) or 0) + float(values.get("Lock", values.get("Locked", 0)) or 0)
        if quantity > 0:
            holdings.append((coin, quantity))
    return holdings


def liquidate(port: RoostooExchangePort, *, rounds: int, poll_seconds: float) -> None:
    for attempt in range(1, rounds + 1):
        pending = _pending(port)
        for order in pending:
            order_id = order.get("OrderID")
            if order_id is None:
                raise RuntimeError(f"pending order has no OrderID: {order}")
            result = port.cancel_order(order_id=order_id)
            if result.get("Success") is False:
                raise RuntimeError(f"could not cancel order {order_id}: {result.get('ErrMsg')}")

        if pending:
            time.sleep(poll_seconds)

        for position in _shorts(port):
            pair = position["Pair"]
            result = port.close_short(pair, close_pct=100)
            if result.get("Success") is False:
                raise RuntimeError(f"could not close short {pair}: {result.get('ErrMsg')}")

        for coin, quantity in _spot_holdings(port):
            result = port.place_order(coin, "SELL", quantity, order_type="MARKET")
            if result.get("Success") is False:
                raise RuntimeError(f"could not sell {coin}: {result.get('ErrMsg')}")

        time.sleep(poll_seconds)
        remaining_orders = _pending(port)
        remaining_shorts = _shorts(port)
        remaining_spot = _spot_holdings(port)
        print(f"round {attempt}/{rounds}: orders={len(remaining_orders)} "
              f"shorts={len(remaining_shorts)} spot={len(remaining_spot)}")
        if not remaining_orders and not remaining_shorts and not remaining_spot:
            return

    raise RuntimeError("liquidation incomplete; inspect the account and order history before restarting")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--state-dir", default="var/live_comp",
                        help="existing bot state directory, used for the account lock")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args()
    if os.environ.get("ROOSTOO_CONFIRM_LIVE") != "YES":
        print("Refusing: set ROOSTOO_CONFIRM_LIVE=YES to authorize real liquidation orders", file=sys.stderr)
        return 2
    if args.rounds < 1 or args.poll_seconds < 0:
        print("--rounds must be positive and --poll-seconds must be non-negative", file=sys.stderr)
        return 2

    settings = Settings.load(args.config)
    client = RoostooClient(settings=settings.exchange)
    if not client.api_key or not client.api_secret:
        print("Refusing: Roostoo credentials are missing", file=sys.stderr)
        return 2
    port = RoostooExchangePort(client)
    lock = None
    try:
        lock = AccountLock(account_lock_path(settings, port), state_dir=args.state_dir)
        print("Starting full account liquidation. Existing open orders will be canceled.")
        liquidate(port, rounds=args.rounds, poll_seconds=args.poll_seconds)
        print("Liquidation verified: no open orders, shorts, or non-USD spot holdings remain.")
        return 0
    except Exception as exc:
        log.exception("liquidation failed")
        print(f"LIQUIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
