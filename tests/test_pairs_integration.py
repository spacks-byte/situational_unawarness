from copy import deepcopy

import pandas as pd
import pytest

from tests.test_cointegration import fixture
from tests.test_market_engine import candle
from tradebot.core.clock import SimClock
from tradebot.core.config import Settings
from tradebot.data.engine import MarketDataEngine
from tradebot.data.market import binance_public_fetch, _empty
from tradebot.exchange.replay import ReplayExchangePort
from tradebot.live.account import AccountRunner
from tradebot.strategy.library.cointegration import STEP


def test_30m_stream_is_closed_only_and_independent_of_15m():
    clock = SimClock(pd.Timestamp("2026-10-01T01:15Z").to_pydatetime())
    md = MarketDataEngine(clock, {"15m": lambda *a: _empty(), "30m": lambda *a: _empty()},
                          {("PEPE", "30m"): 86400, ("PEPE", "15m"): 86400}, stream_url="test")
    event = candle(pd.Timestamp("2026-10-01T01:00Z"), closed=True)
    event["k"]["i"] = "30m"
    md.ingest(event)
    read = md.fetch("30m")
    assert read("PEPE", pd.Timestamp("2026-10-01T00:00Z"), pd.Timestamp("2026-10-01T02:00Z")).empty
    clock.advance(900)
    result = read("PEPE", pd.Timestamp("2026-10-01T00:00Z"), pd.Timestamp("2026-10-01T02:00Z"))
    assert len(result) == 1
    assert md.fetch("15m")("PEPE", result.index[0], result.index[0]+STEP).empty
    urls = []
    class Socket:
        def __init__(self, url, **kw):
            urls.append(url)
        def run_forever(self, **kw):
            md._stop.set()
    md.websocket_factory = Socket
    md._stream_loop()
    assert "kline_30m" in urls[0] and "kline_15m" in urls[0] and "bookTicker" not in urls[0]


def test_binance_30m_pagination_has_no_skipped_or_duplicate_bar():
    calls = []
    start = pd.Timestamp("2026-01-01T00:00Z")
    first_ms = int(start.timestamp()*1000)
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            begin = calls[-1]["startTime"]
            count = 1000 if len(calls) == 1 else 1
            return [[begin+i*1_800_000, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 0] for i in range(count)]
    class HTTP:
        def get(self, url, *, params, timeout):
            calls.append(params)
            return Response()
    fetch = binance_public_fetch(session=HTTP(), interval="30m")
    result = fetch("FIL", start, start+1001*STEP)
    assert calls[1]["startTime"] == first_ms+1000*1_800_000
    assert len(result) == 1001 and result.index.is_unique
    assert (result.index.to_series().diff().dropna() == STEP).all()


def test_shared_account_runs_pairs_observation_without_orders_and_restarts(tmp_path):
    config, data, _ = fixture()
    clock = SimClock(config.cycle_start)
    settings = Settings.load("config/market-making.yaml")
    settings.cointegration = config
    settings.live.strategies = ["cointegration-pairs"]
    settings.live.state_dir = str(tmp_path / "shared")
    settings.live.kill_file = str(tmp_path / "KILL")
    settings.market_making.rxm_state_dir = str(tmp_path / "no-legacy")
    # The account still has an inactive MM allocation; provide its marks only.
    bars = dict(data, PEPE=data["FIL"])
    port = ReplayExchangePort(bars, clock, intervals={c: "30m" for c in bars})
    fetch = lambda c, a, b: data[c].loc[(data[c].index >= a) & (data[c].index < b)]
    runner = AccountRunner(settings, mode="simulate", port=port, clock=clock, pairs_fetch=fetch)
    try:
        clock.advance(3600)
        result = runner.run_once()
        assert result["status"] == "OK", result
        runtime = runner.runtimes["cointegration-pairs"]
        assert runtime.pending()
        assert not runtime.ledger.positions()  # no inferred fills in observation mode
        saved = deepcopy(runtime.state)
        assert all(interval == "30m" for _, interval in runner.market_data.windows)
        assert not {"place_order", "open_short", "close_short", "cancel_order"} & set(port.calls)
    finally:
        runner.close()
    runner = AccountRunner(settings, mode="simulate", port=port, clock=clock, pairs_fetch=fetch)
    try:
        assert runner.run_once()["status"] == "OK"
        assert runner.runtimes["cointegration-pairs"].state == saved
        assert not {"place_order", "open_short", "close_short", "cancel_order"} & set(port.calls)
    finally:
        runner.close()
