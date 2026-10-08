# Backtesting

Weight-strategy backtests use the configured Binance spot history. The original research dataset contains 5m and 15m bars for all Roostoo pairs, 2025-10-01 to 2026-09-30. The simulation follows the competition rules:

- **$100,000** starting portfolio.
- **Limit entries and spot exits**, with a **0.05%** maker fee; short covers execute at market.
- Long and short positions at **1x** (no leverage).
- Results are scored on **return, Sharpe, Sortino and Calmar**.

## Run a backtest

```bash
pip install -e ".[dev]"
python -m tradebot data                                                                  # download data (once; re-runs only fetch new files)
python -m tradebot backtest --strategy ma_crossover --params fast=80,slow=400             # full year
python -m tradebot backtest --strategy ma_crossover --params fast=80,slow=400 --windows   # 7-day competition windows
```

Main options:

| Flag | Default | Meaning |
|---|---|---|
| `--symbols` | `BTC,ETH,SOL,BNB,XRP` | Coins to trade |
| `--interval` | `15m` | `5m` or `15m` |
| `--start` / `--end` | full year | Date range, `YYYY-MM-DD` (end exclusive) |
| `--last-hours` / `--last-minutes` | off | Most recent X hours or minutes, ending at the latest completed candle |
| `--cash` | `100000` | Starting portfolio |
| `--maker-fee` | `0.0005` | Spot limit order fee |
| `--short-open-fee` / `--short-close-fee` | `0.001` / `0.001` | Fees on opening a short (charged on collateral, even for limit opens) and on closing one (always market) |
| `--limit-offset-bps` | `0` | Place buys this far below the last close and sells this far above it. Better prices, fewer fills. |
| `--limit-fill` | `through` | `through`: the price must trade past your limit to fill. `touch`: reaching it is enough. |
| `--band` | `0.01` | Don't trade if a coin's weight would change by less than this |
| `--windows` | off | Score many 7-day periods instead of one full year (see below) |

A full-year run prints a metrics table next to an equal-weight buy-and-hold benchmark. It saves `equity.csv`, `trades.csv` and `summary.json` under `results/`.

For a recent intraday window, use `--last-hours 6` or `--last-minutes 30` instead
of `--start` / `--end`. Fractional amounts are accepted when they span whole
candles; for example, 30 minutes with 5m candles, or 1.5 hours with 15m candles.
Relative ranges are limited to 90 days and cannot be combined with `--windows`.
The end is exclusive, rounded down to a completed UTC candle boundary when the
run is submitted; saved results include the exact start and end. Warm-up is
loaded before the requested window and is excluded from performance results.
Weight-strategy CLI runs require downloaded history covering warm-up through
the end; MM uses its configured cache/download providers. Missing recent data
fails explicitly; the range is never shifted back to an older cached window.

```bash
python -m tradebot backtest --strategy ma_crossover --interval 5m --last-minutes 30
python -m tradebot backtest --strategy mm-10m-fluctuation --last-hours 6
```

## Independent MM backtests

`mm-10m-fluctuation` uses `MMFluctuation.generate_quotes` with persistent EWMA
features, a frozen startup anchor and fixed lots. All starting capital goes to
separate long-only symbol books (PEPE/BONK/1000CHEEMS at 85% / 7.5% / 7.5%
by default). Change the universe using the dashboard Symbol universe field or
CLI `--allocations`, with up to 50 symbols. Allocations may be zero and must
total 100%; zero-weight symbols are disabled and require no history or
instrument rules. Active symbols require available history and Roostoo rules.
The live shared-account 90/10 allocation and live state files are not used.
The existing `replay --shared` command is unchanged; combined MM/RXM simulation
is outside this independent backtest.

```bash
python -m tradebot backtest --strategy mm-10m-fluctuation \
  --start 2026-09-21 --end 2026-10-05 --cash 100000 \
  --archive-cache ../shared-backtest/data \
  --instrument-rules-path path/to/saved-exchange-info.json \
  --no-download-missing

# Execution stress and optional final inventory exit:
python -m tradebot backtest --strategy mm-10m-fluctuation \
  --start 2026-09-21 --end 2026-10-05 \
  --penetration-ticks 1 --penetration-probability .5 --random-seed 42 \
  --market-slippage-bps 10 --liquidate-mm
```

MM requires start/end times or a relative duration, and the fixed `1s` interval. Start is
inclusive; end is exclusive. Current-rule snapshots are historical assumptions,
not reconstructed historical Roostoo rules. Set `backtest.instrument_rules_path`
to saved exchangeInfo JSON. If necessary, the loader fetches **only public,
read-only exchangeInfo** and saves a snapshot. No credentials or trading client
are required. `backtest.instrument_rules` can override individual pair fields,
for example `PEPE/USD: {PricePrecision: 8}`. Missing or invalid precision,
minimum-notional or tradability rules produce an actionable error.

