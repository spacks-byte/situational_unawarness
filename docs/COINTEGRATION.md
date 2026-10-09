# Cointegration integration: steps 1–3

`cointegration-pairs` supports observation and explicitly enabled live execution.
The default configuration is execution-capable, but dry-run and replay modes never
submit orders. A live run must also set `cointegration.execution: execute`,
provide a bounded `cycle_end`, and pass the existing live confirmation guard.

## Implemented behavior

- The current 13 pairs and thresholds, including BNB–LISTA 2.75/0.31, are defined in
  `core/cointegration.py`. A test compares them with the archived handoff configuration.
- Signals use complete, synchronized Binance spot USDT 30-minute candles. A reset
  fits log(A) on log(B) using 2,880 preceding closes; alpha/beta remain frozen.
- Z-scores use the preceding 960 residuals, excluding the current close. Entry is
  strictly `abs(z) > X`, exit strictly `abs(z) < Y`. A zero crossing alone does not
  exit. Holding-time exits take priority. The last training close cannot enter.
- Allocation uses the sample standard deviation of 60 daily observations of
  `0.5 * (return_A - return_B)`. Inverse-volatility weights and budgets stay fixed.
  Legs enter with equal dollars and retain their quantities. Beta affects the
  signal, not the hedge quantity. Idle allocations remain idle.
- No new coin cap, stop-loss, momentum filter or covariance weighting was added.

`PairRuntime` stores its configuration, cycle ID, fitted models, rolling residual
history, budgets, positions and last processed candle in SQLite. Each completed
candle commits with its decisions and pending intents. Duplicate candles do
nothing; a missing candle raises an error without advancing state. Configuration
changes on restart are rejected. A new configuration/reset needs a new database.

Confirmed executions can be attributed through `record_fill`; a pending instruction
is never treated as a fill. Restarting between the two leg fills preserves both
the filled quantity and the outstanding leg. The live observation loop never
calls `fill_reference`. That explicitly named method exists for offline backtests
and defaults to the handoff's 5-bps long fees, 10-bps short fees and 2-bps slippage.
Future market execution must use actual fills and fees (configured spot taker fee
is 10 bps), rather than those reference assumptions.

## CLI backtesting

The standard CLI now dispatches `cointegration-pairs` to its own fixed-quantity
engine. No exchange credentials are required. From the repository root with the
project virtual environment activated:

```sh
PYTHONPATH=src python -m tradebot backtest \
  --strategy cointegration-pairs --last-hours 24 --cash 10000 \
  --pair-cost-model market
```

Or select an exact UTC period (start inclusive, end exclusive, both on half-hour
boundaries):

```sh
PYTHONPATH=src python -m tradebot backtest \
  --strategy cointegration-pairs --start 2026-09-01 --end 2026-09-29 \
  --windows --window-days 14 --pair-cost-model market
```

`--windows` resets capital, fits the model and recomputes inverse-volatility
weights using only preceding data at each window start. Defaults are 14 days
and no overlap; `--step-days`, if supplied, must equal `--window-days`. The date
range must contain whole windows. Reports include sample count, mean, median,
minimum, maximum, range, sample standard deviation and sample variance across
windows. They do not compound or concatenate reset equity curves.

Without `--windows`, the requested period is one cycle: model and allocation
stay frozen for its entire duration, even beyond 14 days. The 14-day limit on
each trade's holding time still applies. No thresholds are optimized by this
command, and selected dates are not automatically an untouched holdout.

Data comes from the configured `data.dir` or `--data-dir`. The loader accepts
the standard Binance Parquet tree and flat `ASSETUSDT_30m.csv.gz` files with an
`open_time` column. It downloads missing 30m candles from Binance public spot
klines by default, caches them, and includes 60 days plus one candle of warm-up.
Use `--no-download-missing` for strictly offline use. Missing or invalid history
aborts the run; it does not drop pairs, fill gaps, or change the window.

Default capital is `cointegration.reference_capital` (10,000); `--cash` overrides
it. The pair universe and thresholds come from `cointegration.pairs`. Interval
is fixed at 30m. Relative periods end at the most recently completed candle.

