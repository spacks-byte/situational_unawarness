"""
Local copy of the live market data, so a restart or a stream/REST outage does not start from nothing.

Layout under `root` (one directory per interval and coin, one file per UTC day):

    1s/PEPE/2026-10-07.csv        today's file: append-only, one line per closed candle
    1s/PEPE/2026-10-06.parquet    earlier days: compacted (sorted, de-duplicated)
    15m/BTC/...

Everything runs on one background writer thread fed by a queue: the market-data producer only
enqueues, so a slow or failing disk never delays market data or trading. Disk safety comes first:
when free space drops below `min_free_bytes` the store stops writing (and says so in `status()`)
and trading carries on; it resumes by itself once space is back. Files older than `retention_days`
are deleted. One-second candles take at most ~7 MB per coin per day after compaction (measured on
incompressible synthetic data; real ticks compress better) plus ~16 MB for today's CSV, so the three
MM coins at 30 days stay under ~0.7 GB. 15-minute candles are negligible. Nothing extra is
downloaded: the store only keeps what the producer already received.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path
import queue
import shutil
import threading

import pandas as pd

from tradebot.data.market import COLUMNS, _empty

log = logging.getLogger(__name__)
_STOP = object()


class MarketDataStore:
    def __init__(self, root, *, retention_days: int = 30, min_free_bytes: int = 1 << 30,
                 now=lambda: datetime.now(timezone.utc), disk_usage=shutil.disk_usage,
                 maintenance_seconds: float = 3600.0) -> None:
        self.root = Path(root).expanduser().resolve()
        self.retention_days = retention_days
        self.min_free_bytes = min_free_bytes
        self.now = now
        self.disk_usage = disk_usage
        self.maintenance_seconds = maintenance_seconds
        self.paused: str | None = None          # why writing is suspended (disk guard / write error)
        self.written = 0
        self.dropped = 0
        self.last_error: str | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=10_000)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()           # file access: writer thread vs load()
        self._last_maintenance = 0.0

    # ---------------------------------------------------------------- producer side (never blocks)
    def submit(self, coin: str, interval: str, frame: pd.DataFrame) -> None:
        if frame is None or not len(frame):
            return
        try:
            self._queue.put_nowait((coin, interval, frame.copy()))
        except queue.Full:
            self.dropped += len(frame)          # memory stays bounded; the REST repair refills gaps

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(name="market-store", target=self._run, daemon=True)
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout=timeout)
        self._thread = None

    def flush(self) -> None:
        """Write everything queued so far on the calling thread (tests, shutdown without a thread)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not _STOP:
                self._write(*item)

    # ---------------------------------------------------------------- writer thread
    def _run(self) -> None:
        self.maintain()
        while True:
            try:
                item = self._queue.get(timeout=5)
            except queue.Empty:
                item = None
            if item is _STOP:
                self.flush()
                return
            if item is not None:
                self._write(*item)
            if self.now().timestamp() - self._last_maintenance >= self.maintenance_seconds:
                self.maintain()

    def _disk_ok(self) -> bool:
        try:
            free = self.disk_usage(self.root).free
        except OSError as exc:
            free, self.last_error = 0, str(exc)
        if free < self.min_free_bytes:
            if self.paused is None:
                log.warning("market store paused: %.2f GB free < %.2f GB; trading continues",
                            free / 1e9, self.min_free_bytes / 1e9)
            self.paused = f"low disk: {free / 1e9:.2f} GB free"
            return False
        if self.paused:
            log.info("market store resumed")
        self.paused = None
        return True

    def _write(self, coin: str, interval: str, frame: pd.DataFrame) -> None:
        if not self._disk_ok():
            self.dropped += len(frame)
            return
        frame = frame.reindex(columns=COLUMNS)
        try:
            with self._lock:
                for day, rows in frame.groupby(frame.index.tz_convert("UTC").date):
                    path = self._day(interval, coin, day, ".csv")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    out = rows.copy()
                    # pandas indexes can use seconds, milliseconds, microseconds
                    # or nanoseconds; the on-disk contract always uses milliseconds.
                    out.insert(0, "open_time_ms", out.index.as_unit("ms").asi8)
                    out.to_csv(path, mode="a", header=not path.exists(), index=False)
            self.written += len(frame)
            self.last_error = None
        except OSError as exc:
            self.dropped += len(frame)
            self.last_error = str(exc)
            log.warning("market store write failed (trading continues): %s", exc)

    # ---------------------------------------------------------------- maintenance
    def maintain(self) -> None:
        """Compact finished days to Parquet and delete days past retention."""
        self._last_maintenance = self.now().timestamp()
        today = self.now().date()
        cutoff = today - timedelta(days=self.retention_days)
        with self._lock:
            for path in sorted(self.root.glob("*/*/*.*")):
                try:
                    day = date.fromisoformat(path.stem)
                except ValueError:
                    continue
                try:
                    if day < cutoff:
                        path.unlink()
                    elif path.suffix == ".csv" and day < today:
                        self._compact(path)
                except (OSError, ValueError) as exc:
                    self.last_error = f"{path.name}: {exc}"
                    log.warning("market store maintenance %s: %s", path, exc)

    def _compact(self, csv: Path) -> None:
        target = csv.with_suffix(".parquet")
        frames = [pd.read_parquet(target)] if target.exists() else []
        frames.append(self._read_csv(csv))       # last: newer rows win the de-duplication
        merged = _dedupe(pd.concat(frames))
        temp = target.with_suffix(".parquet.tmp")
        merged.to_parquet(temp, compression="zstd")
        temp.replace(target)
        csv.unlink()

    # ---------------------------------------------------------------- reads
    def load(self, coin: str, interval: str, start, end) -> pd.DataFrame:
        """Stored candles with start <= open_time < end (empty frame if none)."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        frames = []
        with self._lock:
            day = start.date()
            while day <= (end - pd.Timedelta(microseconds=1)).date():
                for suffix in (".parquet", ".csv"):
                    path = self._day(interval, coin, day, suffix)
                    if not path.exists():
                        continue
                    try:
                        frames.append(pd.read_parquet(path) if suffix == ".parquet" else self._read_csv(path))
                    except Exception as exc:     # a torn last line or a bad file never stops startup
                        log.warning("market store: unreadable %s (%s)", path, exc)
                day += timedelta(days=1)
        if not frames:
            return _empty()
        data = _dedupe(pd.concat(frames))
        return data[(data.index >= start) & (data.index < end)]

    def status(self) -> dict:
        return {"root": str(self.root), "written": self.written, "dropped": self.dropped,
                "queued": self._queue.qsize(), "paused": self.paused, "last_error": self.last_error,
                "retention_days": self.retention_days}

    # ---------------------------------------------------------------- helpers
    def _day(self, interval: str, coin: str, day: date, suffix: str) -> Path:
        return self.root / interval / coin / f"{day.isoformat()}{suffix}"

    @staticmethod
    def _read_csv(path: Path) -> pd.DataFrame:
        raw = pd.read_csv(path, on_bad_lines="skip")
        raw = raw[pd.to_numeric(raw["open_time_ms"], errors="coerce").notna()]
        index = pd.to_datetime(raw["open_time_ms"].astype("int64"), unit="ms", utc=True).rename("open_time")
        out = raw[COLUMNS].apply(pd.to_numeric, errors="coerce").set_axis(index)
        return out.dropna(subset=["open", "high", "low", "close"])


def _dedupe(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame.index = frame.index.rename("open_time")
    if "trades" in frame:
        frame["trades"] = frame["trades"].fillna(0).astype("int64")
    return frame
