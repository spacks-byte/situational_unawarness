"""
Score RXM presets on competition-length windows (14 days, each starting from cash) with the
limit-order engine in backtest/engine.py.

    python -m backtest.experiments                          # both presets, discovery period
    python -m backtest.experiments --preset comp --holdout  # 2026-08-16 -> 09-30 (final check only)
    python -m backtest.experiments --preset comp --params k=4,tilt=0.2 --tag " k4"   # a what-if
    python -m backtest.experiments --preset comp --lockin 0 # competition mode without the lock-in

Score = the judges' composite 0.4*Sortino + 0.3*Sharpe + 0.3*Calmar per window (daily returns,
365-day annualisation, as in backtest/metrics.py). Calmar is capped at 50 per window so one
tiny-drawdown window can't dominate an average. P>3.3% / P>5.2% bracket the estimated top-20 cut.
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.engine import BacktestConfig, load_universe
from backtest.strategies.rxm import PRESETS, ResidualMomentum, preset
from backtest.windows import evaluate_windows

UNIVERSE = ("BTC,ETH,SOL,BNB,XRP,DOGE,ADA,AVAX,LINK,LTC,DOT,TRX,NEAR,APT,UNI,AAVE,FIL,ICP,HBAR,XLM,"
            "SUI,ARB,SEI,FET,PEPE,SHIB,BONK,FLOKI,CRV,ZEC,PENDLE,CAKE,CFX,ZEN,WLD").split(",")
DISCOVERY = ("2024-01-01", "2026-08-16")
HOLDOUT = ("2026-06-01", "2026-10-01")       # data incl. warm-up; windows start >= 2026-08-16
HOLDOUT_START = pd.Timestamp("2026-08-16", tz="UTC")
SAVE_DIR = Path(__file__).resolve().parent.parent / "results" / "windows"   # per-window csv
WINDOW_DAYS, WARMUP_DAYS = 14, 45


def composite(row) -> float:
    cal = min(row["calmar"], 50) if np.isfinite(row["calmar"]) else 0.0
    return 0.4 * np.nan_to_num(row["sortino"]) + 0.3 * np.nan_to_num(row["sharpe"]) + 0.3 * cal


def parse_params(text: str) -> dict:
    """'k=4,tilt=0.2,lookbacks=72/168' -> {'k': 4, 'tilt': 0.2, 'lookbacks': '72/168'}"""
    out = {}
    for item in filter(None, (text or "").split(",")):
        k, v = item.split("=", 1)
        out[k.strip()] = v if "/" in v else (float(v) if "." in v else int(v))
    return out


def run_windows(params: dict, cfg: BacktestConfig, data, step: int, after=None) -> pd.DataFrame:
    w = evaluate_windows(ResidualMomentum(**params), data, "15m", cfg, window_days=WINDOW_DAYS,
                         step_days=step, warmup_days=WARMUP_DAYS)
    if after is not None:
        w = w[w["start"] >= after]
    w["composite"] = w.apply(composite, axis=1)
    return w


def summarise(name: str, w: pd.DataFrame) -> dict:
    return {
        "variant": name, "windows": len(w),
        "med_ret": w.total_return.median(), "mean_ret": w.total_return.mean(),
        "p10_ret": w.total_return.quantile(0.1), "win%": (w.total_return > 0).mean(),
        "P>3.3%": (w.total_return > 0.033).mean(), "P>5.2%": (w.total_return > 0.052).mean(),
        "med_mdd": w.max_drawdown.median(), "med_sharpe": w.sharpe.median(),
        "med_composite": w.composite.median(), "fill": w.fill_rate.median(),
        "trades/win": w.num_trades.median(), "bh_med_ret": w.bh_total_return.median(),
    }


def make_config(engine_extra: dict, offset=None, conservative=False, lockin=None, latency=0) -> BacktestConfig:
    extra = dict(engine_extra, latency_bars=latency)
    if offset is not None:
        extra["limit_offset_bps"] = offset
    if lockin is not None:
        extra["lockin_return"], extra["lockin_scale"] = lockin
    return BacktestConfig(gap_improvement=not conservative, **extra)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--preset", default="comp,neutral", help=f"comma list of {sorted(PRESETS)}")
    p.add_argument("--holdout", action="store_true")
    p.add_argument("--step", type=int, default=7, help="days between window starts (7 = half-overlapping)")
    p.add_argument("--offset", type=float, default=None, help="limit offset in bp (default: preset's 5)")
    p.add_argument("--conservative", action="store_true", help="no price improvement on gapped limit fills")
    p.add_argument("--latency", type=int, default=0,
                   help="stress test: limit prices are N bars (15m each) stale on arrival, crossed limits pay taker")
    p.add_argument("--lockin", default=None, help="'return,scale' e.g. 0.06,0.3 ; '0' disables")
    p.add_argument("--params", default="", help="strategy overrides, e.g. k=4,tilt=0.2")
    p.add_argument("--tag", default="", help="label appended to variant names")
    a = p.parse_args(argv)

    start, end = HOLDOUT if a.holdout else DISCOVERY
    data = load_universe(UNIVERSE, "15m", start, end)
    lockin = None if a.lockin is None else ((0.0, 1.0) if float(a.lockin.split(",")[0]) == 0
                                           else tuple(float(x) for x in a.lockin.split(",")))
    step = 1 if a.holdout else a.step
    rows = []
    for name in a.preset.split(","):
        params, engine = preset(name)
        params.update(parse_params(a.params))
        cfg = make_config(engine, a.offset, a.conservative, lockin, a.latency)
        w = run_windows(params, cfg, data, step, HOLDOUT_START if a.holdout else None)
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        w.to_csv(SAVE_DIR / f"{(name + a.tag).replace(' ', '_')}{'_HOLD' if a.holdout else ''}.csv", index=False)
        rows.append(summarise(name + a.tag, w))
    out = pd.DataFrame(rows).set_index("variant")
    pd.set_option("display.width", 220)
    print(f"{'HOLD-OUT' if a.holdout else 'DISCOVERY'} {start} -> {end}, {WINDOW_DAYS}-day windows, step {step}d, "
          f"params+={parse_params(a.params)}, lockin={lockin or 'preset'}, conservative={a.conservative}, "
          f"latency={a.latency} bars")
    print(out.round(3).to_string())


if __name__ == "__main__":
    main()
