# Roostoo API client

`tradebot.exchange.client.RoostooClient` is the only code that talks HTTP to Roostoo. The live engine reaches it through `RoostooExchangePort` (`tradebot.exchange.port`); scripts can use it directly.

```python
from tradebot.exchange import RoostooClient

client = RoostooClient()                       # keys from .env, settings from config/default.yaml defaults
client.ticker("BTC")["Data"]["BTC/USD"]["LastPrice"]
client.place_order("BTC", "BUY", 0.01, price=80000)   # LIMIT (price given)
```

## Methods

| Method | Endpoint | Notes |
|---|---|---|
| `server_time()` | GET `/v3/serverTime` | |
| `exchange_info(refresh=False)` | GET `/v3/exchangeInfo` | Cached for `exchange_info_ttl_seconds` |
| `pair_info(pair)` | (cached exchangeInfo) | `PricePrecision`, `AmountPrecision`, `MiniOrder`, ... |
| `ticker(pair=None)` | GET `/v3/ticker` | All pairs when `pair` is omitted |
| `balance()` | GET `/v3/balance` (signed) | Wallet under `SpotWallet` |
| `pending_count()` | GET `/v3/pending_count` (signed) | |
| `place_order(pair, side, quantity, price=None, order_type=None)` | POST `/v3/place_order` (signed) | LIMIT when `price` is given. Quantity and price are floored to the pair's precision; LIMIT notional must be ≥ `MiniOrder`. |
| `query_order(order_id=None, pair=None, pending_only=None, offset=None, limit=None)` | POST `/v3/query_order` (signed) | `order_id` can't be combined with other filters |
| `cancel_order(order_id=None, pair=None, cancel_all=False)` | POST `/v3/cancel_order` (signed) | Cancelling *every* order requires `cancel_all=True` |
| `open_short(pair, collateral, price=None)` | POST `/v6/short_open` (signed) | 1x; LIMIT when `price` is given |
| `close_short(pair, close_qty=None, close_pct=None)` | POST `/v6/short_close` (signed) | Market only. Pass exactly one of `close_qty` or `close_pct`. |
| `short_positions()` | GET `/v6/short_positions` (signed) | |

Pairs can be given as `"BTC"`, `"BTC/USD"` or `"BTCUSDT"`.

## Behaviour

- **Signing:** HMAC-SHA256 of the alphabetically sorted `key=value&...` payload, signed with `ROOSTOO_API_SECRET` and sent as `MSG-SIGNATURE` together with `RST-API-KEY`. It's implemented once, in `_sign`.
- **Timestamps:** local time corrected by an offset from `/v3/serverTime`. The offset is re-synced every `server_time_resync_seconds`, instead of costing an extra request before every call.
- **Timeouts and throttling:** every request has a timeout (`request_timeout_seconds`), and requests are spaced at least `min_request_interval_seconds` apart.
- **Retries:**
  - Read requests (GET and `query_order`) are retried with backoff on connection errors, HTTP 429 and HTTP 5xx.
  - **Requests that change orders are never retried.** A timeout there means the outcome is unknown, which the engine's intent journal resolves.
- **Errors:** they raise `RoostooError`, which carries `status` and the response `body`. Roostoo also returns `Success: false` with an `ErrMsg` inside an HTTP 200 for business errors (e.g. "no order matched"). Those are returned to the caller and logged as warnings.
- **Logging:** every request is logged via `logging` as method, path, HTTP status, `Success` flag and latency. Order requests also log their parameters.

## Configuration

- **Keys:** in `.env` at the repo root, copied from `.env.example`:

  ```
  ROOSTOO_API_KEY=...
  ROOSTOO_API_SECRET=...
  # BASE_URL=https://mock-api.roostoo.com   (optional; overrides config)
  ```

- **Connection settings:** in the `exchange:` section of `config/default.yaml`.
- **Fees:** in `fees:` in the same file.

## Manual testing

`python -m tradebot api` opens an interactive menu.
- **Options 1–6 are read-only:** server time, exchange info, ticker, balance, portfolio worth, and pending orders plus shorts.
- **Options 7–10 change the account:** limit order, cancel, open short, close short. Each one asks you to type `yes` before sending.
