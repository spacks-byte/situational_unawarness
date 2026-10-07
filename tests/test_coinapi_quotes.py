from unittest.mock import Mock

import pytest

from scripts.coinapi_quotes import fetch_quotes


def request(row=None, status=200, text=""):
    session = Mock()
    response = session.get.return_value
    response.status_code, response.text = status, text
    response.json.return_value = [row or dict(
        symbol_id="BINANCE_SPOT_PEPE_USDT", time_exchange="2026-10-04T15:01:00.1234567Z",
        time_coinapi="2026-10-04T15:01:00.1235567Z", bid_price=.00000381, ask_price=.00000382)]
    return session


def fetch(session):
    return fetch_quotes("test-key", "BINANCE_SPOT_PEPE_USDT", "2026-10-04T15:01:00Z",
                        "2026-10-04T15:02:00Z", 1, session)


def test_bid_ask_midpoint_timestamps_and_bounded_request():
    session = request()
    rows, metadata = fetch(session)
    assert rows[0]["midpoint"] == pytest.approx(.000003815)
    assert rows[0]["time_exchange"].endswith("123456700+00:00")
    assert metadata["limit_reached"] and not metadata["full_window_coverage_claimed"]
    kwargs = session.get.call_args.kwargs
    assert kwargs["params"]["limit"] == 1
    assert kwargs["headers"]["X-CoinAPI-Key"] == "test-key"
    assert "test-key" not in session.get.call_args.args[0]


def test_api_error_redacts_key():
    with pytest.raises(ValueError, match=r"HTTP 403: denied \[REDACTED\]"):
        fetch(request(status=403, text="denied test-key"))


def test_request_uses_coinapi_seven_digit_timestamp_precision():
    session = request()
    session.get.return_value.json.return_value = []
    fetch_quotes("test-key", "BINANCE_SPOT_PEPE_USDT", "2026-10-04T15:01:00.000000100Z",
                 "2026-10-04T15:01:00.999999900Z", session=session)
    assert session.get.call_args.kwargs["params"]["time_end"] == "2026-10-04T15:01:00.9999999Z"


@pytest.mark.parametrize("field,value", [("bid_price", .1), ("ask_price", float("nan")),
    ("time_exchange", "2026-10-05T00:00:00Z"), ("symbol_id", "OTHER")])
def test_invalid_data_rejected(field, value):
    session = request()
    session.get.return_value.json.return_value[0][field] = value
    with pytest.raises(ValueError):
        fetch(session)
