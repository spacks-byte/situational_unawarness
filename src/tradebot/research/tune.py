"""
Tune RXM's knobs without fooling yourself.

    python -m tradebot.research.tune                                   # default grid, objective "qualify"
    python -m tradebot.research.tune --grid "k=2,3,4,5;tilt=0,0.15,0.3,0.45;gross=1.0;lockin=0,0.05,0.06,0.07"
    python -m tradebot.research.tune --objective score                 # rank by median composite instead

Protocol (read docs/STRATEGY_SPEC.md "Change control" before changing a frozen value):
  1. TRAIN 2024-01 -> 2025-06 picks the ranking. VALIDATE 2025-06 -> 2026-08-15 is only reported.
     The hold-out (2026-08-16 -> 09-30) is never touched here; run it once with
     `python -m tradebot.research.experiments --preset comp --holdout` after you freeze a change.
  2. Prefer a PLATEAU: a config whose grid neighbours score almost as well. A lone spike is noise.
  3. Adopt a change only if it beats the frozen preset on TRAIN *and* VALIDATE, by more than the
     noise (the `se` column: ~1 standard error of the objective). Otherwise keep the frozen values.

Speed: the signal depends only on (k, tilt, gross, lookbacks), so weights are computed once over the
full history and reused across windows and lock-in settings. Rolling inputs (14d horizon, 30d beta,
7d vol) are fully warmed inside the 45-day warm-up, so this equals per-window recomputation
(checked by tests/test_rxm.py::test_precomputed_weights_match_per_window).
"""
import argparse
import itertools
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot.backtest.windows import evaluate_windows
from tradebot.data.loader import load_universe
from tradebot.research.experiments import (DISCOVERY, UNIVERSE, WARMUP_DAYS, WINDOW_DAYS, composite, make_config,
                                           summarise)
from tradebot.strategy.base import Strategy
from tradebot.strategy.library.rxm import ResidualMomentum, preset

SPLIT = pd.Timestamp("2025-06-01", tz="UTC")
OUT = Path("results") / "tune.csv"
DEFAULT_GRID = "k=2,3,4,5;tilt=0,0.15,0.3,0.45;gross=1.0;lockin=0,0.06"
OBJECTIVES = {
    # probability of clearing the estimated top-20 cut (~+5.2%) = what Screen 2 rewards
    "qualify": lambda w: (w.total_return > 0.052).mean(),
    # median judges' composite = what Screen 3 rewards
    "score": lambda w: w.composite.median(),
    # median return minus half the lower tail: a risk-aware middle ground
    "blend": lambda w: w.total_return.median() + 0.5 * w.total_return.quantile(0.1),
}


class FixedWeights(Strategy):
    """Replays weights precomputed on the full history (fast path for grids)."""
    name = "fixed"

    def __init__(self, weights: pd.DataFrame):
        super().__init__()
        self.weights = weights

    def generate_weights(self, data):
        idx = next(iter(data.values())).index
        for df in data.values():
            idx = idx.union(df.index)
        return self.weights.reindex(index=idx, columns=list(data)).fillna(0.0)


def parse_grid(text: str) -> dict:
    grid = {}
    for part in filter(None, text.split(";")):
        key, vals = part.split("=", 1)
        grid[key.strip()] = [float(v) if "." in v or key.strip() != "k" else int(v) for v in vals.split(",")]
    return grid


def objective_se(w: pd.DataFrame, objective: str, n_boot: int = 200, seed: int = 0) -> float:
    """Bootstrap standard error of the objective over windows (overlap makes it an underestimate)."""
    rng = np.random.default_rng(seed)
    f = OBJECTIVES[objective]
    vals = [f(w.iloc[rng.integers(0, len(w), len(w))]) for _ in range(n_boot)]
    return float(np.std(vals))


_DATA = None


def _init_worker():
    global _DATA
    _DATA = load_universe(UNIVERSE, "15m", *DISCOVERY)


