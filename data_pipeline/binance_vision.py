"""
Download historical spot klines from Binance Vision (https://data.binance.vision)
and store them as one Parquet file per symbol/interval for backtesting.

Usage:
    python -m data_pipeline.binance_vision
    python -m data_pipeline.binance_vision --symbols BTC,ETH --intervals 15m --start 2025-01-01
"""
import argparse
import hashlib
import io
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://data.binance.vision"
S3_LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "binance"
RAW_DIR = DATA_DIR / "raw"
KLINES_DIR = DATA_DIR / "klines"

DEFAULT_INTERVALS = ["5m", "15m"]
QUOTE = "USDT"

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
KEEP_COLUMNS = [
    "open", "high", "low", "close", "volume",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote",
]

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


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

def to_binance_symbol(pair_or_coin: str) -> str:
    """BTC, BTC/USD, BTCUSDT -> BTCUSDT"""
    s = pair_or_coin.strip().upper()
    if "/" in s:
        s = s.split("/")[0]
    if s.endswith(QUOTE):
        return s
    return f"{s}{QUOTE}"


def roostoo_symbols() -> List[str]:
    """Binance symbols for every pair listed on Roostoo."""
    from dotenv import load_dotenv

    roostoo_dir = REPO_ROOT / "crypto-roostoo-api"
    load_dotenv(roostoo_dir / ".env")  # utilities.py reads BASE_URL at import time
    sys.path.insert(0, str(roostoo_dir))
    from utilities import get_exchange_info  # noqa: E402

    info = get_exchange_info()
    if not info or not info.get("TradePairs"):
        raise RuntimeError(
            "Could not fetch Roostoo exchange info. "
            "Check crypto-roostoo-api/.env or pass --symbols explicitly."
        )
    return sorted(to_binance_symbol(p) for p in info["TradePairs"])


def symbol_exists(symbol: str) -> bool:
    """Check Binance Vision's bucket listing for any daily kline data for the symbol."""
    params = {"delimiter": "/", "prefix": f"data/spot/daily/klines/{symbol}/"}
    resp = _session.get(S3_LIST_URL, params=params, timeout=30)
    resp.raise_for_status()
    return "<CommonPrefixes>" in resp.text


# ---------------------------------------------------------------------------
# File planning & download
# ---------------------------------------------------------------------------

def _month_starts(start: date, end: date) -> List[date]:
    months = []
    cur = start.replace(day=1)
    while cur < end:
        months.append(cur)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def _days(start: date, end: date) -> List[date]:
    return [start + timedelta(days=i) for i in range((end - start).days)]


def _monthly_path(symbol: str, interval: str, month: date) -> str:
    return (f"data/spot/monthly/klines/{symbol}/{interval}/"
            f"{symbol}-{interval}-{month:%Y-%m}.zip")


def _daily_path(symbol: str, interval: str, day: date) -> str:
    return (f"data/spot/daily/klines/{symbol}/{interval}/"
            f"{symbol}-{interval}-{day:%Y-%m-%d}.zip")


def download(remote_path: str) -> Optional[Path]:
    """
    Download a zip (and verify its .CHECKSUM) into RAW_DIR.
    Returns the local path, or None if the file doesn't exist on Binance Vision.
    Already-downloaded files are reused.
    """
    local = RAW_DIR / remote_path
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
                print(f"  [WARN] checksum mismatch for {remote_path} (attempt {attempt + 1})")
                continue

        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(".part")
        tmp.write_bytes(content)
        tmp.replace(local)
        return local

    raise RuntimeError(f"Checksum verification failed twice for {remote_path}")


