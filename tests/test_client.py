import hashlib
import hmac
import time

import pytest
import requests

from tradebot.core.config import ExchangeSettings
from tradebot.exchange.client import RoostooClient, RoostooError
from tradebot.exchange.port import RoostooExchangePort

EXCHANGE_INFO = {"TradePairs": {"BTC/USD": {"PricePrecision": 2, "AmountPrecision": 5, "MiniOrder": 1}}}


class FakeResponse:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Plays back scripted responses (or exceptions) per path and records every request."""

    def __init__(self, script):
        self.script = {path: list(items) for path, items in script.items()}
        self.calls = []

    def request(self, method, url, headers=None, params=None, data=None, timeout=None):
        path = url.split("roostoo.test", 1)[1]
        self.calls.append({"method": method, "path": path, "headers": headers, "params": params,
                           "data": data, "timeout": timeout})
        item = self.script[path].pop(0) if len(self.script[path]) > 1 else self.script[path][0]
        if isinstance(item, Exception):
            raise item
        return item


def make_client(script, **settings):
    session = FakeSession(script)
    config = ExchangeSettings(base_url="https://roostoo.test", min_request_interval_seconds=0,
                              retry_backoff_seconds=0, **settings)
    client = RoostooClient(api_key="key", api_secret="secret", settings=config, session=session)
    client.base_url = "https://roostoo.test"
    client._time_offset_ms, client._time_synced_at = 0, time.monotonic()  # skip server time sync
    return client, session


def test_signed_request_uses_sorted_payload_hmac_and_timeout():
    client, session = make_client({"/v3/balance": [FakeResponse(body={"Success": True})]})

    client.balance()

    call = session.calls[0]
    query = "&".join(f"{k}={v}" for k, v in sorted(call["params"].items()))
    expected = hmac.new(b"secret", query.encode(), hashlib.sha256).hexdigest()
    assert call["headers"]["MSG-SIGNATURE"] == expected
    assert call["headers"]["RST-API-KEY"] == "key"
    assert call["timeout"] == 10.0


def test_read_requests_retry_but_orders_never_do():
    client, session = make_client({
        "/v3/ticker": [requests.ConnectionError("reset"), FakeResponse(body={"Success": True, "Data": {}})],
        "/v3/exchangeInfo": [FakeResponse(body=EXCHANGE_INFO)],
        "/v3/place_order": [requests.Timeout("slow")],
    })

    assert client.ticker("BTC")["Success"] is True
    assert sum(c["path"] == "/v3/ticker" for c in session.calls) == 2

    with pytest.raises(RoostooError):
        client.place_order("BTC", "BUY", 0.1, price=50000)
    assert sum(c["path"] == "/v3/place_order" for c in session.calls) == 1


def test_http_error_raises_with_response_body():
    client, _ = make_client({"/v3/balance": [FakeResponse(status=400, body=None, text='{"ErrMsg":"bad sig"}')]},
                            max_retries=0)

    with pytest.raises(RoostooError) as err:
        client.balance()
    assert err.value.status == 400
    assert "bad sig" in err.value.body


def test_exchange_info_is_cached_and_orders_are_floored_to_precision():
    client, session = make_client({
        "/v3/exchangeInfo": [FakeResponse(body=EXCHANGE_INFO)],
        "/v3/place_order": [FakeResponse(body={"Success": True, "OrderDetail": {"OrderID": 1}})],
    })

    client.place_order("BTC/USD", "buy", 0.1234567, price=50000.129)
    client.place_order("BTC", "SELL", 0.5, price=50000)

    assert sum(c["path"] == "/v3/exchangeInfo" for c in session.calls) == 1
    first_order = next(c for c in session.calls if c["path"] == "/v3/place_order")["data"]
    assert first_order["quantity"] == "0.12345"
    assert first_order["price"] == "50000.12"
    assert first_order["type"] == "LIMIT" and first_order["side"] == "BUY" and first_order["pair"] == "BTC/USD"


def test_guards_reject_unsafe_or_invalid_requests():
    client, _ = make_client({"/v3/exchangeInfo": [FakeResponse(body=EXCHANGE_INFO)]})

    with pytest.raises(ValueError):
        client.cancel_order()                       # would cancel every order
    with pytest.raises(ValueError):
        client.close_short("BTC", close_qty=1, close_pct=50)
    with pytest.raises(ValueError):
        client.place_order("BTC", "BUY", 0.00001, price=50000)  # below MiniOrder of $1
    with pytest.raises(ValueError):
        client.place_order("BTC", "HOLD", 1, price=1)


def test_port_maps_engine_calls_to_client():
    client, session = make_client({
        "/v3/query_order": [FakeResponse(body={"Success": True, "OrderMatched": []})],
        "/v3/exchangeInfo": [FakeResponse(body=EXCHANGE_INFO)],
        "/v6/short_close": [FakeResponse(body={"Success": True})],
    })
    port = RoostooExchangePort(client)

    assert port.list_open_orders() == {"Success": True, "OrderMatched": []}
    assert port.close_short("BTC", close_qty=0.25) == {"Success": True}
    pending = next(c for c in session.calls if c["path"] == "/v3/query_order")["data"]
    close = next(c for c in session.calls if c["path"] == "/v6/short_close")["data"]
    assert pending["pending_only"] == "TRUE"
    assert close["pair"] == "BTC/USD" and close["close_qty"] == "0.25000"
