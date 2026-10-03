# Strategy specification: Residual Cross-Sectional Momentum (RXM)

**Team 87, Situational Unawareness (HKUST).** Roostoo × Susquehanna × AWS Quant Hackathon, live Oct 4–17, 2026.
Version 1.1, Oct 3, 2026 (change request CR-1, §9.1; ported to the `tradebot` package). Status: **frozen for live trading.** Strategy changes only through a new git commit with evidence.

| Document | Purpose |
|---|---|
| **This file** | The single source of truth: what we trade, why, how, the evidence, and the risks |
| §10 of this file | How to test the strategy: competition windows and the four-step validation |
| `docs/ENGINE.md` | The live engine the strategy runs on |
| Research archive | Full research log, point-in-time validation (2020–2026), skeptic audit, dashboard: LuisSandejas/Web3_Competition_susquehanna_Situational_Unawareness |

---

## 1. Executive summary
- **What:** a daily-rebalanced, long/short, cross-sectional momentum portfolio on liquid crypto.
  - The signal is each coin's **residual** trend: its own move after removing its BTC beta. It is
    averaged over 3, 7 and 14 days and normalised by the coin's volatility.
- **Why it should work:** documented intermediate-horizon momentum in crypto (Liu, Tsyvinski & Wu
  2022). Residualising against BTC removes the market factor; idiosyncratic trends diffuse slowly
  through a retail-dominated, attention-driven market.
- **Honest out-of-sample expectation:**
  - Neutral book: Sharpe ≈ 1 (0.8–1.2 across universes and eras).
  - Competition mode adds a 0.3 net-long tilt and a lock-in. That roughly doubles the chance of
    a ≥ +5.2% fortnight (≈ 20% → ≈ 33–39%), and roughly doubles the depth of a bad fortnight
    (p10 ≈ −6% → ≈ −11%). These figures were measured on v1.0 (lock at +5%, no rank buffer);
    v1.1 has not been re-run on the point-in-time universe.
- **What we do not claim:** earlier in-sample Sharpe figures of 2+ came from a survivor universe.
  They do not survive point-in-time testing and are not our expectation.

## 2. Strategy skeleton (all layers)

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ L0  DATA            Binance public klines (15m) for the universe; Roostoo ticker for marks │
│                     → BarBuffer (45-day rolling window), stale-feed detection             │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ L1  UNIVERSE        Liquid Roostoo crypto pairs with full history (35 names); excluded:    │
│                     stablecoins, bStocks/PAXG, names < 90 days of history, stalled feeds   │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ L2  SIGNAL (alpha)  residual momentum, 3 horizons, vol-normalised, cross-sectional z-score │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ L3  PORTFOLIO       long top-k / short bottom-k with a rank buffer, inverse-vol weights,   │
│                     gross G, tilt τ                                                        │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ L4  COMPETITION     lock-in: return since start ≥ +6% → all weights × 0.3, permanently     │
│     OVERLAY         (competition mode only)                                                 │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ L5  EXECUTION       daily at 00:00 UTC; limit orders only, 5 bp passive; re-quote ≤ 1 per│
│                     15 min; drift band 1%; short covers via short_close (market)         │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ L6  RISK            engine risk gate (gross, per-coin, short collateral, cash reserve,   │
│                     kill switch); snapshot sanity checks; NO drawdown brakes             │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ L7  MONITORING      engine audit JSONL + SQLite intent journal; strategy state JSON;     │
│                     one log line per signal                                              │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

