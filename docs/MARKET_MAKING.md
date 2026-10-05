# Independent strategies in one account engine

`config/market-making.yaml` enables one account engine with independently selectable strategies:
RXM produces target weights through the existing rebalance engine, and
`mm-10m-fluctuation` produces `QuoteBatch` objects through the engine's dedicated
quote executor. The default standalone RXM profile remains available.

`live.strategies` is the explicit roster. The profile selects both independent
strategies by default; `--strategies` can select either one. An inactive strategy
is not instantiated and requires no feature data. Its allocated capital stays in
its own book; selection never transfers money or liquidates its holdings. Existing
resting orders remain owned and reconciled even when their strategy is inactive.
A strategy's feature-source failure does not prevent another strategy from running;
account reconciliation failures block new risk across the account.

## Shared market data

`tradebot.data.engine.MarketDataEngine` is independent of both strategies. One
producer owns the public Binance WebSocket connection and REST bootstrap/repair.
For each active instrument it receives best bid/ask updates and completed one-second
candles, then publishes a cached snapshot every second. RXM also subscribes to
completed 15-minute candles and retains its 50-day warmup. Overlapping instruments
share one subscription. MM-only startup does not load RXM's history or runtime;
RXM-only startup does not load MM's history or runtime.

Streaming, one-second publication, and history repair run separately from order
execution. A Roostoo rate-limit wait or an idle strategy does not stop data updates.
Strategies read copies of cached completed candles; their reads never initiate
live network requests. Missing candles are repaired from REST without interpolation.
Closed events received slightly ahead of local time stay hidden until the interval
closes on the local clock. Snapshots carry timestamps and separate candle/BBO staleness flags; `status.json`
includes connection, gap and source-error status. A disconnected feed reconnects
and repairs history. Stale observations cannot create MM quotes.

The stream endpoint is configurable with `live.market_stream_url` (default
`wss://stream.binance.com:9443/stream`); REST uses `live.klines_url`. These are
**Binance reference prices**. The execution coordinator still checks fresh Roostoo
bid/ask prices before submitting MM orders. The one-second data cadence does not
change MM's 600-second order refresh or RXM's existing rebalance schedule.

Replay uses the same cache with an injected clock and local candle sources, without
threads or network. It delivers elapsed observations deterministically; it never
fabricates one-second quotes from RXM's 15-minute historical candles.

The live stream follows Binance's [documented kline and book-ticker streams](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-streams/~).

## Deferred: strategy-neutral position ownership

**Flagged for a future build; do not implement in this change.** The current
`AccountCoordinator` hardcodes MM and RXM books. That is a temporary implementation
limitation, not the intended ownership model: additional strategies will be added,
and position ownership must not be restricted to these two names.

The future position ledger must support these fields:

| Field | Meaning |
| --- | --- |
| `strategy` | Strategy that owns the position. |
| `symbol` | Instrument held by that strategy. |
| `position` | Position quantity. |
| `price` | Price recorded for the position. |

Capital allocation, reservations and account reconciliation must operate across
strategy-owned records rather than assume that all remaining positions belong to
RXM. The precise meaning of `price`, accounting extensions and migration of current
books should be specified when that future build is authorized. The existing
coordinator's MM/RXM-specific docstring describes only its current implementation.

## Capital and positions

On first initialization, the allocator assigns **70% of reconciled account equity
to MM and 30% to RXM**. This is a percentage of the current account value, not a
fixed $100,000 assumption. Within MM, PEPE receives 85%, BONK 7.5%, and 1000CHEEMS
7.5%. On a $100,000 account the budgets are therefore $59,500, $5,250, $5,250,
and $30,000 for RXM.

The total MM allocation must be available as unreserved USD. Existing identifiable
RXM positions and orders remain RXM-owned. If those holdings leave insufficient
cash for MM, initialization reports the shortfall and sends no orders; it never
liquidates positions to fund an allocation. Unknown pending orders prevent startup.
The original RXM journal identifies imported orders. Set `market_making.rxm_state_dir`
to the existing RXM state directory (default `var/live_comp`).

Budgets, anchors, fixed lots, EWMA cursors, quote deadlines, and ownership persist
in `portfolio.db`. Restarts do not redistribute capital or infer MM ownership from
the aggregate wallet. Each coin keeps its P&L. Changing the top-level capital
fraction after initialization requires a separate accounting migration; changing
the file does not transfer money. There are no live 14-day resets or terminal
liquidations. The RXM start-equity and lock-equity baselines are scaled on migration
so the capital transfer does not manufacture a return or trigger a lock-in.

**MM is long-only.** Both short-open and short-close calls are denied for MM.
A bid requires its coin's cash, including the 5-bps fee reserve. An ask requires
MM-owned inventory and cannot exceed it. RXM cannot sell that inventory or spend
MM cash. RXM retains its existing strategy and short policy on its own allocation.

