"""
RXM: residual cross-sectional momentum. Spec and evidence: docs/STRATEGY.md.

Once a day (00:00 UTC):
  1. Residual return of each coin over horizon L, net of its 30-day beta to BTC:
         e_i,L = ln(P_i,t / P_i,t-L) - beta_i * ln(P_BTC,t / P_BTC,t-L)
  2. Divide by the coin's 7-day volatility, z-score across coins, average over the horizons:
         s_i = mean_L zscore_i( e_i,L / (sigma_i * sqrt(L)) )        L in {3d, 7d, 14d}
  3. Long the top k and short the bottom k by s_i, inverse-vol weights within each side.
     Long side = gross * (1 + tilt) / 2, short side = gross * (1 - tilt) / 2.
  4. Rank buffer: a coin already held keeps its slot while it still ranks within k + buffer.
     Fewer swaps means fewer fees.

Weights at bar t use data up to the close of bar t only; the simulator and the engine trade them
during the next bar. `scores` and `weights` are split so research tools can score once and try many
portfolio settings.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tradebot.strategy.base import Strategy

UNIVERSE = ("BTC,ETH,SOL,BNB,XRP,DOGE,ADA,AVAX,LINK,LTC,DOT,TRX,NEAR,APT,UNI,AAVE,FIL,ICP,HBAR,XLM,"
            "SUI,ARB,SEI,FET,PEPE,SHIB,BONK,FLOKI,CRV,ZEC,PENDLE,CAKE,CFX,ZEN,WLD").split(",")

# Frozen presets: strategy params plus the backtest settings they were validated with.
PRESETS = {
    "comp": dict(params=dict(k=3, gross=1.0, tilt=0.3, buffer=2),
                 backtest=dict(limit_offset_bps=5.0, lockin_return=0.06, lockin_scale=0.3)),
    "neutral": dict(params=dict(k=5, gross=0.9, tilt=0.0),
                    backtest=dict(limit_offset_bps=5.0)),
}


class ResidualMomentum(Strategy):
    name = "rxm"

    def __init__(self, k: int = 3, gross: float = 1.0, tilt: float = 0.3, buffer: int = 0,
                 lookbacks: str = "72/168/336", rebalance_h: int = 24, beta_days: int = 30, vol_days: int = 7):
        super().__init__(k=k, gross=gross, tilt=tilt, buffer=buffer, lookbacks=lookbacks,
                         rebalance_h=rebalance_h, beta_days=beta_days, vol_days=vol_days)
        self.k, self.gross, self.tilt, self.buffer = int(k), float(gross), float(tilt), int(buffer)
        self.horizons = [int(h) for h in str(lookbacks).replace(",", "/").split("/") if h]
        self.rebalance_h, self.beta_days, self.vol_days = int(rebalance_h), int(beta_days), int(vol_days)

    @classmethod
    def preset(cls, name: str) -> "ResidualMomentum":
        return cls(**PRESETS[name]["params"])

    def generate_weights(self, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        score, sigma = self.scores(data)
        return self.weights(score, sigma)

    def scores(self, data: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Cross-sectional score per bar and coin (NaN = not eligible), and each coin's per-bar volatility."""
        raw_close = pd.DataFrame({c: df["close"] for c, df in data.items()}).sort_index()
        volume = pd.DataFrame({c: df["volume"] for c, df in data.items()}).reindex(raw_close.index)
        close = raw_close.ffill()
        bph = _bars_per_hour(close.index)
        log_price = np.log(close)
        r = log_price.diff()

        window = self.vol_days * 24 * bph
        sigma = r.rolling(window, min_periods=window * 2 // 7).std()
        # A stalled feed is forward-filled; its volatility collapses and 1/vol would hand it a whole
        # side of the book. So a coin with no fresh bar, no volume or no price change in 24 h is
        # ineligible, and volatility is floored at 0.05x the cross-sectional median.
        day = 24 * bph
        flat = close.diff().abs().rolling(day, min_periods=day).sum() <= 0
        stale = raw_close.isna() | (volume.rolling(day, min_periods=1).sum() <= 0) | flat
        sigma = sigma.clip(lower=0.05 * sigma.median(axis=1), axis=0)

        beta = None
        if "BTC" in close:
            bw = self.beta_days * 24 * bph
            beta = r.rolling(bw, min_periods=bw // 3).cov(r["BTC"]).div(
                r["BTC"].rolling(bw, min_periods=bw // 3).var(), axis=0)
        parts = []
        for hours in self.horizons:
            lag = hours * bph
            move = log_price - log_price.shift(lag)
            if beta is not None:
                move = move - beta.mul(log_price["BTC"] - log_price["BTC"].shift(lag), axis=0)
            part = move / (sigma * np.sqrt(lag))
            if len(self.horizons) > 1:
                part = part.sub(part.mean(axis=1), axis=0).div(part.std(axis=1), axis=0)
            parts.append(part)
        score = (sum(parts) / len(parts)).where(sigma.notna() & ~stale)
        return score, sigma

    def weights(self, score: pd.DataFrame, sigma: pd.DataFrame) -> pd.DataFrame:
        """Top-k long / bottom-k short with inverse-vol sizing, decided on rebalance bars and held between."""
        index = score.index
        rebalance = pd.Series((index.hour % self.rebalance_h == 0) & (index.minute == 0), index=index)
        rank = score.rank(axis=1, ascending=False)
        n = score.notna().sum(axis=1).to_numpy()
        k = np.minimum(self.k, n // 2)                                   # the two books never overlap
        if self.buffer:
            in_long, in_short = self._buffered_books(rank, n, k, rebalance.to_numpy())
        else:
            in_long, in_short = rank.le(k, axis=0), rank.gt(n - k, axis=0)
        inverse_vol = 1.0 / sigma
        top = in_long.astype(float) * inverse_vol
        bottom = in_short.astype(float) * inverse_vol
        half = self.gross / 2
        w = top.div(top.sum(axis=1), axis=0).fillna(0) * half * (1 + self.tilt) \
            - bottom.div(bottom.sum(axis=1), axis=0).fillna(0) * half * (1 - self.tilt)
        w = w.where(rebalance, np.nan).ffill().fillna(0.0)
        gross = w.abs().sum(axis=1)
        return w.div(gross.where(gross > 1.0, 1.0), axis=0).fillna(0.0)

    def _buffered_books(self, rank: pd.DataFrame, n: np.ndarray, k: np.ndarray, rebalance: np.ndarray):
        """Book membership with hysteresis, carried from one rebalance bar to the next.

        Membership depends on the path, so it is rebuilt from the first bar of the data on every call.
        The backtest warm-up and the live bar buffer are both much longer than a coin's typical stay
        in the book, so the starting point no longer matters by the time anything trades.
        """
        ranks = rank.to_numpy()
        in_long, in_short = np.zeros(ranks.shape, bool), np.zeros(ranks.shape, bool)
        longs: list[int] = []
        shorts: list[int] = []
        for t in np.flatnonzero(rebalance):
            longs = _keep_then_fill(ranks[t], longs, k[t], self.buffer)
            shorts = _keep_then_fill(n[t] + 1 - ranks[t], shorts, k[t], self.buffer, taken=longs)
            in_long[t, longs], in_short[t, shorts] = True, True
        return (pd.DataFrame(in_long, index=rank.index, columns=rank.columns),
                pd.DataFrame(in_short, index=rank.index, columns=rank.columns))


def _keep_then_fill(rank: np.ndarray, members: list[int], k: int, buffer: int, taken=()) -> list[int]:
    """k names for one book: incumbents still ranked within k + buffer, then the best outsiders."""
    order = [i for i in np.argsort(rank, kind="stable") if not np.isnan(rank[i]) and i not in taken]
    keep = [i for i in order if i in members and rank[i] <= k + buffer][:k]
    return keep + [i for i in order if i not in keep][:k - len(keep)]


def _bars_per_hour(index: pd.DatetimeIndex) -> int:
    return max(1, int(round(pd.Timedelta(hours=1) / (index[1] - index[0]))))
