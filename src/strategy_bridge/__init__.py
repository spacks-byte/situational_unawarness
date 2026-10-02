"""Bridge between the backtested RXM strategy (backtest/strategies/rxm.py) and the live execution engine."""
from __future__ import annotations

from pathlib import Path

from src.engine.config import ExecutionConfig

COMPETITION_CONFIG_PATH = Path(__file__).with_name("competition_config.yaml")


def load_competition_config(**overrides) -> ExecutionConfig:
    """ExecutionConfig for live/competition runs (live_mode stays False unless passed explicitly)."""
    cfg = ExecutionConfig.from_yaml(COMPETITION_CONFIG_PATH)
    return ExecutionConfig(**{**cfg.model_dump(), **overrides})
