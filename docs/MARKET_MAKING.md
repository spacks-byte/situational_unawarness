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
A strategy's feature-source failure does not prevent another strategy from running.
Reconciliation findings produce durable advisory issues. They never refuse orders
or halt strategies; order sizing still respects owned resources and venue balances.

## Shared market data

`tradebot.data.engine.MarketDataEngine` is independent of both strategies. One
producer owns the public Binance WebSocket connection and REST bootstrap/repair.
For each MM instrument it receives best bid/ask updates and completed one-second
candles, then publishes a cached snapshot every second. RXM instruments get only
completed 15-minute candles (and its 50-day warmup): no one-second or book-ticker
streams are opened for coins that no consumer quotes. Overlapping instruments
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
`wss://stream.binance.com:9443/stream`), followed by `live.market_stream_fallback_urls`
(default the market-data-only `wss://data-stream.binance.vision/stream`), tried in
order whenever a connection cannot be established. REST uses `live.klines_url`.
TLS verification is always on; the handshake uses the `certifi` CA bundle so a bare
Windows/Python install does not need `SSL_CERT_FILE`. RXM never depends on the
stream: with it down, its closed 15-minute candles arrive through the REST repair
pass (every 30 s), well inside the bridge's 30-minute late-bar grace. These are
**Binance reference prices**. The execution coordinator still checks fresh Roostoo
bid/ask prices before submitting MM orders. The live profile refreshes MM orders
every 270 seconds; RXM's existing rebalance schedule is unchanged.

The MM quote policy uses a 30-second signal-decay constant, 30-second alpha EWMA
half-life, 300-second volatility EWMA half-life, a 330-second signal horizon,
and a 270-second quote refresh. Its volatility-spread and inventory-skew
coefficients are 100.0 and 30.0 respectively. The extra one-tick minimum is
disabled; outward tick rounding and the existing fee/spread filters remain
active. Sizing, `c2`, and inventory limits are unchanged.

Replay uses the same cache with an injected clock and local candle sources, without
threads or network. It delivers elapsed observations deterministically; it never
fabricates one-second quotes from RXM's 15-minute historical candles.

### Local copy of market data

Live runs keep every closed candle the producer receives under `live.market_store_dir`
(default `var/market`): one-second candles for the MM coins and RXM's 15-minute
candles, one file per coin per UTC day. Today's file is an append-only CSV; finished
days are compacted to Parquet (zstd) and days older than
`live.market_store_retention_days` (30) are deleted. A background thread does all
disk work, so a slow or failing disk never delays market data or orders. Below
`live.market_store_min_free_gb` (1 GB) free space the copy pauses (reported in
`status.json` under `market_data.store`) and resumes on its own; trading continues.

