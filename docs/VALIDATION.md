# Out-of-sample validation: residual momentum, `final` and `comp lock`

*Oct 2 2026. Independent daily-bar re-implementation, not the team engine. The validation scripts (`v1`–`v8`), the skeptic audit and the full research log are archived in the research repo (LuisSandejas/Web3_Competition_susquehanna_Situational_Unawareness, commit 150dbdd, `research/`). Strategy names: `final` = preset `neutral`, `comp lock` = preset `comp`. Nothing was tuned on these data.*


## Method
- **Data.** Every Binance spot USDT pair, 735 in total and 656 after removing stablecoins, fiat, wrapped and gold tokens, and leveraged tokens (\*UP/\*DOWN/\*BULL/\*BEAR). Delisted coins are included. Daily klines from 2019 to 2026-08 set the universe. **1h klines** were then downloaded for the union of every monthly top-75 (471 symbols, Nov 2019 to Sep 30 2026). µs and ms timestamps are detected per row.
- **Point-in-time universe.** At each month start, coins are ranked by trailing-30-day quote volume, using only data from before that date. A coin needs at least 90 days of history in its *current* listing; a gap of more than 7 days starts a new asset, which separates LUNA from LUNA 2.0, FTT before and after its relisting, and so on. The top 35/50/75 are kept. No delisted coin was held without a next-day price.
- **Strategy.** The spec is followed exactly: 1h bars, L ∈ {72,168,336}h, 30-day beta, 7-day vol, cross-sectional z-scores inside the eligible universe, and a rebalance at 00:00 UTC. Costs are 5 bp on long turnover and 10 bp on short turnover. A **+5 bp slippage** run is also included.
- **Windows.** Non-overlapping 14-day windows start every 14 days from 2020-01-01. They are cut at 2024-01-01 and run again from 2024-01-01 to 2026-09-20, giving 104 + 71 windows. Each window starts from cash and pays its entry cost; the signal is warmed up on earlier data. The comp lock checks equity at each 00:00, which is the previous day's close.
- **Sharpe and drawdown.** Sharpe is computed on the concatenated daily series of the windows, with a 10-day block bootstrap (2,000 draws) for the 95% CI. MaxDD is measured on the same concatenated series.
- **Benchmark.** Equal-weight buy-and-hold of the window's universe.
- **Limitations.** Daily-bar simulation, not the team's engine; the lock-in is checked only daily.

## A + B. Non-overlapping 14-day windows (base costs)
| Era | N | Strategy | Windows | Mean | Median | p10 | % >0 | P(>3.3%) | P(>5.2%) | Sharpe [95% CI] | MaxDD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2020-2023 | 35 | final | 104 | +1.5% | +0.8% | -5.4% | 57% | 33% | 21% | 1.20 [0.28, 2.09] | -36% |
| 2020-2023 | 35 | comp lock | 104 | +2.9% | +4.2% | -9.3% | 67% | 57% | 43% | 1.63 [0.71, 2.54] | -43% |
| 2020-2023 | 35 | EW buy&hold | 104 | +2.8% | +2.3% | -18.1% | 60% | 47% | 42% | 0.86 [-0.03, 1.81] | -91% |
| 2020-2023 | 50 | final | 104 | +1.3% | -0.4% | -6.5% | 49% | 32% | 24% | 0.87 [-0.14, 1.83] | -51% |
| 2020-2023 | 50 | comp lock | 104 | +2.0% | +4.4% | -12.4% | 65% | 56% | 43% | 1.01 [-0.03, 2.01] | -70% |
| 2020-2023 | 50 | EW buy&hold | 104 | +2.8% | +2.3% | -19.0% | 58% | 48% | 40% | 0.85 [-0.07, 1.82] | -92% |
| 2020-2023 | 75 | final | 104 | +1.4% | +0.5% | -7.9% | 53% | 35% | 24% | 0.82 [-0.21, 1.76] | -54% |
| 2020-2023 | 75 | comp lock | 104 | +2.7% | +4.1% | -12.6% | 64% | 56% | 45% | 1.24 [0.26, 2.18] | -56% |
| 2020-2023 | 75 | EW buy&hold | 104 | +3.2% | +2.4% | -19.6% | 59% | 48% | 42% | 0.94 [-0.01, 1.95] | -89% |
| 2024-2026 | 35 | final | 71 | +0.9% | -0.3% | -4.6% | 48% | 27% | 18% | 0.90 [-0.33, 2.02] | -38% |
| 2024-2026 | 35 | comp lock | 71 | +0.4% | +1.4% | -10.0% | 54% | 45% | 32% | 0.25 [-1.12, 1.33] | -60% |
| 2024-2026 | 35 | EW buy&hold | 71 | -1.0% | -2.7% | -18.0% | 41% | 32% | 30% | -0.28 [-1.47, 0.84] | -87% |
| 2024-2026 | 50 | final | 71 | +1.5% | -0.1% | -5.7% | 49% | 32% | 20% | 1.24 [-0.06, 2.48] | -28% |
| 2024-2026 | 50 | comp lock | 71 | +0.8% | +0.4% | -11.3% | 51% | 37% | 32% | 0.43 [-1.01, 1.56] | -46% |
| 2024-2026 | 50 | EW buy&hold | 71 | -1.4% | -2.6% | -18.2% | 39% | 32% | 24% | -0.41 [-1.55, 0.70] | -90% |
| 2024-2026 | 75 | final | 71 | +1.3% | +0.8% | -6.2% | 55% | 27% | 23% | 0.99 [-0.20, 2.21] | -35% |
| 2024-2026 | 75 | comp lock | 71 | -0.3% | -0.6% | -11.1% | 49% | 41% | 34% | -0.15 [-1.53, 0.91] | -58% |
| 2024-2026 | 75 | EW buy&hold | 71 | -1.4% | -2.8% | -18.5% | 39% | 32% | 28% | -0.43 [-1.57, 0.67] | -92% |

