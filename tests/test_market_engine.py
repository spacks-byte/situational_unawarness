from datetime import UTC, datetime
import threading

import pandas as pd

from tradebot.core.clock import RealClock, SimClock
from tradebot.data.engine import MarketDataEngine
from tradebot.data.market import _empty


def candle(ts, *, interval="1s", closed=True, price=10):
    return {"s": "PEPEUSDT", "k": {"x": closed, "i": interval, "t": int(pd.Timestamp(ts).timestamp()*1000),
            "o": price, "h": price, "l": price, "c": price, "v": 1, "q": price, "n": 1, "V": 0, "Q": 0}}


def history(coin, start, end):
    index = pd.date_range(start, end, inclusive="left", freq="s")
    return pd.DataFrame({"open": 10., "high": 10., "low": 10., "close": 10.}, index=index)


def test_shared_views_do_not_fetch_live_or_mutate_producer():
    clock = SimClock(datetime(2026, 1, 1, 1, tzinfo=UTC))
    calls = []
    def source(c, a, b):
        calls.append((c, a, b))
        return history(c, a, b)
    md = MarketDataEngine(clock, {"1s": source}, {("PEPE", "1s"): 3600}, stream_url="unused")
    md.advance()
    first, second = md.fetch("1s"), md.fetch("1s")
    start, end = pd.Timestamp(clock.now())-pd.Timedelta(seconds=10), pd.Timestamp(clock.now())
    a = first("PEPE", start, end)
    a.loc[:, "close"] = 500
    assert second("PEPE", start, end).close.eq(10).all()
    assert len(calls) == 1
    md.ingest(candle(end, closed=False))
    md.ingest(candle(end+pd.Timedelta(seconds=10)))
    assert first("PEPE", start, end+pd.Timedelta(days=1)).index[-1] == end-pd.Timedelta(seconds=1)
    assert len(calls) == 1


def test_gap_repair_empty_history_staleness_and_source_failures():
    clock = SimClock(datetime(2026, 1, 1, 1, tzinfo=UTC))
    end = pd.Timestamp(clock.now())
    broken = True
    def source(c, a, b):
        if c == "BONK":
            raise ConnectionError("offline")
        df = history(c, a, b)
        return df.drop(end-pd.Timedelta(seconds=2), errors="ignore") if broken else df
    md = MarketDataEngine(clock, {"1s": source}, {("PEPE", "1s"): 10, ("BONK", "1s"): 10})
    md.start()
    assert md.status()["gaps"] == ["PEPE/1s"]
    assert "BONK/1s" in md.status()["errors"]
    assert len(md.fetch("1s")("PEPE", end-pd.Timedelta(seconds=10), end)) == 9
    broken = False
    md.advance(force=True)
    assert md.status()["gaps"] == []
    assert len(md.fetch("1s")("PEPE", end-pd.Timedelta(seconds=10), end)) == 10
    md.ingest({"s": "PEPEUSDT", "b": "9", "a": "11"})
    md.connected = True
    assert not md.publish()["quotes"]["PEPE"]["book_stale"]
    clock.advance(5)
    quote = md.publish()["quotes"]["PEPE"]
    assert quote["stale"] and quote["book_stale"]
    md.connected = False
    assert md.publish()["quotes"]["PEPE"]["book_stale"]


def test_replay_catchup_is_causal_for_both_intervals():
    clock = SimClock(datetime(2026, 1, 1, 1, tzinfo=UTC))
    def coarse(c, a, b):
        return history(c, a, b).resample("15min").first()
    md = MarketDataEngine(clock, {"1s": history, "15m": coarse},
                          {("PEPE", "1s"): 30, ("PEPE", "15m"): 3600})
    md.start()
    now = pd.Timestamp(clock.now())
    assert md.fetch("15m")("PEPE", now-pd.Timedelta(hours=1), now).index[-1] == now-pd.Timedelta(minutes=15)
    clock.advance(5)
    assert md.fetch("1s")("PEPE", now, pd.Timestamp(clock.now())).index.tolist() == list(pd.date_range(now, periods=5, freq="s"))


def test_live_publishes_each_second_while_history_repair_is_blocked():
    clock = RealClock()
    repair_started, release, three_updates = threading.Event(), threading.Event(), threading.Event()
    stream_started = threading.Event()
    def source(c, a, b):
        if stream_started.is_set():
            repair_started.set()
            assert release.wait(5)
        return _empty()
    sockets = []
    class Socket:
        def __init__(self, url, on_open, on_message, on_error):
            self.url, self.opened, self.message = url, on_open, on_message
            self.stopped = threading.Event()
            sockets.append(self)
        def run_forever(self, **kwargs):
            stream_started.set()
            self.opened(self)
            now = pd.Timestamp(clock.now()).floor("s")
            self.message(self, candle(now-pd.Timedelta(seconds=1)))
            self.stopped.wait(5)
        def close(self):
            self.stopped.set()
    md = MarketDataEngine(clock, {"1s": source, "15m": source},
                          {("PEPE", "1s"): 3600, ("PEPE", "15m"): 3600},
                          stream_url="wss://test/stream", websocket_factory=Socket)
    ticks = set()
    def subscriber(snapshot):
        if repair_started.is_set():
            ticks.add(snapshot["timestamp"])
            if len(ticks) >= 3:
                three_updates.set()
    md.subscribe(subscriber)
    md.start()
    try:
        assert repair_started.wait(2)
        assert three_updates.wait(4)
        assert md.fetch("1s")("PEPE", pd.Timestamp(clock.now())-pd.Timedelta(seconds=10), pd.Timestamp(clock.now())).shape[0] == 1
        assert sockets[0].url.count("pepeusdt@kline_1s") == 1
        assert "pepeusdt@kline_15m" in sockets[0].url
    finally:
        release.set()
        md.close()
    assert not any(t.is_alive() for t in md._threads)


