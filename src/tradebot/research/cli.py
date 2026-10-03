"""
`python -m tradebot research ...`: test the RXM strategy before trusting it (docs/STRATEGY.md §5).

    python -m tradebot research windows                      # comp preset, 14-day competition windows
    python -m tradebot research windows --preset neutral --params k=4
    python -m tradebot research validate                     # the four steps, 200 / 100 permutations
    python -m tradebot research validate --perms 50 --wf-perms 20     # quick look
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot.backtest.cli import parse_params
from tradebot.core.config import Settings
from tradebot.data.loader import load_universe
from tradebot.research import validation as v
from tradebot.research import windows as w
from tradebot.strategy.library.rxm import PRESETS, UNIVERSE, ResidualMomentum


def add_parser(subparsers) -> None:
    p = subparsers.add_parser("research", help="Validate the RXM strategy", description=__doc__,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="research", required=True)

    win = sub.add_parser("windows", help="14-day competition windows through the full simulator")
    win.add_argument("--preset", default="comp", choices=sorted(PRESETS))
    win.add_argument("--params", default="", help="override strategy params, e.g. k=4,buffer=0")
    win.add_argument("--lockin", type=float, help="override the lock-in return (0 = off)")
    win.add_argument("--start", default="2024-01-01")
    win.add_argument("--end", default="2026-10-01")
    win.set_defaults(handler=_windows)

    val = sub.add_parser("validate", help="In-sample, in-sample permutation, walk-forward, walk-forward permutation")
    val.add_argument("--start", default="2024-01-01", help="first bar loaded (the first weeks only warm up)")
    val.add_argument("--is-start", default="2024-02-15", help="in-sample period start")
    val.add_argument("--is-end", default="2025-06-01", help="in-sample period end (exclusive)")
    val.add_argument("--end", default="2026-10-01")
    val.add_argument("--grid", default="", help='e.g. "k=2,3,4;tilt=0,0.3;buffer=0,2" (default: v.DEFAULT_GRID)')
    val.add_argument("--cost-bps", type=float, default=10.0, help="cost per unit of turnover")
    val.add_argument("--perms", type=int, default=200, help="in-sample permutations")
    val.add_argument("--wf-perms", type=int, default=100, help="walk-forward permutations")
    val.add_argument("--train-days", type=int, default=365)
    val.add_argument("--test-days", type=int, default=30)
    val.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    val.set_defaults(handler=_validate)


def _windows(args, settings: Settings) -> int:
    params = {**PRESETS[args.preset]["params"], **parse_params(args.params)}
    overrides = {} if args.lockin is None else {"lockin_return": args.lockin}
    config = w.preset_config(args.preset, settings.backtest, **overrides)
    data = load_universe(list(UNIVERSE), "15m", args.start, args.end, data_dir=settings.data.dir)
    windows = w.evaluate(ResidualMomentum(**params), data, config)
    print(f"RXM {params} | lock-in {config.lockin_return:g} x{config.lockin_scale:g} | {len(data)} coins")
    print(w.by_split(windows).round(3).to_string())
    out = _out_dir(settings, "windows")
    windows.to_csv(out / "windows.csv", index=False)
    print(f"\nSaved to {out}")
    return 0


def _validate(args, settings: Settings) -> int:
    grid = v.Grid(_parse_grid(args.grid) if args.grid else v.DEFAULT_GRID)
    data = load_universe(list(UNIVERSE), "15m", args.start, args.end, data_dir=settings.data.dir)
    is_start, is_end = pd.Timestamp(args.is_start, tz="UTC"), pd.Timestamp(args.is_end, tz="UTC")
    out = _out_dir(settings, "validate")
    report: dict = {"grid": grid.params, "cost_bps": args.cost_bps}

    print(f"1. In-sample excellence ({args.is_start} -> {args.is_end}, {len(grid.configs)} configs)")
    in_sample = v.config_returns(v.truncate(data, is_end), grid, args.cost_bps).loc[is_start:]
    table = v.in_sample(in_sample, grid)
    print(table.head(8).round(3).to_string())
    best = table.index[0]
    report["in_sample"] = {"best": best, **table.loc[best].round(4).to_dict()}

    print(f"\n2. In-sample permutation test ({args.perms} shuffles, best of the grid each time)")
    p_is, null_is = v.in_sample_permutation_test(data, grid, is_start, is_end, table.at[best, "sharpe"],
                                                 args.perms, args.cost_bps, args.workers)
    print(f"   real best Sharpe {table.at[best, 'sharpe']:.2f} | shuffled best: median {np.median(null_is):.2f}, "
          f"95th pct {np.quantile(null_is, 0.95):.2f} | p = {p_is:.3f}")
    report["in_sample_permutation"] = {"p": p_is, "null_median": float(np.median(null_is)),
                                       "null_p95": float(np.quantile(null_is, 0.95))}

    print(f"\n3. Walk-forward ({args.train_days}-day training window, re-fit every {args.test_days} days)")
    returns = v.config_returns(data, grid, args.cost_bps)
    oos, picks = v.walk_forward(returns, args.train_days, args.test_days)
    frozen = returns[grid.label(_frozen(grid))].loc[oos.index] if _frozen(grid) else None
    real_wf = float(v.sharpe(oos))
    print(f"   out of sample {oos.index[0]:%Y-%m-%d} -> {oos.index[-1]:%Y-%m-%d}: Sharpe {real_wf:.2f}, "
          f"return {np.expm1(np.log1p(oos).sum()):+.1%}, profit factor {v.profit_factor(oos):.2f}")
    if frozen is not None:
        print(f"   frozen comp config over the same days: Sharpe {float(v.sharpe(frozen)):.2f}, "
              f"return {np.expm1(np.log1p(frozen).sum()):+.1%}")
    print(pd.DataFrame(picks)[["from", "config", "train_sharpe", "test_return"]].to_string(index=False))
    report["walk_forward"] = {"sharpe": real_wf, "picks": picks}

    print(f"\n4. Walk-forward permutation test ({args.wf_perms} shuffles of everything after the first training window)")
    p_wf, null_wf = v.walk_forward_permutation_test(data, grid, oos.index[0], real_wf, args.wf_perms,
                                                    args.cost_bps, args.train_days, args.test_days, args.workers)
    print(f"   real Sharpe {real_wf:.2f} | shuffled: median {np.median(null_wf):.2f}, "
          f"95th pct {np.quantile(null_wf, 0.95):.2f} | p = {p_wf:.3f}")
    report["walk_forward_permutation"] = {"p": p_wf, "null_median": float(np.median(null_wf)),
                                          "null_p95": float(np.quantile(null_wf, 0.95))}

    returns.to_csv(out / "daily_returns.csv")
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\nSaved to {out}")
    return 0


def _frozen(grid: v.Grid) -> dict | None:
    frozen = {k: PRESETS["comp"]["params"][k] for k in grid.params if k in PRESETS["comp"]["params"]}
    return frozen if frozen in grid.configs else None


def _parse_grid(text: str) -> dict:
    grid = {}
    for part in filter(None, text.split(";")):
        key, values = part.split("=", 1)
        grid[key.strip()] = [int(x) if key.strip() in ("k", "buffer") else float(x) for x in values.split(",")]
    return grid


def _out_dir(settings: Settings, kind: str) -> Path:
    out = Path(settings.backtest.results_dir) / f"rxm_{kind}_{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    return out
