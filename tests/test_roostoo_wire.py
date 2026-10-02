"""Live path all the way to the HTTP request: strategy -> engine -> RepegPort -> ThrottledPort ->
LibraryExchangePort -> the raw Roostoo client in crypto-roostoo-api/, with `requests` replaced by
a fake Roostoo server (a ReplayExchangePort behind the documented endpoints). Checks the shape of
what we would put on the wire. Nothing here touches the network or reads credentials.
"""
import hashlib
import hmac
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
import requests

from src.engine.app import Engine
from src.engine.clock import SimClock
from src.strategy_bridge import load_competition_config
from src.strategy_bridge.live_strategy import CompetitionStrategy
from src.strategy_bridge.market_data import BarBuffer
from src.strategy_bridge.repeg import RepegPort
from src.strategy_bridge.replay_port import ReplayExchangePort
from src.strategy_bridge.throttle import ThrottledPort
from tests.test_strategy_bridge import _frame_fetch, _universe

SECRET = "test-secret"


class _Response:
    status_code = 200

    def __init__(self, body):
        self._body, self.text = body, str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class FakeRoostoo:
    """Answers the client's HTTP calls from a ReplayExchangePort and records every request."""

    def __init__(self, sim, clock):
        self.sim, self.clock, self.requests = sim, clock, []

    def request(self, method, url, headers=None, data=None, params=None, **_):
        path, p = urlparse(url).path, dict(data or params or {})
        self.requests.append({"method": method, "path": path, "payload": p, "headers": dict(headers or {})})
        s = self.sim
        if path == "/v3/serverTime":
            return _Response({"ServerTime": int(self.clock.now().timestamp() * 1000)})
        if path == "/v3/exchangeInfo":
            return _Response(s.get_exchange_info())
        if path == "/v3/ticker":
            return _Response(s.get_ticker(p.get("pair")))
        if path == "/v3/balance":
            return _Response(s.get_balance())
        if path == "/v3/query_order":
            return _Response(s.query_order(pending_only=p.get("pending_only") == "TRUE"))
        if path == "/v3/cancel_order":
            return _Response(s.cancel_order(order_id=int(p["order_id"])))
        if path == "/v3/place_order":
            return _Response(s.place_order(p["pair"], p["side"], float(p["quantity"]),
                                           float(p["price"]) if "price" in p else None, p["type"]))
        if path == "/v6/short_positions":
            return _Response(s.get_short_positions())
        if path == "/v6/short_open":
            return _Response(s.open_short(p["pair"], float(p["collateral"]), float(p["price"]) if "price" in p else None))
        if path == "/v6/short_close":
            return _Response(s.close_short(p["pair"], close_qty=float(p["close_qty"]) if "close_qty" in p else None,
                                           close_pct=float(p["close_pct"]) if "close_pct" in p else None))
        raise AssertionError(f"unexpected endpoint {path}")


@pytest.fixture
def wire(monkeypatch, tmp_path):
    frames = _universe(n=10, days=70, end="2026-03-01")
    tiny = frames["C9USDT"]                                  # a PEPE/SHIB-sized price (~1e-5)
    frames["C9USDT"] = tiny.assign(**{c: tiny[c] * 1e-7 for c in ("open", "high", "low", "close")})
    clock = SimClock(datetime(2026, 2, 25, 0, 16, tzinfo=UTC))
    server = FakeRoostoo(ReplayExchangePort(frames, clock, initial_usd=100_000), clock)
    monkeypatch.setattr(requests, "request", server.request)
    monkeypatch.setattr(requests, "get", lambda url, **k: server.request("GET", url, **k))
    monkeypatch.setattr(requests, "post", lambda url, **k: server.request("POST", url, **k))

    from src.engine.ports.library_port import LibraryExchangePort   # imports the raw client
    import roostoo_api
    raw = [getattr(roostoo_api, m)._raw for m in ("balance", "shorts", "trades", "utilities")] + [sys.modules["utilities"]]
    for module in raw:                                       # dummy credentials: nothing real is ever read or sent
        monkeypatch.setattr(module, "BASE_URL", "http://roostoo.invalid", raising=False)
        monkeypatch.setattr(module, "ROOSTOO_API_KEY", "test-key", raising=False)
        monkeypatch.setattr(module, "ROOSTOO_API_SECRET", SECRET, raising=False)

    port = RepegPort(ThrottledPort(LibraryExchangePort(), clock, max_per_minute=25), offset_bps=5)
    engine = Engine(port, config=load_competition_config(dry_run=False, live_mode=True),
                    state_path=tmp_path / "e.db", audit_path=tmp_path / "a.jsonl", clock=clock)
    strat = CompetitionStrategy(BarBuffer(list(frames), fetch=_frame_fetch(frames)), mode="comp",
                                state_path=tmp_path / "s.json", clock=clock)
    results = engine.run(strat, max_iterations=3)
    engine.close()
    return server, results


def _plain_number(text):
    return "e" not in text.lower() and float(text) > 0


def test_first_rebalance_reaches_the_exchange(wire):
    server, results = wire
    assert results[0]["status"] == "EXECUTED"
    assert {op["status"] for op in results[0]["operations"]} <= {"SENT", "RESOLVED"}
    paths = [r["path"] for r in server.requests]
    assert "/v3/place_order" in paths and "/v6/short_open" in paths


def test_order_requests_have_the_documented_shape(wire):
    server, _ = wire
    buys = [r["payload"] for r in server.requests if r["path"] == "/v3/place_order"]
    shorts = [r["payload"] for r in server.requests if r["path"] == "/v6/short_open"]
    assert len(buys) == 3 and len(shorts) == 3               # comp preset: 3 names per side
    for p in buys:
        assert set(p) == {"pair", "side", "type", "quantity", "price", "timestamp"}
        assert p["pair"].endswith("/USD") and p["side"] == "BUY" and p["type"] == "LIMIT"
        assert _plain_number(p["quantity"]) and _plain_number(p["price"]), p
    for p in shorts:
        assert set(p) == {"pair", "collateral", "order_type", "price", "timestamp"}
        assert p["pair"].endswith("/USD") and p["order_type"] == "LIMIT"
        assert _plain_number(p["collateral"]) and _plain_number(p["price"]), p
    assert sum(float(p["quantity"]) * float(p["price"]) for p in buys) + sum(float(p["collateral"]) for p in shorts) \
        == pytest.approx(98_000, rel=0.01)                   # the 0.98 gross cap of a $100k account


def test_signed_requests_carry_a_valid_signature(wire):
    server, _ = wire
    signed = [r for r in server.requests if "MSG-SIGNATURE" in r["headers"]]
    assert {"/v3/balance", "/v3/place_order", "/v6/short_open", "/v3/query_order"} <= {r["path"] for r in signed}
    for r in signed:
        query = "&".join(f"{k}={v}" for k, v in sorted(r["payload"].items()))
        assert r["headers"]["MSG-SIGNATURE"] == hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        assert r["headers"]["RST-API-KEY"] == "test-key"
