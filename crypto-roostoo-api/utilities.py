import os
from dotenv import load_dotenv
import requests
import time
import hmac
import hashlib

# Load environment variables
load_dotenv()

# Get environment variables
ROOSTOO_API_KEY = os.getenv('ROOSTOO_API_KEY')
ROOSTOO_API_SECRET = os.getenv('ROOSTOO_API_SECRET')
BASE_URL = os.getenv('BASE_URL')


def get_server_timestamp():
    """Get server timestamp to avoid time sync issues."""
    try:
        response = requests.get(f"{BASE_URL}/v3/serverTime")
        if response.status_code == 200:
            server_time = response.json().get('ServerTime')
            if server_time is not None:
                return str(server_time)
    except Exception as e:
        print(f"Warning: Could not get server time, using local time: {e}")
    
    # Fallback to local time
    return str(int(time.time() * 1000))


def check_server_time():
    """Checks server time. (Auth: RCL_NoVerification)"""
    url = f"{BASE_URL}/v3/serverTime"
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for bad status codes
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error checking server time: {e}")
        return None


def test_check_server_time():
    print("--- Checking Server Time ---")
    server_time = check_server_time()
    if server_time:
        print(f"Server time: {server_time.get('ServerTime')}")


def get_exchange_info():
    """Gets exchange info. (Auth: RCL_NoVerification)"""
    url = f"{BASE_URL}/v3/exchangeInfo"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error getting exchange info: {e}")
        return None


def get_trade_pair_info(pair):
    """Return exchange rules for a pair, or None when the pair is unknown."""
    info = get_exchange_info() or {}
    trade_pairs = info.get('TradePairs', {})
    if not isinstance(trade_pairs, dict):
        return None
    pair_info = trade_pairs.get(pair)
    return pair_info if isinstance(pair_info, dict) else None


def get_amount_precision(pair):
    """Return the exchange-defined amount precision for a trading pair."""
    pair_info = get_trade_pair_info(pair) or {}
    precision = pair_info.get('AmountPrecision')
    try:
        precision = int(precision)
    except (TypeError, ValueError):
        return None
    return precision if precision >= 0 else None


def get_mini_order(pair):
    """Return the exchange-defined minimum order notional for a pair."""
    pair_info = get_trade_pair_info(pair) or {}
    try:
        minimum = float(pair_info.get('MiniOrder'))
    except (TypeError, ValueError):
        return None
    return minimum if minimum >= 0 else None


def test_get_exchange_info():
    print("--- Getting Exchange Info ---")
    info = get_exchange_info()
    # print(info)
    if info:
        print(f"Is running: {info.get('IsRunning')}")
        print(f"Initial Wallet: {info.get('InitialWallet')}")
        print(f"Available pairs: {list(info.get('TradePairs', {}).keys())}")


def get_ticker(pair=None):
    """Gets market ticker. (Auth: RCL_TSCheck)"""
    url = f"{BASE_URL}/v3/ticker"
    # Use server timestamp to avoid time sync issues
    timestamp = get_server_timestamp()
    params = {
        'timestamp': timestamp
    }
    if pair:
        params['pair'] = pair
        
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error getting ticker: {e}")
        return None


def test_get_ticker(coin=None):
    if coin is None:
        print("--- Getting Ticker (All) ---")
        ticker_all = get_ticker()
        if ticker_all:
            print(f"Got data for {len(ticker_all.get('Data', {}))} pairs.")
    else:
        print(f"\n--- Getting Ticker ({coin}) ---")
        coin = coin if "/" in coin else f"{coin}/USD"
        ticker_btc = get_ticker(pair=coin)
        if ticker_btc:
            # print(ticker_btc)
            print(f"{coin} Last Price: {ticker_btc.get('Data', {}).get(coin, {}).get('LastPrice')}")


def get_available_sub():
    """Get available legacy WebSocket subscription topics."""
    url = f"{BASE_URL}/v3/available_sub"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error getting available subscriptions: {e}")
        return None


def test_get_available_sub():
    print("--- Getting Available Subscriptions ---")
    result = get_available_sub()
    if result:
        print(f"Available subscriptions: {result.get('AvailableSub', {})}")


def get_pending_count():
    """Get the number of pending orders. (Auth: RCL_TopLevelCheck)"""
    url = f"{BASE_URL}/v3/pending_count"
    timestamp = get_server_timestamp()
    params = {'timestamp': timestamp}
    query_string = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    signature = hmac.new(
        ROOSTOO_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "RST-API-KEY": ROOSTOO_API_KEY,
        "MSG-SIGNATURE": signature,
    }

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error getting pending order count: {e}")
        print(f"Response text: {e.response.text if e.response else 'N/A'}")
        return None


def test_get_pending_count():
    print("--- Getting Pending Order Count ---")
    result = get_pending_count()
    if result:
        print(f"Pending orders: {result.get('TotalPending', 0)}")