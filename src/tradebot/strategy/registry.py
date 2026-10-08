"""Name -> Strategy class lookup used by the CLI. Register new strategies here."""
from tradebot.strategy.base import Strategy
from tradebot.strategy.library.ma_crossover import MACrossover
from tradebot.strategy.library.rxm import ResidualMomentum

from tradebot.strategy.library.mm_fluctuation import MMFluctuation
from tradebot.strategy.library.cointegration import CointegrationPairs

STRATEGIES: dict[str, type[Strategy]] = {
    CointegrationPairs.name: CointegrationPairs,
    MMFluctuation.name: MMFluctuation,
    MACrossover.name: MACrossover,
    ResidualMomentum.name: ResidualMomentum,
}
