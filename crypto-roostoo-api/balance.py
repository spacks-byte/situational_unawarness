import os
from dotenv import load_dotenv
import requests
import time
import hmac
import hashlib
from utilities import get_server_timestamp

# Load environment variables
load_dotenv()

# Get environment variables
ROOSTOO_API_KEY = os.getenv('ROOSTOO_API_KEY')
ROOSTOO_API_SECRET = os.getenv('ROOSTOO_API_SECRET')
BASE_URL = os.getenv('BASE_URL')


def get_balance():
    """Gets account balance. (Auth: RCL_TopLevelCheck)"""
    url = f"{BASE_URL}/v3/balance"

    # === Required parameters ===
    # Use server timestamp to avoid time sync issues
    timestamp = get_server_timestamp()
    params = {"timestamp": timestamp}

    # === Generate signature ===
    # Sort parameters alphabetically by key before concatenating
    query_string = "&".join([f"{key}={value}" for key, value in sorted(params.items())])
    signature = hmac.new(
        ROOSTOO_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    # === Headers ===
    headers = {
        "RST-API-KEY": ROOSTOO_API_KEY,
        "MSG-SIGNATURE": signature
    }
    
    try:
        # === Send GET request ===
        # 2. Send the request
        # In a GET request, the payload is sent as 'params'
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error getting balance: {e}")
        print(f"Response text: {e.response.text if e.response else 'N/A'}")
        return None


def test_get_balance():
    print("--- Getting Balance ---")
    balance = get_balance()
    balance = balance.get('SpotWallet', {})
    for coin in balance:
        print(f"{coin} Free: {balance.get(coin, {}).get('Free')} Locked: {balance.get(coin, {}).get('Lock')}")
    # print(balance)