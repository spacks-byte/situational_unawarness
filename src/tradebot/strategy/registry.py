"""Name -> Strategy class lookup used by the CLI. Register new strategies here."""
from tradebot.strategy.base import Strategy
from tradebot.strategy.library.ma_crossover import MACrossover

STRATEGIES: dict[str, type[Strategy]] = {
    MACrossover.name: MACrossover,
}