| Layer | Code | Based on | Evidence |
|---|---|---|---|
| L0 Data | `tradebot/live/bars.py`, `tradebot/data/` | Binance Vision and public klines (Roostoo mirrors exchange prices) | Data audit: 0 gaps, 0 duplicates, ms→µs timestamps handled |
| L1 Universe | `tradebot/strategy/library/rxm.py: UNIVERSE` | Liquidity and history requirements | Point-in-time vs survivor gap measured (§6) |
| L2 Signal | `tradebot/strategy/library/rxm.py` (`ResidualMomentum.scores`) | XS momentum literature; residualisation; ensemble over horizons | Placebo p < 0.003; lag decay smooth; out-of-sample Sharpe ≈ 1 |
| L3 Portfolio | same file (`weights`; k, gross, tilt, buffer; frozen `PRESETS`) | Momentum lives in extreme ranks; inverse-vol risk budgeting | Top-k beat a covariance optimizer (v3 tied, not adopted) |
| L4 Overlay | `tradebot/backtest/simulator.py` (`lockin_return`); `tradebot/live/rxm.py` | Two-stage scoring: qualify on return, then rank on risk ratios | Discovery P(>5.2%) 18% → 26%; out-of-sample 33–39% vs 20% |
| L5 Execution | `tradebot/engine/` (limit_only); `tradebot/live/rxm.py` | Fee wall: 10 bp per long round trip, 20 bp per short | 5 bp offset t = 3.7; faster rebalancing loses money |
| L6 Risk | `tradebot/engine/risk/`, `config/competition.yaml` | Rules (1×, directional only, 30 calls/min); research showed brakes hurt | Unit-tested; drawdown brake measured −2.9 composite |
| L7 Monitoring | `tradebot/engine/state/` | Judges audit trade logs and commit history | Every order intent and outcome is journaled |

## 3. Formal definition

Notation: coins i = 1…N; decision time t = 00:00 UTC daily; 15-minute bars; P = price.

**L2. Signal**
```
r_i(b)      = ln P_i(b) − ln P_i(b−1)                                (15m log return)
β_i,t       = Cov(r_i, r_BTC) / Var(r_BTC)        over 30 days
σ_i,t       = std(r_i)                            over 7 days (floored at 0.05× cross-sectional median)
e_i,t(L)    = [ln P_i,t − ln P_i,t−L] − β_i,t · [ln P_BTC,t − ln P_BTC,t−L]
x_i,t(L)    = e_i,t(L) / (σ_i,t · √L)                                 L ∈ {3d, 7d, 14d}
z_i,t(L)    = (x_i,t(L) − mean_j x_j,t(L)) / std_j x_j,t(L)
s_i,t       = (z_i,t(3d) + z_i,t(7d) + z_i,t(14d)) / 3
```
Eligibility: a fresh bar exists, 24h volume > 0, the price moved within the last 24h, and the coin
has a valid σ and β. Ineligible coins are excluded from ranking.

**L3. Portfolio**
```
k_eff  = min(k, ⌊N_eligible/2⌋)
L_t    = top k_eff by s,   S_t = bottom k_eff by s
         with buffer b: a member of L_{t−1} stays while its rank ≤ k_eff + b; free slots go to the
         best-ranked non-members. Same rule, mirrored, for S_t.
w_i    = +G(1+τ)/2 · (1/σ_i) / Σ_{j∈L}(1/σ_j)     for i ∈ L
w_i    = −G(1−τ)/2 · (1/σ_i) / Σ_{j∈S}(1/σ_j)     for i ∈ S
Σ|w| ≤ 1                                            (no leverage)
```

**L4. Lock-in** (competition mode)
```
R_t = Equity_t / Equity_start − 1   (realised, observed at t−1)
if R_t ≥ 6% (two consecutive polls):  w ← 0.3 · w  for the rest of the event (sticky)
```

**Parameters (frozen):**

| | Neutral preset `neutral` | **Competition preset `comp`** |
|---|---|---|
| k (names per side) | 5 | 3 |
| G (gross) | 0.9 | 1.0 (live 0.98 for fee headroom) |
| τ (tilt) | 0 | 0.3 → 65% long / 35% short |
| Rank buffer b | 0 | 2 (keep a held coin while it ranks in the top / bottom 5) |
| Lock-in | off | +6% → ×0.3 |
| Horizons | 3/7/14d | 3/7/14d |
| Rebalance | daily, 00:00 UTC | daily, 00:00 UTC |
| Limit offset | 5 bp passive | 5 bp passive |

## 4. Execution and costs
- **Fees:** maker 0.05% per order, so a long round trip costs 10 bp. Short open and short close are
  0.1% each (20 bp round trip), and the close is a market order. Turnover is ≈ 0.7× equity per day,
  so fees run ≈ 20% a year. **Fees are the largest controllable drag.**
- **Order flow (limit orders only, engine `order_policy: limit_only`):**
  1. Daily target at 00:00 UTC (computed at 00:15, when that bar has closed).
  2. Buys rest 5 bp below and short opens 5 bp above the last price. Sells of coins that leave the
     book go at the last price. Unfilled limits are cancelled after one 15-minute bar.
  3. A coin still off target at the next bar gets a new signal and a fresh limit.
  4. Weights inside the 1% drift band are held.
  5. Buys are capped to the USD that is free now, so they never wait on unfilled sells.
