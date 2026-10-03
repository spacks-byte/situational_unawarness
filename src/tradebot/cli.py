"""
Command-line entry point: `python -m tradebot <command>` (or `tradebot <command>` once installed).

Commands:
  data      download historical klines from Binance Vision
  backtest  backtest a strategy (full period or rolling competition windows)
  api       interactive Roostoo API test menu
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from tradebot.backtest import cli as backtest_cli
from tradebot.core.config import Settings
from tradebot.core.log import setup_logging


def _data_command(args, settings: Settings) -> int:
    from tradebot.data.binance_vision import download_klines
    from tradebot.exchange.client import RoostooClient, RoostooError

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        try:
            pairs = RoostooClient(settings=settings.exchange).exchange_info().get("TradePairs", {})
        except RoostooError as e:
            print(f"[ERROR] Could not list Roostoo pairs ({e}); pass --symbols explicitly")
            return 1
        symbols = sorted(pairs)

    intervals = [i.strip() for i in (args.intervals or ",".join(settings.data.intervals)).split(",") if i.strip()]
    end = args.end or datetime.now(timezone.utc).date()
    start = args.start or end - timedelta(days=settings.data.lookback_days)
    print(f"Range: {start} -> {end} (end exclusive) | intervals: {intervals} | symbols: {len(symbols)}")

    report = download_klines(symbols, intervals, start, end, settings.data.dir, args.workers or settings.data.workers)
    print(f"\nDone: {len(report.written)} datasets written to {settings.data.dir}/klines")
    for label, items in (("Not on Binance", report.missing), ("No data in range", report.empty)):
        if items:
            print(f"{label} ({len(items)}): {', '.join(items)}")
    if report.failed:
        print(f"Failed ({len(report.failed)}), re-run to retry: {', '.join(report.failed)}")
        return 1
    return 0


def _api_command(args, settings: Settings) -> int:
    from tradebot.exchange.manual import run_menu

    run_menu(settings)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradebot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="Config YAML (default: $TRADEBOT_CONFIG or config/default.yaml)")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--log-file", help="Also write logs to this file (rotating)")
    sub = parser.add_subparsers(dest="command", required=True)

    data = sub.add_parser("data", help="Download historical klines from Binance Vision")
    data.add_argument("--symbols", help="Comma-separated coins (default: every Roostoo pair)")
    data.add_argument("--intervals", help="Comma-separated intervals (default: config data.intervals)")
    data.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD inclusive (default: lookback_days ago)")
    data.add_argument("--end", type=date.fromisoformat, help="YYYY-MM-DD exclusive (default: today UTC)")
    data.add_argument("--workers", type=int)
    data.set_defaults(handler=_data_command)

    backtest_cli.add_parser(sub)

    api = sub.add_parser("api", help="Interactive Roostoo API test menu")
    api.set_defaults(handler=_api_command)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    settings = Settings.load(args.config)
    return args.handler(args, settings)


if __name__ == "__main__":
    sys.exit(main())
