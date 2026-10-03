# Roostoo Execution Engine Design

## Scope and intent

The engine is intentionally split from network access (see `docs/ARCHITECTURE.md` for the whole system):

1. `tradebot.exchange.client.RoostooClient` is the single source of truth for network access and request signing.
2. The execution engine (`tradebot.engine`) translates a strategy target portfolio into minimal, safe, auditable exchange actions through the `ExchangePort` interface.

The engine is not a signal generator. It consumes `TargetPortfolio` messages from a strategy module and reconciles them against actual wallet, short, and pending-order state before sending orders.

## High-level architecture

- Strategy layer: emits typed target allocations and optional flatten instructions.
- Intake layer: accepts strategy messages via in-process call. (A file/queue watcher is planned, not implemented.)
- Engine core: validates inputs, diff against actual state, and computes the minimal action set.
- Risk layer: applies position caps, circuit breakers, stale-data guards, and execution bans before orders go out.
- Port layer: abstracts the exchange calls and keeps live/backtest behavior behind a common interface.
- Persistence layer: SQLite stores intents and signal receipts; a JSONL audit log stores decisions. (Fills, pending shorts and equity snapshots are planned, not implemented.)
- Analytics layer: `tradebot.core.metrics`, shared with the backtester (Sharpe, Sortino, Calmar, drawdown, return).

## State machine

### Intent lifecycle

- `PENDING_SEND`: intent has been created locally but not yet sent to the exchange.
- `SENT`: the exchange call was made and the response was received.
- `RESOLVED`: the outcome is confirmed against actual state.
- `UNCERTAIN`: the call returned `None`, timed out, or an inconsistent result requires manual or automated reconciliation.
- `CANCELLED`: the pending order was cancelled and confirmed.

### Pending short lifecycle

- `PLACED`: a limit short was accepted and its order ID was stored locally.
- `PENDING`: the short is waiting for the market to hit the entry trigger.
- `FILLED`: the short appears in open short positions or grows an existing position; pending-count may drop.
- `CANCELLED`: the order was cancelled and the collateral + fee release was confirmed.
- `STALE`: the pending short no longer matches the target and was cancelled due to TTL or target mismatch.

## Clock seam

All time-dependent behavior must use a `Clock` protocol instead of direct calls to `time.sleep`, `time.time`, `time.monotonic`, or `datetime.now`.

- `RealClock`: production clock and real timeouts.
- `SimClock`: deterministic, advanceable clock for tests and backtests.

This keeps backtest and live mode logic identical while allowing the simulator to advance time without waiting.

## Verified Roostoo contract

The Roostoo API documentation establishes these behaviors (the docs submodule was never committed, so this list is the team's record):

- Pending spot and short orders are identified through `query_order(pending_only=True)`.
- A pending short returns `Status: PENDING` and its `ID` is the order ID used by `cancel_order`.
- A filled market short returns `Status: OPEN`; open positions use the same position ID.
- Limit short orders remain pending until the market reaches the requested price.
- `MiniOrder` is a minimum USD notional: `Price * Quantity` must exceed it.
- Short opens charge `0.1%` of collateral immediately, including pending limit opens.
- Short closes fill immediately at the current best ask and charge `0.1%` of close value.
- Free USD excludes locked short collateral; normalized equity adds collateral and unrealized P&L back.
- `/v3/balance` returns the wallet under `SpotWallet` (verified against the mock exchange, Oct 2026).
- Opening on an existing short pair merges quantity and collateral into the weighted-average position.

## Design principles

- The engine treats the target as the desired end state and diffs it against real state.
- The system is idempotent per `signal_id` and records all actions in an audit trail.
- Unknown exchange outcomes are never retried blindly; they are resolved by comparing snapshots before and after the call.
- The engine depends only on the common port interface and not on direct HTTP or HMAC code.
- The backtest path is isolated and must never touch live credentials, live trade logs, or the live DB.
