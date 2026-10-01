# Roostoo Autonomous Order Execution Engine

This repository contains a production-oriented order execution engine for Roostoo strategies. It accepts a desired `TargetPortfolio`, compares it with normalized account state, applies risk controls, and sends only the required exchange actions.

## Architecture

```text
Strategy(snapshot)
    -> TargetPortfolio validation
    -> EngineRuntime
    -> pending-order cleanup
    -> stop-loss/take-profit monitoring
    -> reconciliation plan
    -> risk gate
    -> child-order slicing and routing
    -> ExchangePort
    -> intent journal and audit log
```

The engine never contains HTTP, HMAC, or endpoint logic. Those concerns stay behind `LibraryExchangePort`. The same engine path works with `MockExchangePort` or a teammate's simulator implementing the same `ExchangePort` contract.

## Main Components

- `src/engine/app.py`: public `Engine` facade.
- `src/engine/runtime.py`: clock-driven strategy loop.
- `src/engine/schema/models.py`: validated strategy target contract.
- `src/engine/state/snapshot.py`: common live/simulator state normalization.
- `src/engine/reconcile/plan.py`: desired-state diffing and close-before-open planning.
- `src/engine/reconcile/pending.py`: stale pending-order cancellation.
- `src/engine/risk/manager.py`: exposure, cash, fee, loss, drawdown, and kill-switch checks.
- `src/engine/execution/runner.py`: idempotent order execution, urgency routing, and child slicing.
- `src/engine/monitor/position_monitor.py`: stop-loss and take-profit alerts.
- `src/engine/state/intent_store.py`: SQLite signal and intent persistence.
- `src/engine/state/audit_log.py`: append-only JSONL execution audit.
- `src/engine/analytics/metrics.py`: return and drawdown metrics.
- `src/engine/ports/mock_port.py`: deterministic simulator.
- `src/engine/ports/library_port.py`: live Roostoo adapter.

## Strategy Contract

A strategy receives one normalized snapshot and returns a `TargetPortfolio`:

```python
from datetime import UTC, datetime

from src.engine.schema.models import LongTarget, TargetPortfolio


def strategy(snapshot):
    return TargetPortfolio(
        strategy_id="example",
        strategy_version="v1",
        signal_id="example-001",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", weight=0.25)],
    )
```

A snapshot contains `cash_usd`, `longs`, `shorts`, `pending_orders`, `prices`, `entry_prices`, and `equity_usd`. Symbols are normalized to uppercase. Timestamps must be timezone-aware. Duplicate targets on one side are rejected.

### Strategy integration

The strategy is a pure callback. It receives state and returns intent; it never calls an exchange method directly.

```python
from datetime import UTC, datetime

from src.engine.schema.models import LongTarget, TargetPortfolio


def strategy(snapshot):
    btc_price = snapshot["prices"].get("BTC", 0.0)
    if btc_price <= 0:
        return TargetPortfolio(
            strategy_id="my-strategy",
            strategy_version="v1",
            signal_id="flat-no-price",
            timestamp=datetime.now(UTC),
        )

    return TargetPortfolio(
        strategy_id="my-strategy",
        strategy_version="v1",
        signal_id=f"my-strategy-{snapshot['equity_usd']:.2f}",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", weight=0.25)],
        reason="Maintain 25% BTC allocation",
    )
```

The target is a desired end state, not an order instruction. For example, `weight=0.25` means the engine calculates the required USD notional from current equity. The engine handles the difference, order sizing, risk checks, idempotency, and execution.

## Simulator Usage

```python
from src.engine.app import Engine
from src.engine.config import ExecutionConfig
from src.engine.ports.mock_port import MockExchangePort

engine = Engine(
    MockExchangePort(initial_wallet={"USD": 50_000.0}),
    config=ExecutionConfig(dry_run=False),
    state_path="backtest_state.db",
    audit_path="backtest_audit.jsonl",
)

results = engine.run(strategy, max_iterations=100)
engine.close()
```

