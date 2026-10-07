from abc import ABC
from typing import Dict

import pandas as pd


class Strategy(ABC):
    """
    Base class for all strategies.

    Strategies declare output_kind="weights" (default) or "quotes". The engine
    executes QuoteBatch outputs through its quote executor. Weight outputs retain
    the existing DataFrame contract described below:

    A weight strategy turns market data into *target portfolio weights*:
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

    output_kind: str = "weights"

    def generate_weights(self, data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """
        Args:
            data: {coin: DataFrame} with columns open, high, low, close, volume,
                  quote_volume, trades, taker_buy_base, taker_buy_quote,
                  indexed by UTC open_time (see tradebot.data.load_universe).
        Returns:
            DataFrame of target weights as described in the class docstring.
        """

        raise NotImplementedError("This strategy outputs quotes, not weights")

    def generate_quotes(self, data, **context):
        raise NotImplementedError("This strategy outputs weights, not quotes")

    def generate(self, data, **context):
        """Dispatch explicitly; existing weight strategies retain their DataFrame contract."""
        if self.output_kind == "quotes":
            return self.generate_quotes(data, **context)
        return self.generate_weights(data)

    def __repr__(self):
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.name}({args})"
