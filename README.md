# Situational Unawareness: Roostoo trading bot

An autonomous trading bot for the [Roostoo](https://app.roostoo.com) mock crypto exchange, built for the SIG hackathon. Each team gets $100,000. Bots trade spot longs and 1x shorts, and are judged on return, Sharpe, Sortino and Calmar.

One Python package, `tradebot`, contains:
- **Historical data:** a one-year download of Binance spot candles for every Roostoo pair.
- **A backtester:** it simulates the competition rules (limit orders at 0.05%, short fees, 1x shorts with liquidation) and scores strategies on rolling 7-day windows.
- **A live execution engine:** it turns a strategy's target portfolio into limit orders on Roostoo, with risk checks, idempotency and an audit trail.

The trading strategy itself is in progress. The current status and the missing pieces are in [docs/REVIEW.md](docs/REVIEW.md).

## Quickstart

Python 3.11 or later.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                   # add your Roostoo API key and secret

python -m pytest -q                                     # test suite
python -m tradebot data                                 # download 1 year of 5m/15m candles (~640 MB)
python -m tradebot backtest --strategy ma_crossover --params fast=80,slow=400            # full year
python -m tradebot backtest --strategy ma_crossover --params fast=80,slow=400 --windows  # 7-day windows
python -m tradebot api                                  # interactive Roostoo API menu
```

All settings live in [config/default.yaml](config/default.yaml). API keys go only in `.env`.

## Layout

```
src/tradebot/
  core/       config, symbols, metrics, clock, logging
  exchange/   Roostoo client, ExchangePort interface, mock exchange, API menu
  data/       Binance Vision downloader and loaders
  strategy/   Strategy interface and strategy library
  backtest/   limit-order simulator, competition windows, CLI
  engine/     live execution engine
config/       default.yaml
tests/        pytest suite
docs/         architecture, review, guides
```

## Documentation

| Doc | Read it for |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the system fits together: layers, data flows, conventions |
| [REVIEW.md](docs/REVIEW.md) | Fixed issues, missing features by priority, backtest vs live differences |
| [BACKTESTING.md](docs/BACKTESTING.md) | Writing and backtesting a strategy (for the quant) |
| [ENGINE.md](docs/ENGINE.md) | The live engine: strategy contract, order policy, safety, persistence |
| [ROOSTOO_API.md](docs/ROOSTOO_API.md) | The Roostoo client and the manual API menu |
| [DESIGN.md](docs/DESIGN.md) | Engine design notes and verified Roostoo exchange behaviour |

## License

See [LICENSE](LICENSE).
