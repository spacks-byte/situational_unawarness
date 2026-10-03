import pandas as pd


def interval_to_timedelta(interval: str) -> pd.Timedelta:
    """Kline interval string ('1s', '5m', '15m', '1h', '1d') -> Timedelta. 'm' means minutes."""
    unit = interval[:-1] + "min" if interval.endswith("m") else interval
    return pd.Timedelta(unit)