Two cost models are available:

- `reference` (default): long fees from `fees.spot_maker`, normally 5 bps; short
  opening/closing fees from `fees.short_open`/`fees.short_close`, normally 10 bps.
- `market`: long fees from `fees.spot_taker`, normally 10 bps; same short fees.

Both fill each leg at the next candle open, with additive fees/slippage; neither
simulates limit-order fill probability. Remaining positions close at the final
candle close, and that close cannot generate a new entry. Slippage defaults to
2 bps per leg/fill. Set `backtest.pair_cost_model` and
`backtest.pair_slippage_bps` in YAML, or override with `--pair-cost-model` and
`--market-slippage-bps`. Fee flags are fractions, not bps.

The research execution model does not model funding, borrow, liquidation, venue
quantity rounding, live risk limits or fee headroom. It reuses fixed pair budgets
after losses. The `market` setting changes modeled fees, not live execution
eligibility. Annualized volatility and Sharpe use UTC daily returns, including
partial first/last days; short runs have very few observations. Non-overlapping
windows reset the account, but that does not prove their returns are statistically
independent.

Results go to a new timestamped directory under `backtest.results_dir`, or a new
directory specified with `--out`. Files include `report.html`, `summary.json`,
`windows.csv`, `equity.csv`, `trades.csv`, `weights.csv`, `pair_metrics.csv`,
`exposures.csv` and `signals.csv.gz`. Window runs put per-cycle CSVs in separate
`window_NNN` subdirectories. The HTML is standalone and includes equity graphs;
JSON includes effective costs, configuration, source paths and candle hashes.
`--no-save` suppresses reports but may still cache downloaded market data.

For an immediately reproducible offline example:

```sh
PYTHONPATH=src python -m tradebot backtest \
  --strategy cointegration-pairs \
  --start 2026-10-05T17:30:00Z --end 2026-10-06T17:30:00Z \
  --data-dir tests/fixtures/cointegration/recent_24h/data \
  --no-download-missing --pair-cost-model reference --cash 10000
```

This produces the archived net P&L of approximately +12.6452 USDT after reference
costs. Use the market cost model to assess the proposed market-order execution.

## Shared data and account runner

`MarketDataEngine` supports 1s, 15m and 30m independently. Thirty-minute-only assets
receive their own candle subscriptions, without one-second/book subscriptions.
The REST fetcher paginates at the requested interval; incomplete candles stay hidden
until the injected clock reaches their close. Missing histories are never filled
forward or removed from the universe to renormalize weights.

The pairs consumer retains 74 days plus one candle in memory: 60 days plus the
extra close needed for volatility, and 14 days for restart catch-up. The existing
disk-store retention remains separately configurable; older warmup can be restored
over REST. Offline shared replays use local 30m Parquet only and never download.

Use `config/cointegration-live.yaml` for the active cointegration-only profile, or
`config/cointegration-observe.yaml` for a non-mutating observation run. Set
`cycle_start` explicitly to the intended UTC half-hour boundary and always set
`cycle_end` for a bounded cycle.
The reference capital is a virtual observation budget, not permission to transfer
money from another strategy. The existing physical account still has its configured
MM/RXM allocations. The active profile disables market making and RXM and sets the MM allocation to
zero, leaving 100% of reconciled equity available to cointegration owners. No
automatic refit or compounding policy has been selected.

For an account with an existing shared runner, add `cointegration-pairs` to that
runner's roster and configure its cycle; do not start a second process on the same
account. It uses the same market producer and account lock. `PAUSE_PAIRS` pauses
its decisions. Observation state is stored at
`<state_dir>/cointegration-pairs/observation.db`, separate from physical ownership.
Unexecuted intents remain pending, including after the final cycle candle;
`finished` means the requested candle interval was consumed, not that orders filled.
Execution submits both market legs, requires confirmed fills, and compensates a
completed first leg if the second leg is rejected. Confirmed fills are written to
the shared owner ledger as well as the pair runtime.

## Strategy/pair/leg ownership

New physical account snapshots use version 2. The same `portfolio.db` transaction
persists the existing order/event journal and these tables:

