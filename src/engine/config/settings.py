from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ExecutionConfig(BaseModel):
    """YAML-configurable execution and risk settings for the engine."""

    model_config = ConfigDict(extra="forbid")

    quote_currency: str = "USD"
    supports_shorting: bool = True
    supports_leverage: bool = False
    supports_limit_orders: bool = True
    supports_stop_orders: bool = False
    live_mode: bool = False
    dry_run: bool = True
    spot_limit_fee: float = 0.0005
    spot_market_fee: float = 0.001
    short_open_fee: float = 0.001
    short_close_fee: float = 0.001
    no_trade_band_pct: float = 0.01
    min_trade_interval_seconds: int = 30
    max_child_order_pct: float = 0.25
    max_gross_exposure: float = 2.0
    max_net_exposure: float = 1.0
    max_per_symbol_exposure: float = 0.35
    max_positions: int = 20
    max_effective_leverage: float = 2.5
    min_cash_reserve_usd: float = 500.0
    max_total_short_collateral_usd: float = 10000.0
    max_daily_loss_usd: float = 2000.0
    max_drawdown_pct: float = 0.25
    stale_data_seconds: int = 60
    spread_guard_pct: float = 0.02
    price_deviation_pct: float = 0.05
    fat_finger_limit_pct: float = 0.2
    max_order_value_usd: float = 25000.0
    short_collateral_mode: str = "auto"
    strategy_poll_interval_seconds: int = 5
    equity_snapshot_interval_seconds: int = 300
    heart_beat_interval_seconds: int = 60
    fill_timeout_seconds: int = 30
    pending_short_ttl_seconds: int = 900

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ExecutionConfig":
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Config file does not exist: {file_path}")
        with file_path.open("r", encoding="utf-8") as handle:
            payload: dict[str, Any] = yaml.safe_load(handle) or {}
        return cls(**payload)

    @classmethod
    def default(cls) -> "ExecutionConfig":
        return cls()
