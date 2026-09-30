import os
from dotenv import load_dotenv
import requests
import time

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
        coin = f"{coin}/USD"
        ticker_btc = get_ticker(pair=coin)
        if ticker_btc:
            # print(ticker_btc)
            print(f"{coin} Last Price: {ticker_btc.get('Data', {}).get(coin, {}).get('LastPrice')}")