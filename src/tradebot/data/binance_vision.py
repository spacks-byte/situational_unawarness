"""
Download historical spot klines from Binance Vision (https://data.binance.vision) and store them
as one Parquet file per symbol/interval: <data_dir>/klines/<interval>/<BINANCE_SYMBOL>.parquet.

Monthly zips are used for completed months, daily zips for the current month (and for any month
whose monthly zip isn't published yet). Zips are cached under <data_dir>/raw and SHA-256 verified.
Run through the CLI: `python -m tradebot data --symbols BTC,ETH --intervals 15m`.
"""
from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from tradebot.core.intervals import interval_to_timedelta
from tradebot.core.symbols import to_binance, to_coin

logger = logging.getLogger(__name__)

BASE_URL = "https://data.binance.vision"
S3_LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
KEEP_COLUMNS = [
    "open", "high", "low", "close", "volume",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote",
]


def klines_path(data_dir: str | Path, symbol: str, interval: str) -> Path:
    return Path(data_dir) / "klines" / interval / f"{to_binance(symbol)}.parquet"


def _make_session() -> requests.Session:
    """Session that retries dropped connections and 5xx responses with backoff."""
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    session = requests.Session()
    session.mount("https://", adapter)
    return session


_session = _make_session()
_hosts = [BASE_URL, S3_LIST_URL]   # reordered at runtime so a blocked host is only tried once


def symbol_exists(symbol: str) -> bool:
    """Check Binance Vision's bucket listing for any daily kline data for the symbol."""
    params = {"delimiter": "/", "prefix": f"data/spot/daily/klines/{to_binance(symbol)}/"}
    resp = _session.get(S3_LIST_URL, params=params, timeout=30)
    resp.raise_for_status()
    return "<CommonPrefixes>" in resp.text


# ---------------------------------------------------------------------------
# File planning & download
# ---------------------------------------------------------------------------

def _next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def _month_starts(start: date, end: date) -> list[date]:
    months, cur = [], start.replace(day=1)
    while cur < end:
        months.append(cur)
        cur = _next_month(cur)
    return months


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days)]


def _monthly_path(symbol: str, interval: str, month: date) -> str:
    return f"data/spot/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{month:%Y-%m}.zip"


def _daily_path(symbol: str, interval: str, day: date) -> str:
    return f"data/spot/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{day:%Y-%m-%d}.zip"


