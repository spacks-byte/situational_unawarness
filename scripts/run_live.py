"""Live runner: CompetitionStrategy -> Engine -> LibraryExchangePort (Roostoo). See docs/LIVE_RUNBOOK.md.

    # 1) TEST account, dry run (reads balances/tickers, sends NO orders)
    python3 scripts/run_live.py --state-dir results/live_test_dry
    # 2) TEST account, real orders
    ROOSTOO_CONFIRM_LIVE=YES python3 scripts/run_live.py --live --state-dir results/live_test
    # 3) Competition account (Oct 4): same as 2 with the competition .env and a fresh state dir

Credentials and BASE_URL come from crypto-roostoo-api/.env (never pass them on the command line).
Each account/mode needs its OWN --state-dir: the engine journal makes signal_ids one-shot and the
strategy state holds start equity + lock-in.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.experiments import UNIVERSE  # noqa: E402
from src.engine.app import Engine  # noqa: E402
from src.engine.clock import RealClock  # noqa: E402
from src.strategy_bridge import load_competition_config  # noqa: E402
from src.strategy_bridge.live_strategy import CompetitionStrategy  # noqa: E402
from src.strategy_bridge.live_strategy import mode_spec  # noqa: E402
from src.strategy_bridge.market_data import BarBuffer, binance_public_fetch  # noqa: E402
from src.strategy_bridge.repeg import RepegPort  # noqa: E402
from src.strategy_bridge.throttle import ThrottledPort  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="comp", choices=["comp", "neutral"])
    p.add_argument("--state-dir", required=True)
    p.add_argument("--live", action="store_true", help="send real orders (needs ROOSTOO_CONFIRM_LIVE=YES)")
    p.add_argument("--max-http-per-min", type=int, default=25)
    p.add_argument("--no-repeg", action="store_true", help="send limits at the snapshot price (no fresh ticker per order)")
    a = p.parse_args(argv)
    live = a.live and os.environ.get("ROOSTOO_CONFIRM_LIVE") == "YES"
    if a.live and not live:
        print("--live needs ROOSTOO_CONFIRM_LIVE=YES in the environment", file=sys.stderr)
        return 2

    state = Path(a.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(state / "bot.log")])
    log = logging.getLogger("run_live")

    from src.engine.ports.library_port import LibraryExchangePort   # imports the raw client (.env)

    clock = RealClock()
    cfg = load_competition_config(dry_run=not live, live_mode=live)
    port = ThrottledPort(LibraryExchangePort(), clock, max_per_minute=a.max_http_per_min)
    if not a.no_repeg:   # latency guard: price each limit off a ticker read just before it is sent
        port = RepegPort(port, mode_spec(a.mode)["limit_offset_bps"])
    engine = Engine(port, config=cfg, state_path=state / "engine_state.db", audit_path=state / "engine_audit.jsonl",
                    clock=clock)
    buffer = BarBuffer(list(UNIVERSE), fetch=binance_public_fetch(), window_days=50)
    strategy = CompetitionStrategy(buffer, mode=a.mode, state_path=state / "strategy_state.json", clock=clock)
    log.info("starting mode=%s live=%s dry_run=%s poll=%ss state=%s", a.mode, cfg.live_mode, cfg.dry_run,
             cfg.strategy_poll_interval_seconds, state)
    try:
        while True:
            started = time.monotonic()
            try:
                result = engine.run_once(strategy)
                if result["status"] != "DUPLICATE":
                    log.info("%s %s ops=%s reasons=%s", result["signal_id"], result["status"],
                             [(o["kind"], o["symbol"], round(o["amount_usd"], 2), o["status"]) for o in result.get("operations", [])],
                             result.get("reasons"))
            except Exception:                        # keep the loop alive; the next poll re-reads state
                log.exception("loop iteration failed")
            clock.sleep(max(1.0, cfg.strategy_poll_interval_seconds - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
