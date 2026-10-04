# Backtesting and tuning RXM

How to reproduce the numbers, test a change and (only if it earns it) adopt it. The strategy is in
`src/tradebot/strategy/library/rxm.py`, and its frozen values are in `PRESETS` in the same file. The spec and
evidence are in `docs/STRATEGY_SPEC.md`.

## 0. Setup (once)
```bash
python -m pip install -r requirements.txt
python -m tradebot data --intervals 15m --start 2024-01-01 --end 2026-10-01 \
  --symbols BTC,ETH,SOL,BNB,XRP,DOGE,ADA,AVAX,LINK,LTC,DOT,TRX,NEAR,APT,UNI,AAVE,FIL,ICP,HBAR,XLM,SUI,ARB,SEI,FET,PEPE,SHIB,BONK,FLOKI,CRV,ZEC,PENDLE,CAKE,CFX,ZEN,WLD
python -m pytest -q          # all green; the golden-weights test checks the frozen presets
```
The download is about 335 MB into `data/` (gitignored). It retries via the S3 mirror if
`data.binance.vision` is blocked.

## 1. Reproduce the frozen numbers (about 4 minutes)
```bash
python -m tradebot.research.experiments                     # comp + neutral, discovery 2024-01 -> 2026-08-15
```
Expected (129 half-overlapping 14-day windows):

| preset | med_ret | p10_ret | P>3.3% | P>5.2% | med_sharpe |
|---|---|---|---|---|---|
| comp | +3.4% | −6.4% | 50% | 35% | 2.89 |
| neutral | +1.4% | −4.5% | 29% | 18% | 1.86 |

These are **in-sample**. The out-of-sample planning numbers are lower (spec §6.3): comp P(>5.2%) is
about 33–39% and its median is about 0 to +3%.

## 2. The knobs

| Knob | Where | Frozen comp | What it trades off |
|---|---|---|---|
| `buffer` | strategy | 2 | A held coin stays while it ranks in the top/bottom k + buffer. More = fewer swaps and fees, slower to drop a fading coin |
| `k` | strategy | 3 | Names per side. Fewer = more concentrated and higher variance (helps qualify, hurts Sharpe) |
| `tilt` | strategy | 0.3 | Net long = gross × tilt. More = more beta (wins in rallies, loses in crashes) |
| `gross` | strategy | 1.0 | Total exposure. Hard cap 1.0 (no leverage) |
| `lookbacks` | strategy | 72/168/336 (h) | Momentum horizons. Shorter = more turnover = more fees |
| `rebalance_h` | strategy | 24 | Faster loses money (1h −3.6%, 4h −0.8%, 24h +1.4% median) |
| `lockin_return`, `lockin_scale` | engine (`PRESETS[...]["engine"]`) | 0.06, 0.3 | Once up 6%, cut to 30% exposure to protect Sharpe/Sortino/Calmar. Keep it above the +5.2% cut-off |
| `limit_offset_bps` | engine | 5 | Passive limit distance. More = better price, fewer fills |

## 3. Try one change
```bash
python -m tradebot.research.experiments --preset comp --params k=4,tilt=0.2 --tag " k4t.2"
python -m tradebot.research.experiments --preset comp --lockin 0.07,0.3 --tag " lock7"
python -m tradebot.research.experiments --preset comp --lockin 0 --tag " nolock"
python -m tradebot.research.experiments --preset comp --conservative   # pessimistic fills: always run before adopting
python -m tradebot.research.experiments --preset comp --latency 1      # stress: limit prices 15 min stale on arrival
```
Per-window results are saved to `results/windows/*.csv`.

- `--latency` is a worst-case stress, not a forecast: live orders are re-priced just before they are
  sent (`tradebot/live/repeg.py`). Compare variants under it; do not read its level as the
  expected return.
- Ideas already tested and rejected are listed in spec §9.1.

## 4. Grid search, the honest way
```bash
python -m tradebot.research.tune                                      # default grid (k x tilt x gross x lock-in)
python -m tradebot.research.tune --objective score                    # rank by the judges' composite instead
python -m tradebot.research.tune --grid "k=3,4;tilt=0.2,0.3,0.4;gross=1.0;lockin=0.05,0.06,0.07"
```
- **TRAIN** (2024-01 → 2025-06) ranks the configs. **VALIDATE** (2025-06 → 2026-08-15) is only reported.
- The **plateau** column is the mean train score of each config's grid neighbours. Prefer a config
  on a plateau over a lone spike.
- `train_se` / `val_se` give about 1 standard error. Windows overlap, so the true noise is larger.
- The script prints a verdict. It only suggests a change if the best config beats the frozen
  preset on **both** splits by more than 1 se.
- The results table is saved to `results/tune.csv`.

**Objectives:**
- `qualify` = P(14-day return > 5.2%). This is Screen 2: top 20 by return.
- `score` = median composite. This is Screen 3.
- `blend` = median + ½·p10.

## 4b. Is the edge real? Four-step permutation validation
```bash
python -m tradebot.research.validation                         # ~2 min on 8 cores
python -m tradebot.research.validation --perms 50 --wf-perms 20   # quick look
```
This follows Masters' permutation tests, as presented by neurotrader in "How I Develop Trading
Strategies":

1. **In-sample.** A 16-config grid (k 2–5, tilt 0 / 0.3, buffer 0 / 2) on 2024-02-15 → 2025-06-01,
   with the plateau column.
2. **In-sample permutation.** The bars are shuffled with one shuffle shared by every coin, which
   destroys every time pattern and keeps the cross-section. The grid is re-optimised on each shuffle.
   p is the share of shuffles whose best score matches the real best.
3. **Walk-forward.** The config is re-picked every 30 days on the trailing 365 days, and only the next
   30 days are scored.
4. **Walk-forward permutation.** Everything after the first training window is shuffled, and step 3 is
   re-run.

Steps 2–4 score the signal on daily closes, with 10 bp per unit of turnover. The lock-in and the fills
are what `experiments` covers.

Result on Oct 3, 2026 (200 / 100 shuffles):

| Step | Result |
|---|---|
| In-sample | best Sharpe 2.21; comp config 2.18 |
| In-sample permutation | p = 0.005 (none of 200 shuffles reached it) |
| Walk-forward | Sharpe 1.51 out of sample |
| Walk-forward permutation | p = 0.02 |

The edge is very unlikely to be fitted noise. The test says nothing about how big the edge is.

## 5. Adopting a change (change control, spec §9)
1. Write the hypothesis in `docs/STRATEGY_SPEC.md` §9 **before** running the grid.
2. The candidate must pass §4's verdict and look no worse with `--conservative`.
3. The hold-out (2026-08-16 → 09-30) **was spent on Oct 3, 2026** (spec §9.1). It can no longer
   validate a change. A new change needs fresh data: download a later period and keep it unseen.
4. Edit `PRESETS` in `src/tradebot/strategy/library/rxm.py`. Then:
   - regenerate `tests/golden_rxm_weights.csv` (see the test);
   - update the spec tables;
   - commit with the numbers in the message.

**Rules of thumb:**
- More than 150 configurations have already been tried, so a small in-sample gain is expected from luck alone.
- When in doubt, keep the frozen values.
- Never change the mode during the live event.

## 6. Live-like replay (no network)
```bash
python -m tradebot --config config/competition.yaml replay --start 2026-09-01T00:16 --days 7   # the live runner on replayed prices
python -m tradebot dashboard --source backtest --out results/dashboard.html
```
