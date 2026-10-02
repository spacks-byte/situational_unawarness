"""
RXM: Residual Cross-sectional Momentum (team 87, "Situational Unawareness").

Formal spec, evidence and out-of-sample results: docs/STRATEGY_SPEC.md.

Once a day (00:00 UTC):
  1. Residual return over horizon L (in bars), with beta_i the coin's 30-day beta to BTC:
         e_i,L = ln(P_i,t / P_i,t-L) - beta_i * ln(P_BTC,t / P_BTC,t-L)
  2. Vol-normalise (sigma_i = 7-day per-bar vol), z-score across coins, average the horizons:
         s_i = mean_L  zscore_i( e_i,L / (sigma_i * sqrt(L)) )        L in {3d, 7d, 14d}
  3. Long the top k, short the bottom k by s_i, inverse-vol weights inside each side.
     Side sizes: long = gross * (1 + tilt) / 2, short = gross * (1 - tilt) / 2.
Between rebalances the weights are held (the engine re-trades only on >1% drift).

Turnover control (`buffer`, 2 in the competition preset): a coin already in the long (short) book
keeps its place while it still ranks in the top (bottom) k + buffer; only the slots that frees up go
to the best-ranked outsiders. Fewer swaps, fewer fees.

Two optional layers, off in every preset (tested, not adopted: docs/STRATEGY_SPEC.md §9):
  * `max_share`: no coin may hold more than this fraction of its side (inverse-vol sizing otherwise
    hands most of a side to the calmest coin); the excess is spread over the other names.
  * `trend_days` + `tilt_down`: the net-long tilt follows the market. While BTC is above its price
    `trend_days` ago the book runs `tilt`; otherwise it runs `tilt_down`.

Presets (PRESETS below): "comp" = competition mode (k=3, tilt 0.3, gross 1.0, rank buffer 2, +6%
lock-in in the engine config), "neutral" = the market-neutral book (k=5, gross 0.9, no tilt).

All weights at bar t use data up to the close of bar t only; the engine trades them during t+1.
"""
from typing import Dict

import numpy as np
import pandas as pd

from backtest.strategy import Strategy

# Strategy params + engine overlay per preset. These are the FROZEN values (docs/STRATEGY_SPEC.md).
PRESETS = {
    "comp":    dict(params=dict(k=3, gross=1.0, tilt=0.3, buffer=2),
                    engine=dict(lockin_return=0.06, lockin_scale=0.3)),
    "neutral": dict(params=dict(k=5, gross=0.9, tilt=0.0), engine=dict()),
}
LIMIT_OFFSET_BPS = 5.0     # passive limit offset used by every preset


def _bars_per_hour(index: pd.DatetimeIndex) -> int:
    step = (index[1] - index[0]) / pd.Timedelta(hours=1)
    return max(1, int(round(1 / step)))


