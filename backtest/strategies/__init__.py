from backtest.strategies.ma_crossover import MACrossover
from backtest.strategies.rxm import ResidualMomentum

# Register new strategies here so the CLI can find them by name
STRATEGIES = {
    MACrossover.name: MACrossover,
    ResidualMomentum.name: ResidualMomentum,
}
