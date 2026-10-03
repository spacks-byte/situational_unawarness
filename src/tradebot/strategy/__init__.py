"""Strategy interface (target weights per bar) and the strategy library."""
from tradebot.strategy.base import Strategy
from tradebot.strategy.registry import STRATEGIES

__all__ = ["STRATEGIES", "Strategy"]
