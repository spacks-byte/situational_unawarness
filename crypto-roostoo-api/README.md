# crypto-roostoo-api

Python client for the Roostoo simulated crypto exchange API. Used by `live_bot.py` to place real (or dry-run) trades during the hackathon competition.

## Core Logic

### `utilities.py` — Server Info & Ticker

- `get_server_timestamp()` — Fetches server time to avoid clock sync issues with HMAC signatures.
- `check_server_time()` — Returns the raw server time response.
- `get_exchange_info()` — Returns exchange metadata (available pairs, initial wallet, running status).
- `get_trade_pair_info(pair)` — Returns exchange rules for one pair.
- `get_amount_precision(pair)` — Returns the pair's exchange-defined quantity precision.
- `get_mini_order(pair)` — Returns the pair's minimum order notional.
- `get_ticker(pair=None)` — Fetches current market prices. Pass a pair like `"BTC/USD"` for a specific coin, or `None` for all.
- `get_pending_count()` — Returns the number of pending orders.
- `get_available_sub()` — Returns legacy WebSocket subscription capabilities when supported by the server.

### `balance.py` — Account Balance

- `get_balance()` — Returns the full spot wallet (free + locked balances per coin). Requires HMAC authentication.

### `trades.py` — Order Management

- `place_order(pair_or_coin, side, quantity, price=None, order_type=None)` — Place MARKET or LIMIT orders. Auto-detects order type if not specified.
- `query_order(order_id=None, pair=None, pending_only=None, offset=None, limit=None)` — Query existing orders with optional pagination.
- `cancel_order(order_id=None, pair=None)` — Cancel orders by ID, pair, or all.

### `shorts.py` — Short Positions

- `open_short(pair_or_coin, collateral, price=None)` — Open or add to a market or limit short.
- `close_short(pair_or_coin, close_qty=None, close_pct=None)` — Partially or fully close a short.
- `get_short_positions()` — Get open shorts and live unrealized P&L.

Short requests use the documented `/v6` endpoints and the same HMAC authentication as spot trading.
Quantities and limit prices are floored using `AmountPrecision` and `PricePrecision` from `exchangeInfo`.

### `portfolio_worth.py` — Portfolio Valuation

- `get_portfolio_worth(include_zero_balances=False)` — Computes current USD value of all holdings by fetching live balances and prices. Returns per-asset breakdown and total.

### `purchase_by_value.py` — Buy by USD Amount

- `buy_coin_by_value(pair_or_coin, usd_value, max_attempts=16)` — Buys a coin targeting a specific USD notional. Automatically retries with progressively rounded quantities to handle step-size constraints.

### `manual_api_test.py` — Interactive Test Menu

Interactive CLI menu for manually testing all API endpoints.

## Environment Variables

Create a `.env` file in this directory (copy from `.env.example`):

```
ROOSTOO_API_KEY=your_roostoo_api_key_here
ROOSTOO_API_SECRET=your_roostoo_api_secret_here
BASE_URL=https://api.roostoo.com
```

| Variable | Required | Description |
|----------|----------|-------------|
| `ROOSTOO_API_KEY` | Yes | Your Roostoo API key |
| `ROOSTOO_API_SECRET` | Yes | Your Roostoo API secret (used for HMAC-SHA256 signing) |
| `BASE_URL` | Yes | API base URL (`https://api.roostoo.com`) |

## Authentication

Authenticated endpoints use HMAC-SHA256 signatures:
1. Sort all request parameters alphabetically by key
2. Concatenate as `key=value&key=value`
3. Sign with `ROOSTOO_API_SECRET` using SHA-256
4. Send in the `MSG-SIGNATURE` header alongside `RST-API-KEY`

## How to Run

**Interactive API test menu:**

```bash
cd crypto-roostoo-api
python manual_api_test.py
```

**Individual modules** (each has a `test_*` function at the bottom):

```bash
cd crypto-roostoo-api
python balance.py
python portfolio_worth.py
python purchase_by_value.py
```

> Note: This package is not meant to be run standalone in production. It is imported by `live_bot.py` in the parent directory.
