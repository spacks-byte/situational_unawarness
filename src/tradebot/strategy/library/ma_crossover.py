from typing import Dict

import pandas as pd

from tradebot.strategy.base import Strategy


class MACrossover(Strategy):
    """
    Baseline: hold a symbol while its fast SMA is above its slow SMA.
    With allow_short=1, short it while the fast SMA is below the slow SMA instead of sitting in cash.
    Each symbol gets an equal 1/N slice of equity.
    """

    name = "ma_crossover"

    def __init__(self, fast: int = 20, slow: int = 100, allow_short: int = 0):
        if fast >= slow:
            raise ValueError("fast must be < slow")
        super().__init__(fast=fast, slow=slow, allow_short=allow_short)
        self.fast = fast
        self.slow = slow
        self.allow_short = bool(allow_short)

    def generate_weights(self, data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        signals = {}
        for symbol, df in data.items():
            fast_ma = df["close"].rolling(self.fast).mean()
            slow_ma = df["close"].rolling(self.slow).mean()
            signal = (fast_ma > slow_ma).astype(float)
            if self.allow_short:
                signal -= (fast_ma < slow_ma).astype(float)
            signals[symbol] = signal
        return pd.DataFrame(signals) / len(data)
