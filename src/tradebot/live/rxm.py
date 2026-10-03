"""
RXM as a live engine strategy: `strategy(snapshot) -> TargetPortfolio` for `Engine.run_once`.

- Signal: identical to the backtest. The weights are the last row of `ResidualMomentum.generate_weights`
  on the bar buffer, recomputed once per 00:00 UTC bar (at 00:15, when that bar has closed).
- Late data: if a coin's 00:00 bar hasn't arrived, the decision waits up to `bar_grace` for it rather
  than dropping the coin for the whole day.
- Orders: only coins whose weight is off by more than `band` (or that must be exited) are traded.
  The rest are pinned at their current size, so the engine leaves them alone. Buys rest
  `limit_offset_bps` below the last price and short opens the same amount above it.
- Re-quotes: while a coin is still off target at a new 15m bar (a limit expired unfilled), a new
  signal id re-places it at the new price.
- Hold: with no signal yet (or stale data) the current book is held. The target is never empty,
  because an empty target would close everything.
- Lock-in: once equity is up `lockin_return` since the start, on two polls in a row, every weight is
  scaled by `lockin_scale` for good. Start equity and the lock are saved to a JSON file, so restarts
  keep them.
- Bad reads: a snapshot with zero equity, or a >50% jump from the previous one (a partial API read),
  raises SnapshotRejected before any order is planned.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from tradebot.core.clock import Clock, RealClock
from tradebot.engine.schema import LongTarget, ShortTarget, TargetPortfolio
from tradebot.live.bars import BAR, BarBuffer, last_closed_bar
from tradebot.strategy.library.rxm import PRESETS, ResidualMomentum

log = logging.getLogger(__name__)
VERSION = "rxm-1.1"


class SnapshotRejected(RuntimeError):
    """The account snapshot looks broken; plan nothing this cycle."""


class StrategyState:
    """Start equity and lock-in, stored as JSON and written atomically."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def update(self, **values: Any) -> None:
        self.data.update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str))
        os.replace(tmp, self.path)