def download(remote_path: str, raw_dir: Path) -> Optional[Path]:
    """
    Download a zip (verifying its .CHECKSUM) into raw_dir, reusing files already there.
    Returns the local path, or None if the file doesn't exist on Binance Vision.
    """
    local = raw_dir / remote_path
    if local.exists():
        return local

    for attempt in range(2):
        resp, url = None, None
        # Some networks block the data.binance.vision CDN; the S3 bucket behind it serves the same files.
        for host in list(_hosts):
            try:
                resp = _session.get(f"{host}/{remote_path}", timeout=120)
                url = f"{host}/{remote_path}"
                break
            except requests.RequestException:
                if len(_hosts) > 1 and _hosts[0] == host:
                    _hosts.append(_hosts.pop(0))     # demote the failing host for all later files
                continue
        if resp is None:
            raise requests.ConnectionError(f"could not reach Binance Vision for {remote_path}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        content = resp.content

        checksum = _session.get(url + ".CHECKSUM", timeout=30)
        if checksum.status_code == 200:
            expected = checksum.text.split()[0].strip().lower()
            if hashlib.sha256(content).hexdigest() != expected:
                logger.warning("Checksum mismatch for %s (attempt %d)", remote_path, attempt + 1)
                continue

        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(".part")
        tmp.write_bytes(content)
        tmp.replace(local)
        return local

    raise RuntimeError(f"Checksum verification failed twice for {remote_path}")


def fetch_files(symbol: str, interval: str, start: date, end: date, raw_dir: Path,
                pool: ThreadPoolExecutor) -> list[Path]:
    """Monthly zips for completed months; daily zips for the current month or unpublished months."""
    symbol = to_binance(symbol)
    today = datetime.now(timezone.utc).date()
    current_month = today.replace(day=1)
    months = _month_starts(start, end)

    past_months = [m for m in months if m < current_month]
    monthly = list(pool.map(lambda m: (m, download(_monthly_path(symbol, interval, m), raw_dir)), past_months))
    files = [path for _, path in monthly if path]

    daily_months = [m for m, path in monthly if path is None]
    if current_month in months:
        daily_months.append(current_month)

    days: list[date] = []
    for m in daily_months:
        # Today's daily file isn't published until tomorrow
        days += _days(max(m, start), min(_next_month(m), end, today))

    files += [p for p in pool.map(lambda d: download(_daily_path(symbol, interval, d), raw_dir), days) if p]
    return files


# ---------------------------------------------------------------------------
# Parsing & storage
# ---------------------------------------------------------------------------

def read_kline_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as zf:
        with zf.open(zf.namelist()[0]) as f:
            df = pd.read_csv(io.TextIOWrapper(f), header=None, names=KLINE_COLUMNS)

    # Some files ship with a header row: drop anything non-numeric
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
    df = df.dropna(subset=["open_time"])

    # Spot files from 2025-01-01 onwards use microseconds instead of milliseconds
    ts = df["open_time"].astype("int64")
    ts = ts.where(ts < 10**14, ts // 1000)
    df.index = pd.to_datetime(ts, unit="ms", utc=True)
    df.index.name = "open_time"

    out = df[KEEP_COLUMNS].apply(pd.to_numeric, errors="coerce")
    out["trades"] = out["trades"].astype("int64")
    return out


def count_gaps(df: pd.DataFrame, interval: str) -> int:
    if df.empty:
        return 0
    expected = int((df.index[-1] - df.index[0]) / interval_to_timedelta(interval)) + 1
    return expected - len(df)


def build_symbol(symbol: str, interval: str, start: date, end: date, data_dir: Path,
                 pool: ThreadPoolExecutor) -> Optional[dict]:
    """Download and store one symbol/interval. Returns summary stats, or None if there's no data."""
    files = fetch_files(symbol, interval, start, end, data_dir / "raw", pool)
    if not files:
        return None

    df = pd.concat([read_kline_zip(p) for p in files])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df.index >= pd.Timestamp(start, tz="UTC")) & (df.index < pd.Timestamp(end, tz="UTC"))]
    if df.empty:
        return None

    out = klines_path(data_dir, symbol, interval)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        # Merge with what's stored so a narrow download never discards existing history
        df = pd.concat([pd.read_parquet(out), df])
        df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_parquet(out)
    return {"rows": len(df), "first": df.index[0], "last": df.index[-1],
            "gaps": count_gaps(df, interval), "files": len(files)}


@dataclass
class DownloadReport:
    written: list[tuple[str, str, dict]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # not listed on Binance spot
    empty: list[str] = field(default_factory=list)     # listed, but no data in range
    failed: list[str] = field(default_factory=list)    # network errors after retries


def download_klines(symbols: list[str], intervals: list[str], start: date, end: date,
                    data_dir: str | Path, workers: int = 8) -> DownloadReport:
    """Download klines for coins (any symbol form) over [start, end). Failures are reported, not raised."""
    data_dir = Path(data_dir)
    report = DownloadReport()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for coin in sorted({to_coin(s) for s in symbols}):
            try:
                exists = symbol_exists(coin)
            except requests.RequestException as e:
                logger.error("%s: listing failed: %s", coin, e)
                report.failed.append(coin)
                continue
            if not exists:
                logger.info("%s: not on Binance spot, skipped", coin)
                report.missing.append(coin)
                continue
            for interval in intervals:
                try:
                    stats = build_symbol(coin, interval, start, end, data_dir, pool)
                except (requests.RequestException, RuntimeError) as e:
                    logger.error("%s %s: %s", coin, interval, e)
                    report.failed.append(f"{coin} {interval}")
                    continue
                if stats is None:
                    logger.info("%s %s: no data in range", coin, interval)
                    report.empty.append(f"{coin} {interval}")
                    continue
                report.written.append((coin, interval, stats))
                logger.info("%-10s %-4s rows=%7d %s -> %s gaps=%d", coin, interval, stats["rows"],
                            f"{stats['first']:%Y-%m-%d %H:%M}", f"{stats['last']:%Y-%m-%d %H:%M}", stats["gaps"])
    return report
