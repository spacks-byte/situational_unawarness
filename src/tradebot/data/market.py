"""Strategy-independent public candle sources and historical frame normalization."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd

from tradebot.core.symbols import to_binance, to_coin
from tradebot.data.binance_vision import klines_path

COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote"]
BINANCE_DATA_API = "https://data-api.binance.vision"
FetchFn = Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame]


def _empty():
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


def binance_public_fetch(base_url: str = BINANCE_DATA_API, session=None, timeout: float = 10.0, interval: str = "15m") -> FetchFn:
    """Live fetch from Binance public klines (`/api/v3/klines`, no credentials), 1000 bars per request."""

    if interval not in {"15m", "1s"}:
        raise ValueError("unsupported live candle interval")
    interval_ms = 1000 if interval == "1s" else 900_000
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
                "symbol": to_binance(coin), "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000})
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            rows.extend(batch)
            cursor = int(batch[-1][0]) + interval_ms
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