On start the cache is filled from disk first; REST then fetches only the gap since
the last stored bar (and the head of RXM's 50-day window beyond the retention).
A restart therefore needs a few REST calls instead of a full warmup download, and
starts with history even if Binance REST is unreachable. It costs no extra bandwidth
(the data is already received) and no Roostoo requests. Worst-case size is ~7 MB per
coin per day of one-second candles, under 0.7 GB for three MM coins at 30 days.
Replay and tests never write it.

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

On first initialization, the allocator assigns **90% of reconciled account equity
to MM and 10% to RXM**. This is a percentage of the current account value, not a
fixed $100,000 assumption. The live market-making profile assigns all MM capital to
PEPE. On a $100,000 account the budgets are therefore $90,000 for PEPE and $10,000
for RXM.

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
The current profile uses `market_making.capital.mm_fraction: 0.90`; an existing
`portfolio.db` initialized at 0.70 must be explicitly migrated because the
coordinator blocks rather than silently moving capital.

Every live order attempt is queued in the local `portfolio.db` outbox and
uploaded to Supabase when `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are present.
This includes `SUBMITTING`, `PENDING`, `FILLED`, `CANCELED`, `REJECTED`, and
`UNCERTAIN` lifecycle states. Active attempts have a null `resolved_at`; later
state changes upsert the same `(bot_id, intent_id)` row. Zero-fill rows are
telemetry only and never create accounting fills. Roostoo has no partial
executions; explicit corrections can replace previously uploaded phantom
executions.

The supplied table should be altered before enabling this in production so
strategy ownership is queryable and retries are idempotent:

```sql
alter table public.trade_transactions
  add column if not exists strategy text not null default 'unknown';

create unique index if not exists trade_transactions_bot_intent_uidx
on public.trade_transactions (bot_id, intent_id);
```

The application writes `strategy` as `mm-10m-fluctuation` or `rxm`. The
`bot_id` should identify the deployment/account, for example
`team87-competition-1`, rather than a generic value.

**MM is long-only.** Both short-open and short-close calls are denied for MM.
A bid requires its coin's cash, including the 5-bps fee reserve. An ask requires
MM-owned inventory and cannot exceed it. RXM cannot sell that inventory or spend
MM cash. RXM retains its existing strategy and short policy on its own allocation.

## Quote mechanics

The combined formula, EWMA half-lives (300s volatility, 30s alpha), volatility floor,
strict **greater-than-10-bps** round-trip filter, and positive return after spot
fees are ported from the selected PoC. Each coin starts with a fixed base lot worth
5% of its budget at the startup anchor; the inventory cap is 40% of its budget.

The strategy consumes 3,600 consecutive completed Binance one-second candles for
warmup and applies one additional second of feature lag. At decision second `t`,
the formula uses observations through `t-2`; the last completed candle at `t-1`
sets current capacity and the startup anchor. Observed empty candles are valid;
missing seconds are never interpolated. Gaps reset the warmup requirement while
preserving the original anchor and lot. Stale or incomplete data produces no quotes.
Live candles arrive a little after their second ends, so the quote bridge decides at
"last complete second + 1 s" when that is at most `max_data_delay_seconds` (5 s)
behind the wall clock; with fresh data (always in replay) it decides at the wall clock.

For midpoint experiments, set `market_making.reference_source: midpoint`. The
quote bridge then reads the latest Binance best bid/ask snapshot after fetching
candles and uses `(bid + ask) / 2` for quote prices and inventory capacity.
Volatility, alpha, startup anchor and fixed lot remain candle-based. Book freshness
is measured against wall time even when candle features are delayed. Missing,
crossed, locked, future-dated or stale books suppress quotes; there is no candle
fallback. `max_book_age_seconds` defaults to 2 seconds. Candle-only replays retain
the default `reference_source: candle_close`; midpoint historical replays require
recorded best bid/ask observations and cannot recover them from candle OHLC.

`market_making.enforce_one_tick_distance` defaults to `true`. Set it to `false`
to remove the full-tick minimum distance from the reference while retaining the
2-bps minimum distance, outward tick rounding and existing fee/spread filters.
This option does not change the backtest fill-penetration assumption. These are
opt-in experiment settings; the deployed configuration has not been switched.

Every 180 seconds the live engine cancels MM-owned limits by ID, reconciles final
fills, then calculates replacements. The live profile does not require a one-tick
quote distance from the reference price, although exchange tick snapping and the
minimum fee-covering spread checks still apply. It reserves both sides before
submission, without
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
journaled. Submission intents commit before the network call. A cancel ACK never
frees inventory or cash by itself: the final order state must confirm cancellation
or a fill.

## Reconciliation and recovery

Every sync reads pending orders, wallet, short positions and tickers, applies fills,
and compares the ledger with the venue. Separate venue reads can briefly disagree
during a fill, so a sync with findings reads once more before recording an issue.
`engine/state/issues.py` persists its scope, evidence, occurrence count and resolution.
Issues are advisory at every age and severity. After two clean observations an issue
is resolved; its history remains in state/events. Old `restrictions` migrate into
issues and cannot gate orders after restart.

A failed account read reports `DEGRADED` and retries. User pause/kill controls,
configuration validation, ownership/balance checks, and prevention of duplicate
uncertain submissions remain independent of reconciliation issues.

**Execution evidence.** PENDING reserves the entire accepted quantity. CANCELED and
REJECTED execute nothing. FILLED requires the complete accepted quantity and a
positive execution price. FilledQuantity on a canceled order is sometimes a placeholder;
it is never combined with the limit price to invent a trade. CANCELLED is accepted as
an input spelling alias of CANCELED. Contradictory evidence is reported without
changing confirmed accounting or preventing other valid operations.

**Cash tolerance.** `max(cash_tolerance_floor_usd, cash_tolerance_bps x fill notional)`,
default `max($0.05, 3 bps)`. 3 bps is deliberately below the smallest fee (the 5 bps
maker fee): a doubled or missing fee is never absorbed as rounding, while sub-cent
commission rounding always is. The configurable range is 0-30 bps. A gap beyond it
is traced to the strategy whose fills caused it and keeps that attribution while it
persists; with no traceable owner it reports `cash:account`.

**Lost spot responses.** The intent stays `SUBMITTING` with its reservation; only a duplicate
submission of that unresolved operation is deferred. Each sync searches the venue's order history (paged; completeness
never assumes the page order) for exactly one unowned order with the same pair, side,
price, quantity and a creation time within `evidence_window_seconds` (30 s). One match
is adopted. Several matches stay unresolved. No match in a provably complete history
more than two windows later means the order never executed and the intent is
rejected. Nothing is ever resubmitted blindly.

**Lost short responses.** Roostoo has no client order IDs, so the evidence is the
short position against `before_short_qty` and resting SHORT_OPEN rows. A position move
within 2% of the request is booked (a recovered close re-baselines RXM's residual
cash, since its P&L and fee were only in the lost reply); one unowned resting row
is adopted for a limit open; no move after two windows means not executed.

**Cancels.** A cancel marks the order `CANCELING`. Its reservation is kept and no
replacement is quoted on that coin/side until the venue shows a terminal state. An
unacknowledged or unsettled cancel is re-sent after `cancel_retry_seconds` (30 s).

**Restarts.** Intents and their states are durable, so a restart mid-submit or
mid-cancel resumes exactly the recovery above.

## Request budget

All Roostoo requests share one limiter at 25 requests/minute (venue limit 30); the
cap is never raised to absorb contention. Each loop runs RXM before MM, so an RXM
rebalance never queues behind an MM refresh in the same loop. Measured costs
(`tests/test_request_budget.py`):

| Operation | Requests |
| --- | --- |
| account sync | 4 (+1 per order that left the book) |
| MM quote submit / cancel | 2 / 2 |
| RXM spot order / cancel | 1 / 3 (previously 5 / 6) |

`status.json` reports `http_by_strategy`, `http_last_minute`, and
`account.quote_stats` (submitted quotes, quote age, quotes dropped at their deadline).

API shapes and precision rules are based on the [Roostoo API documentation](https://github.com/roostoo/Roostoo-API-Documents).
The candle source uses Binance's documented [one-second klines](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market).

## Run and stop

Run from the repository root after installing `.[dev]`. Stop the standalone RXM
process before starting the shared coordinator. Both updated runners take the same
exclusive OS lock per Roostoo account, independent of the working directory:
`%LOCALAPPDATA%\tradebot\locks\account-<id>.lock` on Windows,
`$XDG_STATE_HOME/tradebot/locks/` (default `~/.local/state/tradebot/locks/`) elsewhere,
or `$TRADEBOT_LOCK_DIR`. `<id>` is a hash of the API URL and key; no credential is
written. `<lock>.holder.json` names the holding pid, host and state directory. The
OS releases the lock when the process dies. `market_making.account_lock` overrides
the path. An older already-running binary does not take this lock and must be
stopped explicitly. All exchange requests share one 25-calls/minute limiter.

## Preflight and migration

Both commands are read-only: the venue is read through a port that refuses every
order, cancel and short call, SQLite files are opened `mode=ro`, and the lock is
probed without being taken.

```bash
python -m tradebot --config config/market-making.yaml account preflight   # exit 0 = ready
python -m tradebot --config config/market-making.yaml account explain     # no network
```

`preflight` reports equity, the 90/10 split, the MM funding shortfall, spot and
short positions (attributed to RXM at takeover), resting orders that are in neither
the RXM journal nor `portfolio.db`, a held account lock, an active standalone RXM
runner (recent `status.json` in `rxm_state_dir`), and an existing ledger whose
allocation differs from the config. `explain` shows the active advisory issues with
reasons, intents still awaiting the venue, rounding adjustments, quote stats and
the recent recovery events.

Migration from the standalone RXM runner (operator steps; nothing is automatic):

1. Run `account preflight` while the old runner is still trading. Note blockers.
2. Resolve any shortfall or unknown order **by hand**. Preflight never sells,
   adopts or cancels anything, and the coordinator will not either.
3. Stop the old runner and wait for its loop to end. Keep its state directory.
4. Run `account preflight` again: it must exit 0 (no lock holder, legacy runner idle).
5. Start a dry run with a new state directory and check `status.json`.
6. Start execution with its own new state directory; never reuse dry-run state and
   never delete `portfolio.db`. To roll back, stop the coordinator and restart the
   old runner on its untouched state directory; positions stay where they are.

If the existing standalone RXM book should be fully closed before migration, use
the execution-only operator script below with the old runner stopped. It cancels
resting orders, closes shorts, sells all non-USD spot balances at market, and
reconciles for a bounded number of rounds. It requires
`ROOSTOO_CONFIRM_LIVE=YES`, acquires the account lock, and returns failure unless
the account has no open orders, shorts, or non-USD spot balances:

```bash
ROOSTOO_CONFIRM_LIVE=YES python scripts/liquidate_account.py \
  --config config/market-making.yaml \
  --state-dir var/live_comp
```

This script does not alter `var/live_comp` and does not start the replacement
strategy. Review the exchange fills and run `account preflight` after it
finishes; liquidation is a prerequisite, not a substitute for reconciliation.

Read-only dry run (including blocked order and cancellation transport):

```bash
PYTHONPATH=src python3 -m tradebot --config config/market-making.yaml live \
  --strategies mm-10m-fluctuation --state-dir var/shared-dry-run --max-loops 3
```

Selection examples (use the same account state directory when changing the active roster):

```bash
# RXM only, on its allocated 10%.
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
The allocator is configured by `market_making.capital.mm_fraction: 0.90`; RXM gets
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

## Account remediation (2026-10-07)

- `python -m pytest -q` on Windows: all tests pass, including portable account
  locking (`tests/test_locking.py`), venue-shaped failure injection
  (`tests/test_venue_failures.py`: P1 docs-shaped pending rows, P2 rounding sweep,
  P3 lost short open/close, P4 delayed and unacknowledged cancels, restarts
  mid-submit/mid-cancel, history paging order, strategy independence), request
  budget (`tests/test_request_budget.py`), stream fallback/TLS/REST-only RXM
  (`tests/test_market_engine.py`) and read-only preflight (`tests/test_preflight.py`).
- Read-only public-data smoke test (no keys, no Roostoo): `stream.binance.com` returned
  HTTP 451 (restricted location) from this host; the fallback `data-stream.binance.vision`
  connected with verified TLS (certifi). The local store wrote 410 candles in 12 s and a
  restart loaded 299 one-second and 96 fifteen-minute rows from disk.
- Live one-second candles can arrive after the MM refresh reads the cache (0.05-0.5 s
  in one measurement, 2-3 s earlier the same day). MM now decides at "last complete
  second + 1 s" when that is at most `max_data_delay_seconds` (5 s) behind; with the
  old wall-clock rule a read at +0.05 s into a second quoted 0 of 15 times, with the
  new one 15 of 15 (docs/REVIEW.md section 7).
- Venue behaviour is still inferred from documentation and the simulator. The real
  order protocol in `docs/ACCOUNT_VALIDATION.md` must be run on a separate test account
  before deployment.
