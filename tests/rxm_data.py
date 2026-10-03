"""Synthetic coin universes for the RXM tests (BTC plus trending and mean-reverting coins)."""
import numpy as np
import pandas as pd


def universe(n_coins: int = 10, days: int = 70, end: str = "2026-03-01", seed: int = 1) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    index = pd.date_range(end=pd.Timestamp(end, tz="UTC") - pd.Timedelta(minutes=15), periods=days * 96,
                          freq="15min", name="open_time")
    out = {}
    for i in range(n_coins):
        close = 100 * np.exp(np.cumsum(rng.normal((i - n_coins / 2) * 3e-5, 0.004 + 0.001 * i, len(index))))
        volume = rng.uniform(50, 150, len(index))
        out["BTC" if i == 0 else f"C{i}"] = pd.DataFrame(
            {"open": close, "high": close * 1.002, "low": close * 0.998, "close": close, "volume": volume},
            index=index)
    return out
