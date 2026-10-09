"""Frozen cointegration handoff parameters and explicit shadow-cycle settings."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PAIR_THRESHOLDS = (
    ("FIL-MIRA", 1.5, .3), ("AVNT-MIRA", 2.25, .7), ("EIGEN-ONDO", 1.75, .001),
    ("HEMI-TUT", 2.5, .1), ("HEMI-POL", 2.25, .02), ("FORM-TAO", 2.25, .05),
    ("1000CHEEMS-TAO", 2., .15), ("EDEN-LTC", 2., .3), ("CAKE-POL", 1.5, .001),
    ("BIO-EIGEN", 3., .7), ("CFX-ENA", 2.5, .2), ("AVNT-FET", 2.25, .001),
    ("BNB-LISTA", 2.75, .31),
)


class PairSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    pair: str
    entry_z: float = Field(gt=0)
    exit_z: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_pair(self):
        bases = self.pair.split("-")
        if len(bases) != 2 or bases[0] == bases[1] or any(not b.isalnum() or b != b.upper() for b in bases):
            raise ValueError("pair must name two distinct uppercase base assets in fixed A-B order")
        if self.exit_z >= self.entry_z:
            raise ValueError("exit threshold must be below entry threshold")
        return self


class CointegrationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    execution: Literal["observe", "execute"] = "execute"
    intended_order_type: Literal["MARKET"] = "MARKET"
    cycle_start: datetime | None = None
    cycle_end: datetime | None = None
    reference_capital: float = Field(default=10_000., gt=0)
    train_days: Literal[60] = 60
    z_window_days: Literal[20] = 20
    volatility_days: Literal[60] = 60
    max_hold_days: Literal[14] = 14
    pairs: list[PairSpec] = Field(default_factory=lambda: [
        PairSpec(pair=p, entry_z=x, exit_z=y) for p, x, y in PAIR_THRESHOLDS])

    @model_validator(mode="after")
    def validate_cycle(self):
        if not self.pairs or len({p.pair for p in self.pairs}) != len(self.pairs):
            raise ValueError("pairs must be nonempty and unique")
        for value in (self.cycle_start, self.cycle_end):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None
                                      or value.minute % 30 or value.second or value.microsecond):
                raise ValueError("cycle boundaries must be timezone-aware 30-minute boundaries")
        if self.cycle_end is not None and (self.cycle_start is None or self.cycle_end <= self.cycle_start):
            raise ValueError("cycle end requires an earlier cycle start")
        return self

    @property
    def assets(self):
        return sorted({a for p in self.pairs for a in p.pair.split("-")})

    @property
    def retention_seconds(self):
        # Regression needs 60d; daily volatility includes one preceding close.
        # Keep one cycle too, allowing bounded restart catch-up without refitting.
        return (self.train_days + self.max_hold_days) * 86400 + 1800
