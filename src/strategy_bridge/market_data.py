"""Rolling 15m bar buffer that feeds the backtested strategy in live/sim runs.

The buffer holds, per Binance symbol (e.g. ``BTCUSDT``), a DataFrame indexed by UTC bar *open*
time with the same columns as ``data_pipeline.loader.load_klines`` (open, high, low, close,
volume, quote_volume, trades, taker_buy_base, taker_buy_quote). Only *closed* bars are kept:
a bar opened at ``t`` is complete at ``t + 15m``.

Network access is never done implicitly. ``BarBuffer`` takes a ``fetch`` callable
``fetch(binance_symbol, start, end) -> DataFrame`` (bars with open_time in ``[start, end)``);
``binance_public_fetch`` is the live implementation, tests and the mock demo inject a local one.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

BAR = pd.Timedelta(minutes=15)
COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume", "trades",
           "taker_buy_base", "taker_buy_quote"]
DEFAULT_KLINES_DIR = Path(__file__).resolve().parents[2] / "data" / "binance" / "klines" / "15m"
BINANCE_DATA_API = "https://data-api.binance.vision"   # public market-data mirror, no key needed

FetchFn = Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame]


# --------------------------------------------------------------------------- symbol mapping
def roostoo_to_binance(pair_or_coin: str) -> str:
    """'BTC/USD' or 'BTC' -> 'BTCUSDT'."""
    coin = str(pair_or_coin).strip().upper()
    if coin.endswith("USDT") and "/" not in coin:
        return coin
    return coin.split("/", 1)[0] + "USDT"


def binance_to_roostoo(symbol: str) -> str:
    """'BTCUSDT' -> 'BTC/USD'."""
    return f"{binance_to_coin(symbol)}/USD"


def binance_to_coin(symbol: str) -> str:
    """'BTCUSDT' -> 'BTC' (the engine's normalized symbol)."""
    s = str(symbol).strip().upper()
    return s[:-4] if s.endswith("USDT") else s.split("/", 1)[0]


def _ts(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def last_closed_bar_open(now: datetime | pd.Timestamp) -> pd.Timestamp:
    """Open time of the most recent bar that is fully closed at ``now``."""
    return _ts(now).floor("15min") - BAR


# --------------------------------------------------------------------------- fetchers
def parquet_fetch(klines_dir: str | Path = DEFAULT_KLINES_DIR) -> FetchFn:
    """Fetch function backed by the local parquet files (used by the mock demo and tests)."""
    cache: dict[str, pd.DataFrame] = {}
    base = Path(klines_dir)

    def fetch(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        if symbol not in cache:
            path = base / f"{symbol}.parquet"
            cache[symbol] = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
        df = cache[symbol]
        return df[(df.index >= start) & (df.index < end)]

    return fetch


def binance_public_fetch(base_url: str = BINANCE_DATA_API, session=None, timeout: float = 10.0) -> FetchFn:
    """Live fetch from Binance public klines (``/api/v3/klines``, no credentials).

    Paginates 1000 bars per request. Only used by the live runner; never called from tests.
    """

    def fetch(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        import requests  # local import so importing this module never needs network libs

        http = session or requests
        rows: list[list] = []
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000) - 1
        while cursor <= end_ms:
            resp = http.get(f"{base_url}/api/v3/klines", timeout=timeout, params={
                "symbol": symbol, "interval": "15m", "startTime": cursor, "endTime": end_ms, "limit": 1000})
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
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="open_time"))
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                     "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
    df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True).rename("open_time")
    out = df[COLUMNS].astype(float)
    out["trades"] = out["trades"].astype("int64")
    return out


# --------------------------------------------------------------------------- buffer
class BarBuffer:
    """Rolling window of closed 15m bars per Binance symbol, updated incrementally."""

    def __init__(self, symbols: Iterable[str], fetch: FetchFn, window_days: int = 50) -> None:
        if window_days < 45:
            raise ValueError("window_days must be >= 45 (strategy needs 30d beta + 14d lookback)")
        self.symbols = [roostoo_to_binance(s) for s in symbols]
        self.fetch = fetch
        self.window = pd.Timedelta(days=window_days)
        self.frames: dict[str, pd.DataFrame] = {}
        self.last_update: pd.Timestamp | None = None

    # ------------------------------------------------------------------ loading
    def bootstrap(self, now: datetime | pd.Timestamp) -> None:
        """Load the full window ending at the last bar closed at ``now``."""
        end = last_closed_bar_open(now) + BAR
        start = end - self.window
        for symbol in self.symbols:
            self.frames[symbol] = self._safe_fetch(symbol, start, end)
        self.last_update = _ts(now)
        log.info("BarBuffer bootstrapped %d symbols up to %s", len(self.frames), end - BAR)

    def update(self, now: datetime | pd.Timestamp) -> int:
        """Append bars closed since the last update. Returns the number of new bars."""
        if not self.frames:
            self.bootstrap(now)
            return sum(len(df) for df in self.frames.values())
        target_last = last_closed_bar_open(now)
        added = 0
        for symbol in self.symbols:
            df = self.frames.get(symbol)
            last = df.index[-1] if df is not None and len(df) else None
            if last is not None and last >= target_last:
                continue
            start = (last + BAR) if last is not None else target_last + BAR - self.window
            new = self._safe_fetch(symbol, start, target_last + BAR)
            if len(new):
                df = new if df is None or not len(df) else pd.concat([df, new])
                df = df[~df.index.duplicated(keep="last")].sort_index()
                added += len(new)
            if df is not None and len(df):
                df = df[df.index >= target_last + BAR - self.window]
            self.frames[symbol] = df
        self.last_update = _ts(now)
        return added

    def _safe_fetch(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        try:
            df = self.fetch(symbol, start, end)
        except Exception as exc:  # network/data errors must never kill the trading loop
            log.warning("bar fetch failed for %s: %s", symbol, exc)
            return pd.DataFrame(columns=COLUMNS)
        if df is None or not len(df):
            return pd.DataFrame(columns=COLUMNS)
        df = df[(df.index >= start) & (df.index < end)]       # drop the still-open bar, if any
        return df[[c for c in COLUMNS if c in df.columns]]

    # ------------------------------------------------------------------ access
    def data(self) -> dict[str, pd.DataFrame]:
        """{binance_symbol: DataFrame} for symbols with data (the strategy's input format)."""
        return {s: df for s, df in self.frames.items() if df is not None and len(df)}

    def lagging(self, bar: pd.Timestamp) -> list[str]:
        """Symbols whose newest bar is older than ``bar`` (their fetch failed or the feed is late)."""
        return [s for s in self.symbols
                if self.frames.get(s) is None or not len(self.frames[s]) or self.frames[s].index[-1] < bar]

    def last_bar(self) -> pd.Timestamp | None:
        """Open time of the newest bar across symbols."""
        lasts = [df.index[-1] for df in self.frames.values() if df is not None and len(df)]
        return max(lasts) if lasts else None
