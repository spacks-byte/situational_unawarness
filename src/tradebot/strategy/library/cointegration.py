"""Causal spread signals; no exchange I/O, rebalancing or execution assumptions."""
import numpy as np
import pandas as pd

from tradebot.core.cointegration import CointegrationConfig
from tradebot.strategy.base import Strategy

STEP = pd.Timedelta(minutes=30)


def matches_candle_grid(index, expected):
    """Compare exact instants, regardless of timestamp precision or UTC tz implementation."""
    # Index.equals can reject equal instants after Parquet/REST merges in pandas 3
    # when both the datetime unit and timezone object differ. Do not round times:
    # elementwise equality still rejects gaps, duplicates, ordering and off-grid bars.
    return (isinstance(index, pd.DatetimeIndex) and index.tz is not None
            and len(index) == len(expected) and bool((index == expected).all()))


def common_closes(data, assets, start, end):
    """Require the complete common grid, without silently dropping an asset/bar."""
    expected = pd.date_range(start, end, freq=STEP, inclusive="left")
    closes = {}
    for asset in assets:
        frame = data.get(asset)
        if frame is None or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            raise ValueError(f"missing timezone-aware history: {asset}")
        frame = frame.loc[(frame.index >= start) & (frame.index < end)]
        if not matches_candle_grid(frame.index, expected):
            raise ValueError(f"incomplete, duplicated or unordered 30m history: {asset}")
        values = frame["close"].to_numpy(float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError(f"invalid close: {asset}")
        closes[asset] = values
    return pd.DataFrame(closes, index=expected)


class CointegrationPairs(Strategy):
    name = "cointegration-pairs"
    output_kind = "pairs"

    def __init__(self, config=None):
        self.config = config or CointegrationConfig()

    def fit(self, data, start):
        start = pd.Timestamp(start)
        if start.tz is None or start != start.floor(STEP):
            raise ValueError("cycle start must be a timezone-aware 30m boundary")
        cfg = self.config
        closes = common_closes(data, cfg.assets, start-pd.Timedelta(days=60)-STEP, start)
        logs = np.log(closes.iloc[-2880:])
        models, inverse = {}, {}
        boundaries = pd.date_range(start-pd.Timedelta(days=60), start, freq="D")
        for spec in cfg.pairs:
            a, b = spec.pair.split("-")
            if np.std(logs[b]) < 1e-10:
                raise ValueError(f"constant training log price: {b}")
            alpha, beta = np.linalg.lstsq(np.column_stack([np.ones(len(logs)), logs[b]]), logs[a], rcond=None)[0]
            residuals = logs[a] - alpha - beta * logs[b]
            daily = closes.loc[boundaries-STEP, [a, b]].to_numpy()
            returns = daily[1:] / daily[:-1] - 1
            sigma = float(np.std(.5*(returns[:, 0]-returns[:, 1]), ddof=1))
            if not np.isfinite(sigma) or sigma <= 1e-12:
                raise ValueError(f"invalid daily pair volatility: {spec.pair}")
            inverse[spec.pair] = 1/sigma
            models[spec.pair] = dict(alpha=float(alpha), beta=float(beta), sigma=sigma,
                                     history=residuals.iloc[-960:].tolist())
        total = sum(inverse.values())
        for pair, model in models.items():
            model["weight"] = inverse[pair]/total
            model["budget"] = cfg.reference_capital*model["weight"]
        return models

    def decide(self, spec, model, closes, *, direction=0, entered_at=None, closed_at=None, terminal=False):
        a, b = spec.pair.split("-")
        if any(not np.isfinite(closes[c]) or closes[c] <= 0 for c in (a, b)):
            raise ValueError("invalid completed close")
        spread = float(np.log(closes[a])-model["alpha"]-model["beta"]*np.log(closes[b]))
        history = np.asarray(model["history"])
        mean, std = float(history.mean()), float(history.std(ddof=1))
        z = (spread-mean)/std if std > 1e-12 else None
        if z is not None and not np.isfinite(z):
            z = None
        target, reason = direction, None
        if terminal:
            target, reason = 0, "end_of_data" if direction else None
        elif direction and pd.Timestamp(closed_at)-pd.Timestamp(entered_at) >= pd.Timedelta(days=14):
            target, reason = 0, "time_stop"
        elif direction and z is not None and abs(z) < spec.exit_z:
            target, reason = 0, "z_exit"
        elif not direction and z is not None and abs(z) > spec.entry_z:
            target, reason = 1 if z < 0 else -1, "z_entry"
        return dict(spread=spread, mean=mean, std=std, z=z, target=target, reason=reason)
