"""Strategy-neutral market data: one producer, cached candles and one-second quotes.

The live producer runs independently of execution and its API waits. Consumers
read immutable copies of completed candles; they never initiate a live fetch.
Replay advances the same cache on the injected clock using local history sources.
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging
import math
import threading

import numpy as np
import pandas as pd

from tradebot.core.symbols import to_binance, to_coin
from tradebot.data.market import _empty

log = logging.getLogger(__name__)


def tls_options():
    """Certificate bundle for the WebSocket handshake. Verification stays on; certifi only supplies
    the CA store that a bare Windows/Python install often lacks ("unable to get local issuer")."""
    try:
        import certifi
    except ImportError:          # the system store is used, still verified
        return {}
    import ssl
    return {"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": certifi.where()}


INTERVALS = {"1s": pd.Timedelta(seconds=1), "15m": pd.Timedelta(minutes=15),
             "30m": pd.Timedelta(minutes=30)}


class MarketDataEngine:
    def __init__(self, clock, sources, windows, *, stream_url=None, websocket_factory=None, store=None):
        """windows maps (coin, interval) to retained seconds, with no strategy names.

        stream_url=None selects deterministic local-history replay. Live mode uses
        sources for bootstrap/gap repair, and WebSocket streams for updates. stream_url
        may be a list: the producer moves to the next endpoint after a failed connection.
        Only coins with a one-second window get one-second klines and book tickers;
        coarse-only consumers (RXM) receive closed candles from the stream when it is up
        and from the REST repair pass (every 30 s) otherwise, so they never depend on it.
        store (tradebot.data.store.MarketDataStore) keeps a local copy of every closed
        candle received; on start the cache is filled from it first and REST only
        backfills what is missing since.
        """
        self.clock, self.sources = clock, dict(sources)
        self.windows = {(to_coin(c), interval): seconds for (c, interval), seconds in windows.items()}
        if any(i not in INTERVALS or seconds < INTERVALS[i].total_seconds()
               for (_, i), seconds in self.windows.items()):
            raise ValueError("invalid candle interval or retention")
        self.symbols = sorted({c for c, _ in self.windows})
        self.quoted = sorted({c for c, i in self.windows if i == "1s"})
        urls = [stream_url] if isinstance(stream_url, str) else list(stream_url or [])
        self.stream_urls = [u for u in urls if u]
        self.stream_url = self.stream_urls[0] if self.stream_urls else None
        self.websocket_factory = websocket_factory
        self.store = store
        self.frames = {}
        self.books = {}
        self.errors = {}
        self.gaps = set()
        self.connected = False
        self.stream_error = None
        self.published = {"timestamp": None, "quotes": {}}
        self._lock = threading.RLock()
        self._producer_lock = threading.Lock()
        self._stop = threading.Event()
        self._repair = threading.Event()
        self._threads = []
        self._socket = None
        self._subscribers = []
        self._last_advance = None

    def subscribe(self, callback):
        """Receive a snapshot every second, independently of strategy execution."""
        self._subscribers.append(callback)

    def start(self):
        if self.store is not None:
            self.store.start()
            self._load_local()
        self.advance(force=True)
        if self.stream_url:
            if self.websocket_factory is None:
                import websocket
                websocket.setdefaulttimeout(10)  # Bound connection/handshake waits.
                self.websocket_factory = websocket.WebSocketApp
            for name, target in (("market-stream", self._stream_loop),
                                 ("market-publish", self._publish_loop), ("market-repair", self._repair_loop)):
                thread = threading.Thread(name=name, target=target, daemon=True)
                self._threads.append(thread)
                thread.start()

    def close(self):
        self._stop.set()
        self._repair.set()
        if self._socket:
            self._socket.close()
        for thread in self._threads:
            thread.join(timeout=2)
        if self.store is not None:
            self.store.close()

    def _load_local(self):
        """Seed the cache from the local store; REST then fetches only the tail since its last bar."""
        now = pd.Timestamp(self.clock.now()).floor("s")
        for (coin, interval), retention in self.windows.items():
            end = now.floor(INTERVALS[interval])
            start = end-pd.Timedelta(seconds=retention)
            try:
                local = self.store.load(coin, interval, start, end)
                if not len(local):
                    continue
                self._merge(coin, interval, local, persist=False)
                if local.index[0] > start:
                    # The store keeps fewer days than this window (e.g. RXM's 50-day warmup with
                    # 30-day retention): fetch the older head; advance() fetches the tail.
                    head = self.sources[interval](coin, start, local.index[0])
                    if head is not None and len(head):
                        self._merge(coin, interval, head[(head.index >= start) & (head.index < local.index[0])],
                                    persist=False)      # older than the store keeps anyway
            except Exception as exc:      # a bad local file or failed head fetch: full REST bootstrap
                log.warning("local market data %s %s unusable: %s", coin, interval, exc)
                with self._lock:
                    self.frames.pop((coin, interval), None)
                    self.gaps.discard((coin, interval))

    def _merge(self, coin, interval, data, persist=True):
        if data is None or not len(data):
            return
        frame = data.copy()
        if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            raise ValueError("market candles require UTC timestamps")
        frame.index = frame.index.tz_convert("UTC")
        # Venue time can lead local time slightly. Keep the current interval's
        # closed event pending, but never expose it before its local close time.
        # Reject timestamps beyond that interval instead of advancing our cursor.
        frame = frame[frame.index <= pd.Timestamp(self.clock.now()).floor(INTERVALS[interval])]
        if frame.empty:
            return
        prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
        if not np.isfinite(prices).all() or (prices <= 0).any():
            raise ValueError("invalid market candle prices")
        if not (frame.index == frame.index.floor(INTERVALS[interval])).all():
            raise ValueError("candle timestamps are not interval-aligned")
        key = (coin, interval)
        with self._lock:
            old = self.frames.get(key)
            if persist and self.store is not None:
                fresh = frame if old is None else frame[~frame.index.isin(old.index)]
                self.store.submit(coin, interval, fresh)
            if old is not None and len(old):
                frame = pd.concat([old, frame])
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            retention = self.windows.get(key, 120)
            cutoff = pd.Timestamp(self.clock.now())-pd.Timedelta(seconds=retention)
            frame = frame[frame.index >= cutoff.floor(INTERVALS[interval])]
            self.frames[key] = frame
            if len(frame) > 1 and (frame.index.to_series().diff().dropna() != INTERVALS[interval]).any():
                self.gaps.add(key)
            else:
                self.gaps.discard(key)
            self.errors.pop(key, None)

    def advance(self, *, force=False):
        """Replay catch-up or live bootstrap/repair; never interpolate missing bars."""
        now = pd.Timestamp(self.clock.now()).floor("s")
        if not force and now == self._last_advance:
            return
        with self._producer_lock:
            for key, retention in self.windows.items():
                if self._stop.is_set():
                    break
                coin, interval = key
                end = now.floor(INTERVALS[interval])
                with self._lock:
                    frame = self.frames.get(key)
                    closed_count = frame.index.searchsorted(end) if frame is not None else 0
                    last = frame.index[closed_count-1] if closed_count else None
                    has_gap = key in self.gaps
                start = end-pd.Timedelta(seconds=retention) if last is None or has_gap else last+INTERVALS[interval]
                if start >= end:
                    continue
                try:
                    data = self.sources[interval](coin, start, end)
                    if data is not None and len(data):
                        data = data[(data.index >= start) & (data.index < end)]
                    self._merge(coin, interval, data)
                except Exception as exc:
                    with self._lock:
                        self.errors[key] = str(exc)
                    log.warning("market data %s %s: %s", coin, interval, exc)
            self._last_advance = now
        self.publish()

    def fetch(self, interval):
        if interval not in INTERVALS:
            raise ValueError("unsupported interval")

        def read(coin, start, end):
            if not self.stream_url:
                # Local replay can deliver all elapsed one-second observations in
                # a batch when simulated time advances, including execution waits.
                self.advance()
            closed_end = min(pd.Timestamp(end), pd.Timestamp(self.clock.now()).floor(INTERVALS[interval]))
            with self._lock:
                frame = self.frames.get((to_coin(coin), interval))
                if frame is None:
                    return _empty()
                return frame[(frame.index >= start) & (frame.index < closed_end)].copy()
        return read

    def ingest(self, message):
        """Normalize a raw/combined Binance event; ignore unfinished candles."""
        event = json.loads(message) if isinstance(message, str) else message
        event = event.get("data", event)
        coin = to_coin(event.get("s", ""))
        if coin not in self.symbols:
            return
        if "b" in event and "a" in event and "k" not in event:
            bid, ask = float(event["b"]), float(event["a"])
            if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or bid > ask:
                raise ValueError("invalid best bid/ask")
            with self._lock:
                self.books[coin] = {"bid": bid, "ask": ask, "received_at": self.clock.now().isoformat()}
            return
        candle = event.get("k")
        if not candle or not candle.get("x") or candle.get("i") not in INTERVALS:
            return
        ts = pd.Timestamp(int(candle["t"]), unit="ms", tz="UTC")
        interval = candle["i"]
        if (coin, interval) not in self.windows:
            return
        data = pd.DataFrame({"open": [float(candle["o"])], "high": [float(candle["h"])],
                             "low": [float(candle["l"])], "close": [float(candle["c"])],
                             "volume": [float(candle["v"])], "quote_volume": [float(candle["q"])],
                             "trades": [int(candle["n"])], "taker_buy_base": [float(candle["V"])],
                             "taker_buy_quote": [float(candle["Q"])]}, index=pd.DatetimeIndex([ts]))
        self._merge(coin, interval, data)

    def publish(self):
        now = pd.Timestamp(self.clock.now()).floor("s")
        quotes = {}
        with self._lock:
            for coin in self.quoted:
                quote = deepcopy(self.books.get(coin, {}))
                frame = self.frames.get((coin, "1s"))
                if frame is not None and len(frame):
                    closed_count = frame.index.searchsorted(now)
                    if closed_count:
                        ts = frame.index[closed_count-1]
                        quote.update(last=float(frame.iloc[closed_count-1]["close"]), candle_time=ts.isoformat(),
                                     stale=ts < now-pd.Timedelta(seconds=2))
                quote.setdefault("stale", True)
                quote["book_stale"] = (not self.connected or "received_at" not in quote or
                    pd.Timestamp(quote["received_at"]) < now-pd.Timedelta(seconds=2))
                quotes[coin] = quote
            snapshot = {"timestamp": now.isoformat(), "quotes": quotes, "connected": self.connected}
            self.published = snapshot
        for callback in list(self._subscribers):
            try:
                callback(deepcopy(snapshot))
            except Exception:
                log.exception("market-data subscriber failed")
        return deepcopy(snapshot)

    def snapshot(self):
        with self._lock:
            return deepcopy(self.published)

    def status(self):
        with self._lock:
            return {"connected": self.connected, "stream_url": self.stream_url, "stream_error": self.stream_error,
                    "published_at": self.published["timestamp"],
                    "symbols": self.symbols, "errors": {f"{c}/{i}": e for (c, i), e in self.errors.items()},
                    "gaps": [f"{c}/{i}" for c, i in sorted(self.gaps)],
                    "store": self.store.status() if self.store is not None else None}

    def _stream_loop(self):
        streams = []
        for coin in self.symbols:
            symbol = to_binance(coin).lower()
            if (coin, "1s") in self.windows:
                streams.extend([f"{symbol}@kline_1s", f"{symbol}@bookTicker"])
            for interval in INTERVALS:
                if interval != "1s" and (coin, interval) in self.windows:
                    streams.append(f"{symbol}@kline_{interval}")
        endpoint = 0
        def opened(_):
            self.stream_error = None
            self.connected = True
            self._repair.set()
        def failed(_, error):
            self.stream_error = str(error)
            log.warning("market-data stream: %s", error)
        def received(_, message):
            try:
                self.ingest(message)
            except Exception:
                log.exception("invalid market-data event")
        while not self._stop.is_set():
            self.stream_url = self.stream_urls[endpoint % len(self.stream_urls)]
            url = self.stream_url+"?streams="+"/".join(streams)
            was_connected = False
            try:
                self._socket = self.websocket_factory(url, on_open=opened, on_message=received, on_error=failed)
                self._socket.run_forever(ping_interval=20, ping_timeout=10, http_proxy_timeout=10,
                                         sslopt=tls_options())
            except Exception as exc:
                failed(self._socket, exc)
                log.exception("market-data stream disconnected")
            finally:
                was_connected, self.connected = self.connected, False
            if not was_connected:
                endpoint += 1            # never connected here: try the next endpoint
            self._stop.wait(2)

    def _publish_loop(self):
        while not self._stop.is_set():
            self.publish()
            fraction = self.clock.now().timestamp() % 1
            self._stop.wait(max(.01, 1-fraction))

    def _repair_loop(self):
        while not self._stop.is_set():
            self._repair.wait(30)
            self._repair.clear()
            if not self._stop.is_set():
                self.advance(force=True)
