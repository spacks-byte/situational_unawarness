# Live execution engine

The engine (`src/tradebot/engine/`) accepts a desired `TargetPortfolio`, compares it with the normalized account state, applies risk controls, and sends only the exchange actions needed to get there. It never contains HTTP, HMAC or endpoint code. Exchange access goes through the `ExchangePort` interface (`tradebot.exchange.port`).

```text
strategy(snapshot) -> TargetPortfolio
    -> EngineRuntime.run_once
        -> cancel stale pending orders (older than fill_timeout_seconds)
        -> read + normalize exchange snapshot
        -> stop-loss / take-profit monitor (adds symbols to flatten)
        -> ExecutionRunner.execute
            -> idempotency check (signal_id)
            -> reconciliation plan (close before open)
            -> no-trade band, live-mode guard, risk gate
            -> child-order slicing and dispatch through ExchangePort
            -> intent journal (SQLite) + audit log (JSONL)
```

## Strategy contract

A strategy is a pure callback. It receives a snapshot and returns intent; it never calls the exchange.

```python
from datetime import UTC, datetime

from tradebot.engine import LongTarget, TargetPortfolio


def strategy(snapshot):
    if snapshot["prices"].get("BTC", 0.0) <= 0:
        return TargetPortfolio(strategy_id="my-strategy", strategy_version="v1",
                               signal_id="flat-no-price", timestamp=datetime.now(UTC))
    return TargetPortfolio(
        strategy_id="my-strategy",
        strategy_version="v1",
        signal_id="my-strategy-2026-10-01T12:00",   # one id per decision: repeats are ignored
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", weight=0.25)],
        reason="Maintain 25% BTC allocation",
    )
```

- **Snapshot fields:** `cash_usd`, `longs` and `shorts` (USD by coin), `pending_orders`, `prices` (Roostoo last price by coin), `entry_prices` (shorts only) and `equity_usd`.
- **Symbols** are bare coins (`"BTC"`); see `tradebot.core.symbols`.
- **Targets are a desired end state.** `weight=0.25` means the engine works out the USD amount from current equity.
- **Shorts** are given as `ShortTarget(symbol, collateral_usd)`, which is 1x: collateral equals the short's size.
- **Symbols left out of a target are closed.** That includes any coin in the wallet the strategy didn't mention.

## Order policy

`execution.order_policy` in `config/default.yaml`:

| Policy | Spot buys and sells, short opens | Short closes |
|---|---|---|
| `limit_only` (default) | LIMIT at the target's `limit_price`, or else the snapshot's last price | MARKET. Roostoo's `short_close` takes no price. |
| `limit_or_market` | LIMIT only if the target sets `limit_price` and urgency isn't `high`; otherwise MARKET | MARKET |

**Unfilled limit orders** are cancelled once they are older than `fill_timeout_seconds` (default 900 s, one 15-minute bar). The next signal then re-places them at the new price. The backtester models the same one-bar order life, so both behave the same way.

## Running against the mock exchange

```python
from tradebot.core.clock import SimClock
from tradebot.core.config import ExecutionConfig
from tradebot.engine import Engine
from tradebot.exchange import MockExchangePort

clock = SimClock()
engine = Engine(MockExchangePort(clock=clock), config=ExecutionConfig(dry_run=False), clock=clock,
                state_path="var/sim_state.db", audit_path="var/sim_audit.jsonl")
results = engine.run(strategy, max_iterations=100)
engine.close()
```

`MockExchangePort` starts with $100,000 and charges fees from the shared `FeeSchedule`. It models immediate spot fills, pending limit shorts, short fees, merged shorts and partial short closes.

## Running against Roostoo

```python
from tradebot.engine import Engine
from tradebot.exchange import RoostooExchangePort

engine = Engine(RoostooExchangePort())   # execution settings come from config/default.yaml
```

Live order sending requires **both** `execution.dry_run: false` and `execution.live_mode: true`. With `dry_run: true`, nothing is sent: dispatch returns a fake `DryRun` success. Before enabling live mode:

1. **Rotate any exposed credentials.** Put the new key in the root `.env`, copied from `.env.example`.
2. Run `python -m pytest -q`.
3. Run the read-only checks in `python -m tradebot api` (options 1–6).
4. Use a separate `state_dir` for live state.
5. Start with one symbol and a small `max_order_value_usd`.

There is no unattended live runner yet; see `docs/REVIEW.md`.

## Execution safety

- **Order of trades:** reductions are executed before additions.
- **Pending orders:** pending buys and short opens count toward exposure, so they aren't sent twice.
- **Small trades:** opening amounts below `no_trade_band_pct` of equity are skipped. Large amounts are split by `max_child_order_pct`.
- **Duplicates:** a repeated `signal_id` does nothing. A signal rejected by risk checks can be retried.
- **Unknown outcomes** become `UNCERTAIN`. They are resolved by comparing before/after snapshots, never by blindly retrying. The HTTP client also never retries an order request by itself.
- **Risk checks** run before anything is sent: kill switch, daily loss, drawdown, cash reserve, exposure and short-collateral limits.

## Persistence and audit

The intent journal (`<state_dir>/engine_state.db`) tracks each order intent through `PENDING_SEND` → `SENT` → `RESOLVED` / `UNCERTAIN`.
- Signal receipts are stored separately, so even a signal that produced no orders can't run twice.
- The audit log (`<state_dir>/engine_audit.jsonl`) records risk decisions, operations, duplicates and outcomes.
- Every HTTP request is logged by `tradebot.exchange.client` with endpoint, status, success and latency.

## Implementing another exchange port

A simulator or other venue must implement the `ExchangePort` Protocol in `tradebot/exchange/port.py`:
- `get_exchange_info`, `get_ticker`, `get_balance`
- `place_order`, `cancel_order`, `query_order`, `list_open_orders`
- `open_short`, `close_short`, `get_short_positions`
- `get_pending_count`, `get_server_time`

Responses must use Roostoo's shapes, for example `SpotWallet` for balances. The strongest compatibility test is to run the same strategy, starting state, prices and `SimClock` against `MockExchangePort` and the new port, then compare snapshots, plans, operation order and final exposure.
