"""
Live candles for backtested strategies: a rolling buffer of closed 15m bars per coin.

The buffer holds, per coin (e.g. "BTC"), a DataFrame indexed by UTC bar *open* time with the same
columns as `tradebot.data.load_klines` (open, high, low, close, volume, quote_volume, trades,
taker_buy_base, taker_buy_quote), so `Strategy.generate_weights(buffer.data())` sees exactly what it
saw in the backtest. Only *closed* bars are kept: a bar opened at t is complete at t + 15m.

Network access is never implicit. `BarBuffer` takes a `fetch(coin, start, end) -> DataFrame`
callable (bars with open_time in [start, end)):
  - `binance_public_fetch`: live, Binance public klines REST (no API key)
  - `parquet_fetch`: the downloaded history (tests, replay simulations)
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

import pandas as pd

from tradebot.core.symbols import to_binance, to_coin
from tradebot.data.binance_vision import klines_path

log = logging.getLogger(__name__)

BAR = pd.Timedelta(minutes=15)
COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume", "trades",
           "taker_buy_base", "taker_buy_quote"]
BINANCE_DATA_API = "https://data-api.binance.vision"   # public market-data mirror, no key needed

FetchFn = Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame]


def _ts(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def last_closed_bar_open(now: datetime | pd.Timestamp) -> pd.Timestamp:
    """Open time of the most recent bar that is fully closed at `now`."""
    return _ts(now).floor("15min") - BAR


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="open_time"))


# --------------------------------------------------------------------------- fetchers
def parquet_fetch(data_dir: str | Path, interval: str = "15m") -> FetchFn:
    """Fetch from downloaded Parquet history (`python -m tradebot data`)."""
    cache: dict[str, pd.DataFrame] = {}

    def fetch(coin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        coin = to_coin(coin)
        if coin not in cache:
            path = klines_path(data_dir, coin, interval)
            cache[coin] = pd.read_parquet(path) if path.exists() else _empty()
        df = cache[coin]
        return df[(df.index >= start) & (df.index < end)]

    return fetch


def binance_public_fetch(base_url: str = BINANCE_DATA_API, session=None, timeout: float = 10.0) -> FetchFn:
    """Live fetch from Binance public klines (`/api/v3/klines`, no credentials), 1000 bars per request."""

    http = session

    def fetch(coin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        nonlocal http
        if http is None:
            import requests  # local import: importing this module never needs network libraries
            http = requests.Session()   # keep-alive: one TLS handshake for the whole bootstrap
        rows: list[list] = []
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000) - 1
        while cursor <= end_ms:
            resp = http.get(f"{base_url}/api/v3/klines", timeout=timeout, params={
                "symbol": to_binance(coin), "interval": "15m", "startTime": cursor, "endTime": end_ms, "limit": 1000})
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            rows.extend(batch)
            cursor = int(batch[-1][0]) + 15 * 60 * 1000
            if len(batch) < 1000:
                break
        return klines_rows_to_frame(rows)

    return fetch


def klines_rows_to_frame(rows: list[list]) -> pd.DataFrame:
    """Binance kline arrays -> DataFrame in the loader's format."""
    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                     "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
    df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True).rename("open_time")
    out = df[COLUMNS].astype(float)
    out["trades"] = out["trades"].astype("int64")
    return out


# --------------------------------------------------------------------------- buffer
class BarBuffer:
    """Rolling window of closed 15m bars per coin, updated incrementally. Fetch errors never raise."""

    def __init__(self, symbols: Iterable[str], fetch: FetchFn, window_days: int = 50) -> None:
        if window_days < 45:
            raise ValueError("window_days must be >= 45 (RXM needs a 30-day beta + 14-day lookback)")
        self.symbols = list(dict.fromkeys(to_coin(s) for s in symbols))
        self.fetch = fetch
        self.window = pd.Timedelta(days=window_days)
        self.frames: dict[str, pd.DataFrame] = {}
        self.last_update: pd.Timestamp | None = None
        self.failed: set[str] = set()       # coins whose last fetch raised

    def bootstrap(self, now: datetime | pd.Timestamp) -> None:
        """Load the full window ending at the last bar closed at `now`."""
        end = last_closed_bar_open(now) + BAR
        start = end - self.window
        for coin in self.symbols:
            self.frames[coin] = self._safe_fetch(coin, start, end)
        self.last_update = _ts(now)
        log.info("candle buffer bootstrapped: %d coins up to %s", len(self.data()), end - BAR)

    def update(self, now: datetime | pd.Timestamp) -> int:
        """Append bars closed since the last update. Returns the number of new bars."""
        if not self.frames:
            self.bootstrap(now)
            return sum(len(df) for df in self.frames.values())
        target_last = last_closed_bar_open(now)
        added = 0
        for coin in self.symbols:
            df = self.frames.get(coin)
            last = df.index[-1] if df is not None and len(df) else None
            if last is not None and last >= target_last:
                continue
            start = (last + BAR) if last is not None else target_last + BAR - self.window
            new = self._safe_fetch(coin, start, target_last + BAR)
            if len(new):
                df = new if df is None or not len(df) else pd.concat([df, new])
                df = df[~df.index.duplicated(keep="last")].sort_index()
                added += len(new)
            if df is not None and len(df):
                df = df[df.index >= target_last + BAR - self.window]
            self.frames[coin] = df
        self.last_update = _ts(now)
        return added

    def _safe_fetch(self, coin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        try:
            df = self.fetch(coin, start, end)
        except Exception as exc:  # network/data errors must never kill the trading loop
            if coin not in self.failed:
                log.warning("candle fetch failed for %s: %s", coin, exc)
            self.failed.add(coin)
            return _empty()
        self.failed.discard(coin)
        if df is None or not len(df):
            return _empty()
        df = df[(df.index >= start) & (df.index < end)]       # drop the still-open bar, if any
        return df[[c for c in COLUMNS if c in df.columns]]

    def data(self) -> dict[str, pd.DataFrame]:
        """{coin: DataFrame} for coins with data: the strategy's input format."""
        return {s: df for s, df in self.frames.items() if df is not None and len(df)}

    def lagging(self, bar: pd.Timestamp) -> list[str]:
        """Coins whose newest bar is older than `bar` (their fetch failed or the feed is late)."""
        return [s for s in self.symbols
                if self.frames.get(s) is None or not len(self.frames[s]) or self.frames[s].index[-1] < bar]

    def last_bar(self) -> pd.Timestamp | None:
        """Open time of the newest bar across coins."""
        lasts = [df.index[-1] for df in self.frames.values() if df is not None and len(df)]
        return max(lasts) if lasts else None
