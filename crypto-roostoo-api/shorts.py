import hashlib
import hmac
import os
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import requests
from dotenv import load_dotenv

from utilities import get_server_timestamp, get_trade_pair_info


load_dotenv()

ROOSTOO_API_KEY = os.getenv('ROOSTOO_API_KEY')
ROOSTOO_API_SECRET = os.getenv('ROOSTOO_API_SECRET')
BASE_URL = os.getenv('BASE_URL')


def _normalize_pair(pair_or_coin):
    pair = str(pair_or_coin).strip().upper()
    return pair if "/" in pair else f"{pair}/USD"


def _floor_to_precision(value, precision):
    scale = Decimal(1).scaleb(-precision)
    return Decimal(str(value)).quantize(scale, rounding=ROUND_DOWN)


def _format_decimal(value):
    return format(value, 'f')


def _pair_precision(pair, field):
    pair_info = get_trade_pair_info(pair) or {}
    try:
        precision = int(pair_info.get(field))
    except (TypeError, ValueError):
        return None
    return precision if precision >= 0 else None


def _signed_request(method, path, payload):
    payload = dict(payload)
    payload['timestamp'] = get_server_timestamp()
    query_string = "&".join(
        f"{key}={value}" for key, value in sorted(payload.items())
    )
    signature = hmac.new(
        ROOSTOO_API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        'RST-API-KEY': ROOSTOO_API_KEY,
        'MSG-SIGNATURE': signature,
    }
    if method == 'POST':
        headers['Content-Type'] = 'application/x-www-form-urlencoded'

    try:
        response = requests.request(
            method,
            f"{BASE_URL}{path}",
            headers=headers,
            data=payload if method == 'POST' else None,
            params=payload if method == 'GET' else None,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error calling {path}: {e}")
        print(f"Response text: {e.response.text if e.response else 'N/A'}")
        return None


def open_short(pair_or_coin, collateral, price=None):
    """Open or add to a short position using market or limit execution."""
    pair = _normalize_pair(pair_or_coin)
    try:
        collateral_value = Decimal(str(collateral))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise ValueError('collateral must be numeric') from e
    if not collateral_value.is_finite() or collateral_value < Decimal('1'):
        raise ValueError('collateral must be at least 1')

    payload = {
        'pair': pair,
        'collateral': _format_decimal(collateral_value),
    }
    if price is not None:
        try:
            price_value = Decimal(str(price))
        except (InvalidOperation, TypeError, ValueError) as e:
            raise ValueError('price must be numeric') from e
        if not price_value.is_finite() or price_value <= 0:
            raise ValueError('price must be greater than 0')
        price_precision = _pair_precision(pair, 'PricePrecision')
        if price_precision is not None:
            price_value = _floor_to_precision(price_value, price_precision)
        payload['order_type'] = 'LIMIT'
        payload['price'] = _format_decimal(price_value)

    return _signed_request('POST', '/v6/short_open', payload)


def close_short(pair_or_coin, close_qty=None, close_pct=None):
    """Close part or all of an open short position."""
    if close_qty is None and close_pct is None:
        raise ValueError('close_qty or close_pct is required')

    pair = _normalize_pair(pair_or_coin)
    payload = {'pair': pair}
    if close_qty is not None:
        try:
            quantity = Decimal(str(close_qty))
        except (InvalidOperation, TypeError, ValueError) as e:
            raise ValueError('close_qty must be numeric') from e
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError('close_qty must be greater than 0')
        amount_precision = _pair_precision(pair, 'AmountPrecision')
        if amount_precision is not None:
            quantity = _floor_to_precision(quantity, amount_precision)
        if quantity <= 0:
            raise ValueError('close_qty is below the pair amount precision')
        payload['close_qty'] = _format_decimal(quantity)
    if close_pct is not None:
        try:
            percentage = Decimal(str(close_pct))
        except (InvalidOperation, TypeError, ValueError) as e:
            raise ValueError('close_pct must be numeric') from e
        if not percentage.is_finite() or not Decimal('0') < percentage <= Decimal('100'):
            raise ValueError('close_pct must be greater than 0 and at most 100')
        payload['close_pct'] = _format_decimal(percentage)

    return _signed_request('POST', '/v6/short_close', payload)


def get_short_positions():
    """Return all currently open short positions with live P&L."""
    return _signed_request('GET', '/v6/short_positions', {})


def test_get_short_positions():
    print("--- Getting Open Short Positions ---")
    result = get_short_positions()
    if result:
        for position in result.get('Positions', []):
            print(
                f"{position.get('Pair')}: qty={position.get('ShortQty')} "
                f"PNL={position.get('UnrealizedPNL')}"
            )


def test_open_short():
    pair = input("Enter coin or pair to short (e.g., BTC or BTC/USD): ").strip()
    collateral = input("Enter USD collateral: ").strip()
    price = input("Enter limit price, or press Enter for market: ").strip()
    result = open_short(pair, collateral, price=price or None)
    print(f"Open short response: {result}")


def test_close_short():
    pair = input("Enter coin or pair to close (e.g., BTC or BTC/USD): ").strip()
    close_qty = input("Enter quantity, or press Enter to use a percentage: ").strip()
    close_pct = None
    if not close_qty:
        close_pct = input("Enter close percentage (1-100): ").strip()
    result = close_short(
        pair,
        close_qty=close_qty or None,
        close_pct=close_pct,
    )
    print(f"Close short response: {result}")