- **API budget:** one engine cycle is 5 requests; polling every 60 s is about 5/min idle. The limit is 30.
- **Measured: why not faster?**

  | Rebalance every | 1h | 4h | 12h | **24h** |
  |---|---|---|---|---|
  | Median 14-day return | −3.6% | −0.8% | +0.4% | **+1.4%** |

  Minute-level prediction models reach a 58–64% hit rate but net ≈ 0 after fees (the fee wall).

## 5. Risk management
- **Structural:** long/short (short leg present in both modes), inverse-vol sizing, k_eff guard,
  stale-feed exclusion, volatility floor.
- **Engine risk gate (L6):** rejects a plan above 1× gross, 0.60 of equity in one coin, $60k of
  short collateral or below the $200 cash reserve (`config/competition.yaml`), and honours the kill
  switch. The strategy raises before planning on a broken snapshot (zero equity or a >50% jump).
- **Deliberately absent:** drawdown brakes and stop-losses. Tested twice: they sell into crypto's
  mean reversion and lowered the composite score (−2.9).
- **Event risk:** **no event rule.** Across 80 CPI and 55 FOMC days, 2020–26, neither mode loses on
  release days. A "halve the tilt around CPI" rule was not significant (p ≥ 0.06), and its best
  variant went the wrong way. CPI on **Oct 14** is traded through unchanged.

## 6. Evidence

**6.1 Validity checks** (independent code, rebuilt from raw data)
- **No look-ahead:**
  - Signal-lag decay is smooth: Sharpe 2.44 → 1.81 → 0.69 → 0.53 → 0.13 at 0/1/2/3/5 days of lag
    (2024–26 universe).
  - Scrambling future prices leaves past weights identical.
- **Better than chance:**
  - Beats 300 turnover-matched placebos (p < 0.003).
  - The inverted signal scores Sharpe −3.8.
- **Realistic hit rates:** 45% per position (average win +7.5% vs average loss −4.9%); 53% per day.

**6.2 Out-of-sample:** non-overlapping 14-day windows, point-in-time top-N universe chosen by
trailing 30-day volume from all 656 Binance USDT pairs, including delisted coins.

| | Neutral 2020–23 | Neutral 2024–26 | Comp 2020–23 | Comp 2024–26 | Buy & hold |
|---|---|---|---|---|---|
| Sharpe (N = 35/50/75) | 1.20 / 0.87 / 0.82 | 0.90 / 1.24 / 0.99 | 1.63 / 1.01 / 1.24 | 0.25 / 0.43 / −0.15 | 0.9 / −0.3 to −0.4 |
| Median 14d | −0.4% to +0.8% | −0.3% to +0.8% | +4.1% to +4.4% | −0.6% to +1.4% | +2.3% / −2.7% |
| p10 | −5% to −8% | −5% to −6% | −9% to −13% | −10% to −11% | −18% to −20% |
| P(> +5.2%) | 21–24% | 18–23% | 43–45% | 32–34% | 24–42% |

- **By regime:**
  - The neutral book earned in trending alt markets: 2021 (Sharpe 2.5–2.8) and 2026 (2.1–3.3).
  - It was flat to negative in 2022–23 (−0.8 to +0.2), always with far smaller losses than buy & hold.
- **Crash test, Oct 10, 2025 ($19B liquidations):**
  - Neutral made **+3.1% to +3.4%** that day.
  - Competition mode lost **2–5%**.
  - The equal-weight market fell 23.6%.

**6.3 Expectations for Oct 4–17, 2026** (pooled out-of-sample)

| | Median 14d | p10 | P(> 0) | P(> +5.2%) |
|---|---|---|---|---|
| Neutral | ≈ 0% to +0.4% | ≈ −6% | ≈ 51% | ≈ 20% |
| **Competition** | ≈ +0.4% to +3.3% | ≈ −11% | 51–60% | **33–39%** |

## 7. Why competition mode
The estimated top-20 cut-off for 130–150 teams is ≈ +5.2% over 14 days, and higher in a rally.
- **Neutral** clears it in ≈ 20% of fortnights.
- **Competition mode** clears it in ≈ 33–39%.

