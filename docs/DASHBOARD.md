<!-- Agent handoff: see root AGENTS.md before starting the next feature. -->

**TODO — highest-priority follow-up after the execution/reconciliation fix and dashboard
server deployment:** show persisted account reconciliation issues as dashboard flags,
including affected strategy/symbol/order, evidence, recurrence and resolution. They
must never gate trading. Do not implement badges, notifications or issue-management
controls in the current execution fix. Backend signals already belong to that fix.

# Trading dashboard and Guard

## Interactive research desk (Supabase)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m tradebot desk --port 8766
# Open http://127.0.0.1:8766
```

The interactive desk reads `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` from the
repository-root `.env` (existing environment variables take precedence). Run it
from the repository root. Credentials stay on the Python server; the browser
only calls local dashboard endpoints. The server binds to loopback, checks Host
and Origin, and requires a session token for backtest submissions. It does not
place orders or write to Supabase. Do not expose this local server through a public proxy.

### Research

- Choose **RXM** (competition / neutral presets), **MA crossover**, or
  **MM fluctuation**, capital, and inclusive start / exclusive end in UTC.
  RXM/MA expose symbols, candle interval, weights and execution controls.
  MM has an editable symbol universe with allocation fields generated for each
  ticker. New symbols start at 0%; allocations must total 100%. Zero-weight
  symbols are skipped, including history/rule loading. MM uses fixed one-second
  candles and exposes symbol allocations, refresh,
  warm-up, feature lag, fixed lots, inventory cap, one-tick distance,
  penetration ticks/probability/seed and optional terminal liquidation.
  Market slippage is available for every strategy. MM's maker fee is frozen
  at 5 bps; all entered capital goes to independent MM books.
  See [MM backtesting](BACKTESTING.md#independent-mm-backtests) for data sources,
  saved instrument rules, assumptions and equivalent CLI/configuration settings.
- Runs use the existing `run_backtest` implementation. RXM receives 45 days of
  warm-up; MA receives enough bars for its slow average. Missing candles fail
  the run explicitly instead of silently shrinking the universe. Periods are
  limited to 90 days and completed candles.
- Historical Binance spot / USDT candles are loaded from configured local
  Parquet files when coverage is complete, or downloaded through Binance's
  public market-data API. Complete windows are cached in `var/dashboard/candles`.
  Initial RXM downloads cover its full 35-symbol universe and may take a few minutes.
- P&L includes simulator fees. Sharpe and Sortino use UTC daily close-to-close
  returns, annualized with 365 days and a zero risk-free rate. Calmar is
  annualized compounded return divided by maximum drawdown. First-day returns
  start from initial capital; partial boundary days count. Undefined ratios
  display **—**. Drawdown uses every simulation bar, including initial capital
  in the peak, so first-bar losses are included.
- Hover over equity, drawdown, prices and execution markers. Buy/cover markers
  are green; sell/short markers are red. Backtest execution times are bar times,
  because OHLC candles do not reveal the exact intrabar fill time.
- **Quotes CSV** exports all submitted limit orders (including unfilled orders),
  original limit price, fill outcome, execution price and expiry. Quotes expire
  after one bar for RXM/MA. MM exports lifecycle IDs, posting/expiry/fill times,
  posting tick, assigned penetration and terminal status. **Trades CSV** exports executed orders only, including market
  covers, quantities, prices and fees. CSVs contain full-resolution rows even
  when long price/equity curves are reduced for display. Quantities follow the
  weight simulator's notional sizing for RXM/MA; MM uses saved Roostoo precision
  and minimum-order rules. Terminal MM exits are included in trades.
- One backtest runs at a time. The five most recent results/exports remain in
  server memory until eviction or restart. Reloading the page resumes the latest
  run for that browser tab. Completed results and exports are also persisted to
  `<backtest.results_dir>/dashboard/<job-id>/` with effective configuration and
  MM data/rule provenance.

### Live tables and comparison

The data source is the existing `trade_transactions` table, filtered to
`environment=live`, paginated in full and refreshed every 30 seconds while the
page is visible. Latest records are deduplicated by account and exchange order ID.
Strategy and account filters apply to both live tables; status and symbol/order
search additionally filter the blotter.

The **Strategy P&L** table shows realized, unrealized and total P&L per strategy,
plus account and open-position counts. It respects the strategy/account filters
and aggregates accounts only after calculating each account's cost basis
separately. Realized P&L uses weighted-average entry cost on closed quantities,
including fully closed positions; unrealized P&L uses current Binance USDT marks.
These values cover all available recorded history and exclude cash trading fees;
coin-denominated fees adjust holdings and cost basis. Only confirmed full executions
affect either component. Missing entry history makes the affected
strategy totals unavailable; missing marks affect unrealized and total P&L while
retaining known realized P&L. Order-status and search filters affect only the
blotter, not strategy totals.

Positions are reconstructed separately for each account, strategy, symbol and
long/short side using cumulative filled quantities and weighted-average entry
prices. Raw Roostoo responses take precedence over legacy execution labels. Canceled
orders have zero executed quantity. Legacy partial labels without authoritative full-fill
evidence remain audit records and do not create positions or execution markers.
USD trade entries are treated as USDT at 1:1; displayed position P&L is
**unrealized and before fees**, marked to Binance spot USDT. Base-currency fees
reduce long holdings when recorded. Missing marks and insufficient position
history show **—**, not a zero price or invented cost basis. This is a ledger
view, not an authoritative exchange balance: transfers and trades missing from
Supabase cannot be reconstructed. Canceled orders remain in the blotter with zero executed quantity.

Enable **Show live executions** to show actual fills beside the simulated
executions. Choose a live strategy and symbol; the account filter also applies.
The live chart is strictly bounded by the first submitted transaction and last
recorded resolution for that strategy/account. The chart explicitly labels this
as the **recorded activity window**. Live markers use final fill timestamps and
full order executions; Roostoo has no partial executions.

**TODO — strategy lifecycle:** add a `strategy_runs` table with strategy, bot ID,
activation, deactivation and heartbeat timestamps, including restart intervals.
Replace the inferred bounds in `dashboard/remote.py::execution_window` and
`dashboard/research.py::live_executions` with those intervals. Transaction gaps
cannot prove inactivity, and the final transaction cannot prove the strategy
has stopped. Until lifecycle data exists, retain the recorded-window label.

**TODO — account reconciliation:** add periodic exchange position snapshots and
execution events to distinguish incomplete trade history and account inventory that predates a strategy's recorded orders.

### Implementation and verification

- `dashboard/remote.py`: read-only Supabase/Binance adapters, order normalization,
  position reconstruction and recorded activity bounds.
- `dashboard/research.py`: request validation, simulator integration, daily
  metrics, CSVs and live execution charts.
- `dashboard/server.py`: local HTTP API and background run lifecycle.
- `dashboard/app.html`, `app.css`, `app.js`: responsive UI, charts and filters.

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_server.py tests/test_backtest.py tests/test_metrics.py
```

