"""
`python -m tradebot --config config/competition.yaml live`: run RXM on Roostoo, unattended.

    python -m tradebot --config config/competition.yaml live                        # paper: reads, sends nothing
    TRADEBOT_CONFIRM_LIVE=YES python -m tradebot --config config/competition.yaml live --live

Every poll: read the account, get RXM's target, let the engine send what's missing. A failed
cycle is logged and retried at the next poll; it never stops the bot. Restarts are safe, because
positions are read from the exchange and the strategy keeps its start equity and lock-in in
<state-dir>/strategy_state.json. Use a fresh --state-dir for each account.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from tradebot.core.clock import RealClock
from tradebot.core.config import Settings
from tradebot.engine import Engine
from tradebot.exchange.client import RoostooClient
from tradebot.exchange.port import RoostooExchangePort
from tradebot.live.bars import BarBuffer, binance_fetch
from tradebot.live.rxm import CompetitionStrategy
from tradebot.strategy.library.rxm import PRESETS, UNIVERSE

log = logging.getLogger(__name__)


def add_parser(subparsers) -> None:
    p = subparsers.add_parser("live", help="Run the RXM strategy on Roostoo (paper unless --live)",
                              description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", default="comp", choices=sorted(PRESETS))
    p.add_argument("--state-dir", default="var/live", help="engine journal, audit log and strategy state")
    p.add_argument("--live", action="store_true", help="send real orders (needs TRADEBOT_CONFIRM_LIVE=YES)")
    p.set_defaults(handler=run)


def run(args, settings: Settings) -> int:
    config = settings.execution
    if args.live:
        if os.environ.get("TRADEBOT_CONFIRM_LIVE") != "YES":
            print("--live sends real orders: set TRADEBOT_CONFIRM_LIVE=YES to confirm")
            return 2
        config = config.model_copy(update={"dry_run": False, "live_mode": True})
    state = Path(args.state_dir)
    clock = RealClock()
    engine = Engine(RoostooExchangePort(RoostooClient(settings=settings.exchange)), config=config, clock=clock,
                    state_path=state / "engine_state.db", audit_path=state / "engine_audit.jsonl")
    strategy = CompetitionStrategy(BarBuffer(UNIVERSE, binance_fetch()), args.mode, clock=clock,
                                   state_path=state / "strategy_state.json")
    log.info("RXM %s | orders %s | state %s", args.mode, "LIVE" if args.live else "paper (dry run)", state)
    try:
        while True:
            try:
                result = engine.run_once(strategy)
                if result["status"] != "DUPLICATE":
                    log.info("%s %s %s", result["signal_id"], result["status"],
                             [(o["kind"], o["symbol"], round(o["amount_usd"], 2), o["status"]) for o in result["operations"]])
            except Exception:
                log.exception("cycle failed; retrying at the next poll")
            clock.sleep(config.strategy_poll_interval_seconds)
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        engine.close()
    return 0
