"""
Command-line entry point: `python -m tradebot <command>` (or `tradebot <command>` once installed).

Commands:
  data       download historical klines from Binance Vision
  backtest   backtest a strategy (full period or rolling competition windows)
  live       run the bot unattended on Roostoo (dry run unless --live)
  replay     simulate the live bot on downloaded candles (no network)
  dashboard  build the trading-desk dashboard (HTML)
  account    read-only checks of the shared MM/RXM account (preflight, explain)
  api        interactive Roostoo API test menu
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from tradebot.backtest import cli as backtest_cli
from tradebot.core.config import Settings
from tradebot.core.log import LOG_FORMAT, setup_logging


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


def _live_overrides(args, settings: Settings) -> Settings:
    live = {k: v for k, v in (("mode", args.mode), ("state_dir", args.state_dir)) if v}
    if getattr(args, "strategies", None):
        live["strategies"] = [name.strip() for name in args.strategies.split(",") if name.strip()]
        names = live["strategies"]
        if not names or len(names) != len(set(names)) or any(n not in {"rxm", "mm-10m-fluctuation"} for n in names):
            raise ValueError("--strategies requires unique rxm / mm-10m-fluctuation names")
        if not settings.market_making.enabled and names != ["rxm"]:
            raise ValueError("MM/account selection requires --config config/market-making.yaml")
        if len(names) == 1:
            live["strategy"] = names[0]
    return settings.model_copy(update={"live": settings.live.model_copy(update=live)}) if live else settings


def _live_command(args, settings: Settings) -> int:
    from tradebot.live.runner import LiveRunner

    if args.live and os.environ.get("ROOSTOO_CONFIRM_LIVE") != "YES":
        print("--live sends real orders: set ROOSTOO_CONFIRM_LIVE=YES in the environment to confirm", file=sys.stderr)
        return 2
    try:
        settings = _live_overrides(args, settings)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    log_file = Path(settings.live.state_dir) / "bot.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=20_000_000, backupCount=10, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    try:
        if settings.market_making.enabled:
            from tradebot.live.account import AccountRunner
            runner = AccountRunner(settings, mode="live" if args.live else "dry-run")
        else:
            runner = LiveRunner(settings, mode="live" if args.live else "dry-run")
    except (RuntimeError, ValueError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    return runner.run(max_iterations=args.max_loops)


def _replay_command(args, settings: Settings) -> int:
    from tradebot.live.replay import run_replay

    try:
        settings = _live_overrides(args, settings)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    if settings.market_making.enabled:
        from tradebot.live.shared_replay import run_shared_replay
        if args.mm_cache:
            settings.market_making.replay_cache_dir = args.mm_cache
        run_shared_replay(settings, args.start, args.days, args.cash, args.out, keep_state=args.keep_state)
    else:
        run_replay(settings, args.start, args.days, args.cash, args.out, keep_state=args.keep_state)
    return 0


def _dashboard_command(args, settings: Settings) -> int:
    from tradebot.dashboard.build import main as dashboard_main

    return dashboard_main(args.dashboard_args)


def _desk_command(args, settings: Settings) -> int:
    from tradebot.dashboard.server import main as server_main

    return server_main(['--port', str(args.port)], settings=settings)


def _account_command(args, settings: Settings) -> int:
    import json

    from tradebot.live import preflight as checks

    if args.state_dir:
        settings.live.state_dir = args.state_dir
    if args.action == "explain":
        print(json.dumps(checks.explain(settings.live.state_dir, events=args.events), indent=2, default=str))
        return 0
    if not settings.market_making.enabled:
        print("[ERROR] preflight checks the shared account: pass --config config/market-making.yaml", file=sys.stderr)
        return 2
    from tradebot.exchange import RoostooClient, RoostooExchangePort
    from tradebot.live.runner import account_lock_path

    client = RoostooClient(settings=settings.exchange)
    if not client.api_key or not client.api_secret:
        print("[ERROR] Roostoo credentials missing", file=sys.stderr)
        return 2
    port = RoostooExchangePort(client)
    if args.action == 'repair-fills':
        import os
        from pathlib import Path
        from tradebot.live.fill_repair import audit, apply_manifest
        from tradebot.dashboard.remote import SupabaseLedger
        from tradebot.core.locking import AccountLock
        bot_id = os.getenv('TRADEBOT_BOT_ID', 'tradebot')
        path = Path(settings.live.state_dir) / 'portfolio.db'
        ledger = SupabaseLedger()
        remote = ledger.rows() if ledger.url and ledger.key else []
        if args.apply:
            manifest = json.loads(Path(args.apply).read_text())
            with AccountLock(account_lock_path(settings, port), state_dir=settings.live.state_dir):
                report = apply_manifest(path, manifest, port, bot_id=bot_id, remote_rows=remote)
        else:
            report = audit(path, port, bot_id=bot_id, remote_rows=remote)
        content = json.dumps(report, indent=2, allow_nan=False)
        if args.output:
            Path(args.output).write_text(content+'\n')
        print(content)
        return 0 if report.get('applicable') or report.get('applied') or report.get('already_applied') or not report.get('errors') else 1
    report = checks.preflight(settings, port, lock_path=account_lock_path(settings, port))
    print(json.dumps(report, indent=2, default=str) if args.json else checks.render(report))
    return 0 if report["ready"] else 1


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

    live = sub.add_parser("live", help="Run the bot unattended on Roostoo (dry run unless --live)")
    live.add_argument("--live", action="store_true",
                      help="send real orders (also needs ROOSTOO_CONFIRM_LIVE=YES); default is a dry run")
    live.add_argument("--strategies", help="independent account strategies: rxm, mm-10m-fluctuation, or both comma-separated")
    live.add_argument("--mode", help="strategy preset, e.g. comp | neutral (default: config live.mode)")
    live.add_argument("--state-dir", help="journal, logs and status (default: config live.state_dir)")
    live.add_argument("--max-loops", type=int, help="stop after N loops (testing)")
    live.set_defaults(handler=_live_command)

    replay = sub.add_parser("replay", help="Simulate the live bot on downloaded candles (no network)")
    replay.add_argument("--start", default="2026-09-01T00:16", help="UTC start time")
    replay.add_argument("--days", type=float, default=7.0)
    replay.add_argument("--cash", type=float, default=100_000.0)
    replay.add_argument("--strategies", help="independent account strategies to replay, comma-separated")
    replay.add_argument("--mode", help="strategy preset (default: config live.mode)")
    replay.add_argument("--state-dir", help=argparse.SUPPRESS)
    replay.add_argument("--out", default="results/replay")
    replay.add_argument("--mm-cache", help="verified shared-backtest 1s cache root for shared-account replay")
    replay.add_argument("--keep-state", action="store_true", help="keep previous state (restart test)")
    replay.set_defaults(handler=_replay_command)

    dash = sub.add_parser("dashboard", help="Build the trading-desk dashboard (see docs/DASHBOARD.md)",
                          add_help=False)
    dash.add_argument("dashboard_args", nargs=argparse.REMAINDER)
    dash.set_defaults(handler=_dashboard_command)

    desk = sub.add_parser('desk', help='Serve the interactive Supabase research dashboard locally')
    desk.add_argument('--port', type=int, default=8765)
    desk.set_defaults(handler=_desk_command)

    account = sub.add_parser("account", help="Account checks and reviewed fill recovery (no exchange orders)")
    account.add_argument("action", choices=["preflight", "explain", "repair-fills"],
                         help="preflight: venue and local state; explain: advisory issues; repair-fills: dry-run recovery")
    account.add_argument("--state-dir", help="Shared account state dir (default: live.state_dir)")
    account.add_argument("--json", action="store_true", help="preflight: print the full JSON report")
    account.add_argument("--events", type=int, default=20, help="explain: recent journal events to show")
    account.add_argument('--output', help='repair-fills: save the dry-run manifest/report')
    account.add_argument('--apply', metavar='MANIFEST', help='repair-fills: explicitly apply a reviewed manifest locally; queues telemetry corrections')
    account.set_defaults(handler=_account_command)

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
