from typing import Optional

import pandas as pd

from data_pipeline.binance_vision import KLINES_DIR, to_binance_symbol


def load_klines(symbol: str, interval: str,
                start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """
    Load downloaded klines for a symbol ("BTC", "BTC/USD" or "BTCUSDT").

    Returns a DataFrame indexed by UTC open_time with columns
    open, high, low, close, volume, quote_volume, trades, taker_buy_base, taker_buy_quote.
    `start` is inclusive, `end` exclusive (any string pandas can parse).
    """
    path = KLINES_DIR / interval / f"{to_binance_symbol(symbol)}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"No data at {path}. Run: python -m data_pipeline.binance_vision "
            f"--symbols {symbol} --intervals {interval}"
        )

    df = pd.read_parquet(path)
    if start is not None:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        df = df[df.index < pd.Timestamp(end, tz="UTC")]
    return df
