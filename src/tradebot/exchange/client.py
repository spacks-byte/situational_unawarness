"""
Roostoo REST client: the only module that talks HTTP to the exchange.

- One requests.Session; every call has a timeout.
- One signing helper: HMAC-SHA256 over the alphabetically sorted `k=v&k=v` payload,
  sent as RST-API-KEY / MSG-SIGNATURE headers.
- Server time is synced once and refreshed periodically instead of before every call.
- exchangeInfo is cached (TTL), so pair rules (precision, MiniOrder) cost no extra requests.
- A minimum interval between requests keeps the bot far from rate limits.
- Idempotent requests are retried with backoff. Order-changing requests are NEVER retried
  automatically: a timeout there means "unknown outcome", which the engine resolves itself.
- Every request is logged (endpoint, HTTP status, Success flag, latency).
Errors raise RoostooError (with the response body) instead of printing and returning None.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Optional

import requests
from dotenv import find_dotenv, load_dotenv

from tradebot.core.config import ExchangeSettings
from tradebot.core.symbols import to_pair

logger = logging.getLogger(__name__)

API_KEY_ENV = "ROOSTOO_API_KEY"
API_SECRET_ENV = "ROOSTOO_API_SECRET"
BASE_URL_ENV = "BASE_URL"


class RoostooError(Exception):
    """Transport failure, HTTP error or unparseable response from Roostoo."""

    def __init__(self, message: str, status: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _floor(value: Any, decimals: int) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise ValueError(f"not a number: {value!r}") from e
    if not number.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return number.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def _fmt(value: Decimal | float) -> str:
    return format(Decimal(str(value)), "f")


class RoostooClient:
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None,
                 settings: Optional[ExchangeSettings] = None,
                 session: Optional[requests.Session] = None) -> None:
        load_dotenv(find_dotenv(usecwd=True))  # .env in the working directory or a parent
        self.settings = settings or ExchangeSettings()
        self.api_key = api_key or os.environ.get(API_KEY_ENV)
        self.api_secret = api_secret or os.environ.get(API_SECRET_ENV)
        self.base_url = (os.environ.get(BASE_URL_ENV) or self.settings.base_url).rstrip("/")
        self.session = session or requests.Session()
        self._last_request = 0.0
        self._time_offset_ms: Optional[int] = None
        self._time_synced_at = 0.0
        self._exchange_info: Optional[dict] = None
        self._exchange_info_at = 0.0

    # ------------------------------------------------------------------ transport

    def _throttle(self) -> None:
        wait = self.settings.min_request_interval_seconds - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _sign(self, payload: dict[str, Any]) -> dict[str, str]:
        if not self.api_key or not self.api_secret:
            raise RoostooError(f"Missing credentials: set {API_KEY_ENV} and {API_SECRET_ENV} in .env")
        query = "&".join(f"{k}={v}" for k, v in sorted(payload.items()))
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return {"RST-API-KEY": self.api_key, "MSG-SIGNATURE": signature}

    def _request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None, *,
                 signed: bool = False, timestamp: bool = True, retry: bool = True) -> dict:
        payload = {k: v for k, v in (payload or {}).items() if v is not None}
        attempts = 1 + (self.settings.max_retries if retry else 0)
        url = f"{self.base_url}{path}"

        for attempt in range(1, attempts + 1):
            if timestamp:
                payload["timestamp"] = self.timestamp_ms()
            headers = self._sign(payload) if signed else {}
            if method == "POST":
                headers["Content-Type"] = "application/x-www-form-urlencoded"

            self._throttle()
            started = time.monotonic()
            try:
                response = self.session.request(
                    method, url, headers=headers,
                    params=payload if method == "GET" else None,
                    data=payload if method == "POST" else None,
                    timeout=self.settings.request_timeout_seconds,
                )
            except requests.RequestException as e:
                logger.warning("%s %s failed (attempt %d/%d): %s", method, path, attempt, attempts, e)
                if attempt < attempts:
                    time.sleep(self.settings.retry_backoff_seconds * attempt)
                    continue
                raise RoostooError(f"{method} {path}: {e}") from e

            latency_ms = (time.monotonic() - started) * 1000
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < attempts:
                logger.warning("%s %s -> HTTP %d (attempt %d/%d), retrying",
                               method, path, response.status_code, attempt, attempts)
                time.sleep(self.settings.retry_backoff_seconds * attempt)
                continue
            if response.status_code >= 400:
                logger.error("%s %s -> HTTP %d in %.0fms: %s",
                             method, path, response.status_code, latency_ms, response.text[:500])
                raise RoostooError(f"{method} {path}: HTTP {response.status_code}",
                                   status=response.status_code, body=response.text)
            try:
                body = response.json()
            except ValueError as e:
                raise RoostooError(f"{method} {path}: response is not JSON",
                                   status=response.status_code, body=response.text) from e

            success = body.get("Success", True) if isinstance(body, dict) else True
            log = logger.info if success else logger.warning
            log("%s %s -> %d success=%s in %.0fms%s", method, path, response.status_code, success,
                latency_ms, "" if success else f" err={body.get('ErrMsg')!r}")
            return body
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ time

    def server_time(self) -> dict:
        return self._request("GET", "/v3/serverTime", timestamp=False)

    def timestamp_ms(self) -> str:
        """Local time corrected by the server offset, re-synced every server_time_resync_seconds."""
        now = time.monotonic()
        if self._time_offset_ms is None or now - self._time_synced_at > self.settings.server_time_resync_seconds:
            local_before = int(time.time() * 1000)
            try:
                server = int(self.server_time()["ServerTime"])
                self._time_offset_ms = server - local_before
            except (RoostooError, KeyError, TypeError, ValueError) as e:
                logger.warning("Server time sync failed, using local clock: %s", e)
                self._time_offset_ms = self._time_offset_ms or 0
            self._time_synced_at = now
        return str(int(time.time() * 1000) + self._time_offset_ms)

    # ------------------------------------------------------------------ market data

    def exchange_info(self, refresh: bool = False) -> dict:
        age = time.monotonic() - self._exchange_info_at
        if refresh or self._exchange_info is None or age > self.settings.exchange_info_ttl_seconds:
            self._exchange_info = self._request("GET", "/v3/exchangeInfo", timestamp=False)
            self._exchange_info_at = time.monotonic()
        return self._exchange_info

    def pair_info(self, pair: str) -> dict:
        """Exchange rules for one pair (precision, MiniOrder, ...); {} if unknown."""
        info = self.exchange_info().get("TradePairs", {})
        return info.get(to_pair(pair), {}) if isinstance(info, dict) else {}

    def ticker(self, pair: Optional[str] = None) -> dict:
        return self._request("GET", "/v3/ticker", {"pair": to_pair(pair) if pair else None})

    # ------------------------------------------------------------------ account

    def balance(self) -> dict:
        return self._request("GET", "/v3/balance", signed=True)

    def pending_count(self) -> dict:
        return self._request("GET", "/v3/pending_count", signed=True)

    # ------------------------------------------------------------------ spot orders

    def _precision(self, pair: str, field: str) -> Optional[int]:
        try:
            value = int(self.pair_info(pair).get(field))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def place_order(self, pair: str, side: str, quantity: float, price: Optional[float] = None,
                    order_type: Optional[str] = None) -> dict:
        """Place a spot LIMIT (price given) or MARKET order. Quantity/price are floored to pair precision."""
        pair = to_pair(pair)
        side = side.upper()
        order_type = (order_type or ("LIMIT" if price is not None else "MARKET")).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if order_type not in {"LIMIT", "MARKET"}:
            raise ValueError("order_type must be LIMIT or MARKET")
        if order_type == "LIMIT" and price is None:
            raise ValueError("LIMIT orders require a price")

        amount_precision = self._precision(pair, "AmountPrecision")
        qty = _floor(quantity, amount_precision) if amount_precision is not None else Decimal(str(quantity))
        if qty <= 0:
            raise ValueError(f"quantity {quantity} is below the {pair} amount precision")

        payload: dict[str, Any] = {"pair": pair, "side": side, "type": order_type, "quantity": _fmt(qty)}
        if order_type == "LIMIT":
            price_precision = self._precision(pair, "PricePrecision")
            px = _floor(price, price_precision) if price_precision is not None else Decimal(str(price))
            if px <= 0:
                raise ValueError(f"price {price} is below the {pair} price precision")
            minimum = self.pair_info(pair).get("MiniOrder")
            if minimum is not None and qty * px < Decimal(str(minimum)):
                raise ValueError(f"order notional {qty * px} is below the {pair} minimum of {minimum}")
            payload["price"] = _fmt(px)

        logger.info("place_order %s %s %s qty=%s price=%s", pair, side, order_type, payload["quantity"],
                    payload.get("price", "market"))
        return self._request("POST", "/v3/place_order", payload, signed=True, retry=False)

    def query_order(self, order_id: Optional[int | str] = None, pair: Optional[str] = None,
                    pending_only: Optional[bool] = None, offset: Optional[int] = None,
                    limit: Optional[int] = None) -> dict:
        payload: dict[str, Any] = {}
        if order_id is not None:
            payload["order_id"] = str(order_id)  # the API rejects order_id combined with other filters
        else:
            payload["pair"] = to_pair(pair) if pair else None
            if pending_only is not None:
                payload["pending_only"] = "TRUE" if pending_only else "FALSE"
            payload["offset"] = str(offset) if offset is not None else None
            payload["limit"] = str(limit) if limit is not None else None
        return self._request("POST", "/v3/query_order", payload, signed=True)  # read-only: safe to retry

    def cancel_order(self, order_id: Optional[int | str] = None, pair: Optional[str] = None,
                     cancel_all: bool = False) -> dict:
        """Cancel one order, all orders on a pair, or (only with cancel_all=True) every order."""
        if order_id is None and pair is None and not cancel_all:
            raise ValueError("pass order_id or pair, or cancel_all=True to cancel every order")
        payload = {"order_id": str(order_id)} if order_id is not None else {"pair": to_pair(pair) if pair else None}
        return self._request("POST", "/v3/cancel_order", payload, signed=True, retry=False)

    # ------------------------------------------------------------------ shorts (/v6)

    def open_short(self, pair: str, collateral: float, price: Optional[float] = None) -> dict:
        """Open or add to a 1x short with USD collateral; LIMIT when a price is given, else MARKET."""
        pair = to_pair(pair)
        collateral_value = Decimal(str(collateral))
        if not collateral_value.is_finite() or collateral_value < 1:
            raise ValueError("collateral must be at least 1")
        payload: dict[str, Any] = {"pair": pair, "collateral": _fmt(collateral_value)}
        if price is not None:
            price_precision = self._precision(pair, "PricePrecision")
            px = _floor(price, price_precision) if price_precision is not None else Decimal(str(price))
            if px <= 0:
                raise ValueError("price must be greater than 0")
            payload.update(order_type="LIMIT", price=_fmt(px))
        logger.info("open_short %s collateral=%s price=%s", pair, payload["collateral"], payload.get("price", "market"))
        return self._request("POST", "/v6/short_open", payload, signed=True, retry=False)

    def close_short(self, pair: str, close_qty: Optional[float] = None, close_pct: Optional[float] = None) -> dict:
        """Close part or all of a short at market (the API takes no price). Pass close_qty or close_pct."""
        if (close_qty is None) == (close_pct is None):
            raise ValueError("pass exactly one of close_qty or close_pct")
        pair = to_pair(pair)
        payload: dict[str, Any] = {"pair": pair}
        if close_qty is not None:
            precision = self._precision(pair, "AmountPrecision")
            qty = _floor(close_qty, precision) if precision is not None else Decimal(str(close_qty))
            if qty <= 0:
                raise ValueError("close_qty is below the pair amount precision")
            payload["close_qty"] = _fmt(qty)
        else:
            pct = Decimal(str(close_pct))
            if not 0 < pct <= 100:
                raise ValueError("close_pct must be in (0, 100]")
            payload["close_pct"] = _fmt(pct)
        logger.info("close_short %s %s", pair, {k: v for k, v in payload.items() if k != "pair"})
        return self._request("POST", "/v6/short_close", payload, signed=True, retry=False)

    def short_positions(self) -> dict:
        return self._request("GET", "/v6/short_positions", signed=True)