| CLI flag | Default | Behavior |
|---|---|---|
| `--allocations` | `PEPE=.85,BONK=.075,1000CHEEMS=.075` | Nonnegative fractions totaling 1; keys set the MM universe |
| `--refresh-seconds` | 600 | Sync older fills, cancel, release reservations, replace quotes |
| `--mm-warmup-seconds` | 3600 | Observed seconds before trading |
| `--feature-lag-seconds` | 1 | Additional completed-candle feature lag, smaller than warm-up |
| `--lot-fraction` | .05 | Fixed fraction of starting symbol capital at startup anchor |
| `--inventory-fraction` | .40 | Long inventory capacity, valued at latest completed close |
| `--no-one-tick-distance` | not set | Disable the existing minimum one-tick quote distance |
| `--penetration-ticks` | 0 | Additional ticks below bids / above asks required for filling |
| `--penetration-probability` | 1 | Probability each side requires that penetration; otherwise touch |
| `--random-seed` | 0 | Native SplitMix64, keyed by seed, absolute posting second and side |
| `--market-slippage-bps` | 0 | Adverse market price adjustment; also applies to RXM/MA covers |
| `--liquidate-mm` | off | Cancel quotes and sell inventory at final close, with slippage and taker fee |
| `--archive-cache` | configured roots | Repeat for fallback roots, including optional shared-backtest data |
| `--candle-store-dir` | `var/market` | Read local one-second candle-store files |
| `--no-download-missing` | downloads enabled | Require cached candles and saved/overridden rules |

The frozen MM maker fee is 5 bps, including reservations and spread acceptance.
The taker fee comes from `fees.spot_taker` (10 bps by default). Bids reserve cash
including fees; asks reserve already-owned inventory. Sale proceeds cannot fund
simultaneously posted bids. A posted side fills once, in full, at its quote price
when a trade-containing second reaches its assigned threshold. The threshold
uses the posting tick and native comparison tolerance; its random assignment is
retained until fill/cancellation and shared across coins for the same second/side.
There is no queue, partial-fill, impact or price-improvement model. Market
slippage never changes limit executions; forced short liquidation losses remain
capped at posted collateral. Midpoint reference requests are rejected without
recorded historical bid/ask observations.

History is processed in daily chunks. Configured verified binary archives are
checked first, then local candle stores / klines / normalized caches, then Binance
monthly/daily archives. Only recent unpublished archive tails (last three days)
fall back to REST. Missing, duplicate, malformed or corrupt seconds are never
interpolated. Errors identify symbol and period, including warm-up. The optional
sibling shared-backtest cache is not assumed to exist or cover any requested day.

MM saves full-resolution `equity.csv`, all posted quotes and terminal statuses in
`quotes.csv`, executed fills (including optional terminal exits) in `trades.csv`,
and effective configuration, source hashes, rules, assumptions and metrics in
`summary.json`. Quote IDs are symbol-scoped. Quote posting/expiry/resolution times
are explicit; fill times label candle closes, not known intrasecond timestamps.
Equity labels candle opens to group the last second with its own UTC day.
Terminal costs replace the last equity point, avoiding an extra daily observation.
Drawdown uses every second before display downsampling. CLI `--windows` currently
applies to weight strategies; use explicit MM periods.

**TODO — Superday data-source integration:** add Superday as another one-second
archive provider through `SecondHistory`'s normalized UTC OHLC/volume/trades
interface and the same strict validation/provenance contract. Connector code,
credentials, provider-specific schemas and source-precedence decisions are
deferred until that integration is requested.

### Verification of the MM implementation

The cached 2026-09-21 through 2026-10-05 three-symbol baseline, using saved Roostoo
rules and $100,000, produced 11,535 quotes, 8,931 fills, $8,335.79 maker fees and
$121,275.55 final marked equity. CLI and browser-submitted dashboard results
matched quote/fill exports and all shared metrics exactly across 1,209,600
one-second equity observations. These are candle-proxy simulation results.
`tests/test_mm_backtest.py` covers execution stress, accounting, chunk boundaries,
data errors and exports. `scripts/validate_mm_policy.py` matched 144 native
quote decisions per coin using identical input features and starting books;
`tests/fixtures/mm_execution_native.json` records native 3-tick, 50%-probability
SplitMix64 assignments for seeds 0/17/123 and absolute posting seconds.

## Competition windows (`--windows`)

The competition lasts about a week and starts from cash, so a single 12-month result can be misleading. `--windows` tests the strategy on every 7-day period in the year (about 330, one starting each day):
- Each window starts at $100,000 in cash.
- The strategy can see the 30 days before the window to warm up its indicators, but it can't trade until the window starts.