**Sharpe and P(>5.2%) with +5 bp slippage** (values are for N35 / N50 / N75):

| Era | Mode | Sharpe | Median 14d | P(>5.2%) |
|---|---|---|---|---|
| 2020-2023 | final | 0.79 / 0.46 / 0.44 | +0.2% / -1.0% / -0.0% | 19% / 22% / 21% |
| 2020-2023 | comp lock | 1.36 / 0.74 / 1.02 | +3.9% / +3.8% / +3.7% | 40% / 42% / 42% |
| 2024-2026 | final | 0.41 / 0.77 / 0.54 | -0.8% / -0.6% / +0.3% | 14% / 18% / 21% |
| 2024-2026 | comp lock | -0.13 / 0.08 / -0.48 | +0.4% / -0.2% / -1.6% | 31% / 32% / 37% |

**By year:** median 14-day return, with the Sharpe of the year's daily series in brackets.

| Year | Windows | final N35 / N50 / N75 | comp lock N35 / N50 / N75 | EW B&H N50 |
|---|---|---|---|---|
| 2020 | 27 | +1.0% (1.6) / +0.3% (0.9) / +1.1% (0.9) | +4.0% (1.6) / +4.6% (1.9) / +5.4% (1.9) | +7.6% (1.8) |
| 2021 | 26 | +4.4% (2.5) / +4.2% (2.8) / +3.3% (2.7) | +9.5% (3.2) / +8.6% (2.3) / +7.4% (2.3) | +4.9% (1.7) |
| 2022 | 26 | +0.6% (-0.3) / -1.2% (-0.5) / +0.1% (-0.8) | +2.1% (-0.1) / +1.3% (-1.0) / +2.0% (0.2) | -3.7% (-1.2) |
| 2023 | 25 | -0.4% (0.2) / -1.0% (-0.8) / -1.3% (-0.7) | +3.8% (1.3) / +3.7% (0.1) / -0.4% (-0.1) | +0.5% (0.9) |
| 2024 | 27 | -0.1% (0.0) / -1.6% (0.4) / -0.8% (-0.3) | +1.2% (-0.1) / +2.5% (0.8) / +0.3% (-0.6) | -1.9% (0.4) |
| 2025 | 26 | -1.3% (0.4) / -0.3% (1.3) / +1.2% (1.6) | -1.2% (-0.8) / -0.8% (0.2) / -0.8% (0.6) | -3.9% (-1.0) |
| 2026 | 18 | +2.5% (3.3) / +2.5% (2.7) / +1.8% (2.1) | +4.2% (2.2) / +0.5% (0.2) / -0.6% (-0.8) | -1.8% (-0.9) |

