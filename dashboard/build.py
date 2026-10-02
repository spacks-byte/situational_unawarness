"""
Build the static trading-desk dashboard (one self-contained HTML file, no server, no network).

    python -m dashboard.build --source backtest --out results/dashboard.html
    python -m dashboard.build --source engine --engine-dir results/engine --out results/dashboard.html --watch 30

--source backtest   runs the frozen 'comp' preset over the last --days of data/binance/klines/15m
--source engine     reads a live/mock engine run (audit JSONL + intent SQLite + snapshot JSON)
--watch N           rebuild every N seconds; the page auto-reloads at the same cadence
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict

from dashboard.sources import load_backtest, load_engine, to_json

TEMPLATE = Path(__file__).with_name("template.html")


def render(model: Dict[str, Any], refresh_seconds: int | None = None) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = to_json(model).replace("</", "<\\/")  # never close the <script> early
    html = html.replace("/*__DATA__*/", payload)
    if refresh_seconds:
        html = html.replace("<!--__REFRESH__-->", f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">')
    return html


def write_html(model: Dict[str, Any], out: str | Path, refresh_seconds: int | None = None) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(render(model, refresh_seconds), encoding="utf-8")
    tmp.replace(out)  # atomic: a browser reload never sees a half-written file
    return out


def build_once(args) -> Path:
    if args.source == "backtest":
        model = load_backtest(days=args.days, end=args.end, variant=args.variant, kill_file=args.kill_file)
    else:
        model = load_engine(args.engine_dir, audit=args.audit, db=args.db, snapshot=args.snapshot,
                            initial=args.initial, kill_file=args.kill_file)
    return write_html(model, args.out, args.watch or None)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=("backtest", "engine"), default="backtest")
    p.add_argument("--out", default="results/dashboard.html")
    p.add_argument("--watch", type=int, default=0, help="rebuild every N seconds (0 = once)")
    p.add_argument("--kill-file", default="KILL", help="guard kill-switch file (default ./KILL)")
    g = p.add_argument_group("backtest source")
    g.add_argument("--days", type=int, default=14)
    g.add_argument("--end", default=None, help="window end date (exclusive), default = end of local data")
    g.add_argument("--variant", default="comp", choices=("comp", "neutral"))
    g = p.add_argument_group("engine source")
    g.add_argument("--engine-dir", default="results/engine")
    g.add_argument("--audit", default=None, help="audit JSONL (default: newest *audit*.jsonl in --engine-dir)")
    g.add_argument("--db", default=None, help="intent SQLite (default: newest *.db in --engine-dir)")
    g.add_argument("--snapshot", default=None, help="snapshot JSON (default: latest_snapshot.json / *snapshot*.json)")
    g.add_argument("--initial", type=float, default=100_000.0)
    args = p.parse_args(argv)

    while True:
        t0 = time.time()
        try:
            out = build_once(args)
            print(f"[dashboard] wrote {out} ({out.stat().st_size / 1024:.0f} KiB) in {time.time() - t0:.1f}s", flush=True)
        except Exception as exc:  # keep watching through transient read errors (file mid-write etc.)
            if not args.watch:
                raise
            print(f"[dashboard] build failed: {exc!r}", file=sys.stderr, flush=True)
        if not args.watch:
            return 0
        time.sleep(max(1, args.watch - (time.time() - t0)))


if __name__ == "__main__":
    raise SystemExit(main())
