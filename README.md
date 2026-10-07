# Situational Unawareness: Roostoo trading bot

Team 87's autonomous trading bot for the [Roostoo](https://app.roostoo.com) mock crypto exchange (Roostoo × Susquehanna × AWS Quant Hackathon, live Oct 4–17, 2026). Each team gets $100,000. Bots trade spot longs and 1x shorts, and are judged on return, Sharpe, Sortino and Calmar.

**The strategy is RXM, residual cross-sectional momentum.** Once a day it goes long the coins with the strongest trend after removing their BTC beta and short the weakest, sized by inverse volatility, with a competition lock-in. The full specification and evidence are in [docs/STRATEGY_SPEC.md](docs/STRATEGY_SPEC.md).

An optional [shared-account market-making profile](docs/MARKET_MAKING.md) allocates
70% of current equity to the long-only `mm-10m-fluctuation` strategy and 30% to RXM.
Each strategy is independently selectable with `--strategies`; neither requires
the other to run. MM uses its own ledger and fixed ten-minute quotes for PEPE, BONK and
1000CHEEMS. Run `python -m tradebot --config config/market-making.yaml account preflight`
(read-only) before a takeover; see the doc for migration, replay and dry-run commands.

One Python package, `tradebot`, contains:
- **Historical data:** Binance spot candles for every Roostoo pair.
- **A backtester:** it simulates the competition rules (limit orders at 0.05%, 0.1% short fees, 1x shorts, lock-in) and scores strategies on competition-length windows.
- **A live bridge:** it feeds live Binance candles into the backtested strategy, so the exact backtest code decides live.
- **A live execution engine:** it turns targets into passive limit orders on Roostoo, with risk checks, a pre-trade guard, idempotency and an audit trail.
- **An unattended runner:** it recovers from errors with backoff and supports a kill switch, a heartbeat and a status file.

## Quickstart

Python 3.11 or later.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                   # add your Roostoo API key and secret

python -m pytest -q                                     # test suite
python -m tradebot data                                 # download candles (~640 MB per year)
python -m tradebot backtest --strategy rxm --params k=3,tilt=0.3,buffer=2 --windows --window-days 14

# The bot (config/competition.yaml = RXM competition settings)
python -m tradebot --config config/competition.yaml replay --days 7              # simulate on past candles
python -m tradebot --config config/competition.yaml live --state-dir var/dry     # dry run: reads, never sends
ROOSTOO_CONFIRM_LIVE=YES python -m tradebot --config config/competition.yaml live --live --state-dir var/live_comp
```

Settings live in [config/default.yaml](config/default.yaml), with competition overrides in [config/competition.yaml](config/competition.yaml). API keys go only in `.env`. Operating the bot: [docs/LIVE_RUNBOOK.md](docs/LIVE_RUNBOOK.md).

The account profile shares one strategy-neutral market-data producer: cached prices
update every second, while MM and RXM retain their separate decision schedules.

## Layout

```
src/tradebot/
  core/       config, symbols, metrics, clock, logging
  exchange/   Roostoo client, ExchangePort interface, mock and replay exchanges, API menu
  data/       shared one-second market-data engine, Binance downloader and loaders
  strategy/   Strategy interface; library: RXM (the competition strategy), MA crossover
  backtest/   limit-order simulator, competition windows, CLI
  research/   RXM experiments and disciplined tuning
  engine/     live execution engine
  live/       candle bridge, guard, re-pegging, throttle, unattended runner, replay simulation
  dashboard/  static trading-desk dashboard
config/       default.yaml, competition.yaml
tests/        pytest suite
docs/         strategy, operations, architecture, review
```

## Documentation

| Doc | Read it for |
|---|---|
| [STRATEGY_SPEC.md](docs/STRATEGY_SPEC.md) | What we trade and why: the frozen RXM spec, evidence and risks |
| [LIVE_RUNBOOK.md](docs/LIVE_RUNBOOK.md) | Running, watching and stopping the bot; go-live checklist |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the system fits together: layers, data flows, conventions |
| [REVIEW.md](docs/REVIEW.md) | Fixed issues, missing features by priority, backtest vs live differences |
| [VALIDATION.md](docs/VALIDATION.md), [TUNING.md](docs/TUNING.md) | Out-of-sample validation; how to test a change without overfitting |
| [BACKTESTING.md](docs/BACKTESTING.md) | Writing and backtesting a strategy |
| [DASHBOARD.md](docs/DASHBOARD.md) | The dashboard and the guard's checks |
| [ENGINE.md](docs/ENGINE.md), [DESIGN.md](docs/DESIGN.md) | The live engine and verified Roostoo behaviour |
| [ROOSTOO_API.md](docs/ROOSTOO_API.md) | The Roostoo client and the manual API menu |
| [MARKET_MAKING.md](docs/MARKET_MAKING.md) | Shared MM/RXM account: allocation, reconciliation, preflight and migration |
| [ACCOUNT_VALIDATION.md](docs/ACCOUNT_VALIDATION.md) | Real-order checks of the shared account on a separate test account |

## License

See [LICENSE](LICENSE).
