"""Resolve relative backtest ranges against completed UTC candles."""
from __future__ import annotations

import math

import pandas as pd


def relative_period(interval, *, last_hours=None, last_minutes=None, now=None):
    """Return an inclusive start and exclusive end, frozen at submission time."""
    if (last_hours is None) == (last_minutes is None):
        raise ValueError('Specify exactly one of last_hours or last_minutes.')
    minutes = float(last_minutes if last_minutes is not None else last_hours * 60)
    if not math.isfinite(minutes) or not 0 < minutes <= 90 * 24 * 60:
        raise ValueError('Choose a positive backtest period of at most 90 days.')
    duration = pd.Timedelta(minutes=minutes)
    step = pd.Timedelta(interval.replace('m', 'min'))
    if duration < step or duration % step:
        raise ValueError(f'Duration must be a whole number of {interval} candles (at least one).')
    end = pd.Timestamp.now(tz='UTC') if now is None else pd.Timestamp(now)
    end = end.tz_localize('UTC') if end.tzinfo is None else end.tz_convert('UTC')
    end = end.floor(step)
    return end - duration, end