## Quote mechanics

The combined formula, EWMA half-lives (300s volatility, 30s alpha), volatility floor,
strict **greater-than-10-bps** round-trip filter, and positive return after spot
fees are ported from the selected PoC. Each coin starts with a fixed base lot worth
5% of its budget at the startup anchor; the inventory cap is 70% of its budget.

The strategy consumes 3,600 consecutive completed Binance one-second candles for
warmup and applies one additional second of feature lag. At decision second `t`,
the formula uses observations through `t-2`; the last completed candle at `t-1`
sets current capacity and the startup anchor. Observed empty candles are valid;
missing seconds are never interpolated. Gaps reset the warmup requirement while
preserving the original anchor and lot. Stale or incomplete data produces no quotes.

Every 600 seconds the engine cancels MM-owned limits by ID, reconciles final fills,
then calculates replacements. It reserves both sides before submission, without
using anticipated sale proceeds. Prices and sizes stay fixed between refreshes;
a buy filling mid-interval does not create an immediate sell. MM bypasses RXM's
repeg and escalation paths. Before submission a fresh venue bid/ask check skips
marketable quotes. Expired quotes are also skipped after a rate-limit wait.

Roostoo metadata determines price precision, amount precision and minimum order
value. Prices are serialized as decimal tick values to avoid an extra float-floor
shift. The account guard allows noncrossing two-sided quotes, but rejects any new
order that could cross an existing opposing order owned by either strategy.
Existing orders win that conflict. Market short closes are conservatively blocked
when an account-owned opposing limit exists for that coin.

Order IDs, cumulative fills, USD commissions, cost basis and reservations are
journaled. Submission intents commit before the network call. A lost spot response
can be recovered only if history uniquely identifies the submitted order; otherwise
new risk stays blocked. Ambiguous short mutations require operator reconciliation.
A cancel ACK never frees inventory or cash by itself: the final order state must
confirm cancellation or a fill. Ambiguous pending fill quantities, non-USD spot
commissions, unknown orders, unexplained balance changes and invalid state block
trading rather than guessing. Separate venue reads can briefly disagree during a
fill; the coordinator retries those reads up to three times, without sending orders.