class CompetitionStrategy:
    def __init__(self, buffer: BarBuffer, mode: str = "comp", *, state_path: str | Path, clock: Clock | None = None,
                 band: float = 0.01, gross_cap: float = 0.98, min_trade_usd: float = 20.0,
                 stale_after: pd.Timedelta = pd.Timedelta(hours=2), bar_grace: pd.Timedelta = pd.Timedelta(minutes=30),
                 lock_confirmations: int = 2, max_equity_jump: float = 0.5) -> None:
        preset = PRESETS[mode]
        self.mode = mode
        self.strategy = ResidualMomentum(**preset["params"])
        self.offset = preset["backtest"].get("limit_offset_bps", 0.0) / 1e4
        self.lockin_return = preset["backtest"].get("lockin_return", 0.0)
        self.lockin_scale = preset["backtest"].get("lockin_scale", 1.0)
        self.buffer = buffer
        self.clock = clock or RealClock()
        self.band, self.gross_cap, self.min_trade_usd = band, gross_cap, min_trade_usd
        self.stale_after, self.bar_grace = stale_after, bar_grace
        self.lock_confirmations, self.max_equity_jump = lock_confirmations, max_equity_jump
        self.state = StrategyState(state_path)
        if self.state.get("mode") not in (None, mode):
            raise ValueError(f"{state_path} belongs to mode {self.state.get('mode')!r}: never switch modes mid-event")
        self.weights: pd.Series | None = None
        self.weights_bar: pd.Timestamp | None = None
        self._signal: str | None = None
        self._last: TargetPortfolio | None = None
        self._last_bar: pd.Timestamp | None = None
        self._ref_equity: float | None = None
        self._lock_streak = 0

    @property
    def locked(self) -> bool:
        return bool(self.state.get("locked", False))

    def __call__(self, snapshot: dict[str, Any]) -> TargetPortfolio:
        now = pd.Timestamp(self.clock.now()).tz_convert("UTC")
        equity = float(snapshot.get("equity_usd", 0.0))
        reference = self._ref_equity or self.state.get("start_equity")
        if equity <= 0 or (reference and abs(equity / reference - 1) > self.max_equity_jump):
            raise SnapshotRejected(f"implausible equity {equity:.2f} (reference {reference})")
        self._ref_equity = equity
        self._update_lock(equity, now)
        self.buffer.update(now)
        self._refresh_weights(now)

        bar = now.floor("15min")
        if self.weights is None:
            return self._emit(self._hold(snapshot, now), f"{self.mode}-hold-{bar:%Y%m%dT%H%M}", bar)
        signal = f"{self.mode}-{self.weights_bar:%Y%m%dT%H%M}" + ("-L" if self.locked else "")
        longs, shorts, traded = self._targets(snapshot)
        if signal != self._signal:
            self._signal = signal
            return self._emit(self._portfolio(signal, now, longs, shorts, traded), signal, bar)
        if traded and bar != self._last_bar:                            # a limit expired unfilled: re-quote
            requote = f"{signal}-r{bar:%Y%m%dT%H%M}"
            return self._emit(self._portfolio(requote, now, longs, shorts, traded), requote, bar)
        return self._last                                              # unchanged: the engine sees a duplicate

    # ------------------------------------------------------------------ signal
    def _refresh_weights(self, now: pd.Timestamp) -> None:
        last = self.buffer.last_bar()
        if last is None or last < last_closed_bar(now) - self.stale_after:
            if last is not None:
                log.warning("market data stale (last bar %s): holding the current book", last)
            return
        decision_bar = last.floor(f"{self.strategy.rebalance_h}h")
        if self.weights_bar is not None and decision_bar <= self.weights_bar:
            return
        late = self.buffer.lagging(decision_bar)
        if late and now < decision_bar + BAR + self.bar_grace:
            log.info("waiting for the %s bar of %s", decision_bar, late)
            return
        if late:
            log.warning("deciding %s without %s: no bar after %s", decision_bar, late, self.bar_grace)
        self.weights = self.strategy.generate_weights(self.buffer.data()).iloc[-1].fillna(0.0)
        self.weights_bar = decision_bar
        log.info("weights for %s: %s", decision_bar, {c: round(w, 4) for c, w in self.weights.items() if w})

    def scaled_weights(self) -> dict[str, float]:
        """Weights after the lock-in and the gross cap (headroom for fees)."""
        if self.weights is None:
            return {}
        scale = self.lockin_scale if self.locked else 1.0
        weights = {c: float(w) * scale for c, w in self.weights.items() if w}
        gross = sum(abs(w) for w in weights.values())
        return {c: w * self.gross_cap / gross for c, w in weights.items()} if gross > self.gross_cap else weights

    def _targets(self, snapshot: dict[str, Any]) -> tuple[list[LongTarget], list[ShortTarget], list[str]]:
        equity = float(snapshot["equity_usd"])
        prices = snapshot.get("prices") or {}
        longs_now, shorts_now = _exposure(snapshot)
        desired = {c: w for c, w in self.scaled_weights().items() if prices.get(c, 0) > 0}
        longs: list[LongTarget] = []
        shorts: list[ShortTarget] = []
        traded: list[str] = []
        for coin in sorted(set(desired) | {c for c, v in longs_now.items() if v > 0} | {c for c, v in shorts_now.items() if v > 0}):
            w = desired.get(coin, 0.0)
            have_long, have_short = longs_now.get(coin, 0.0), shorts_now.get(coin, 0.0)
            gap = abs(w - (have_long - have_short) / equity)
            must_exit = (w <= 0 and have_long > self.min_trade_usd) or (w >= 0 and have_short > self.min_trade_usd)
            if must_exit or (gap > self.band and gap * equity > self.min_trade_usd):
                traded.append(coin)
                price = prices.get(coin, 0.0)
                if w > 0:
                    longs.append(LongTarget(symbol=coin, weight=min(w, 1.0), limit_price=price * (1 - self.offset)))
                elif w < 0:
                    shorts.append(ShortTarget(symbol=coin, collateral_usd=-w * equity, limit_price=price * (1 + self.offset)))
            else:                                                      # inside the band: pin at the current size
                if have_long > 0:
                    longs.append(LongTarget(symbol=coin, notional_usd=have_long))
                if have_short > 0:
                    shorts.append(ShortTarget(symbol=coin, collateral_usd=have_short))
        return longs, shorts, traded

    def _portfolio(self, signal: str, now, longs, shorts, traded) -> TargetPortfolio:
        reason = f"mode={self.mode} bar={self.weights_bar} locked={self.locked} trade={traded}"
        return TargetPortfolio(strategy_id=f"rxm_{self.mode}", strategy_version=VERSION, signal_id=signal,
                               timestamp=now.to_pydatetime(), longs=longs, shorts=shorts, reason=reason)

    def _hold(self, snapshot: dict[str, Any], now) -> TargetPortfolio:
        longs_now, shorts_now = _exposure(snapshot)
        return TargetPortfolio(strategy_id=f"rxm_{self.mode}", strategy_version=VERSION, signal_id="hold",
                               timestamp=now.to_pydatetime(), reason="no signal yet: hold the current book",
                               longs=[LongTarget(symbol=c, notional_usd=v) for c, v in longs_now.items() if v > 0],
                               shorts=[ShortTarget(symbol=c, collateral_usd=v) for c, v in shorts_now.items() if v > 0])

    def _emit(self, target: TargetPortfolio, signal: str, bar: pd.Timestamp) -> TargetPortfolio:
        target = target.model_copy(update={"signal_id": signal})
        if self._last is None or self._last.signal_id != signal:
            log.info("signal %s: %s", signal, target.reason)
        self._last, self._last_bar = target, bar
        return target

    # ------------------------------------------------------------------ lock-in
    def _update_lock(self, equity: float, now: pd.Timestamp) -> None:
        if self.state.get("start_equity") is None:
            self.state.update(mode=self.mode, start_equity=equity, start_time=now.isoformat(), locked=False)
            log.info("start equity recorded: %.2f", equity)
            return
        if self.lockin_return <= 0 or self.locked:
            return
        gain = equity / float(self.state.get("start_equity")) - 1
        self._lock_streak = self._lock_streak + 1 if gain >= self.lockin_return else 0
        if self._lock_streak >= self.lock_confirmations:
            self.state.update(locked=True, lock_time=now.isoformat(), lock_return=gain)
            log.warning("LOCK-IN at %+.2f%%: weights x%.2f from now on", 100 * gain, self.lockin_scale)


def _exposure(snapshot: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    """USD per coin held long and short, counting resting orders: buys and short opens add, sells subtract."""
    longs = {c: float(v) for c, v in (snapshot.get("longs") or {}).items()}
    shorts = {c: float(v) for c, v in (snapshot.get("shorts") or {}).items()}
    for order in snapshot.get("pending_orders") or []:
        if str(order.get("Status", "PENDING")).upper() != "PENDING":
            continue
        coin = str(order.get("Pair", "")).split("/")[0].upper()
        usd = float(order.get("Quantity") or 0) * float(order.get("Price") or 0)
        side = str(order.get("Side", "")).upper()
        if side == "BUY":
            longs[coin] = longs.get(coin, 0.0) + usd
        elif side == "SELL":
            longs[coin] = longs.get(coin, 0.0) - usd
        elif side == "SHORT_OPEN":
            shorts[coin] = shorts.get(coin, 0.0) + (float(order.get("Collateral") or 0) or usd)
    return longs, shorts