The simulator models market fills, pending limit shorts, short fees, merged shorts, partial short closes, wallet balances, and clock-driven timestamps.

### Connecting a separate backtesting framework

The backtesting framework should implement `ExchangePort`, or expose a small adapter that implements it. The strategy and `Engine` remain unchanged:

```python
from src.engine.app import Engine
from src.engine.config import ExecutionConfig

from teammate_backtest import BacktestExchangePort

port = BacktestExchangePort(
    initial_cash_usd=50_000.0,
    clock=sim_clock,
)
engine = Engine(
    port,
    config=ExecutionConfig(dry_run=False),
    state_path="backtest_state.db",
    audit_path="backtest_audit.jsonl",
    clock=sim_clock,
)

results = engine.run(strategy, max_iterations=1000)
```

The adapter must translate simulator state into the same raw response shapes expected by the port methods. Important behaviors to model are market fills, pending limit orders, cancellation, short collateral and fees, merged shorts, partial closes, and deterministic `SimClock` timestamps.

The strongest compatibility test is to run the same strategy with the same initial state, prices, and `SimClock` against `MockExchangePort` and the teammate adapter, then compare normalized snapshots, plans, operation order, final exposure, and equity curves.

## Live Usage

```python
from src.engine.app import Engine
from src.engine.config import ExecutionConfig
from src.engine.ports.library_port import LibraryExchangePort

engine = Engine(
    LibraryExchangePort(),
    config=ExecutionConfig(
        dry_run=False,
        live_mode=True,
        max_order_value_usd=10.0,
    ),
)
```

Live execution requires both `dry_run=False` and `live_mode=True`. Before enabling it:

1. Rotate any credentials exposed in chat or committed files. `.gitignore` prevents future accidental commits; it does not invalidate a key that was already exposed. Create a new key in Roostoo, replace the values in `.env`, and revoke the old key.
2. Run the full test suite.
3. Run read-only exchange metadata, balance, and ticker checks.
4. Use a separate live SQLite state file and audit log.
5. Start with one symbol and a very small order limit.

Credentials belong in `crypto-roostoo-api/.env`, copied from `.env.example`. Never commit `.env`.

## Execution Safety

- Rebalances close reductions before opening additions.
- Pending orders count toward desired exposure and are not duplicated.
- Opening deltas inside `no_trade_band_pct` are ignored.
- Large deltas are split by `max_child_order_pct`.
- High-urgency orders use market routing; normal and low urgency may use limits.
- Short opens and closes include documented Roostoo fee semantics.
- Duplicate `signal_id` submissions are no-ops.
- Risk-rejected signals can be retried after conditions change.
- Unknown outcomes become `UNCERTAIN` and are resolved from before/after snapshots without blind retries.
- Kill switches, daily loss, drawdown, cash reserve, exposure, and short-collateral limits are enforced before dispatch.

## Persistence and Audit

The intent journal tracks `PENDING_SEND`, `SENT`, `RESOLVED`, and `UNCERTAIN` states. Signal receipts are persisted separately so even zero-order signals cannot execute twice. Each configured engine writes structured UTC JSONL audit events for risk decisions, operations, duplicates, and final outcomes.

## Simulator Contract

A teammate's simulator must implement the `ExchangePort` methods used by the engine:

```text
get_exchange_info
get_ticker
get_balance
place_order
cancel_order
query_order
list_open_orders
open_short
close_short
get_short_positions
get_pending_count
get_server_time
```

Use the same contract tests against both `MockExchangePort` and the teammate's adapter. Given the same strategy, initial state, prices, and `SimClock`, both implementations should produce the same normalized snapshots, plans, operation ordering, and final exposure.

## Verification

Run all tests from the repository root:

```text
.venv/bin/python -m pytest -q
```

Detailed design notes are in `docs/DESIGN.md`. Practical examples are in `docs/USAGE.md`.