class ResidualMomentum(Strategy):
    name = "rxm"

    def __init__(self, k: int = 3, lookbacks: str = "72/168/336", gross: float = 1.0, tilt: float = 0.3,
                 rebalance_h: int = 24, beta_days: int = 30, vol_days: int = 7, buffer: int = 0,
                 max_share: float = 0.0, trend_days: int = 0, tilt_down: float = 0.0):
        super().__init__(k=k, lookbacks=lookbacks, gross=gross, tilt=tilt, rebalance_h=rebalance_h,
                         beta_days=beta_days, vol_days=vol_days, buffer=int(buffer), max_share=max_share,
                         trend_days=int(trend_days), tilt_down=tilt_down)
        self.__dict__.update(self.params)

    @staticmethod
    def _panel(data: Dict[str, pd.DataFrame], col: str) -> pd.DataFrame:
        return pd.DataFrame({s: df[col] for s, df in data.items()}).sort_index()

    def generate_weights(self, data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        raw_close = self._panel(data, "close")
        close = raw_close.ffill()
        vol = self._panel(data, "volume")
        idx = close.index
        bph = _bars_per_hour(idx)
        lp = np.log(close)
        r = lp.diff()

        vw = self.vol_days * 24 * bph
        sigma = r.rolling(vw, min_periods=vw * 2 // 7).std()      # 7d window, 2d minimum (as v2)
        # Live-data robustness: a stalled/halted feed is forward-filled, its vol collapses and 1/vol
        # would hand it a whole side of the book. A coin is ineligible with no fresh bar, no volume or
        # no price change in 24h, and vol is floored at 0.05x the cross-sectional median (real coins
        # never go below ~0.08x in 2024-26), so the floor only binds on a stalled feed.
        flat = close.diff().abs().rolling(24 * bph, min_periods=24 * bph).sum() <= 0
        stale = raw_close.isna() | (vol.rolling(24 * bph, min_periods=1).sum() <= 0) | flat
        sigma = sigma.clip(lower=0.05 * sigma.median(axis=1), axis=0)

        horizons = [int(x) for x in str(self.lookbacks).replace(",", "/").split("/") if x]
        btc = next((c for c in close.columns if c.startswith("BTC")), None)
        beta = None
        if btc is not None:
            bw = self.beta_days * 24 * bph
            beta = r.rolling(bw, min_periods=bw // 3).cov(r[btc]).div(
                r[btc].rolling(bw, min_periods=bw // 3).var(), axis=0)
        parts = []
        for h in horizons:
            L = h * bph
            mom = lp - lp.shift(L)
            if beta is not None:
                mom = mom - beta.mul(lp[btc] - lp[btc].shift(L), axis=0)
            s_h = mom / (sigma * np.sqrt(L))
            if len(horizons) > 1:
                s_h = s_h.sub(s_h.mean(axis=1), axis=0).div(s_h.std(axis=1), axis=0)
            parts.append(s_h)
        score = (sum(parts) / len(parts)).where(sigma.notna() & ~stale)

        rank = score.rank(axis=1, ascending=False)
        n = score.notna().sum(axis=1)
        k = np.minimum(self.k, n.values[:, None] // 2)          # long and short sets never overlap
        rebal = pd.Series((idx.hour % self.rebalance_h == 0) & (idx.minute == 0), index=idx)
        in_long, in_short = rank <= k, rank > (n.values[:, None] - k)
        if self.buffer:
            in_long, in_short = self._buffered_books(rank, n.values, k[:, 0], rebal.values)
        inv = 1.0 / sigma
        top = in_long.astype(float) * inv
        bot = in_short.astype(float) * inv
        long_share = top.div(top.sum(axis=1), axis=0).fillna(0)
        short_share = bot.div(bot.sum(axis=1), axis=0).fillna(0)
        if self.max_share:
            long_share, short_share = _cap_shares(long_share, self.max_share), _cap_shares(short_share, self.max_share)
        tilt = self.tilt
        if self.trend_days and btc is not None:
            uptrend = lp[btc] > lp[btc].shift(self.trend_days * 24 * bph)
            tilt = pd.Series(np.where(uptrend, self.tilt, self.tilt_down), index=idx)
        half = self.gross / 2
        w = (long_share * half).mul(1 + tilt, axis=0) - (short_share * half).mul(1 - tilt, axis=0)
        w = w.where(rebal, np.nan).ffill().fillna(0.0)

        g = w.abs().sum(axis=1)
        return w.div(g.where(g > 1.0, 1.0), axis=0).fillna(0.0)

    def _buffered_books(self, rank: pd.DataFrame, n: np.ndarray, k: np.ndarray, rebal: np.ndarray):
        """Long/short membership with hysteresis, decided at each rebalance bar from the one before.

        Membership depends on the path, so it is rebuilt from the first bar of `data` every call;
        the 45+ days of warm-up the backtest and the live buffer both carry are far longer than a
        coin's stay in the book, so the starting point does not matter by the time we trade.
        """
        R = rank.to_numpy()
        in_long, in_short = np.zeros(R.shape, bool), np.zeros(R.shape, bool)
        longs, shorts = [], []
        for t in np.flatnonzero(rebal):
            longs = _keep_then_fill(R[t], longs, k[t], self.buffer)
            shorts = _keep_then_fill(n[t] + 1 - R[t], shorts, k[t], self.buffer, taken=longs)   # 1 = worst score
            in_long[t, longs], in_short[t, shorts] = True, True
        return (pd.DataFrame(in_long, index=rank.index, columns=rank.columns),
                pd.DataFrame(in_short, index=rank.index, columns=rank.columns))


def _keep_then_fill(rank: np.ndarray, members: list, k: int, buffer: int, taken=()) -> list:
    """k names for one side: incumbents ranked within k + buffer first, then the best outsiders."""
    order = [i for i in np.argsort(rank, kind="stable") if not np.isnan(rank[i]) and i not in taken]
    keep = [i for i in order if i in members and rank[i] <= k + buffer][:k]
    return keep + [i for i in order if i not in keep][:k - len(keep)]


def _cap_shares(shares: pd.DataFrame, cap: float) -> pd.DataFrame:
    """Side shares (rows sum to 1) with no name above `cap`; the excess goes pro rata to the others.
    A cap below 1/k cannot be met in full: every name then sits at the cap and the rest stays in cash."""
    for _ in range(4):
        capped = shares.clip(upper=cap)
        room = capped.where((capped > 0) & (capped < cap), 0.0)
        excess = shares.sum(axis=1) - capped.sum(axis=1)
        shares = capped + room.div(room.sum(axis=1), axis=0).fillna(0.0).mul(excess, axis=0)
    return shares.clip(upper=cap)


def preset(name: str) -> tuple[dict, dict]:
    """(strategy params, extra BacktestConfig fields) for a frozen preset."""
    p = PRESETS[name]
    return dict(p["params"]), dict(p["engine"], limit_offset_bps=LIMIT_OFFSET_BPS)
