"""Reconcile exported MM fills with each coin's ending ledger and P&L."""
import argparse
import csv
import json
import math
from pathlib import Path


def validate(folder):
    folder = Path(folder)
    summary = json.loads((folder / "summary.json").read_text())
    books = summary["account"]["mm"]
    reconstructed = {c: {"cash": b["capital"], "quantity": 0., "fees": 0., "fills": 0} for c, b in books.items()}
    with (folder / "ledger-fills.csv").open() as handle:
        for row in csv.DictReader(handle):
            if row["strategy"] != "mm-10m-fluctuation":
                continue
            if row["side"] not in {"BUY", "SELL"}:
                raise AssertionError("MM short event")
            b = reconstructed[row["coin"]]
            sign = 1 if row["side"] == "BUY" else -1
            b["quantity"] += sign*float(row["quantity"])
            b["cash"] -= sign*float(row["value"])+float(row["fee_delta"])
            b["fees"] += float(row["fee_delta"])
            b["fills"] += 1
    for coin, book in books.items():
        for field in ("cash", "quantity", "fees"):
            if not math.isclose(book[field], reconstructed[coin][field], rel_tol=1e-9, abs_tol=1e-5):
                raise AssertionError((coin, field, book[field], reconstructed[coin][field]))
        if not math.isclose(book["net_pnl"], book["realized_pnl"]+book["unrealized_pnl"], abs_tol=1e-5):
            raise AssertionError((coin, "P&L mismatch"))
        if book["short_open_fees"] or book["short_close_fees"]:
            raise AssertionError((coin, "short fee"))
    result = {"reconciled": True, "mm": reconstructed}
    (folder / "export-reconciliation.json").write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    print(json.dumps(validate(parser.parse_args().folder), indent=2))
