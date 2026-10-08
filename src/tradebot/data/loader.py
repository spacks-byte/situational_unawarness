from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from tradebot.core.config import Settings
from tradebot.core.symbols import to_coin
from tradebot.data.binance_vision import klines_path


def _data_dir(data_dir: Optional[str | Path]) -> Path:
    return Path(data_dir) if data_dir is not None else Path(Settings.load().data.dir)


def load_klines(symbol: str, interval: str, start: Optional[str] = None, end: Optional[str] = None,
                data_dir: Optional[str | Path] = None) -> pd.DataFrame:
    """
    Load downloaded klines for a symbol in any form ("BTC", "BTC/USD" or "BTCUSDT").

    Returns a DataFrame indexed by UTC open_time with columns
    open, high, low, close, volume, quote_volume, trades, taker_buy_base, taker_buy_quote.
    `start` is inclusive, `end` exclusive (any string pandas can parse).
    """
    path = klines_path(_data_dir(data_dir), symbol, interval)
    if not path.exists():
        raise FileNotFoundError(
            f"No data at {path}. Run: python -m tradebot data --symbols {to_coin(symbol)} --intervals {interval}"
        )
    df = pd.read_parquet(path)
    if start is not None:
        df = df[df.index >= pd.to_datetime(start, utc=True)]
    if end is not None:
        df = df[df.index < pd.to_datetime(end, utc=True)]
    return df


def load_universe(symbols: list[str], interval: str, start: Optional[str] = None, end: Optional[str] = None,
                  data_dir: Optional[str | Path] = None) -> dict[str, pd.DataFrame]:
    """Load klines for several symbols, keyed by coin (e.g. "BTC"). Symbols with no rows are skipped."""
    data_dir = _data_dir(data_dir)
    data = {}
    for s in symbols:
        df = load_klines(s, interval, start, end, data_dir)
        if not df.empty:
            data[to_coin(s)] = df
    if not data:
        raise ValueError("No data loaded for any symbol in the given range")
    return data
