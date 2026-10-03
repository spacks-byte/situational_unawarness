from abc import ABC, abstractmethod
from typing import Dict

import pandas as pd


class Strategy(ABC):
    """
    Base class for all strategies.

    A strategy turns market data into *target portfolio weights*:
      - index:   bar open timestamps (UTC), same as the input data
      - columns: coins (keys of `data`, e.g. "BTC")
      - values:  fraction of total equity in that symbol, between -1 and 1:
                 positive = spot long, negative = short (e.g. -0.2 = short notional of 20% of equity)
      - capital used per row = sum of |weights| (shorts are 1x, so they lock collateral equal to
        their size); keep it <= 1. The rest is held as cash; rows above 1 get scaled down.

    The row at time t may use any data up to and including the CLOSE of bar t.
    The engine turns it into limit orders at that close price, resting during bar t+1,
    so there is no look-ahead as long as you don't use future rows
    (e.g. no `.shift(-1)`, no centered windows).
    """

    name: str = "base"

    def __init__(self, **params):
        self.params = params

    @abstractmethod
    def generate_weights(self, data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """
        Args:
            data: {coin: DataFrame} with columns open, high, low, close, volume,
                  quote_volume, trades, taker_buy_base, taker_buy_quote,
                  indexed by UTC open_time (see tradebot.data.load_universe).
        Returns:
            DataFrame of target weights as described in the class docstring.
        """

    def __repr__(self):
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.name}({args})"
