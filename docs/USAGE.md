# Engine Usage

The strategy callback receives one normalized snapshot and returns a `TargetPortfolio`. The engine owns reconciliation, risk checks, idempotency, audit logging, monitoring, and order dispatch.

## Backtest or simulator

```python
from datetime import UTC, datetime

from src.engine.app import Engine
from src.engine.config import ExecutionConfig
from src.engine.ports.mock_port import MockExchangePort
from src.engine.schema.models import LongTarget, TargetPortfolio

port = MockExchangePort(initial_wallet={"USD": 50_000.0})
engine = Engine(
    port,
    config=ExecutionConfig(dry_run=False),
    state_path="backtest_state.db",
    audit_path="backtest_audit.jsonl",
)


def strategy(snapshot):
    return TargetPortfolio(
        strategy_id="example",
        strategy_version="v1",
        signal_id=f"signal-{snapshot['equity_usd']}",
        timestamp=datetime.now(UTC),
        longs=[LongTarget(symbol="BTC", weight=0.25)],
    )

results = engine.run(strategy, max_iterations=100)
engine.close()
```

## Live adapter

```python
from src.engine.app import Engine
from src.engine.config import ExecutionConfig
from src.engine.ports.library_port import LibraryExchangePort

engine = Engine(
    LibraryExchangePort(),
    config=ExecutionConfig(
        dry_run=False,
        live_mode=True,
    ),
)
```

Live mode requires both `dry_run=False` and `live_mode=True`. The raw API library remains behind `LibraryExchangePort`; strategies never call HTTP or HMAC code.

Run the test suite with:

```text
.venv/bin/python -m pytest -q
```