| Table | Responsibility |
| --- | --- |
| `owner_accounts` | Strategy/pair capital, cash, fees, slippage and realized gross P&L where known |
| `owner_positions` | Strategy, pair, leg, symbol, long/short side, quantity, entry basis and collateral |
| `owner_reservations` | Cash reserved by intent, with its owner and instruction payload |
| `owner_fills` | Confirmed fill receipts, with content fingerprints for duplicate detection |
| `owner_migrations` | Reviewed legacy-import plan and fingerprint |

New-owner basis is executed price times quantity, excluding separately recorded
fees. Legacy spot basis already includes acquisition fees; migration preserves it.
Its historical gross realized P&L cannot be reconstructed from that aggregate, so
the new field is null; existing legacy accounting/history is retained.

MM and RXM keep compatibility views used by their existing execution/recovery code.
`ownership_migration.replace_legacy_records` publishes only those two owners in the
same transaction as their snapshot updates. Other strategies' positions and cash
are never rewritten into RXM. Account reconciliation includes all owners, and
owner-scoped read views expose only their allocated cash and positions.

`allocate_owner` performs an explicit accounting transfer from uncommitted RXM
capital. It is not called by observation startup. `record_owned_fill` records
externally confirmed fills without sending requests. Short covers require the
venue's realized P&L. Virtual owners retain their individual entry prices while
the exchange settles a pooled average entry. `short_settlement_adjustment` bridges
that difference for cash reconciliation; it is not added to a pair's trading P&L.

Closing one pair removes only its own quantity. RXM's existing `close_pct=100`
is translated to its owned quantity when another owner shares that short symbol.
Opposite long/short virtual positions remain separate records, with both net and
gross exposure available from `OwnershipLedger.mark`.

Step 4 still needs order submission, venue rounding, fee/collateral headroom checks,
whole-pair reservation against physical funds, outcome reconciliation and handling
for a failed second leg. The reference replay permits fee overdrafts of its virtual
pair budgets to reproduce the research; this is not a live spending authorization.

## Explicit legacy migration

Version-1 files remain readable and are not automatically migrated. A new strategy's
physical allocation requires version 2. The migration command always reads its
source database read-only and writes an entirely separate destination. Existing
destination files, stale plans and edited plans are refused.

From the project directory, with its environment activated:

```sh
PYTHONPATH=src python -m tradebot.engine.state.ownership_migration /path/to/portfolio.db \
  --shorts /path/to/reconciled-short-positions.json > ownership-plan.json

# After reviewing the plan, build a copy; this does not switch a running bot.
PYTHONPATH=src python -m tradebot.engine.state.ownership_migration /path/to/portfolio.db \
  --apply-plan ownership-plan.json --output /path/to/new-portfolio.db
```

The shorts file has the venue's `{"Positions": [...]}` structure. Its quantities
must match the legacy ledger; each open short needs `Pair`, `ShortQty`, `EntryPrice`
and `Collateral`. Omit it when flat. Snapshot fingerprints must still match at
copy time. Adopting the copy for a deployment is a separate operational step;
these commands send no exchange requests and do not change the source file.

## Validation

```sh
python -m pytest -q tests/test_cointegration.py tests/test_ownership.py tests/test_pairs_integration.py
python -m pytest -q
```

The two checksum-verified fixtures reproduce every archived portfolio equity mark,
fees, slippage, allocation, trade quantity/time/reason and net coin exposure. The
one-day case also compares every test-candle spread, rolling mean/std and z-score.
Expected net P&L on reference capital 10,000 is 12.645232157856 for `recent_24h` and
137.282139782812 for `fortnight_43`.

Additional tests exercise restart between leg fills, rollback of failed decision
commits, missing history, duplicate events, owner-only exits, opposite exposures,
unequal shared-short entry prices, legacy reservations/migration and observation
through the actual shared runner without order mutations. Fixture files are copied
unchanged from the developer handoff; their manifests retain source and expected
output checksums. The CLI uses the dedicated pairs backtester because the generic
weight rebalancer does not preserve fixed pair quantities.
