from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """Time abstraction used by the engine and tests.

    The engine must never call `time` or `datetime.now()` directly. All runtime
    timing is routed through this protocol so live and backtest behavior share the
    same logic.
    """

    def now(self) -> datetime:
        ...

    def sleep(self, seconds: float) -> None:
        ...

    def monotonic(self) -> float:
        ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


class SimClock:
    """Advanceable clock for tests and backtests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = (start or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def monotonic(self) -> float:
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (self._now - epoch).total_seconds()

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
