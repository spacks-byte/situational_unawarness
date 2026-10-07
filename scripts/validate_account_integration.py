"""Validate one account hosting independent MM and RXM strategy runtimes.

This integration check uses the profile that enables both strategies, each with
its own decisions, execution path and allocated capital. It prepares RXM's 15m
history from verified cached seconds; MM consumes its own one-second history.
The shared account must keep their cash, inventory and order reservations separate.

For a single-strategy replay, use `tradebot replay --strategies rxm` or
`tradebot replay --strategies mm-10m-fluctuation` with the market-making config.

Run with PYTHONPATH=src python3 scripts/validate_account_integration.py --cache ... --out ...
No downloads and no live exchange access. Results and derived candles stay under --out.
"""
import argparse
import json
from pathlib import Path

import pandas as pd

from tradebot.core.config import Settings
from tradebot.data.binance_vision import klines_path
from tradebot.live.shared_replay import cached_seconds, run_shared_replay
from tradebot.strategy.library.rxm import UNIVERSE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--start", default="2026-09-21T00:00:00Z")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    begin = pd.Timestamp(args.start)
    end = begin+pd.Timedelta(days=args.days)
    out = Path(args.out)
    settings = Settings.load("config/market-making.yaml")
    settings.data.dir = str(out / "inputs")
    settings.market_making.replay_cache_dir = args.cache
    for coin in UNIVERSE:
        path = klines_path(settings.data.dir, coin, "15m")
        if path.exists():
            continue
        parts = []
        for day in pd.date_range((begin-pd.Timedelta(days=50)).floor("D"), end-pd.Timedelta(days=1), freq="D"):
            df = cached_seconds(args.cache, coin, day, day+pd.Timedelta(days=1))
            grouped = df.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                               "volume": "sum", "taker_buy_base": "sum", "trades": "sum"})
            # The binary PoC cache has no quote-volume fields. RXM uses closes and base volume;
            # these unused columns are marked as approximations in provenance.
            grouped["quote_volume"] = grouped["volume"]*grouped["close"]
            grouped["taker_buy_quote"] = grouped["taker_buy_base"]*grouped["close"]
            parts.append(grouped)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.concat(parts).to_parquet(path)
        print(f"prepared {coin}", flush=True)
    summary = run_shared_replay(settings, args.start, args.days, 100_000, out / "replay")
    (out / "validation.json").write_text(json.dumps({
        "source": "SHA256-verified Binance one-second PoC cache; actual AccountRunner/Engine",
        "rxm_inputs": "15m OHLC aggregated from seconds; unused quote-volume fields approximated using close",
        "summary": summary,
    }, indent=2))


if __name__ == "__main__":
    main()
