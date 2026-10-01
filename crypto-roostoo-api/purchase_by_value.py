import time
from typing import Any, Dict, Optional

from trades import place_order
from utilities import get_amount_precision, get_mini_order, get_ticker


def _normalize_pair(pair_or_coin: str) -> str:
    pair_or_coin = pair_or_coin.strip().upper()
    if "/" in pair_or_coin:
        return pair_or_coin
    return f"{pair_or_coin}/USD"


def _fetch_last_price(pair: str) -> Optional[float]:
    ticker = get_ticker(pair=pair)
    if not ticker or not ticker.get("Success", True):
        return None

    data = ticker.get("Data", {})
    pair_data = data.get(pair, {})
    last_price = pair_data.get("LastPrice")
    if last_price is None:
        return None

    try:
        return float(last_price)
    except (TypeError, ValueError):
        return None


def _is_step_size_error(response: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(response, dict):
        return False
    err_msg = str(response.get("ErrMsg", "")).lower()
    return "step size" in err_msg


def _is_success_response(response: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(response, dict):
        return False
    return response.get("Success") is True and str(response.get("ErrMsg", "")) == ""


def _calculate_quantity_with_step_size_reduction(
    usd_value: float,
    reference_price: float,
    attempt: int,
    amount_precision: Optional[int] = None,
) -> float:
    base_quantity = usd_value / reference_price

    if amount_precision is not None:
        scale = 10 ** amount_precision
        return max(0.0, int(base_quantity * scale) / scale)

    if reference_price > 50000:
        strategies = [
            lambda q: round(q, 6),
            lambda q: round(q, 5),
            lambda q: round(q, 4),
            lambda q: round(q, 3),
            lambda q: round(q, 2),
            lambda q: round(q, 1),
            lambda q: max(0.001, round(q * 0.95, 6)),
            lambda q: max(0.0005, round(q * 0.90, 6)),
        ]
    elif reference_price > 1000:
        strategies = [
            lambda q: round(q, 4),
            lambda q: round(q, 3),
            lambda q: round(q, 2),
            lambda q: round(q, 1),
            lambda q: round(q * 0.95, 4),
            lambda q: round(q * 0.90, 3),
            lambda q: round(q * 0.85, 3),
            lambda q: round(q * 0.80, 3),
        ]
    else:
        strategies = [
            lambda q: round(q, 3),
            lambda q: round(q, 2),
            lambda q: round(q, 1),
            lambda q: int(q),
            lambda q: max(1, int(q // 5) * 5),
            lambda q: max(1, int(q // 10) * 10),
            lambda q: max(1, int(q // 25) * 25),
            lambda q: max(1, int(q // 50) * 50),
        ]

    if attempt < len(strategies):
        quantity = float(strategies[attempt](base_quantity))
    else:
        # Fallback to default rounding based on price range
        precision = 6 if reference_price > 50000 else 4 if reference_price > 1000 else 3
        quantity = float(round(base_quantity, precision))

    if quantity <= 0 and attempt < len(strategies):
        # If rounding causes quantity to be 0, try another approach
        # Try direct rounding with 3 decimal places
        quantity = round(base_quantity, 3)

    return max(0.0, quantity)


def buy_coin_by_value(
    pair_or_coin: str,
    usd_value: float,
    max_attempts: int = 16,
) -> Dict[str, Any]:
    """
    Buy a coin by target USD notional using Roostoo MARKET order flow.

    Args:
        pair_or_coin: Coin symbol like "BTC" or pair like "BTC/USD".
        usd_value: USD amount to spend.
        max_attempts: Maximum retry attempts with progressively simpler quantities.

    Returns:
        Dictionary with calculated quantity, reference price, and API response.
    """
    if usd_value <= 0:
        raise ValueError("usd_value must be greater than 0")

    pair = _normalize_pair(pair_or_coin)
    reference_price = _fetch_last_price(pair)
    if reference_price is None or reference_price <= 0:
        raise RuntimeError(f"Failed to fetch valid market price for {pair}")

    mini_order = get_mini_order(pair)
    if mini_order is not None and usd_value < mini_order:
        raise ValueError(
            f"usd_value must be at least the pair minimum order value of {mini_order}"
        )

    amount_precision = get_amount_precision(pair)

    response = None
    quantity = 0.0
    attempts: list[Dict[str, Any]] = []

    # Define simple progressive reduction factors
    reduction_factors = [1.0, 0.95, 0.90, 0.85, 0.80, 0.75, 0.50, 0.25, 0.10]

    for attempt in range(max_attempts):
        # Calculate current USD value based on reduction factors
        current_usd_value = usd_value * reduction_factors[min(attempt, len(reduction_factors) - 1)]
        
        # Calculate quantity using default step size reducer
        trial_quantity = _calculate_quantity_with_step_size_reduction(
            current_usd_value,
            reference_price,
            attempt,
            amount_precision=amount_precision,
        )

        if trial_quantity <= 0:
            continue

        response = place_order(
            pair_or_coin=pair,
            side="BUY",
            quantity=trial_quantity,
            order_type="MARKET",
        )

        # Check if response is valid before processing
        success = bool(_is_success_response(response))
        err_msg = str(response.get("ErrMsg", "")) if isinstance(response, dict) else ""

        attempts.append(
            {
                "attempt": attempt + 1,
                "usd_value": round(float(current_usd_value), 8),
                "quantity": float(trial_quantity),
                "success": success,
                "err_msg": err_msg,
            }
        )

        if success:
            quantity = trial_quantity
            break

        if _is_step_size_error(response):
            # For step size errors, we continue with next attempt (next reduction factor)
            if attempt < max_attempts - 1:
                time.sleep(1)
            continue

        # For non-step-size errors, stop retrying 
        quantity = trial_quantity
        break

    return {
        "pair": pair,
        "usd_value": float(usd_value),
        "reference_price": float(reference_price),
        "calculated_quantity": float(quantity),
        "attempts": attempts,
        "response": response,
    }


def test_buy_coin_by_value() -> None:
    coin = input("Enter coin symbol (e.g., BTC): ").strip().upper()
    usd_value = float(input("Enter USD value to spend: ").strip())

    result = buy_coin_by_value(coin, usd_value)
    print("\n--- Buy By Value Result ---")
    print(f"Pair: {result['pair']}")
    print(f"USD Value: {result['usd_value']}")
    print(f"Reference Price: {result['reference_price']:.2f}")
    print(f"Calculated Quantity: {result['calculated_quantity']}")
    print(f"Attempts Used: {len(result['attempts'])}")
    print(f"API Response: {result['response']}")


if __name__ == "__main__":
    test_buy_coin_by_value()
