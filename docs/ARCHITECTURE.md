# Architecture

The bot is one installable Python package, `tradebot` (`src/tradebot/`), with one config file, one exchange client and one CLI. The backtester and the live engine share the same strategy interface, symbols, fee schedule and metrics. Results from one should therefore predict the other.

## Layers

Dependencies only point downward. Lower layers never import higher ones.

```mermaid
flowchart TB
    cli["cli<br/>python -m tradebot data | backtest | api"]
    subgraph apps[" "]
        backtest["backtest<br/>simulator · windows · cli"]
        engine["engine<br/>runtime · reconcile · risk · execution · state · monitor"]
    end
    strategy["strategy<br/>Strategy base · library · registry"]
    data["data<br/>Binance Vision downloader · Parquet loader"]
    exchange["exchange<br/>RoostooClient · ExchangePort · RoostooExchangePort · MockExchangePort"]
    core["core<br/>config · symbols · metrics · clock · intervals · log"]

    cli --> backtest
    cli --> data
    cli --> exchange
    backtest --> strategy
    backtest --> data
    engine --> exchange
    strategy --> core
    backtest --> core
    engine --> core
    data --> core
    exchange --> core
```

| Package | Responsibility | Key modules |
|---|---|---|
| `core` | Shared foundations, with no I/O except reading config | `config.py`: `Settings` with `fees`, `exchange`, `execution`, `backtest` and `data` sections. `symbols.py`: coin ↔ `BTC/USD` ↔ `BTCUSDT`. `metrics.py`: return, Sharpe, Sortino, Calmar, drawdown. `clock.py`: `Clock`, `RealClock`, `SimClock`. `intervals.py`. `log.py`. |
| `exchange` | Everything that talks to Roostoo | `client.py`: `RoostooClient` (signing, timeouts, retries, caching, throttling, request logging). `port.py`: `ExchangePort` Protocol and `RoostooExchangePort`. `mock.py`: `MockExchangePort`. `manual.py`: interactive API menu. |
| `data` | Historical market data | `binance_vision.py`: download and verify Binance Vision klines into Parquet. `loader.py`: `load_klines`, `load_universe` (keyed by coin). |
| `strategy` | Trading logic as target weights | `base.py`: `Strategy.generate_weights(data) -> DataFrame`. `library/`: concrete strategies. `registry.py`: name → class. |
| `backtest` | Offline evaluation | `simulator.py`: `run_backtest`, `buy_and_hold`. `windows.py`: rolling 7-day competition windows. `cli.py`. |
| `engine` | Live order execution | `runtime.py`: one cycle of the strategy loop. `reconcile/`: plan and stale-order cancellation. `risk/`: risk gate. `execution/runner.py`: order policy, idempotency, dispatch. `state/`: snapshot, intent journal, audit log. `monitor/`: stop-loss and take-profit. `schema/`: `TargetPortfolio`. `app.py`: `Engine` facade. |
| `cli` | Entry point | `cli.py` and `__main__.py` |

## Backtest flow

```mermaid
flowchart LR
    BV[(Binance Vision<br/>zips)] -->|tradebot data| PQ[(data/binance/klines<br/>Parquet per coin)]
    PQ -->|load_universe| D["{coin: OHLCV DataFrame}"]
    D --> S[Strategy.generate_weights]
    S -->|"weights per bar (−1 to 1)"| SIM[simulator.run_backtest]
    CFG[config: backtest + fees] --> SIM
    SIM --> M[core.metrics]
    M --> OUT[(results/: equity.csv,<br/>trades.csv, summary.json)]
```

## Live flow

```mermaid
flowchart LR
    R[(Roostoo API)] <-->|HTTP + HMAC| C[RoostooClient]
    C --- P[RoostooExchangePort]
    P -->|balance, shorts, ticker, open orders| SN[snapshot]
    SN --> ST["strategy(snapshot)"]
    ST -->|TargetPortfolio| RUN[ExecutionRunner]
    RUN --> PL[reconcile plan] --> RK[risk gate] --> DP[dispatch: LIMIT orders,<br/>market short closes]
    DP --> P
    RUN --> J[(var/: intent journal<br/>+ audit log)]
```

There is no live strategy adapter or market-data feed yet; see `docs/REVIEW.md`. The intended bridge is a callable that takes the snapshot, keeps a buffer of recent candles, calls `Strategy.generate_weights` on each completed bar, and converts the last row into a `TargetPortfolio`:
- a positive weight becomes a `LongTarget`;
- a negative weight becomes a `ShortTarget` with `collateral = |w| × equity`;
- the `signal_id` is set once per bar.

## Conventions

- **Symbols:** inside the package a symbol is always the bare coin (`"BTC"`). Convert only at the edges: Roostoo pairs with `to_pair`, Binance with `to_binance` (data layer only).
- **One config:** every tunable lives in `config/default.yaml`, validated by pydantic models that reject unknown keys. Load it with `Settings.load()` (or `$TRADEBOT_CONFIG` / `--config`). CLI flags only override values; they never set their own defaults. Secrets come only from `.env`.
- **One fee schedule:** `fees` (spot maker/taker, short open/close) is shared by the simulator, the engine's risk checks and the mock exchange.
- **Order policy is the same in both modes:**
  - Limit orders at the last close, alive for one bar.
  - Short opens are limit orders but pay the 0.1% short-open fee.
  - Short closes are market orders.
- **Time:** engine code never calls `time` or `datetime.now` directly; it uses an injected `Clock`. A test enforces this, so simulated and live runs share the same logic.
- **Errors and logging:** library code uses `logging.getLogger(__name__)`; only CLIs `print`. The client raises `RoostooError` rather than returning `None`. Order requests are never retried automatically.
- **Units:** returns and drawdowns are decimal fractions (0.05 = 5%); drawdowns are ≤ 0. Money is USD.
- **Typed contracts:** pydantic for external inputs (`TargetPortfolio`, config), dataclasses for internal records.

## Repository layout

```
config/default.yaml     the only config file
src/tradebot/           the package (layers above)
tests/                  pytest suite (engine, client with a fake HTTP session, backtest simulator, core)
docs/                   ARCHITECTURE (this), REVIEW, ENGINE, BACKTESTING, ROOSTOO_API, DESIGN
data/  results/  var/   generated: market data, backtest outputs, engine state (git-ignored)
.env                    API keys (git-ignored; template in .env.example)
```