Its extra return is mostly **beta** (0.3 net long), which is acceptable here because it is
pre-declared and the short leg stays in. The lock-in then converts a qualifying return into a
low-volatility finish for the composite (0.4 Sortino + 0.3 Sharpe + 0.3 Calmar). *A fund would not
run a lock-in. It is an optimisation for this scoring rule, and we disclose it as such.*

## 8. Known limitations and open items
1. **Regime dependence.** The edge clusters in trending alt markets; choppy or bear regimes are flat.
2. **Universe.** Live trades the 35 liquid Roostoo names, a survivor list. Out-of-sample Sharpe ≈ 1
   is the planning number, not the in-sample 2.
3. **Execution realism.**
   - Long exits are limit orders at the last price; the backtest assumes 5 bp better.
   - Roostoo's real fill rules, cancel refunds and `Lock` accounting must be verified on the TEST
     account before Oct 4 (`python -m tradebot --config config/competition.yaml live`).
4. **Outages.** The alpha decays within 1–2 days, so a missed day costs ≈ 25% of the edge. A watchdog
   alerts if there is no daily signal by 02:00 UTC.
5. **Concentration.** With k = 3, a single coin can reach ≈ 0.55 of the book. The engine's per-coin
   cap (0.60) is set accordingly.
6. **Selection.** About 70 configurations were tried in total. Every number in §6.2 used frozen
   parameters on data never used for selection.

## 9. Change control
Strategy, parameter or risk changes require all of the following:
1. A written hypothesis.
2. A discovery test against the frozen spec on identical windows, with paired differences.
3. Out-of-sample confirmation on non-overlapping windows.
4. A git commit quoting the numbers.

No discretionary trades. No manual API calls with competition keys.

### 9.1 Change request CR-1 (Oct 3, 2026): rank buffer and a higher lock-in

Written before the hold-out was run.

**Change (competition preset only):**
- `buffer = 2`: a coin already in the long (short) book keeps its slot while it ranks in the top
  (bottom) k + 2 = 5. Free slots go to the best-ranked outsiders.
- Lock-in at **+6%** instead of +5%. The scale stays ×0.3.

**Hypothesis:**
- The buffer removes swaps caused by small rank changes. Turnover and fees fall by about a quarter,
  and the mean 14-day return rises.
- The +5% lock sits below the estimated +5.2% cut-off. In discovery, 54% of windows locked and more
  than half of those finished below 5.2%. Locking above the cut-off should raise P(> 5.2%).

**Discovery evidence** (train 2024-01 → 2025-06 / validate 2025-06 → 2026-08-15, same windows):

| | P(> 5.2%) | Mean 14d | p10 | Median composite |
|---|---|---|---|---|
| Frozen (buffer 0, lock 5%) | 21% / 32% | 1.4% / 2.3% | −7.3% / −4.0% | 9.5 / 13.7 |
| CR-1 (buffer 2, lock 6%) | 34% / 37% | 2.6% / 2.9% | −6.8% / −5.1% | 12.3 / 14.5 |

- All nine neighbours (buffer 1–3 × lock 5.5–6.5%) beat frozen on P(> 5.2%) and mean return in both
  splits. Conservative fills leave the result unchanged.
- Paired t-statistics on non-overlapping windows are 1.0–2.0, after more than 150 variants were
  tried in total. The gain is not statistically proven.

**Hold-out rule (decided in advance):** run frozen and CR-1 once on 2026-08-16 → 09-30.
- **Adopt** if CR-1's mean 14-day return and its P(> 5.2%) are both at least as good as frozen's.
- **Keep frozen** otherwise, including a mixed result.
- The hold-out has about three independent fortnights, so it can catch a change that is clearly
  harmful. It cannot prove the size of the gain.

**Hold-out result** (run once, Oct 3; 32 daily-step windows starting 2026-08-16 → 09-16):

| | P(> 5.2%) | Mean 14d | Median 14d | p10 | Worst | Median composite | Trades / window |
|---|---|---|---|---|---|---|---|
| Frozen v1.0 | 41% | +4.2% | +4.7% | +2.1% | −5.7% | 19.5 | 141 |
| CR-1 | 62% | +4.9% | +5.4% | +0.2% | −6.5% | 19.6 | 129 |

- CR-1 was better in 23 of 32 windows (paired mean +0.7%). Both conditions of the rule are met:
  **adopted as v1.1.**