The output shows the **10th percentile, median and 90th percentile** of each judged metric, next to buy-and-hold. It also shows the share of windows that made money and the share that beat buy-and-hold. Per-window results are saved to `windows.csv`.

How to read it:
- **The median** is the typical week.
- **The 10th percentile** is a bad week. A strategy with a decent median and a much better 10th percentile than buy-and-hold is what we want.
- **Ignore the size of Calmar** in 7-day windows. Annualizing one week's return gives extreme numbers, so only compare it between strategies.

Options: `--window-days 7`, `--step-days 1`, `--warmup-days 30`. A full windows run takes about 1–2 minutes; use `--step-days 7` for a quick check.

## Write a strategy

A strategy answers one question for every bar: **what fraction of the portfolio should be in each coin?**

1. Create `src/tradebot/strategy/library/my_strategy.py`:

```python
import pandas as pd
from tradebot.strategy.base import Strategy

class MyStrategy(Strategy):
    name = "my_strategy"

    def __init__(self, lookback: int = 50):
        super().__init__(lookback=lookback)
        self.lookback = lookback

    def generate_weights(self, data):
        # data = {"BTC": DataFrame(open, high, low, close, volume, ...), ...}
        weights = {}
        for symbol, df in data.items():
            momentum = df["close"].pct_change(self.lookback)
            weights[symbol] = (momentum > 0).astype(float) / len(data)
        return pd.DataFrame(weights)
```

2. Register it in `src/tradebot/strategy/registry.py`:

```python
STRATEGIES = {MACrossover.name: MACrossover, MyStrategy.name: MyStrategy}
```

3. Run it with `python -m tradebot backtest --strategy my_strategy --params lookback=50`.

### Rules for the weights

- One row per bar and one column per coin. Each value is between **−1 and 1**:
  - **Positive = long.** `0.3` means hold 30% of equity in that coin.
  - **Negative = short.** `-0.3` means a short worth 30% of equity.
  - **0 = no position.**
- Shorts are 1x: a short locks cash equal to its size. So the sum of the absolute weights in each row should be **≤ 1**. For example, `{BTC: 0.5, ETH: -0.5}` uses all the capital. Anything left over is held as cash, and rows over 1 are scaled down.
- A row can use data up to the **close** of its own bar. Orders are placed at that close price and can only fill during the **next** bar, so the backtest can't see the future. Don't add lookahead yourself: no `.shift(-1)` and no `rolling(..., center=True)`.

## How fills are simulated

- **Limit orders:** every buy, sell and new short is a limit order at the last close price, adjusted by `--limit-offset-bps`. It stays open for one bar.
  - A buy fills if the bar's low goes below the limit; a sell or new short fills if the bar's high goes above it.
  - If the price opened past your limit, the order fills at the better open price.
  - Unfilled orders are cancelled, and the strategy's next target is re-tried on the next bar. The `fill_rate` in the results shows how often orders filled.
- **Cash is locked by open orders,** as on Roostoo. A new buy can only use cash you already have, not cash from a sell that's still waiting to fill. So switching from one coin to another takes **two bars**: sell first, then buy.
- **Buying back a short is a market order** at the open with the **0.1% short-close fee**. That's because Roostoo's `close_short` can't take a limit price.
- **Shorts:** opening one locks cash equal to its size and pays the **0.1% short-open fee**, even as a limit order (verified Roostoo behaviour; see `docs/DESIGN.md`). Profit or loss is the entry price minus the current price, times the quantity. A short round trip therefore costs 0.2%, against 0.1% for a long, so strategies that flip in and out of shorts often pay heavily.
- **Liquidation:** a short is force-closed with a market order when its collateral runs out, which at 1x means the price has doubled. This is checked against each bar's high, and at the open if the price jumped past the liquidation level. You can lose at most the posted collateral. Liquidations are counted in the results as `num_liquidations`.

### Short-selling assumptions

Roostoo hasn't published these, so they're adjustable:

| Flag | Default | Meaning |
|---|---|---|
| `--borrow-rate` | `0` | Yearly borrowing fee on the short size (`0.1` = 10%/yr) |
| `--maintenance` | `0` | Liquidate when the short's equity drops to this fraction of its size |

## How metrics are calculated

- **Sharpe and Sortino** use daily returns (UTC days), scaled to a year (×√365). Sortino counts only losing days in its risk measure.
- **Calmar** is the yearly return divided by the max drawdown.
- **Max drawdown** uses every bar, so dips during the day count.

**Trade less.** On the full year, the MA-crossover baseline (80/400) trading in both directions paid $10.3k in limit-order fees and $5.1k in market-order fees, against $4.4k in total for the long-only version. Metrics `limit_order_fees` and `market_order_fees` show the split.
