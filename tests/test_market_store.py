"""Local market-data store: append, compaction, retention, disk guard, and startup from disk."""
from collections import namedtuple
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from tradebot.core.clock import SimClock
from tradebot.data.engine import MarketDataEngine
from tradebot.data.market import COLUMNS
from tradebot.data.store import MarketDataStore

Usage = namedtuple("Usage", "total used free")
PLENTY = lambda _: Usage(1e12, 0, 5e11)          # noqa: E731


def candles(start, n, freq="s", price=10.0):
    index = pd.date_range(start, periods=n, freq=freq, tz="UTC", name="open_time")
    frame = pd.DataFrame({c: price for c in COLUMNS}, index=index)
    frame["trades"] = 1
    return frame


def store_at(tmp_path, when, **kwargs):
    clock = {"now": when}
    store = MarketDataStore(tmp_path / "market", now=lambda: clock["now"], disk_usage=kwargs.pop("disk_usage", PLENTY), **kwargs)
    return store, clock


def test_append_compact_and_load_round_trip(tmp_path):
    day1 = datetime(2026, 10, 6, 23, 59, 50, tzinfo=UTC)
    store, clock = store_at(tmp_path, day1)
    store.submit("PEPE", "1s", candles(day1, 20))               # crosses midnight: two day files
    store.submit("PEPE", "1s", candles(day1 + timedelta(seconds=15), 5, price=11.0))  # overlap, newer wins
    store.flush()
    files = sorted(p.name for p in (tmp_path / "market" / "1s" / "PEPE").iterdir())
    assert files == ["2026-10-06.csv", "2026-10-07.csv"]
    clock["now"] = datetime(2026, 10, 7, 12, tzinfo=UTC)
    store.maintain()                                              # finished day -> Parquet
    files = sorted(p.name for p in (tmp_path / "market" / "1s" / "PEPE").iterdir())
    assert files == ["2026-10-06.parquet", "2026-10-07.csv"]
    data = store.load("PEPE", "1s", day1, day1 + timedelta(seconds=20))
    assert len(data) == 20 and data.index.is_monotonic_increasing and not data.index.has_duplicates
    assert data["close"].iloc[15:20].tolist() == [11.0] * 5
    assert list(data.columns) == COLUMNS and str(data.index.tz) == "UTC"


def test_retention_deletes_old_days(tmp_path):
    now = datetime(2026, 10, 7, 1, tzinfo=UTC)
    store, _ = store_at(tmp_path, now, retention_days=30)
    store.submit("BTC", "15m", candles(now - timedelta(days=31), 4, freq="15min"))
    store.submit("BTC", "15m", candles(now - timedelta(days=29), 4, freq="15min"))
    store.flush()
    store.maintain()
    names = sorted(p.name for p in (tmp_path / "market" / "15m" / "BTC").iterdir())
    assert names == [f"{(now - timedelta(days=29)).date()}.parquet"]


def test_low_disk_stops_saving_and_resumes(tmp_path):
    now = datetime(2026, 10, 7, 1, tzinfo=UTC)
    free = {"bytes": 0.5e9}
    store, _ = store_at(tmp_path, now, min_free_bytes=int(1e9), disk_usage=lambda _: Usage(1e12, 0, free["bytes"]))
    store.submit("PEPE", "1s", candles(now, 10))
    store.flush()
    assert store.status()["paused"].startswith("low disk") and store.status()["dropped"] == 10
    assert not (tmp_path / "market" / "1s").exists()
    free["bytes"] = 50e9
    store.submit("PEPE", "1s", candles(now + timedelta(seconds=10), 10))
    store.flush()
    assert store.status()["paused"] is None and store.status()["written"] == 10


def test_write_errors_and_torn_lines_never_raise(tmp_path):
    now = datetime(2026, 10, 7, 1, tzinfo=UTC)
    store, _ = store_at(tmp_path, now)
    store.submit("PEPE", "1s", candles(now, 5))
    store.flush()
    path = tmp_path / "market" / "1s" / "PEPE" / "2026-10-07.csv"
    with open(path, "a") as handle:
        handle.write("1791334805000,10.0,10")                    # crash mid-line
    assert len(store.load("PEPE", "1s", now, now + timedelta(seconds=10))) == 5
    blocked = MarketDataStore(path, now=lambda: now, disk_usage=PLENTY)   # root is a file: writes fail
    blocked.submit("PEPE", "1s", candles(now, 3))
    blocked.flush()
    assert blocked.status()["dropped"] == 3 and blocked.status()["last_error"]