## C. Event studies (2020-01 → 2026-09, daily strategy P&L from the window series)
CPI dates follow the BLS release schedule, including the 2025 shutdown changes: Oct 24 2025, the cancelled October release, and Dec 18 2025. FOMC dates are statement days, plus the two emergency cuts in March 2020. All dates are hard-coded in `v5_events.py` and checked against the data:
- **CPI.** The BTC 1h move in the 08:30 ET bar was a median 1.5× larger than the same hour on other weekdays.
- **FOMC.** The move in the 14:00 ET bar was 3.2× larger.

Each test compares event days with all other days using a Welch t-test.

| Event (UTC day) | n | final N35 / N50 / N75: mean bp (t) | comp lock N35 / N50 / N75: mean bp (t) | BTC realised vol bp (t) |
|---|---|---|---|---|
| CPI day | 80 | -4 (-0.6) / +16 (+0.3) / +17 (+0.3) | +19 (+0.2) / +40 (+1.1) / +19 (+0.3) | 321 vs 261 (+2.7) |
| CPI eve (day before) | 80 | -25 (-2.1) / -24 (-1.8) / -26 (-1.8) | -9 (-1.2) / +6 (-0.2) / -2 (-0.5) | 300 vs 262 (+2.0) |
| FOMC day | 55 | +15 (+0.3) / +35 (+1.1) / +35 (+0.9) | +101 (+2.2) / +112 (+2.4) / +55 (+1.1) | 351 vs 261 (+2.6) |
| FOMC day+1 | 55 | -24 (-2.2) / -7 (-0.9) / -3 (-0.5) | -12 (-0.9) / +14 (+0.1) / +29 (+0.5) | 302 vs 263 (+1.4) |

- **Volatility.** It is real: BTC realised vol is about 20–35% higher on CPI and FOMC days (t ≈ 2.6–2.7).
- **CPI day.** The strategy does **not** lose on CPI days in either mode (|t| ≤ 1.1).
- **CPI eve.** `final` is about −25 bp on CPI eve, with t between −1.8 and −2.1 for every N. `comp lock` does not show this.
- **FOMC.** `comp lock` gains about +100 bp on FOMC days (t ≈ 2.2–2.4 for N35/N50). That is the 65/35 long tilt riding BTC's average +1% on FOMC days, not the signal.
- **Multiple testing.** With about 24 strategy tests, one or two |t| > 2 results are expected by chance; none survives Bonferroni.

**Pre-declared CPI overlays.** Each overlay is compared with the unmodified strategy in the same windows. The composite is 0.4 Sortino + 0.3 Sharpe + 0.3 Calmar per 14-day window; Calmar's drawdown is floored at 0.1%, and the mean Δ is dominated by Calmar blow-ups, so the median and Wilcoxon test are reported. There are 80–90 affected windows per row.

| Rule | Mode | N35 / N50 / N75: median Δcomposite (Wilcoxon p) | N35 / N50 / N75: mean Δ14d return bp (t) | % windows improved |
|---|---|---|---|---|
| halve gross, CPI day | final | +0.09 (0.62) / -0.01 (0.56) / -0.08 (0.32) | +0 (+0.0) / -11 (-1.1) / -13 (-1.1) | 54% / 50% / 48% |
| halve gross, eve+day | final | -0.14 (0.90) / -0.01 (0.56) / +0.21 (1.00) | +11 (+0.9) / +0 (+0.0) / +1 (+0.1) | 47% / 47% / 56% |
| halve gross, CPI day | comp lock | +0.10 (0.92) / -0.33 (0.11) / -0.05 (0.47) | -20 (-1.1) / -9 (-0.6) / -31 (-1.4) | 51% / 41% / 46% |
| halve gross, eve+day | comp lock | +0.07 (0.86) / -0.13 (0.28) / -0.07 (0.43) | -16 (-0.7) / -13 (-0.9) / -43 (-1.6) | 52% / 43% / 48% |
| halve tilt, CPI day | comp lock | -0.04 (0.34) / -0.16 (0.09) / -0.00 (0.50) | -13 (-1.1) / -8 (-0.9) / -3 (-0.4) | 45% / 40% / 50% |
| halve tilt, eve+day | comp lock | -0.16 (0.32) / -0.22 (0.06) / -0.01 (0.48) | -13 (-1.2) / -15 (-2.0) / -10 (-1.0) | 47% / 38% / 49% |


