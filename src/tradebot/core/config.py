"""
Single configuration model for the whole project.

One YAML file (config/default.yaml, or the path in $TRADEBOT_CONFIG) holds every section:
  fees       - the exchange fee schedule, shared by backtest, engine risk checks and the mock exchange
  exchange   - Roostoo connection settings
  execution  - live engine behaviour and risk limits
  backtest   - simulator settings
  data       - historical data download settings
  live       - unattended live runner and live candle bridge (`python -m tradebot live`)
Secrets never live in YAML: API keys come from the environment / .env (see exchange.client).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from tradebot.core.cointegration import CointegrationConfig

DEFAULT_CONFIG_PATH = Path("config/default.yaml")
CONFIG_ENV_VAR = "TRADEBOT_CONFIG"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeeSchedule(_Section):
    """Roostoo fees as fractions of traded value."""

    spot_maker: float = 0.0005   # limit orders
    spot_taker: float = 0.001    # market orders
    short_open: float = 0.001    # charged on collateral when a short is placed, limit or market
    short_close: float = 0.001   # short closes are always market (no price parameter)


class ExchangeSettings(_Section):
    base_url: str = "https://mock-api.roostoo.com"
    request_timeout_seconds: float = 10.0
    max_retries: int = 3                      # for idempotent requests only; orders are never auto-retried
    retry_backoff_seconds: float = 1.0
    min_request_interval_seconds: float = 0.1  # client-side throttle (no HFT / excessive requests)
    exchange_info_ttl_seconds: int = 3600
    server_time_resync_seconds: int = 600


class ExecutionConfig(_Section):
    """Live engine behaviour and risk limits."""

    # Mode
    live_mode: bool = False
    dry_run: bool = True
    supports_shorting: bool = True
    supports_limit_orders: bool = True
    order_policy: Literal["limit_only", "limit_or_market"] = "limit_only"
    limit_offset_bps: float = 0.0            # passive offset for limits the engine prices itself (e.g. exits)

    # Orders
    no_trade_band_pct: float = 0.01
    max_child_order_pct: float = 0.25
    strategy_poll_interval_seconds: int = 5
    fill_timeout_seconds: int = 900          # one 15m bar: unfilled limits are cancelled and re-placed next bar
    state_dir: str = "var"                   # intent journal (sqlite) and audit log (jsonl)

    # Risk limits
    max_gross_exposure: float = 2.0
    max_net_exposure: float = 1.0
    max_per_symbol_exposure: float = 0.35
    max_positions: int = 20
    min_cash_reserve_usd: float = 500.0
    max_total_short_collateral_usd: float = 10000.0
    max_daily_loss_usd: float = 2000.0
    max_drawdown_pct: float = 0.25
    max_order_value_usd: float = 25000.0

    # Reserved: accepted in config but not implemented by the engine yet (see docs/REVIEW.md)
    quote_currency: str = "USD"
    supports_leverage: bool = False
    supports_stop_orders: bool = False
    min_trade_interval_seconds: int = 30
    max_effective_leverage: float = 2.5
    stale_data_seconds: int = 60
    spread_guard_pct: float = 0.02
    price_deviation_pct: float = 0.05
    fat_finger_limit_pct: float = 0.2
    short_collateral_mode: str = "auto"
    equity_snapshot_interval_seconds: int = 300
    heart_beat_interval_seconds: int = 60
    pending_short_ttl_seconds: int = 900
    # After cancelling stale orders, wait this long before reading the account (the exchange may
    # release the locked USD a moment after it acknowledges the cancel)
    cancel_settle_seconds: float = 2.0

    # Filled from the top-level `fees` section by Settings; not set in YAML
    fees: FeeSchedule = Field(default_factory=FeeSchedule)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ExecutionConfig":
        return Settings.load(path).execution

    @classmethod
    def default(cls) -> "ExecutionConfig":
        return cls()


class BacktestConfig(_Section):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    pair_cost_model: Literal["reference", "market"] = "reference"
    pair_slippage_bps: float = Field(default=2., ge=0, lt=10000)
    market_slippage_bps: float = Field(default=0, ge=0, lt=10000)
    penetration_ticks: int = Field(default=0, ge=0)
    penetration_probability: float = Field(default=1, ge=0, le=1)
    random_seed: int = Field(default=0, ge=0, le=2**63-1)
    liquidate_mm: bool = False
    mm: "BacktestMMConfig" = Field(default_factory=lambda: BacktestMMConfig())
    archive_cache_dirs: list[str] = Field(default_factory=lambda: ["data/mm-1s", "../shared-backtest/data"])
    candle_store_dir: str = "var/market"
    instrument_rules_path: str = "var/backtest/instrument-rules.json"
    instrument_rules: dict[str, dict] = Field(default_factory=dict)
    download_missing: bool = True
    initial_cash: float = Field(default=100_000.0, gt=0)       # competition starting portfolio
    limit_offset_bps: float = 0.0         # buys rest this far below the last close, sells above
    limit_fill: Literal["through", "touch"] = "through"
    rebalance_band: float = 0.01          # skip orders that move a weight by less than this
    min_trade_usd: float = 1.0
    gap_improvement: bool = True          # a limit the open gapped past fills at the better open (False: at the limit)
    latency_bars: int = 0                 # stress test: limits priced this many bars stale; crossed on arrival = taker
    # Competition lock-in overlay: once the return since trade start reaches lockin_return,
    # all target weights are scaled by lockin_scale for the rest of the run (0 = off)
    lockin_return: float = 0.0
    lockin_scale: float = 0.3
    # Shorts are 1x (collateral = notional). Roostoo hasn't published these, so they're assumptions:
    borrow_rate_annual: float = 0.0
    maintenance_margin: float = 0.0
    # CLI defaults
    symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL", "BNB", "XRP"])
    interval: str = "15m"
    window_days: int = 7
    step_days: int = 1
    warmup_days: int = 30
    results_dir: str = "results"

    # Filled from the top-level `fees` section by Settings; not set in YAML
    fees: FeeSchedule = Field(default_factory=FeeSchedule)


class DataConfig(_Section):
    dir: str = "data/binance"
    intervals: list[str] = Field(default_factory=lambda: ["5m", "15m"])
    lookback_days: int = 365
    workers: int = 8


class LiveConfig(_Section):
    """The unattended runner (`python -m tradebot live`) and the live candle bridge."""

    strategies: list[str] = Field(default_factory=list)  # explicit account roster; empty uses legacy strategy
    strategy: str = "rxm"                 # registry name; rxm runs through its frozen presets
    mode: str = "comp"                    # rxm preset (comp | neutral); never change it mid-event
    params: dict[str, Any] = Field(default_factory=dict)   # strategy params for non-preset strategies
    universe: list[str] = Field(default_factory=list)       # coins; empty = the strategy's own universe
    state_dir: str = "var/live"           # engine journal, audit log, strategy state, status, bot.log
    # Candles
    klines_url: str = "https://data-api.binance.vision"
    market_stream_url: str = "wss://stream.binance.com:9443/stream"
    # Tried in order after a failed connection (market-data-only Binance endpoint, same events)
    market_stream_fallback_urls: list[str] = ["wss://data-stream.binance.vision/stream"]
    # Local copy of live market data (shared account engine): restarts and outages start from disk
    market_store_enabled: bool = True
    market_store_dir: str = "var/market"
    market_store_retention_days: int = Field(default=30, ge=1)
    market_store_min_free_gb: float = Field(default=1.0, ge=0)   # below this, stop saving; trading continues
    buffer_days: int = 50                 # >= 45: 30-day beta + 14-day lookback
    bar_grace_minutes: int = 30           # wait this long for a late decision bar before going without it
    stale_after_hours: float = 2.0        # no new candle for this long: hold the book
    # Target construction
    band: float = 0.01                    # only trade symbols more than this off target (as the backtest)
    gross_cap: float = 0.98               # leave cash for fees so a gross-1.0 book passes the cash check
    min_trade_usd: float = 20.0
    lock_confirmations: int = 2           # consecutive polls above the lock-in return before locking
    max_equity_jump: float = 0.5          # reject a snapshot whose equity moved more than this (partial read)
    # Order transport
    max_http_per_minute: int = 25         # Roostoo allows 30
    repeg: bool = True                    # re-price each limit off a fresh ticker just before sending
    repeg_max_move: float = 0.03          # skip the order if the price moved more than this since the signal
    # Escalation ladder for symbols still off target (bounded time to target): attempt n rests
    # ladder_bps[n] passive (first = the strategy offset), then buys/sells cross by cross_bps and short
    # opens go at market. Attempts are ~16 min apart (fill_timeout + one poll). Off = passive forever.
    escalate: bool = False
    ladder_bps: list[float] | None = None  # None = [strategy offset, 0]
    cross_bps: float = 10.0
    # Supervision
    kill_file: str = "KILL"               # create this file to stop all new orders; delete it to resume
    heartbeat_minutes: int = 15
    max_backoff_seconds: int = 900        # cap on the retry delay after consecutive failed loops
    guard: dict[str, Any] = Field(default_factory=dict)     # tradebot.live.guard.GuardConfig overrides


class CapitalAllocation(_Section):
    """Fractions of reconciled equity at first initialization; persisted thereafter."""
    mm_fraction: float = Field(default=0.90, gt=0, lt=1, allow_inf_nan=False)

    @property
    def rxm_fraction(self) -> float:
        return 1.0 - self.mm_fraction


class MarketMakingConfig(_Section):
    enabled: bool = False
    capital: CapitalAllocation = Field(default_factory=CapitalAllocation)
    # The live profile supplies its own symbol allocation; this default remains
    # compatible with the original three-book preset and independent tests.
    allocations: dict[str, float] = Field(default_factory=lambda: {
        "PEPE": 0.85, "BONK": 0.075, "1000CHEEMS": 0.075})
    # ``refresh_seconds`` is retained as a compatibility input for existing
    # configs; new profiles should use ``quote_refresh_seconds``.
    refresh_seconds: int = Field(default=600, ge=1)
    quote_refresh_seconds: int = Field(default=270, ge=1)
    volatility_spread_coefficient: float = Field(default=100.0, gt=0)
    inventory_skew_coefficient: float = Field(default=30.0, gt=0)
    signal_horizon_seconds: int = Field(default=330, ge=1)
    warmup_seconds: int = Field(default=3600, ge=3600)
    feature_lag_seconds: int = Field(default=1, ge=1)
    # Midpoint is opt-in so candle-only historical replays remain reproducible.
    reference_source: Literal["candle_close", "midpoint"] = "candle_close"
    max_book_age_seconds: float = Field(default=2.0, gt=0, le=60)
    enforce_one_tick_distance: bool = False
    # Live 1s candles arrive 2-3 s late: decide at last complete second + 1 s if at most this far behind
    max_data_delay_seconds: float = Field(default=5.0, ge=0, le=60)
    lot_fraction: float = Field(default=0.05, gt=0, le=1)
    inventory_fraction: float = Field(default=0.40, gt=0, le=1)
    # Reconciliation (docs/MARKET_MAKING.md "Reconciliation without a global halt").
    # Cash differences up to cash_tolerance_bps of the notional filled in a sync (min floor) are fee/
    # proceeds rounding: absorbed and journaled. Larger ones restrict only the traced scope.
    cash_tolerance_bps: float = Field(default=3.0, ge=0, le=30)
    cash_tolerance_floor_usd: float = Field(default=0.05, ge=0)
    restriction_clear_syncs: int = Field(default=2, ge=1)   # clean syncs before a restriction lifts
    evidence_window_seconds: float = Field(default=30.0, gt=0)  # lost-response matching window
    cancel_retry_seconds: float = Field(default=30.0, gt=0)     # re-send an unconfirmed cancel after this
    # Existing RXM state is read only during first bootstrap, never guessed from the wallet.
    rxm_state_dir: str = "var/live_comp"
    replay_cache_dir: str = "data/mm-1s"
    # Empty = per-account lock in the user's state dir, keyed to base URL + API key (tradebot.core.locking).
    # Set only for tests or unusual hosts; a relative path is resolved against the start directory.
    account_lock: str = ""

    @model_validator(mode="after")
    def sync_quote_refresh(self):
        # An explicitly supplied legacy value wins, while the new field is
        # mirrored back for callers that still inspect refresh_seconds.
        if self.refresh_seconds != 600:
            self.quote_refresh_seconds = self.refresh_seconds
        self.refresh_seconds = self.quote_refresh_seconds
        return self

    @model_validator(mode="after")
    def validate_allocations(self):
        import math
        if (not self.allocations or any(
            not isinstance(symbol, str) or not symbol.strip() or
            not math.isfinite(value) or value <= 0
            for symbol, value in self.allocations.items()
        ) or not math.isclose(sum(self.allocations.values()), 1.0, abs_tol=1e-12)):
            raise ValueError("MM allocations must be positive fractions summing to one")
        return self


class BacktestMMConfig(MarketMakingConfig):
    """Research controls; independent of the live account allocation and cadence."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    refresh_seconds: int = Field(default=600, ge=1, le=86400)
    quote_refresh_seconds: int = Field(default=270, ge=1, le=86400)
    warmup_seconds: int = Field(default=3600, ge=2, le=86400)
    feature_lag_seconds: int = Field(default=1, ge=0, le=86399)

    @model_validator(mode="after")
    def validate_allocations(self):
        # Override the live preset restriction only for independent backtests.
        import math
        import re
        normalized = {}
        for symbol, weight in self.allocations.items():
            coin = symbol.strip().upper().removesuffix('/USDT').removesuffix('/USD')
            if not re.fullmatch(r'[A-Z0-9]{1,25}', coin) or coin in normalized:
                raise ValueError('MM allocations require distinct coin tickers, e.g. BTC, ETH, PEPE')
            normalized[coin] = weight
        if not 1 <= len(normalized) <= 50 or any(
            not math.isfinite(v) or v < 0 for v in normalized.values()
        ) or not math.isclose(sum(normalized.values()), 1.0, rel_tol=0, abs_tol=1e-12):
            raise ValueError('MM allocations must be nonnegative fractions summing to one, for 1 to 50 symbols')
        self.allocations = normalized
        return self

    @property
    def active_symbols(self) -> list[str]:
        """Zero-weight symbols are disabled and need neither history nor rules."""
        return [symbol for symbol, weight in self.allocations.items() if weight > 0]

    @model_validator(mode="after")
    def validate_history(self):
        if self.feature_lag_seconds >= self.warmup_seconds:
            raise ValueError("MM feature lag must be smaller than warm-up")
        if self.reference_source != "candle_close":
            raise ValueError("MM midpoint backtests require recorded historical bid/ask observations; use candle_close")
        return self


BacktestConfig.model_rebuild()


class Settings(_Section):
    fees: FeeSchedule = Field(default_factory=FeeSchedule)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    market_making: MarketMakingConfig = Field(default_factory=MarketMakingConfig)
    cointegration: CointegrationConfig = Field(default_factory=CointegrationConfig)

    @model_validator(mode="after")
    def _share_fees(self) -> "Settings":
        # One fee schedule for every consumer
        self.execution.fees = self.fees
        self.backtest.fees = self.fees
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        """Load settings from `path`, $TRADEBOT_CONFIG, or config/default.yaml (defaults if none exist)."""
        explicit = path or os.environ.get(CONFIG_ENV_VAR)
        file_path = Path(explicit) if explicit else DEFAULT_CONFIG_PATH
        if not file_path.exists():
            if explicit:
                raise FileNotFoundError(f"Config file does not exist: {file_path}")
            return cls()
        with file_path.open("r", encoding="utf-8") as handle:
            payload: dict[str, Any] = yaml.safe_load(handle) or {}
        return cls(**payload)
