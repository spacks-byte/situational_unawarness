import os
from dotenv import load_dotenv
import requests
import time
import hmac
import hashlib
from decimal import Decimal, ROUND_DOWN
from utilities import get_amount_precision, get_server_timestamp, get_trade_pair_info

# Load environment variables
load_dotenv()

# Get environment variables
ROOSTOO_API_KEY = os.getenv('ROOSTOO_API_KEY')
ROOSTOO_API_SECRET = os.getenv('ROOSTOO_API_SECRET')
BASE_URL = os.getenv('BASE_URL')


def _is_step_size_error(response):
    if not isinstance(response, dict):
        return False
    return "step size" in str(response.get("ErrMsg", "")).lower()


def _is_success_response(response):
    if not isinstance(response, dict):
        return False
    return response.get("Success") is True and str(response.get("ErrMsg", "")) == ""


def _floor_to_decimals(value, decimals):
    scale = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(scale, rounding=ROUND_DOWN))


def _calculate_sell_quantity_with_step_size_reduction(
    base_quantity, attempt, amount_precision=None
):
    if base_quantity <= 0:
        return 0.0

    if amount_precision is not None:
        reduction_factors = [1.0, 0.995, 0.99, 0.98, 0.95, 0.90]
        factor = reduction_factors[min(attempt, len(reduction_factors) - 1)]
        return float(_floor_to_decimals(base_quantity * factor, amount_precision))

    decimal_steps = [6, 5, 4, 3, 2, 1, 0]
    if attempt < len(decimal_steps):
        qty = _floor_to_decimals(base_quantity, decimal_steps[attempt])
    else:
        reduction_factors = [0.995, 0.99, 0.98, 0.95, 0.90]
        idx = min(attempt - len(decimal_steps), len(reduction_factors) - 1)
        reduced = base_quantity * reduction_factors[idx]
        qty = _floor_to_decimals(reduced, max(0, 3 - idx))

    if qty <= 0:
        return 0.0
    return float(qty)