- The lower tail is worse (p10 +0.2% vs +2.1%).
- The equal-weight market returned a median +15% per fortnight in this period. Both versions
  finished far below a plain long position: in a strong rally this strategy does not keep up.
- The hold-out is now spent. It cannot be used to validate any further change.

**Also tested, not adopted** (discovery data, Oct 3):
- Re-ranking every 1–12 hours, shorter horizons, a short-term reversal term: all lose to fees.
- Adding the other Roostoo coins (17–29 names): flat or worse, deeper p10.
- A per-coin share cap, and a tilt that follows the BTC trend (net short in a downtrend): no clear
  gain in both splits. Removed from the code.
- A lock-in that cuts to ×0.1: higher P(> 5.2%) but a lower composite; ×0.3 kept.

## 10. How to test it

Two tools, both reading the downloaded 15m data (`python -m tradebot data`, then point
`data.dir` at it).

**Competition windows** (`python -m tradebot research windows`). The preset runs through the full
simulator: 14-day windows every 7 days, each starting from cash, with the lock-in, limit fills and
fees. This is the number the competition sees. Results are split train / validate at 2025-06-01.

| comp v1.1, 2024-01 → 2026-09 | All (135) | Train (68) | Validate (65) |
|---|---|---|---|
| Median 14-day return | +3.6% | +3.3% | +4.6% |
| p10 | −6.4% | −6.8% | −5.0% |
| P(> 5.2%) | 37% | 34% | 42% |
| Median composite | 14.4 | 12.3 | 16.6 |

**Four-step validation** (`python -m tradebot research validate`, about 2 minutes on 8 cores). The
method is Timothy Masters' permutation testing, as presented by neurotrader in "How I Develop
Trading Strategies". It answers one question: is the edge real, or could optimisation have found it
in noise?

1. **In-sample excellence.** A 16-config grid (k 2–5, tilt 0 / 0.3, buffer 0 / 2) on
   2024-02-15 → 2025-06-01. Look for a winner whose neighbours are also good (the plateau column).
2. **In-sample permutation test.** The bars are shuffled with one shuffle shared by every coin. That
   keeps each coin's return distribution and the cross-coin correlation, and destroys every pattern
   in time. The whole grid is then re-optimised. p is the share of shuffles whose best Sharpe
   matches the real best. Because each shuffle also picks its best config, this charges for the
   selection bias.
3. **Walk-forward.** Every 30 days the best config on the trailing 365 days is picked, and only the
   next 30 days are scored.
4. **Walk-forward permutation test.** Everything after the first training window is shuffled, and
   the walk-forward is re-run.

Steps 2–4 score each config on daily closes (the weights decided at 00:00 earn the next 24 hours,
minus 10 bp per unit of turnover). They test the signal and the portfolio rules. The lock-in and the
fills are covered by the window tool above.

Results (Oct 3, 2026, 200 / 100 shuffles):

| Step | Result |
|---|---|
| 1. In-sample | Best k=5 tilt=0.3 buffer=2, Sharpe 2.21. The frozen comp config (k=3) is second at 2.18; neighbours average 1.75–1.95 |
| 2. In-sample permutation | Shuffled best Sharpe: median 0.34, 95th percentile 1.47. None of 200 reached 2.21: **p = 0.005**, the smallest 200 shuffles can give |
| 3. Walk-forward | Out of sample 2025-01-14 → 2026-09-29: Sharpe 1.51, profit factor 1.32 (the frozen config: Sharpe 2.03 over the same days) |
| 4. Walk-forward permutation | Shuffled walk-forward Sharpe: median −0.51, 95th percentile 0.52. One shuffle in 100 reached 1.51: **p = 0.02** |

How to read them:
- **p values.** A p below 0.01 says the optimisation did not just fit noise. It says nothing about
  the size of the edge.
- **Walk-forward is the honest out-of-sample number,** and it is lower than in-sample, as it should
  be. The frozen config doing better than the re-fitted one over the same days is hindsight: it was
  chosen with that data in view.
- **Absolute returns are flattering.** Daily compounding, no lock-in and a survivor universe all push
  them up. Compare configs and shuffles with each other, never with the competition.

**Before changing anything:** write the hypothesis in §9 first, then run both tools. Keep the frozen
values unless a change wins on both splits and on the walk-forward. The 2026-08-16 → 09-30 hold-out
was spent on CR-1, so a new change needs newer data, kept unseen.
