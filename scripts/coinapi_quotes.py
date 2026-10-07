"""Small historical Binance PEPE bid/ask probe; one request, no pagination.

python3 scripts/coinapi_quotes.py --start 2026-10-04T15:01:00Z --end 2026-10-04T15:02:00Z
Loads COINAPI_API_KEY from the environment or the repository .env; never logs it.
"""
import argparse
import json
import math
import os
from pathlib import Path

from dotenv import load_dotenv
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://rest.coinapi.io/v1/quotes"


def fetch_quotes(key, symbol, start, end, limit=10, session=None):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tzinfo is None or end.tzinfo is None or not start < end:
        raise ValueError("Use timezone-aware start < end")
    if not 1 <= limit <= 100:
        raise ValueError("This smoke test limits each request to 1–100 quotes")
    if not symbol or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in symbol):
        raise ValueError("Invalid CoinAPI symbol ID")
    # CoinAPI accepts at most seven fractional digits (100 ns), not pandas' nine.
    def api_time(stamp):
        stamp = stamp.tz_convert("UTC")
        return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{stamp.value % 1_000_000_000 // 100:07d}Z"
    params = dict(time_start=api_time(start), time_end=api_time(end), limit=limit)
    response = (session or requests).get(f"{BASE}/{symbol}/history", params=params,
        headers={"X-CoinAPI-Key": key, "Accept": "application/json"}, timeout=30,
        allow_redirects=False)
    if response.status_code != 200:
        detail = response.text.replace(key, "[REDACTED]")[:500]
        raise ValueError(f"CoinAPI HTTP {response.status_code}: {detail}")
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("Expected an array of quotes")
    output, previous = [], None
    for row in rows:
        stamp = pd.Timestamp(row["time_exchange"])
        received = pd.Timestamp(row["time_coinapi"])
        bid, ask = float(row["bid_price"]), float(row["ask_price"])
        if row.get("symbol_id", symbol) != symbol:
            raise ValueError("Unexpected symbol in response")
        if stamp.tzinfo is None or received.tzinfo is None or not start <= stamp <= end:
            raise ValueError("Quote timestamp is missing or outside requested window")
        if previous is not None and stamp < previous:
            raise ValueError("Quotes are not ordered by exchange timestamp")
        if not all(math.isfinite(x) and x > 0 for x in (bid, ask)) or bid > ask:
            raise ValueError("Invalid or crossed bid/ask")
        previous = stamp
        output.append(dict(symbol_id=symbol, time_exchange=stamp.isoformat(),
            time_coinapi=received.isoformat(), bid=bid, ask=ask,
            midpoint=bid + (ask-bid)/2, spread=ask-bid))
    return output, dict(symbol=symbol, **params, rows=len(output),
        limit_reached=len(rows) == limit, full_window_coverage_claimed=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BINANCE_SPOT_PEPE_USDT")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path, default=ROOT / "results/coinapi-pepe-smoke/quotes.json")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    key = os.environ.get("COINAPI_API_KEY", "").strip()
    if not key:
        parser.exit(1, "COINAPI_API_KEY is missing from the environment and repository .env\n")
    try:
        rows, metadata = fetch_quotes(key, args.symbol, args.start, args.end, args.limit)
    except requests.RequestException as exc:
        parser.exit(1, f"CoinAPI network request failed ({type(exc).__name__})\n")
    except (ValueError, KeyError, TypeError) as exc:
        parser.exit(1, str(exc).replace(key, "[REDACTED]") + "\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dict(request=metadata, quotes=rows), indent=2) + "\n")
    print(json.dumps(metadata, indent=2))
    if rows:
        print("First quote:", json.dumps(rows[0]))
    print(f"Saved {args.output}")
    if not rows:
        parser.exit(2, "No quotes returned; access/coverage for this window is not established.\n")


if __name__ == "__main__":
    main()
