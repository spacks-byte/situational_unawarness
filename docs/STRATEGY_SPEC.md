# Strategy specification: Residual Cross-Sectional Momentum (RXM)

**Team 87, Situational Unawareness (HKUST).** Roostoo × Susquehanna × AWS Quant Hackathon, live Oct 4–17, 2026.
Version 1.1, Oct 3, 2026 (change request CR-1, §9.1). Status: **frozen for live trading.** Strategy changes only through a new git commit with evidence.

| Document | Purpose |
|---|---|
| **This file** | The single source of truth: what we trade, why, how, the evidence, and the risks |
| `docs/VALIDATION.md` | Out-of-sample validation, 2020–2026, point-in-time universe |
| `docs/TUNING.md` | How to reproduce the backtests, test a change, and grid-search without overfitting |
| `docs/LIVE_RUNBOOK.md`, `docs/DASHBOARD.md` | Operations |
| Research archive | Full research log, skeptic audit and validation scripts: LuisSandejas/Web3_Competition_susquehanna_Situational_Unawareness @ `150dbdd` |

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
│ L5  EXECUTION       daily at 00:00 UTC; limit orders 5 bp passive; re-quote ≤ 1 per 15 min; │
│                     drift band 1%; short covers via short_close (market)                    │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ L6  RISK / GUARD    16 pre-trade and real-time checks (leverage, caps, fat-finger,          │
│                     self-cross, stale data, API budget, kill switch); NO drawdown brakes   │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ L7  MONITORING      dashboard (positions, blotter, P&L, composite score, guard status);    │
│                     audit JSONL + SQLite intent journal; daily heartbeat                    │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

| Layer | Code | Based on | Evidence |
|---|---|---|---|
| L0 Data | `tradebot/live/market_data.py`, `tradebot/data/` | Binance Vision and public klines (Roostoo mirrors exchange prices) | Data audit: 0 gaps, 0 duplicates, ms→µs timestamps handled |
| L1 Universe | `tradebot/strategy/library/rxm.py: UNIVERSE` | Liquidity and history requirements | Point-in-time vs survivor gap measured (§6) |
| L2 Signal | `tradebot/strategy/library/rxm.py` (`ResidualMomentum`) | XS momentum literature; residualisation; ensemble over horizons | Placebo p < 0.003; lag decay smooth; out-of-sample Sharpe ≈ 1 |
| L3 Portfolio | same file (k, gross, tilt; frozen `PRESETS`) | Momentum lives in extreme ranks; inverse-vol risk budgeting | Top-k beat a covariance optimizer (v3 tied, not adopted) |
| L4 Overlay | `tradebot/backtest/simulator.py` lock-in; `tradebot/live/bridge.py` | Two-stage scoring: qualify on return, then rank on risk ratios | Discovery P(>5.2%) 18% → 26%; out-of-sample 33–39% vs 20% |
| L5 Execution | `tradebot/engine/` + bridge, `ThrottledPort` | Fee wall: 10 bp per long round trip, 20 bp per short | 5 bp offset t = 3.7; faster rebalancing loses money |
| L6 Risk | `tradebot/live/guard.py` | Rules (1×, directional only, 30 calls/min); research showed brakes hurt | Unit-tested (`tests/test_guard.py`); drawdown brake measured −2.9 composite |
| L7 Monitoring | `dashboard/` | Judges audit trade logs and commit history | Rendered on backtest and engine runs |

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
- **Order flow:**
  1. Daily target at 00:00 UTC.
  2. Limit orders 5 bp passive, resting about 15 minutes.
  3. Re-quote at most once per 15-minute bar.
  4. Weights inside the 1% drift band are held.
- **API budget:** about 10 requests/min idle, capped at 25/min while rebalancing (`ThrottledPort`).
  The limit is 30.
- **Measured: why not faster?**

  | Rebalance every | 1h | 4h | 12h | **24h** |
  |---|---|---|---|---|
  | Median 14-day return | −3.6% | −0.8% | +0.4% | **+1.4%** |

  Minute-level prediction models reach a 58–64% hit rate but net ≈ 0 after fees (the fee wall).

## 5. Risk management
- **Structural:** long/short (short leg present in both modes), inverse-vol sizing, k_eff guard,
  stale-feed exclusion, volatility floor.
- **Guard (L6):** blocks leverage > 1×, a single coin above 0.40–0.60 of equity, fat-finger prices
  (> 3% from last), self-crossing orders (market-making optics), stale data (> 5 min), API overuse,
  and the kill-switch file. It only *warns* on drawdown.
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
   - ~~Long exits are market orders in the live engine, ≈ 5 bp worse than the backtest.~~ Fixed Oct 3
     (tradebot port): exits are 5 bp passive limits, as in the backtest. Short closes stay market (API).
   - Roostoo's real fill rules, cancel refunds and `Lock` accounting must be verified on the TEST
     account before Oct 4 (LIVE_RUNBOOK).
4. **Outages.** The alpha decays within 1–2 days, so a missed day costs ≈ 25% of the edge. A watchdog
   alerts if there is no daily signal by 02:00 UTC.
5. **Concentration.** With k = 3, a single coin can reach ≈ 0.55 of the book. The guard cap is set
   accordingly.
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
