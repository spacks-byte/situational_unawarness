import math
from typing import Any, Dict, Optional

from balance import get_balance
from utilities import get_ticker


def _to_float(value: Any, default: float = 0.0) -> float:
    """Safely cast numeric-like values to float."""
    try:
        result = float(value)
        if math.isfinite(result):
            return result
        return default
    except (TypeError, ValueError):
        return default


def _extract_quantity(entry: Dict[str, Any]) -> float:
    """Extract total quantity from wallet entry using known field variants."""
    free_amount = _to_float(entry.get("Free", 0.0))
    locked_amount = _to_float(entry.get("Lock", entry.get("Locked", 0.0)))
    return free_amount + locked_amount


def _get_last_price_usd(symbol: str) -> Optional[float]:
    """Fetch ticker last price for SYMBOL/USD pair."""
    pair = f"{symbol.upper()}/USD"
    ticker = get_ticker(pair=pair)
    data = (ticker or {}).get("Data", {})

    if not isinstance(data, dict):
        return None

    pair_data = data.get(pair)
    if not isinstance(pair_data, dict) and len(data) == 1:
        # Some responses can key pair names differently; fallback to first item.
        pair_data = next(iter(data.values()))

    if not isinstance(pair_data, dict):
        return None

    last_price = _to_float(pair_data.get("LastPrice"), default=-1.0)
    if last_price <= 0:
        return None
    return last_price


def get_portfolio_worth(include_zero_balances: bool = False) -> Dict[str, Any]:
    """
    Compute current portfolio USD worth from live balances and live tickers.

    Returns a dictionary with per-asset valuation and the total USD value.
    """
    balance = get_balance() or {}
    wallet = balance.get("Wallet", {})

    if not isinstance(wallet, dict):
        return {
            "success": False,
            "error": "Invalid SpotWallet format",
            "total_usd": 0.0,
            "assets": {},
        }

    assets: Dict[str, Dict[str, Any]] = {}
    total_usd = 0.0

    for coin, entry in wallet.items():
        if not isinstance(entry, dict):
            continue

        symbol = str(coin).upper()
        quantity = _extract_quantity(entry)

        if not include_zero_balances and quantity <= 0:
            continue

        if symbol == "USD":
            price_usd = 1.0
        else:
            price_usd = _get_last_price_usd(symbol)

        if price_usd is None:
            assets[symbol] = {
                "quantity": round(quantity, 12),
                "price_usd": None,
                "value_usd": None,
                "warning": f"Missing ticker for {symbol}/USD",
            }
            continue

        value_usd = quantity * price_usd
        total_usd += value_usd
        assets[symbol] = {
            "quantity": round(quantity, 12),
            "price_usd": round(price_usd, 12),
            "value_usd": round(value_usd, 2),
        }

    return {
        "success": True,
        "total_usd": round(total_usd, 2),
        "assets": dict(sorted(assets.items())),
    }


def test_get_portfolio_worth() -> None:
    print("--- Computing Live Portfolio Worth ---")
    result = get_portfolio_worth()
    if not result.get("success"):
        print(f"Error: {result.get('error')}")
        return

    for symbol, item in result.get("assets", {}).items():
        value = item.get("value_usd")
        if value is None:
            print(f"{symbol:<5} qty={item.get('quantity')} value=N/A ({item.get('warning')})")
        else:
            print(f"{symbol:<5} qty={item.get('quantity')} value=${value:,.2f}")

    print(f"Total Portfolio Value (USD): ${result.get('total_usd', 0.0):,.2f}")


if __name__ == "__main__":
    test_get_portfolio_worth()