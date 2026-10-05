"""Compare live Python decisions to native PoC decisions on identical cached inputs.

The native trace supplies pre-decision inventory/cash, isolating strategy parity
from the live transport and its timing. No live API or downloads are used.
"""
import argparse
import csv
import json
from pathlib import Path
import subprocess

import pandas as pd

from tradebot.live.shared_replay import cached_seconds
from tradebot.strategy.library.mm_fluctuation import MMFluctuation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poc", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--start", default="2026-09-21T00:00:00Z")
    args = parser.parse_args()
    poc, out = Path(args.poc).resolve(), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    binary = out / "native-reference"
    subprocess.run(["c++", "-std=c++17", "-O2", str(poc / "native/engine.cpp"), "-o", str(binary)], check=True)
    start = pd.Timestamp(args.start)
    end = start+pd.Timedelta(days=1)
    warmup = start-pd.Timedelta(seconds=3600)
    results = {}
    for coin in ("PEPE", "BONK", "1000CHEEMS"):
        manifest, trace = out / f"{coin}-files.txt", out / f"{coin}-native.csv"
        manifest.write_text("\n".join(str(poc / "data/binance/spot" / f"{coin}USDT/1s/{d.date()}.bin")
                                      for d in pd.date_range(warmup.floor("D"), start.floor("D"), freq="D"))+"\n")
        subprocess.run([str(binary), "--files", str(manifest), "--start", str(int(start.timestamp())),
                        "--end", str(int(end.timestamp())), "--trace", str(trace), "--output", str(out / f"{coin}-native.json"),
                        "--execution-model", "roostoo", "--model", "combined", "--allow-shorts", "0",
                        "--quote-refresh-seconds", "600", "--min-round-trip-bps", "10", "--feature-lag-seconds", "1",
                        "--fill-penetration-probability", "0", "--tick-size", "1e-8", "--step-size", "1e-8",
                        "--min-notional", "1", "--order-notional", "500", "--max-inventory-notional", "7000"], check=True)
        data = cached_seconds(poc / "data", coin, warmup, end)
        strategy, features = MMFluctuation(), {}
        count = 0
        with trace.open() as handle:
            for row in csv.DictReader(handle):
                if row["quote_posted"] != "1":
                    continue
                now = pd.Timestamp(int(row["timestamp"]), unit="s", tz="UTC")
                book = {"capital": 10000., "quantity": float(row["inventory_before"]),
                        "cash": float(row["cash_before"])+float(row["bid_qty"])*float(row["bid_price"])*1.0005}
                rules = {f"{coin}/USD": {"CanTrade": True, "PricePrecision": 8, "AmountPrecision": 8, "MiniOrder": 1}}
                batch = strategy.generate({coin: data}, now=now.to_pydatetime(), books={coin: book}, features=features, rules=rules)
                obs = batch.observations[coin]
                for name, native in (("bid", "bid_price"), ("ask", "ask_price")):
                    if abs(obs[name]-float(row[native])) > 1e-16:
                        raise AssertionError((coin, now, name, obs[name], row[native]))
                for side, field in (("BUY", "bid_qty"), ("SELL", "ask_qty")):
                    quantity = next((q.quantity for q in batch.quotes if q.side == side), 0)
                    if abs(quantity-float(row[field])) > 1e-6:
                        raise AssertionError((coin, now, side, quantity, row[field]))
                count += 1
        results[coin] = count
    payload = {"matched_decisions": results, "price_tolerance": 1e-16, "quantity_tolerance": 1e-6,
               "inputs": "identical 3600s warmup, 1s lag, 600s refresh, native pre-decision cash/inventory"}
    (out / "validation.json").write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