def test_disconnect_reconnect_repairs_history_and_recovers_status():
    clock = RealClock()
    connected, repair = threading.Event(), threading.Event()
    attempts = []
    def source(*args):
        if connected.is_set():
            repair.set()
        return _empty()
    class Socket:
        def __init__(self, url, on_open, on_message, on_error):
            self.opened, self.error = on_open, on_error
            self.stopped = threading.Event()
            attempts.append(self)
        def run_forever(self, **kwargs):
            if len(attempts) == 1:
                self.error(self, ConnectionError("disconnected"))
                return
            self.opened(self)
            connected.set()
            self.stopped.wait(5)
        def close(self):
            self.stopped.set()
    md = MarketDataEngine(clock, {"1s": source}, {("PEPE", "1s"): 120},
                          stream_url="wss://test/stream", websocket_factory=Socket)
    try:
        md.start()
        assert connected.wait(4)
        md._repair.set()
        assert repair.wait(1)
        assert len(attempts) == 2
        assert md.status()["connected"]
        assert md.status()["stream_error"] is None
    finally:
        md.close()


def test_slightly_early_closed_event_waits_for_local_close_time():
    clock = SimClock(datetime(2026, 1, 1, 1, 0, 0, 500000, tzinfo=UTC))
    md = MarketDataEngine(clock, {"1s": lambda *a: _empty()}, {("PEPE", "1s"): 120}, stream_url="unused")
    ts = pd.Timestamp(clock.now()).floor("s")
    md.ingest(candle(ts, closed=True))
    read = md.fetch("1s")
    assert read("PEPE", ts, ts+pd.Timedelta(seconds=10)).empty
    assert md.publish()["quotes"]["PEPE"]["stale"]
    clock.advance(.5)
    assert read("PEPE", ts, pd.Timestamp(clock.now())).close.tolist() == [10.]
    assert not md.publish()["quotes"]["PEPE"]["stale"]


def bars15(coin, start, end):
    index = pd.date_range(pd.Timestamp(start).ceil("15min"), end, inclusive="left", freq="15min")
    return pd.DataFrame({"open": 10., "high": 10., "low": 10., "close": 10.}, index=index)


def test_coarse_only_coins_get_no_one_second_streams():
    clock = SimClock(datetime(2026, 1, 1, 1, tzinfo=UTC))
    md = MarketDataEngine(clock, {"1s": history, "15m": bars15},
                          {("PEPE", "1s"): 120, ("BTC", "15m"): 86400}, stream_url="wss://test/stream")
    sockets = []
    class Socket:
        def __init__(self, url, **callbacks):
            self.url = url
            sockets.append(self)
        def run_forever(self, **kwargs):
            md._stop.set()
        def close(self):
            pass
    md.websocket_factory = Socket
    md._stream_loop()
    url = sockets[0].url
    assert "pepeusdt@kline_1s" in url and "pepeusdt@bookTicker" in url
    assert "btcusdt@kline_15m" in url and "btcusdt@kline_1s" not in url and "btcusdt@bookTicker" not in url
    md.advance(force=True)
    assert set(md.publish()["quotes"]) == {"PEPE"}           # no quote snapshots for RXM-only coins


def test_stream_verifies_tls_and_falls_back_to_the_next_endpoint():
    clock = RealClock()
    attempts = []
    connected = threading.Event()
    class Socket:
        def __init__(self, url, on_open, on_message, on_error):
            self.url, self.opened, self.error = url, on_open, on_error
            self.stopped = threading.Event()
            attempts.append(self)
        def run_forever(self, **kwargs):
            self.kwargs = kwargs
            if "primary" in self.url:
                self.error(self, ConnectionError("handshake failed"))
                return
            self.opened(self)
            connected.set()
            self.stopped.wait(5)
        def close(self):
            self.stopped.set()
    md = MarketDataEngine(clock, {"1s": lambda *a: _empty()}, {("PEPE", "1s"): 120},
                          stream_url=["wss://primary/stream", "wss://fallback/stream"], websocket_factory=Socket)
    try:
        md.start()
        assert connected.wait(4)
        assert [a.url.split("?")[0] for a in attempts] == ["wss://primary/stream", "wss://fallback/stream"]
        sslopt = attempts[0].kwargs["sslopt"]
        assert sslopt.get("cert_reqs", 2) == 2                # ssl.CERT_REQUIRED: never disabled
        assert sslopt.get("ca_certs") and md.status()["stream_url"] == "wss://fallback/stream"
    finally:
        md.close()


def test_rxm_candles_keep_arriving_over_rest_while_the_stream_is_down():
    clock = RealClock()
    calls = []
    def source(coin, start, end):
        calls.append((coin, start, end))
        return bars15(coin, start, end)
    class DeadSocket:
        def __init__(self, url, on_open, on_message, on_error):
            self.error = on_error
        def run_forever(self, **kwargs):
            self.error(self, ConnectionError("stream unreachable"))
        def close(self):
            pass
    md = MarketDataEngine(clock, {"15m": source}, {("BTC", "15m"): 86400},
                          stream_url=["wss://a/stream", "wss://b/stream"], websocket_factory=DeadSocket)
    try:
        md.start()
        now = pd.Timestamp(clock.now())
        bars = md.fetch("15m")("BTC", now-pd.Timedelta(hours=6), now)
        assert len(bars) >= 23 and bars.index[-1] == now.floor("15min")-pd.Timedelta(minutes=15)
        assert not md.status()["connected"] and md.status()["stream_error"]
        assert calls and not md.status()["errors"]          # served by the REST source alone
    finally:
        md.close()
