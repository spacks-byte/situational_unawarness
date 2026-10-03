"""
Live 15m candles for the strategy: a rolling window of closed bars per coin, topped up every poll.

The source is Binance's public market-data API (no key), the same exchange and interval as the
backtest data. Roostoo only supplies prices for orders. Network access goes through an injected
`fetch(coin, start, end)`, so tests and replays use local data.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

import pandas as pd
import requests

from tradebot.core.symbols import to_binance, to_coin

log = logging.getLogger(__name__)

BAR = pd.Timedelta(minutes=15)
COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote"]
BINANCE_DATA_API = "https://data-api.binance.vision"

Fetch = Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame]


def last_closed_bar(now: pd.Timestamp) -> pd.Timestamp:
    """Open time of the newest bar that has closed at `now`."""
    return pd.Timestamp(now).tz_convert("UTC").floor("15min") - BAR


def binance_fetch(session: requests.Session | None = None, timeout: float = 10.0) -> Fetch:
    """Closed 15m klines from Binance's public API, 1000 bars per request."""
    http = session or requests.Session()

    def fetch(coin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        rows, cursor, end_ms = [], int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1
        while cursor <= end_ms:
            response = http.get(f"{BINANCE_DATA_API}/api/v3/klines", timeout=timeout, params={
                "symbol": to_binance(coin), "interval": "15m", "startTime": cursor, "endTime": end_ms, "limit": 1000})
            response.raise_for_status()
            batch = response.json()
            rows += batch
            if len(batch) < 1000:
                break
            cursor = int(batch[-1][0]) + int(BAR.total_seconds() * 1000)
        return klines_to_frame(rows)

    return fetch


def klines_to_frame(rows: list[list]) -> pd.DataFrame:
    """Binance kline arrays -> the loader's DataFrame format (indexed by UTC open time)."""
    names = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
             "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(rows, columns=names)
    index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True).rename("open_time")
    return df[COLUMNS].astype(float).set_index(index)


class BarBuffer:
    """The last `window_days` of closed 15m bars per coin."""

    def __init__(self, coins: Iterable[str], fetch: Fetch, window_days: int = 50) -> None:
        if window_days < 45:
            raise ValueError("window_days must be >= 45: the strategy needs a 30-day beta and 14-day lookback")
        self.coins = [to_coin(c) for c in coins]
        self.fetch = fetch
        self.window = pd.Timedelta(days=window_days)
        self.frames: dict[str, pd.DataFrame] = {}

    def update(self, now: pd.Timestamp) -> None:
        """Fetch every bar closed since each coin's newest one (the whole window the first time)."""
        newest = last_closed_bar(now)
        oldest = newest + BAR - self.window
        for coin in self.coins:
            frame = self.frames.get(coin)
            if frame is not None and len(frame) and frame.index[-1] >= newest:
                continue
            start = frame.index[-1] + BAR if frame is not None and len(frame) else oldest
            new = self._fetch(coin, start, newest + BAR)
            frame = new if frame is None or not len(frame) else pd.concat([frame, new])
            self.frames[coin] = frame[~frame.index.duplicated(keep="last")].loc[oldest:]

    def data(self) -> dict[str, pd.DataFrame]:
        return {c: f for c, f in self.frames.items() if len(f)}

    def last_bar(self) -> pd.Timestamp | None:
        bars = [f.index[-1] for f in self.frames.values() if len(f)]
        return max(bars) if bars else None

    def lagging(self, bar: pd.Timestamp) -> list[str]:
        """Coins without `bar` yet (fetch failed or the feed is late)."""
        return [c for c in self.coins if c not in self.frames or not len(self.frames[c]) or self.frames[c].index[-1] < bar]

    def _fetch(self, coin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        try:
            frame = self.fetch(coin, start, end)
        except Exception as e:                  # a failed fetch must never stop the trading loop
            log.warning("bar fetch failed for %s: %s", coin, e)
            return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC"))
        return frame[(frame.index >= start) & (frame.index < end)][[c for c in COLUMNS if c in frame]]