- No rule improves the composite with significance. Every Wilcoxon p is ≥ 0.06, and improvements are about a coin flip (38–56% of windows).
- The only near-significant results point the **wrong way**: halving the tilt on CPI eve and day *lowers* the composite (p = 0.06) and the 14-day return (−15 bp, t = −2.0) for N50.
- **An event rule is not supported. Do not add one.**

**Oct 10 2025 crash.** On the UTC day of Oct 10:
- `final` made **+3.4% / +3.4% / +3.1%** (N35/50/75).
- `comp lock` lost **−4.9% / −3.6% / −2.1%** because of its long tilt.
- The EW universe lost −23.6% and BTC −7.6%.

Over the 14-day window that contains the crash (Sep 29 – Oct 12):
- `final` returned −2.1% / −1.4% / +2.0%.
- `comp lock` returned −9.1% / −1.2% / +5.2%.
- EW buy-and-hold returned about −25%.

Neutral mode hedges crashes; competition mode does not.

## D. Verdict

**Is it robust across regimes?** Partly.
- **Out of sample (2020–2023) the signal is positive but modest.** `final` has a Sharpe of **0.8–1.2**. The CI excludes zero only for N35, and the +5 bp-slippage Sharpe is 0.4–0.8.
- **Returns are concentrated in 2021.** `final` had a Sharpe of 2.5–2.8 that year. 2022 (LUNA/FTX) and 2023 were roughly flat to negative (Sharpe −0.8 to +0.2). So the strategy did not "work in every regime": it earned in the trending alt market and treaded water otherwise.
- **2024–2026 looks similar.** `final` has a Sharpe of 0.9–1.2, but no CI excludes zero and the median window is about 0%.
- **The earlier claim of Sharpe ≈ 2.1–2.4 does not survive.** A point-in-time universe on any era gives about **1**, before slippage.
- **`comp lock` depends on the regime.** It did well in 2020–2021, when its 0.3 net long rode the bull (2020–23 Sharpe 1.0–1.6, median 14-day return +4%, P(>5.2%) 43–45%). In 2024–26 the Sharpe is **−0.15 to 0.43** and the median is about 0%. Its edge over `final` is mostly beta.
- **Both modes have much smaller drawdowns per window than the benchmark.** p10 is −5% to −13% against −18% to −20%.
- **A curiosity, not something to act on.** In 2020–23, lagging the signal by 1–2 days *raised* the Sharpe (`v6_checks.py`: N35 1.18 → 1.40 → 1.90); in 2024–26 it lowered it. That is not look-ahead, which would make lag 0 look best. Don't tune on it.

**Realistic expectation for Oct 4–17 2026.** The table pools the PIT non-overlapping windows from `v7_expectation.py`:

| Mode | Sample | Median | p10 | P(>0) | P(>3.3%) | P(>5.2%) |
|---|---|---|---|---|---|---|
| `final` | 2024–26, N35–75 (213) | +0.3% | −5.7% | 51% | 29% | 20% |
| `final` | 2020–26, N35–75 (525) | +0.4% | −6.3% | 52% | 31% | 22% |
| `comp lock` | 2024–26, N35–75 (213) | +0.4% | −11.1% | 51% | 41% | 33% |
| `comp lock` | 2020–26, N35–75 (525) | +3.3% | −11.1% | 60% | 50% | 39% |

Plan on:
- **Neutral mode:** median about 0%, p10 about −6%, P(>0) about 50%, P(>5.2%) about 20%.
- **Competition mode:** median about 0% to +3%, depending on whether the market rallies. p10 is about −11%, P(>0) is 50–60%, and P(>5.2%) is about **33–39%**.

Competition mode is the better qualifying bet: its fatter right tail roughly doubles P(>5.2%). But its downside is twice as deep and its edge is market direction, not skill. Slippage of 5 bp costs about 0.5 pp per window in either mode. Oct 14 2026 is a CPI day inside the window. The evidence says to run the bot unchanged through it.
