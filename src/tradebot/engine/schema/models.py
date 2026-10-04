from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Urgency(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class LongTarget(BaseModel):
    """Desired long-side target state for a single symbol."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1)
    notional_usd: float | None = Field(default=None, gt=0)
    weight: float | None = Field(default=None, gt=0, le=1)
    limit_price: float | None = Field(default=None, gt=0)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=100)
    take_profit_pct: float | None = Field(default=None, gt=0, lt=100)
    urgency: Urgency = Urgency.NORMAL

    @model_validator(mode="after")
    def validate_size(self):
        if self.notional_usd is None and self.weight is None:
            raise ValueError("Either notional_usd or weight must be provided.")
        if self.notional_usd is not None and self.weight is not None:
            raise ValueError("Provide either notional_usd or weight, not both.")
        return self


class ShortTarget(BaseModel):
    """Desired short-side target state for a single symbol."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1)
    collateral_usd: float = Field(..., gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=100)
    take_profit_pct: float | None = Field(default=None, gt=0, lt=100)
    urgency: Urgency = Urgency.NORMAL


class TargetPortfolio(BaseModel):
    """Typed input contract from the strategy layer to the execution engine."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(..., min_length=1)
    strategy_version: str = Field(..., min_length=1)
    signal_id: str = Field(..., min_length=1)
    timestamp: datetime
    ttl_seconds: int | None = Field(default=None, ge=1)
    longs: list[LongTarget] = Field(default_factory=list)
    shorts: list[ShortTarget] = Field(default_factory=list)
    flatten: list[str] = Field(default_factory=list)
    # Limit prices for longs the target drops (exits); symbols not listed exit at the config offset
    exit_prices: dict[str, float] = Field(default_factory=dict)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_targets(self):
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")

        for target in (*self.longs, *self.shorts):
            target.symbol = target.symbol.strip().upper()

        long_symbols = [target.symbol for target in self.longs]
        short_symbols = [target.symbol for target in self.shorts]
        if len(long_symbols) != len(set(long_symbols)):
            raise ValueError("Duplicate long symbols are not allowed")
        if len(short_symbols) != len(set(short_symbols)):
            raise ValueError("Duplicate short symbols are not allowed")
        normalized_flatten = [s.strip().upper() for s in self.flatten]
        self.flatten = list(dict.fromkeys(s for s in normalized_flatten if s))
        self.exit_prices = {s.strip().upper(): float(p) for s, p in self.exit_prices.items() if p and p > 0}
        return self