def place_order(pair_or_coin, side, quantity, price=None, order_type=None):
    """
    Places a new order with improved flexibility and safety checks.

    Args:
        pair_or_coin (str): The asset to trade (e.g., "BTC" or "BTC/USD").
        side (str): "BUY" or "SELL".
        quantity (float or int): The amount to trade.
        price (float, optional): The price for a LIMIT order. Defaults to None.
        order_type (str, optional): "LIMIT" or "MARKET". Auto-detected if not provided.
    """
    print(f"\n--- Placing a new order for {quantity} {pair_or_coin} ---")
    url = f"{BASE_URL}/v3/place_order"

    # 1. Determine the full pair name
    pair = str(pair_or_coin).strip().upper()
    pair = pair if "/" in pair else f"{pair}/USD"

    # 2. Auto-detect order_type if it's not specified
    if order_type is None:
        order_type = "LIMIT" if price is not None else "MARKET"
        print(f"Auto-detected order type: {order_type}")
    order_type = order_type.upper()
    side = side.upper()
    if side not in {'BUY', 'SELL'}:
        raise ValueError("side must be BUY or SELL")
    if order_type not in {'LIMIT', 'MARKET'}:
        raise ValueError("order_type must be LIMIT or MARKET")

    # 3. Validate parameters to prevent errors
    if order_type == 'LIMIT' and price is None:
        print("Error: LIMIT orders require a 'price' parameter.")
        return None
    if order_type == 'MARKET' and price is not None:
        print("Warning: Price is provided for a MARKET order and will be ignored by the API.")

    try:
        normalized_quantity = float(quantity)
    except (TypeError, ValueError) as e:
        raise ValueError("quantity must be numeric") from e
    if normalized_quantity <= 0:
        raise ValueError("quantity must be greater than 0")

    pair_info = get_trade_pair_info(pair) or {}
    amount_precision = pair_info.get('AmountPrecision')
    price_precision = pair_info.get('PricePrecision')
    try:
        amount_precision = int(amount_precision)
    except (TypeError, ValueError):
        amount_precision = None
    try:
        price_precision = int(price_precision)
    except (TypeError, ValueError):
        price_precision = None

    if amount_precision is not None:
        normalized_quantity = _floor_to_decimals(normalized_quantity, amount_precision)
        if normalized_quantity <= 0:
            raise ValueError("quantity is below the pair amount precision")

    normalized_price = price
    if price is not None and price_precision is not None:
        try:
            normalized_price = _floor_to_decimals(float(price), price_precision)
        except (TypeError, ValueError) as e:
            raise ValueError("price must be numeric") from e
        if normalized_price <= 0:
            raise ValueError("price is below the pair price precision")

    minimum_order = pair_info.get('MiniOrder')
    try:
        minimum_order = float(minimum_order)
    except (TypeError, ValueError):
        minimum_order = None
    if (
        order_type == 'LIMIT'
        and minimum_order is not None
        and normalized_quantity * normalized_price < minimum_order
    ):
        raise ValueError(
            f"order notional must be at least the pair minimum order value of {minimum_order}"
        )

    timestamp = get_server_timestamp()

    # 4. Create the request payload
    payload = {
        'pair': pair,
        'side': side,
        'type': order_type,
        'quantity': str(normalized_quantity),
        'timestamp': timestamp
    }
    if order_type == 'LIMIT':
        payload['price'] = str(normalized_price)

    def _submit_order(payload_to_send):
        query_string = "&".join([f"{key}={value}" for key, value in sorted(payload_to_send.items())])
        signature = hmac.new(
            ROOSTOO_API_SECRET.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        headers = {
            "RST-API-KEY": ROOSTOO_API_KEY,
            "MSG-SIGNATURE": signature,
            "Content-Type": "application/x-www-form-urlencoded"
        }

        response = requests.post(url, headers=headers, data=payload_to_send)

        print("--- Placing Order ---")
        print("Status:", response.status_code)
        print("Response:", response.text)

        try:
            return response.json()
        except ValueError:
            print("Error: Failed to parse JSON response")
            return None

    is_sell_market = side.upper() == "SELL" and order_type == "MARKET"
    try:
        base_quantity = float(quantity)
    except (TypeError, ValueError):
        base_quantity = 0.0

    if is_sell_market and base_quantity > 0:
        max_attempts = 12
        last_response = None
        sell_amount_precision = get_amount_precision(pair)
        for attempt in range(max_attempts):
            trial_quantity = _calculate_sell_quantity_with_step_size_reduction(
                base_quantity=base_quantity,
                attempt=attempt,
                amount_precision=sell_amount_precision,
            )
            if trial_quantity <= 0:
                continue

            if attempt > 0:
                print(
                    f"Retry {attempt + 1}/{max_attempts}: "
                    f"attempting SELL quantity={trial_quantity}"
                )

            trial_payload = dict(payload)
            trial_payload["quantity"] = str(trial_quantity)
            last_response = _submit_order(trial_payload)

            if _is_success_response(last_response):
                return last_response

            if _is_step_size_error(last_response):
                if attempt < max_attempts - 1:
                    time.sleep(1)
                continue

            return last_response

        return last_response

    return _submit_order(payload)


def test_place_order(testnum):
    if testnum == 0:
        # Example 1: Place a LIMIT order (by providing a price)
        # The function will correctly identify this as a LIMIT order.
        coin = input("Which coin would you like to use for this transaction? (BTC,BNB,ETH,...): ").upper()
        side = input("Do you want to BUY or SELL?: ").upper()
        amount = float(input("How much of the coin do you want to buy/sell?: "))
        price_input = input("If you want this to be a LIMIT order enter price. Press Enter to skip!: ")
        if price_input == "" or price_input is None:
            place_order(
                pair_or_coin=coin,
                side=side,
                quantity=amount,
            )
        else:
            price = float(price_input)
            place_order(
                pair_or_coin=coin,
                side=side,
                quantity=amount,
                price=price
            )
    elif testnum == 1:
        # Example 1: Place a LIMIT order (by providing a price)
        # The function will correctly identify this as a LIMIT order.
        place_order(
            pair_or_coin="BNB",
            side="SELL",
            quantity=0.1,
            price=965
        )
    elif testnum == 2:
        # Example 2: Place a MARKET order (by not providing a price)
        # The function will correctly identify this as a MARKET order.
        place_order(
            pair_or_coin="BNB/USD",
            side="BUY",
            quantity=0.1
        )
    elif testnum == 3:
        # Example 3: Invalid order (LIMIT without a price)
        # The function will catch this error before sending the request.
        place_order(
            pair_or_coin="ETH",
            side="BUY",
            quantity=0.005,
            order_type="LIMIT"  # Explicitly set, but no price given
        )
    else:
        print("Incorrect test number (0-3)")


def query_order(order_id=None, pair=None, pending_only=None, offset=None, limit=None):
    """Queries orders. (Auth: RCL_TopLevelCheck)"""
    url = f"{BASE_URL}/v3/query_order"
    
    # Use server timestamp to avoid time sync issues
    timestamp = get_server_timestamp()
    payload = {}
    if order_id is not None:
        payload['order_id'] = str(order_id)
    elif pair: # Docs say order_id and pair cannot be sent together
        payload['pair'] = pair
    if order_id is None and pending_only is not None:
        payload['pending_only'] = 'TRUE' if pending_only else 'FALSE'
    if order_id is None and offset is not None:
        payload['offset'] = str(offset)
    if order_id is None and limit is not None:
        payload['limit'] = str(limit)
    payload['timestamp'] = timestamp
                
    # === Create signature ===
    query_string = "&".join([f"{key}={value}" for key, value in sorted(payload.items())])
    signature = hmac.new(
        ROOSTOO_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "RST-API-KEY": ROOSTOO_API_KEY,
        "MSG-SIGNATURE": signature,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        response = requests.post(url, headers=headers, data=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error querying order: {e}")
        print(f"Response text: {e.response.text if e.response else 'N/A'}")
        return None


def test_query_order(coin=None):
    if coin is None:
        coin = "BTC"
    print(f"--- Querying Pending {coin} Orders ---")
    pair = coin if "/" in coin else f"{coin}/USD"
    orders = query_order(pair=pair, pending_only=True)
    if orders and orders.get('Success'):
        print(f"Found {len(orders.get('OrderMatched', []))} matching orders.")
        for n in orders.get('OrderMatched', []):
            print(f"{n.get('Pair')}: {n.get('Side')} {n.get('Quantity')}")
    elif orders:
        print(f"Error: {orders.get('ErrMsg')}")


def cancel_order(order_id=None, pair=None):
    """Cancels orders. (Auth: RCL_TopLevelCheck)"""
    url = f"{BASE_URL}/v3/cancel_order"
    
    # Use server timestamp to avoid time sync issues
    timestamp = get_server_timestamp()
    payload = {}
    if order_id is not None:
        payload['order_id'] = str(order_id)
    elif pair: # Docs say only one is allowed
        payload['pair'] = pair
    # If neither is sent, it cancels all
    payload['timestamp'] = timestamp

    # === Create signature ===
    query_string = "&".join([f"{key}={value}" for key, value in sorted(payload.items())])
    signature = hmac.new(
        ROOSTOO_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "RST-API-KEY": ROOSTOO_API_KEY,
        "MSG-SIGNATURE": signature,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        response = requests.post(url, headers=headers, data=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error canceling order: {e}")
        print(f"Response text: {e.response.text if e.response else 'N/A'}")
        return None


def test_cancel_order(coin=None):
    if coin is None:
        print("\n--- 8. Canceling all pending orders ---")
        cancel_result = cancel_order()
    else:
        pair = coin if "/" in coin else f"{coin}/USD"
        print(f"\n--- 8. Canceling order {pair} ---")
        cancel_result = cancel_order(pair=pair)
    if cancel_result:
        print(f"Cancel Success: {cancel_result.get('Success')}")
        print(f"Canceled List: {cancel_result.get('CanceledList')}")