def test_engine_restarts_from_disk_and_fetches_only_the_tail(tmp_path):
    clock = SimClock(datetime(2026, 10, 7, 1, tzinfo=UTC))
    calls = []

    def source(coin, start, end):
        calls.append((pd.Timestamp(start), pd.Timestamp(end)))
        return candles(start, int((pd.Timestamp(end) - pd.Timestamp(start)).total_seconds()))
    window = {("PEPE", "1s"): 600}
    first = MarketDataEngine(clock, {"1s": source}, window, store=MarketDataStore(tmp_path / "m", disk_usage=PLENTY))
    first.start()
    first.close()                                                 # flushes the writer thread
    assert calls == [(pd.Timestamp(clock.now()) - pd.Timedelta(seconds=600), pd.Timestamp(clock.now()))]
    calls.clear()
    clock.advance(30)                                             # restart 30 s later
    second = MarketDataEngine(clock, {"1s": source}, window, store=MarketDataStore(tmp_path / "m", disk_usage=PLENTY))
    second.start()
    second.close()
    stopped = pd.Timestamp(clock.now()) - pd.Timedelta(seconds=30)
    assert calls == [(stopped, pd.Timestamp(clock.now()))]        # only the gap since the stop
    read = second.fetch("1s")("PEPE", pd.Timestamp(clock.now()) - pd.Timedelta(seconds=600), pd.Timestamp(clock.now()))
    assert len(read) == 600 and not read.index.has_duplicates


def test_window_longer_than_retention_backfills_the_head(tmp_path):
    clock = SimClock(datetime(2026, 10, 7, 1, tzinfo=UTC))
    now = pd.Timestamp(clock.now())
    store = MarketDataStore(tmp_path / "m", disk_usage=PLENTY)
    store.submit("BTC", "15m", candles(now - pd.Timedelta(days=2), 192, freq="15min"))   # last 2 days on disk
    store.flush()
    calls = []

    def source(coin, start, end):
        calls.append((pd.Timestamp(start), pd.Timestamp(end)))
        return candles(pd.Timestamp(start).ceil("15min"), int((pd.Timestamp(end) - pd.Timestamp(start).ceil("15min")) / pd.Timedelta("15min")), freq="15min")
    md = MarketDataEngine(clock, {"15m": source}, {("BTC", "15m"): 5 * 86400}, store=store)
    md.start()
    md.close()
    assert calls[0] == (now - pd.Timedelta(days=5), now - pd.Timedelta(days=2))   # head only
    bars = md.fetch("15m")("BTC", now - pd.Timedelta(days=5), now)
    assert len(bars) == 5 * 96 and bars.index[0] == now - pd.Timedelta(days=5)


def test_replay_mode_keeps_no_store(tmp_path):
    from tests.test_shared_runner import shared
    runner, _, _ = shared(tmp_path)
    try:
        assert runner.market_data.store is None and runner.market_data.status()["store"] is None
    finally:
        runner.close()
    assert not (tmp_path / "var").exists()


def test_default_retention_stays_under_a_gigabyte_worst_case(tmp_path):
    # Random-walk prices and random volumes compress worse than real ticks on a price grid.
    now = datetime(2026, 10, 7, tzinfo=UTC)
    store, clock = store_at(tmp_path, now)
    rng = np.random.default_rng(0)
    day = candles(now, 86_400)
    px = np.exp(np.cumsum(rng.normal(0, 2e-4, 86_400))) * 3.6e-6
    for column in ("open", "high", "low", "close"):
        day[column] = px
    for column in ("volume", "quote_volume", "taker_buy_base", "taker_buy_quote"):
        day[column] = rng.lognormal(10, 2, 86_400)
    day["trades"] = rng.poisson(20, 86_400)
    store.submit("PEPE", "1s", day)
    store.flush()
    clock["now"] = now + timedelta(days=1)
    store.maintain()
    size = (tmp_path / "market" / "1s" / "PEPE" / f"{now.date()}.parquet").stat().st_size
    assert size * 3 * 30 < 0.7e9                                  # 3 MM coins x 30 days retention
