from backtest.strategies.ma_crossover import MACrossover

# Register new strategies here so the CLI can find them by name
STRATEGIES = {
    MACrossover.name: MACrossover,
}
