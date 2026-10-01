# Backtesting

Backtests run on one year of Binance spot candles: 5m and 15m bars for all Roostoo pairs, 2025-10-01 to 2026-09-30. The simulation follows the competition rules:

- **$100,000** starting portfolio.
- **Limit orders only,** with a **0.05%** maker fee.
- Long and short positions at **1x** (no leverage).
- Results are scored on **return, Sharpe, Sortino and Calmar**.

## Run a backtest

```bash
pip install -r requirements.txt
python -m data_pipeline.binance_vision          # download data (once; re-runs only fetch new files)
python -m backtest.run --strategy ma_crossover --params fast=80,slow=400              # full year
python -m backtest.run --strategy ma_crossover --params fast=80,slow=400 --windows    # 7-day competition windows
```

Main options:

| Flag | Default | Meaning |
|---|---|---|
| `--symbols` | `BTC,ETH,SOL,BNB,XRP` | Coins to trade |
| `--interval` | `15m` | `5m` or `15m` |
| `--start` / `--end` | full year | Date range, `YYYY-MM-DD` (end exclusive) |
| `--cash` | `100000` | Starting portfolio |
| `--maker-fee` / `--taker-fee` | `0.0005` / `0.001` | Limit order fee / market order fee (market orders are only used for short buy-backs and liquidations) |
| `--limit-offset-bps` | `0` | Place buys this far below the last close and sells this far above it. Better prices, fewer fills. |
| `--limit-fill` | `through` | `through`: the price must trade past your limit to fill. `touch`: reaching it is enough. |
| `--band` | `0.01` | Don't trade if a coin's weight would change by less than this |
| `--windows` | off | Score many 7-day periods instead of one full year (see below) |

A full-year run prints a metrics table next to an equal-weight buy-and-hold benchmark. It saves `equity.csv`, `trades.csv` and `summary.json` under `results/`.

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

1. Create `backtest/strategies/my_strategy.py`:

```python
import pandas as pd
from backtest.strategy import Strategy

class MyStrategy(Strategy):
    name = "my_strategy"

    def __init__(self, lookback: int = 50):
        super().__init__(lookback=lookback)
        self.lookback = lookback

    def generate_weights(self, data):
        # data = {"BTCUSDT": DataFrame(open, high, low, close, volume, ...), ...}
        weights = {}
        for symbol, df in data.items():
            momentum = df["close"].pct_change(self.lookback)
            weights[symbol] = (momentum > 0).astype(float) / len(data)
        return pd.DataFrame(weights)
```

2. Register it in `backtest/strategies/__init__.py`:

```python
STRATEGIES = {MACrossover.name: MACrossover, MyStrategy.name: MyStrategy}
```

3. Run it with `python -m backtest.run --strategy my_strategy --params lookback=50`.

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
- **Buying back a short is a market order** at the open with the **0.1% taker fee**. That's because Roostoo's `close_short` can't take a limit price. Strategies that flip in and out of shorts often pay this fee a lot.
- **Shorts:** opening one locks cash equal to its size and pays the maker fee. Profit or loss is the entry price minus the current price, times the quantity.
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

**Trade less.** On the full year, the MA-crossover baseline trading in both directions paid $7.8k in limit fees plus $5.2k in market fees on 380 short buy-backs. Market orders were only a fifth of its trades but cost two-fifths of its fees.