API shapes and precision rules are based on the [Roostoo API documentation](https://github.com/roostoo/Roostoo-API-Documents).
The candle source uses Binance's documented [one-second klines](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market).

## Run and stop

Run from the repository root after installing `.[dev]`. Stop the standalone RXM
process before starting the shared coordinator. Both updated runners use the same
exclusive account lock, `var/roostoo-account.lock`; an older already-running binary
must be stopped explicitly. All exchange requests share one 25-calls/minute limiter.

Read-only dry run (including blocked order and cancellation transport):

```bash
PYTHONPATH=src python3 -m tradebot --config config/market-making.yaml live \
  --strategies mm-10m-fluctuation --state-dir var/shared-dry-run --max-loops 3
```

Selection examples (use the same account state directory when changing the active roster):

```bash
# RXM only, on its allocated 30%.
PYTHONPATH=src python3 -m tradebot --config config/market-making.yaml live --strategies rxm
# Both independently, on their respective allocations.
PYTHONPATH=src python3 -m tradebot --config config/market-making.yaml live --strategies mm-10m-fluctuation,rxm
```

For a subsequent operator-controlled launch, use a separate execution state directory:

```bash
ROOSTOO_CONFIRM_LIVE=YES PYTHONPATH=src python3 -m tradebot \
  --config config/market-making.yaml live --live --state-dir var/shared-live
```

No live launch is performed by implementing or testing this change. Dry-run state
cannot be reused for execution. Preserve the execution state directory on restart.
The allocator is configured by `market_making.capital.mm_fraction: 0.70`; RXM gets
the remainder. There is no required dollar-budget CLI flag.

Control files inside the shared state directory:

| File | Behavior |
| --- | --- |
| `PAUSE` | Pause both strategies' new orders; existing orders can still fill. |
| `PAUSE_MM` | Pause MM new orders. |
| `PAUSE_RXM` | Pause RXM new orders. |
| `STOP_MM` | Cancel MM orders only; retain MM holdings and RXM orders. |

The configured `live.kill_file` also pauses both strategies. Remove the relevant
file to resume. SIGINT/SIGTERM stop the coordinator between loops and preserve state;
use `STOP_MM` and wait for confirmed cancellations before stopping if cancellation
is desired. Never delete `portfolio.db` to resolve a live reconciliation mismatch.

`status.json` reports allocated capital, free/reserved cash, per-coin inventory,
fees, realized/unrealized P&L, gross/net exposure, and reconciliation status. RXM's
existing reports remain under `rxm/`. SQLite `events` contains decisions, fills,
reservations and recovery events; `completed_orders` preserves finished order
ownership without growing the active state blob indefinitely. RXM realized/unrealized
P&L for imported holdings uses the bootstrap market value as its cost basis.

## Replay and verification

This is a **Roostoo-mechanics simulation with an explicit historical execution-price
proxy**, not an exact replay of Roostoo fills. Historical Roostoo best bids/asks are
unavailable. MM replay uses trade-containing Binance one-second candle touches
with zero penetration probability; other RXM coins retain 15-minute through fills.
RXM symbols overlapping MM use the same one-second physical exchange data as MM.
Live timing, shared-account crossing conflicts, and venue precision can change
fills relative to independent PoC runs.

With the verified PoC one-second cache and local RXM 15-minute data:

```bash
PYTHONPATH=src python3 -m tradebot --config config/market-making.yaml replay \
  --start 2026-09-21T00:00:00Z --days 14 --cash 100000 \
  --mm-cache ../shared-backtest/data --out results/shared-14d

# Longer matched window; requires the same data coverage plus RXM warmup.
PYTHONPATH=src python3 -m tradebot --config config/market-making.yaml replay \
  --start 2026-09-07T00:00:00Z --days 28 --cash 100000 \
  --mm-cache ../shared-backtest/data --out results/shared-28d
```

Add `--strategies mm-10m-fluctuation` to replay MM alone without loading RXM's
15-minute history, or `--strategies rxm` to replay RXM alone without MM's one-second
history. The inactive strategy's allocation stays idle.

Each replay needs a new output directory; a persisted ledger cannot be paired with
a freshly reset simulated exchange. Restart tests instead retain the simulated
exchange while restarting the actual coordinator. Replay writes `summary.json`,
physical `fills.csv`, `ledger-fills.csv` with strategy ownership and fee deltas,
`ledger-events.csv`, and `orders.csv`. Valuation retains open positions and pending
reservations; it does not add a fictitious terminal sale or liquidation fee.

The reproducible validation script can derive RXM 15-minute OHLCV from the same
verified one-second cache before running the actual coordinator:

```bash
PYTHONPATH=src python3 scripts/validate_account_integration.py \
  --cache ../shared-backtest/data --out results/shared-validation

PYTHONPATH=src python3 scripts/validate_mm_policy.py \
  --poc ../shared-backtest --out results/mm-policy-parity

PYTHONPATH=src python3 scripts/validate_shared_exports.py results/shared-validation/replay

python3 -m pytest -q
```

The policy comparison runs the native PoC for a full day per coin, feeding the same
pre-decision cash/inventory into the Python strategy and comparing quote prices and
sizes at all refreshes. It isolates decision parity from exchange transport timing.
The checked-in native golden vectors also cover tiny prices, coarse ticks, skew and
extreme inputs without requiring the sibling repository in CI.

## Recorded validation (2026-10-06)

- `python3 -m pytest -q`: **225 passed, 3 skipped**. The skips are existing RXM
  parity tests requiring parquet files in the default `data/` directory.
- Native PoC policy parity: **432 matching refresh decisions**, 144 per coin,
  using identical starting inventory/cash, warmup and lag. Price tolerance is
  `1e-16`; base-quantity tolerance is `1e-6`.
- Actual account/strategy engines, cached 2026-09-21 through 2026-10-05:
  **18,327 successful loops and 8,250 physical fills**, with both strategies active.
  Peak Roostoo transport usage was **25 requests/minute**. RXM's normal risk guards
  still reject individual plans when their existing limits are exceeded.
- Independent one-day runs: MM-only **559 fills**, RXM-only **15 fills**. Each uses
  its allocated book and requires only its own historical feature data.
- Export reconciliation passed for all three runs: MM cash, quantities and fees
  reconstructed from fills match ending ledgers; realized plus unrealized P&L
  equals net P&L; MM short fees are zero.
- MM-only 28-day replay, 2026-09-07 through 2026-10-05: **36,372 successful
  loops and 16,383 fills**, with exported cash, quantities and fees reconciled.
  This run includes the near-flat inventory roundoff fix: an additional discrepancy
  allowance of at most `1e-8` USD prevents negligible floating-point residue from
  falsely blocking reconciliation. It does not rewrite balances or positions;
  regression tests still reject a genuine one-PEPE discrepancy and invalid prices.
- A read-only public WebSocket smoke test received fresh PEPE best bid/ask and
  completed one-second candles. This host required its system CA store via
  `SSL_CERT_FILE=/etc/ssl/cert.pem`; TLS verification remained enabled.
- Streaming tests cover completed-candle causality, immutable consumer views,
  missing data, stale BBOs, reconnect/repair, and one-second publication while
  history repair is blocked. Quote tests include rate-limit waits across expiry.

The historical tests validate mechanics and policy parity, not future returns or
an exact match to live Roostoo execution. No live orders were sent.