def fetch_files(symbol: str, interval: str, start: date, end: date,
                pool: ThreadPoolExecutor) -> List[Path]:
    """
    Monthly zips for completed months, daily zips for the current month
    (and for any completed month whose monthly zip isn't published yet).
    """
    today = datetime.now(timezone.utc).date()
    current_month = today.replace(day=1)
    months = _month_starts(start, end)

    past_months = [m for m in months if m < current_month]
    monthly_results = list(pool.map(
        lambda m: (m, download(_monthly_path(symbol, interval, m))), past_months))

    files = [path for _, path in monthly_results if path]

    # Months needing daily files: unpublished monthly zips + the current month
    daily_months = [m for m, path in monthly_results if path is None]
    if any(m == current_month for m in months):
        daily_months.append(current_month)

    days = []
    for m in daily_months:
        month_end = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        # Today's daily file isn't published until tomorrow
        days += _days(max(m, start), min(month_end, end, today))

    files += [p for p in pool.map(
        lambda d: download(_daily_path(symbol, interval, d)), days) if p]
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
    step = pd.Timedelta(interval.replace("m", "min"))
    expected = int((df.index[-1] - df.index[0]) / step) + 1
    return expected - len(df)


def build_symbol(symbol: str, interval: str, start: date, end: date,
                 pool: ThreadPoolExecutor) -> Optional[dict]:
    files = fetch_files(symbol, interval, start, end, pool)
    if not files:
        return None

    df = pd.concat([read_kline_zip(p) for p in files])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    df = df[(df.index >= start_ts) & (df.index < end_ts)]
    if df.empty:
        return None

    out = KLINES_DIR / interval / f"{symbol}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out)

    return {
        "rows": len(df),
        "first": df.index[0],
        "last": df.index[-1],
        "gaps": count_gaps(df, interval),
        "files": len(files),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    today = datetime.now(timezone.utc).date()
    p = argparse.ArgumentParser(description="Download Binance Vision spot klines.")
    p.add_argument("--symbols", help="Comma-separated coins/pairs (e.g. BTC,ETH). "
                                     "Default: all Roostoo pairs.")
    p.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS),
                   help="Comma-separated kline intervals (default: 5m,15m)")
    p.add_argument("--start", type=date.fromisoformat,
                   default=today - timedelta(days=365),
                   help="Start date YYYY-MM-DD, inclusive (default: 365 days ago)")
    p.add_argument("--end", type=date.fromisoformat, default=today,
                   help="End date YYYY-MM-DD, exclusive (default: today UTC)")
    p.add_argument("--workers", type=int, default=8)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]

    if args.symbols:
        symbols = sorted({to_binance_symbol(s) for s in args.symbols.split(",") if s.strip()})
    else:
        try:
            symbols = roostoo_symbols()
        except RuntimeError as e:
            print(f"[ERROR] {e}")
            return 1

    print(f"Range: {args.start} -> {args.end} (end exclusive) | "
          f"intervals: {intervals} | symbols: {len(symbols)}")

    missing: List[str] = []
    failed: List[str] = []
    results: List[Tuple[str, str, dict]] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for symbol in symbols:
            try:
                exists = symbol_exists(symbol)
            except requests.RequestException as e:
                print(f"[FAIL] {symbol}: {e}")
                failed.append(symbol)
                continue
            if not exists:
                print(f"[SKIP] {symbol}: not on Binance spot")
                missing.append(symbol)
                continue
            for interval in intervals:
                try:
                    stats = build_symbol(symbol, interval, args.start, args.end, pool)
                except (requests.RequestException, RuntimeError) as e:
                    print(f"[FAIL] {symbol} {interval}: {e}")
                    failed.append(f"{symbol} {interval}")
                    continue
                if stats is None:
                    print(f"[SKIP] {symbol} {interval}: no data in range")
                    continue
                results.append((symbol, interval, stats))
                print(f"[OK]   {symbol:<16} {interval:<4} rows={stats['rows']:>7} "
                      f"{stats['first']:%Y-%m-%d %H:%M} -> {stats['last']:%Y-%m-%d %H:%M} "
                      f"gaps={stats['gaps']}")

    print(f"\nDone: {len(results)} datasets written to {KLINES_DIR}")
    if missing:
        print(f"Not on Binance ({len(missing)}): {', '.join(missing)}")
    if failed:
        print(f"Failed ({len(failed)}), re-run to retry: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