Provider references: [Supabase REST API](https://supabase.com/docs/guides/api),
[server-side API keys](https://supabase.com/docs/guides/getting-started/api-keys),
[Binance public market data](https://developers.binance.com/en/docs/products/spot/rest-api).

## Static dashboard and Guard

The original `tradebot dashboard` command builds a single static HTML file: no server, no network, everything inline. The Guard
(`tradebot/live/guard.py`) is a second line of defence. The bot calls it before every order batch and on
every poll. Neither one calls an exchange or reads credentials.

## Run

```bash
# Backtest source: frozen 'comp' preset over the last 14 days of data/binance/klines/15m
python -m tradebot dashboard --source backtest --out results/dashboard.html
python -m tradebot dashboard --source backtest --end 2026-09-15 --days 14   # another window

# Engine source: a live or mock engine run directory
python -m tradebot dashboard --source engine --engine-dir var/live_comp --out results/dashboard.html

# Rebuild every 30 s. The page reloads itself at the same rate.
python -m tradebot dashboard --source engine --engine-dir var/live_comp --watch 30
```

The backtest source calls `tradebot.backtest.run_backtest` with `ResidualMomentum` with the frozen `comp` preset (`src/tradebot/strategy/library/rxm.py`)
(k=3, tilt 0.3, gross 1.0, rank buffer 2, residual 3/7/14-day ensemble, daily rebalance) and
`BacktestConfig(limit_offset_bps=5, lockin_return=0.06, lockin_scale=0.3)`. It uses 45 days of
warm-up, and the window starts from cash. A build takes about 4 seconds.

The engine source reads these files from `--engine-dir`. You can also pass each one with
`--audit/--db/--snapshot`.

| File (newest match wins) | Used for |
|---|---|
| `*audit*.jsonl` (`AuditLog`) | blotter (`operation` events), risk rejections, any event with `equity_usd` |
| `*.db` (`IntentJournal`, opened read-only) | intent counts by status (PENDING_SEND/SENT/RESOLVED/UNCERTAIN) |
| `latest_snapshot.json` / `*snapshot*.json` | positions, prices, equity, guard state |
| `snapshots.jsonl` (optional) | equity, gross and net history (one `{"timestamp", "snapshot"}` per line) |

The snapshot can be a bare normalized snapshot (`read_exchange_snapshot` output). It can also be
wrapped with extras that the strategy bridge should write:

```json
{"timestamp": "2026-10-05T12:00:00+00:00",
 "snapshot": {"cash_usd": 0, "longs": {}, "shorts": {}, "prices": {}, "entry_prices": {}, "pending_orders": [], "equity_usd": 100000},
 "target_weights": {"SEI": 0.22, "ZEC": -0.12},
 "scores": {"SEI": 1.52, "ZEC": -1.31},
 "price_times": {"SEI": "2026-10-05T11:59:40+00:00"},
 "locked": false}
```

## Panels

The page is ordered for research first. The live-monitoring panels (Guard, Positions, Blotter,
Execution) sit in a collapsed **Monitoring** section at the bottom.

| Panel | Contents |
|---|---|
| Where the strategy trades | Backtest source only. One coin at a time (buttons show the number of fills): the 15m price with a marker for every order (buy, sell, short, cover; hollow = not filled) and shading while the coin is held long or short. Underneath, that coin's signal score against the score of the k-th best and k-th worst coin, i.e. the level a new coin must reach to enter each book. With the rank buffer, a held coin can stay a little past that line |
| Header | Source, as-of time, lock-in pill (ARMED or LOCKED 0.3×), overall guard status (worst check) |
| KPI strip | Equity, P&L $ and %, today's UTC P&L, return vs the 3.3%/5.2% qualification cut-offs and the lock-in level (progress bar), drawdown now and max, competition day, Sharpe, Sortino, Calmar, composite `0.4·Sortino + 0.3·Sharpe + 0.3·min(Calmar, 50)`, gross, net, lock-in state |
| Equity | Equity curve with dashed reference lines (start, +3.3%, +5.2%, lock-in) and the lock-in time. Drawdown is drawn underneath on its own axis. The crosshair is synced across both |
| Guard | Every check with OK/WARN/BLOCK, the current value and the limit. In backtest mode the order checks replay the last order batch |
| Positions | Symbol, side, qty, entry, last, value, weight (with a bar), target weight, drift (amber when ≥ 3%), uP&L $ and %, contribution to return (realized + unrealized − fees, as % of start equity) |
| Exposure by symbol | Signed weight bars (blue long, orange short) with a tick at the target. Gross and net over time are shown below, with the 100% cap |
| Blotter | Newest first: time, symbol, side, LIMIT/MARKET, qty, price, notional, fee, filled, status. Filter: all / filled / unfilled |
| Execution | Orders, fills, limit fill rate, maker and taker fees, fees in bp of notional, turnover, stats by side. A heartbeat strip shows fills per UTC day (red = a day with no fill). Intent-journal counts appear in engine mode |
| Signal | Today's ranking: ensemble residual-momentum score, annualized vol, longs and shorts, target weight. In backtest mode the dashboard recomputes the score independently. `MATCHES STRATEGY` confirms that its top/bottom-k equals the strategy's own selection |
| P&L attribution | Contribution by symbol across open and closed positions |

## Calling the Guard from the bot

```python
from src.guard import Guard, GuardConfig, ProposedOrder

guard = Guard(GuardConfig(initial_equity_usd=100_000, kill_file="KILL"))
# On restart, replay equity history so the peak and the lock-in latch are correct:
# for ts, eq in history: guard.observe_equity(eq, ts)

def on_poll(snapshot, target_weights, price_times):
    guard.record_api_call(n=3)                       # every REST call you make (ticker, balance, ...)
    report = guard.poll(snapshot, target_weights=target_weights, price_times=price_times,
                        bot_locked=strategy_is_locked)
    log.info(report.summary())                       # e.g. "[guard poll] WARN (WARN:target_drift)"
    audit.record("guard", report.to_dict())          # the dashboard can show it later
    return report

def before_send(snapshot, orders, target_weights, price_times):
    proposed = [ProposedOrder(o.symbol, o.side, o.qty, o.limit_price) for o in orders]  # side: BUY/SELL/SHORT/COVER
    report = guard.pre_trade(snapshot, proposed, target_weights=target_weights, price_times=price_times)
    if not report.allowed:                           # at least one BLOCK
        log.error(report.summary()); return False    # do not send; try again next poll
    send(orders)
    guard.record_orders_sent(len(orders))
    return True

def on_fill(fill_time):
    guard.record_fill(fill_time)                     # feeds the daily heartbeat
```

`touch KILL` in the bot's working directory blocks every new order. `rm KILL` resumes trading.
`guard.kill()` does the same thing from code.

Exposure checks run on the post-trade projection: every proposed order is assumed to fill, with
fees included. They BLOCK only when a limit is breached and the batch makes it worse. A batch that
reduces risk is never blocked.

## Checks and default thresholds (`GuardConfig`)

| Check | OK | WARN | BLOCK |
|---|---|---|---|
| `kill_switch` | no `KILL` file | — | `KILL` file present or `guard.kill()` |
| `gross_exposure` | ≤ 1.00 | > 1.00 (on a poll, or when the batch reduces it) | projected > 1.005 and the batch adds gross; any poll > 1.10 |
| `symbol_weight` | max \|w\| ≤ 0.30 | 0.30–0.40 | > 0.40 and the batch increases it |
| `net_exposure` | −0.05…+0.45 | outside the warn band | outside −0.20…+0.60 and the batch worsens it |
| `short_collateral` | ≤ 40% of equity | 40–45% | > 45% of equity (or `max_short_collateral_usd`) and the batch adds to it |
| `cash_buffer` | ≥ $50 | < $50 | projected cash < $0 (overdraft) |
| `price_band` (fat finger) | limit within 1% of last | 1–3% | > 3%, or no last price for the symbol |
| `order_notional` | ≥ $10 and ≤ cap | an order below the $10 exchange minimum | an order > min($50k, 40% of equity) |
| `self_cross` | one book side per symbol | — | buy and sell on the same symbol in the batch or against resting orders (market-making / wash optics) |
| `order_rate` | ≤ 12 orders/60 s | 13–20 | > 20 |
| `api_budget` | ≤ 18 calls/60 s | > 18 (60% of 30) | > 27 (90% of the 30/min budget) |
| `stale_data` | oldest price ≤ 120 s | 120–300 s, or no timestamps supplied | > 300 s, or a held/ordered symbol has no timestamp |
| `drawdown` | < 5% from peak | ≥ 5%, "ALERT" at ≥ 10% | never. Monitor-only by design: drawdown brakes destroyed value in research |
| `lockin` | armed below +6%; locked with target ≤ 0.32 gross | locked but book still de-risking; bot and guard latch disagree | locked (return reached +6%, sticky) but target gross > 0.3 + 0.02 |
| `target_drift` | max \|actual − target\| < 3% | ≥ 3%, "ALERT" at ≥ 10% | never |
| `daily_heartbeat` | a fill today (UTC), or before 18:00 UTC | no fill by 18:00 UTC, "URGENT" after 22:00 UTC | never |

Every check is also exported as a pure function (`check_*` in `tradebot/live/guard.py`) that returns
`CheckResult(name, status, value, limit, message)`. The tests are in `tests/test_guard.py`.

## Notes for go-live

- The engine's `ExecutionConfig` defaults are `max_order_value_usd = 25_000` and
  `max_total_short_collateral_usd = 10_000`. On a $100k comp-mode book these reject normal
  rebalances: one 30% long is a $30k parent order, and the short book is about $35k. The engine
  checks the parent amount before child slicing. Raise both limits in the live config and leave
  the per-order and short caps to the Guard. A mock run with the defaults returned
  `REJECTED_RISK: max_order_value`.