def _score_combo(job):
    """All lock-in settings for one signal config (weights computed once). Runs in a worker."""
    params, sig_keys, combo, lockins, base_engine, lockin_scale, objective, step = job
    f = OBJECTIVES[objective]
    weights = ResidualMomentum(**params).generate_weights(_DATA)
    rows = []
    for lk in lockins:
        engine = dict(base_engine, lockin_return=lk, lockin_scale=lockin_scale if lk > 0 else 1.0)
        w = evaluate_windows(FixedWeights(weights), _DATA, "15m", make_config(engine),
                             window_days=WINDOW_DAYS, step_days=step, warmup_days=WARMUP_DAYS)
        w["composite"] = w.apply(composite, axis=1)
        tr, va = w[w.start < SPLIT], w[w.start >= SPLIT + pd.Timedelta(days=WINDOW_DAYS)]
        label = " ".join(f"{k}={v}" for k, v in zip(sig_keys, combo)) + f" lock={lk}"
        row = dict(config=label, **dict(zip(sig_keys, combo)), lockin=lk,
                   train=f(tr), train_se=objective_se(tr, objective), validate=f(va),
                   val_se=objective_se(va, objective))
        row.update({f"all_{k}": v for k, v in summarise(label, w).items() if k not in ("variant", "windows")})
        rows.append(row)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--grid", default=DEFAULT_GRID, help='e.g. "k=2,3,4;tilt=0,0.3;gross=1.0;buffer=0,2;lockin=0,0.06"')
    p.add_argument("--objective", default="qualify", choices=sorted(OBJECTIVES))
    p.add_argument("--step", type=int, default=7)
    p.add_argument("--lockin-scale", type=float, default=0.3)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="parallel workers")
    a = p.parse_args(argv)

    grid = parse_grid(a.grid)
    base_params, base_engine = preset("comp")
    sig_keys = [k for k in grid if k != "lockin"]
    lockins = grid.get("lockin", [base_engine.get("lockin_return", 0.0)])
    combos = list(itertools.product(*(grid[k] for k in sig_keys)))
    jobs = [(dict(base_params, **dict(zip(sig_keys, c))), sig_keys, c, lockins, base_engine, a.lockin_scale,
             a.objective, a.step) for c in combos]
    print(f"{len(combos) * len(lockins)} configs, objective={a.objective}, {a.jobs} workers "
          f"(~1 min per signal config per worker)", flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=a.jobs, initializer=_init_worker) as pool:
        for out in pool.map(_score_combo, jobs):
            for row in out:
                print(f"  {row['config']:<45} train {row['train']:.3f}  validate {row['validate']:.3f}", flush=True)
            rows += out
    for row in rows:
        row["frozen"] = all(row[k] == base_params.get(k) for k in sig_keys) and \
            row["lockin"] == base_engine.get("lockin_return", 0.0)

    res = pd.DataFrame(rows).sort_values("train", ascending=False)
    # plateau: mean train objective of grid neighbours (one step away in exactly one knob)
    keys = sig_keys + ["lockin"]
    levels = {k: sorted(set(res[k])) for k in keys}
    def neighbours(r):
        out = []
        for k in keys:
            i = levels[k].index(r[k])
            for j in (i - 1, i + 1):
                if 0 <= j < len(levels[k]):
                    m = np.ones(len(res), bool)
                    for kk in keys:
                        m &= (res[kk] == (levels[k][j] if kk == k else r[kk])).values
                    out += list(res.loc[m, "train"])
        return np.mean(out) if out else np.nan
    res["plateau"] = res.apply(neighbours, axis=1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT, index=False)

    pd.set_option("display.width", 220)
    cols = ["config", "train", "train_se", "plateau", "validate", "val_se", "all_med_ret", "all_p10_ret",
            "all_P>5.2%", "all_med_composite", "frozen"]
    print(f"\nRanked by TRAIN {a.objective} (train < {SPLIT.date()} <= validate). Saved {OUT}")
    print(res[cols].head(a.top).round(3).to_string(index=False))
    fz = res[res.frozen]
    if len(fz):
        b = fz.iloc[0]
        best = res.iloc[0]
        print(f"\nFrozen preset:  train {b.train:.3f}  validate {b.validate:.3f}")
        print(f"Best on train:  train {best.train:.3f}  validate {best.validate:.3f}  ({best.config})")
        beats = (best.train - b.train > best.train_se) and (best.validate - b.validate > best.val_se)
        print("Verdict: " + ("candidate beats the frozen preset on BOTH splits by > 1 se. Write the hypothesis, "
                             "then confirm with tradebot.research.experiments (incl. --conservative) before freezing."
                             if beats and not best.frozen else
                             "no config beats the frozen preset on both splits beyond noise -> keep frozen values."))


if __name__ == "__main__":
    main()
