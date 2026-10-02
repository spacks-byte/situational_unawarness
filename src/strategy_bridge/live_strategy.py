"""Live adapter: the frozen backtest strategy as an engine ``strategy(snapshot)`` callback.

Signal: identical to the backtest. Weights come from ``ResidualMomentum(**preset(mode)[0])
.generate_weights(buffer.data()).iloc[-1]``; with ``rebalance_h=24`` the last row always equals
the row of the latest 00:00 UTC bar (forward-filled), i.e. the backtest's daily decision taken
at that bar's close (00:15 UTC).

Execution semantics (mirrors backtest/engine.py, see docs/LIVE_RUNBOOK.md):
* daily target at/after 00:15 UTC (and on first start): signal_id ``{mode}-{bar:%Y%m%dT%H%M}``;
* a symbol is only traded if its weight differs from the current one (positions + resting
  orders) by more than ``band`` (1%) or a side must be exited; other symbols are frozen at their
  current size, so the engine leaves them alone;
* between rebalances the same target / signal_id is returned (engine reports DUPLICATE, no churn).
  If, at a new 15m bar, some symbol is still off target by more than ``band`` (e.g. a passive limit
  expired unfilled and was cancelled by the engine), a re-quote ``{base}-r{bar}`` is emitted at the
  new price; the backtest likewise re-places unfilled limits every bar;
* lock-in (comp mode): once equity / start_equity - 1 >= 6%, all weights x0.3 for good. Start
  equity and lock state are persisted in a JSON file so restarts don't reset them.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.strategies.rxm import PRESETS, ResidualMomentum, preset
from src.engine.clock import Clock, RealClock
from src.engine.reconcile.plan import _pending_open_exposure
from src.engine.schema.models import LongTarget, ShortTarget, TargetPortfolio
from src.strategy_bridge.market_data import BAR, BarBuffer, binance_to_coin, last_closed_bar_open

log = logging.getLogger(__name__)

# mode = a frozen preset in backtest/strategies/rxm.py (single source of truth for parameters)
MODES = tuple(PRESETS)
STRATEGY_VERSION = "rxm-v1.1-frozen-2026-10-03"


def mode_spec(mode: str) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    params, cfg = preset(mode)
    return {
        "variant": mode,
        "params": params,
        "limit_offset_bps": float(cfg.get("limit_offset_bps", 5.0)),
        "lockin_return": float(cfg.get("lockin_return", 0.0)),
        "lockin_scale": float(cfg.get("lockin_scale", 1.0)),
    }


def compute_target_weights(data: dict[str, pd.DataFrame], params: dict[str, Any]) -> tuple[pd.Timestamp, pd.Series]:
    """Run the backtest strategy on ``data`` and return (bar time, weights of the last bar)."""
    weights = ResidualMomentum(**params).generate_weights(data)
    return weights.index[-1], weights.iloc[-1]


class SnapshotRejected(RuntimeError):
    """Raised instead of returning a target when the account snapshot looks broken.

    The engine has no no-op return value and diffs every target against the snapshot, so a
    partial read (e.g. get_balance failed -> no longs) would turn into real closing orders.
    Raising aborts the iteration before any order; the runner logs it and polls again.
    """


class StrategyState:
    """Tiny JSON store for start equity and lock-in, written atomically."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def update(self, **values: Any) -> None:
        self.data.update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, self.path)


class CompetitionStrategy:
    """Callable ``strategy(snapshot) -> TargetPortfolio`` for ``Engine.run``."""

    def __init__(
        self,
        buffer: BarBuffer,
        mode: str = "comp",
        *,
        state_path: str | Path = "results/live_state/strategy_state.json",
        clock: Clock | None = None,
        band: float = 0.01,
        gross_cap: float = 0.98,
        requote: bool = True,
        stale_after: pd.Timedelta = pd.Timedelta(hours=2),
        lock_confirmations: int = 2,
        min_trade_usd: float = 20.0,
        max_equity_jump: float = 0.5,
        bar_grace: pd.Timedelta = pd.Timedelta(minutes=30),
    ) -> None:
        spec = mode_spec(mode)
        self.mode = mode
        self.params = spec["params"]
        self.offset = spec["limit_offset_bps"] / 1e4
        self.lockin_return = spec["lockin_return"]
        self.lockin_scale = spec["lockin_scale"]
        self.buffer = buffer
        self.clock = clock or RealClock()
        self.band = band
        self.gross_cap = gross_cap
        self.requote = requote
        self.stale_after = stale_after
        self.lock_confirmations = max(1, int(lock_confirmations))
        self.min_trade_usd = min_trade_usd
        self.max_equity_jump = max_equity_jump
        self.bar_grace = bar_grace
        self._ref_equity: float | None = None
        self.state = StrategyState(state_path)
        if self.state.get("mode") not in (None, mode):
            raise ValueError(f"state file {state_path} belongs to mode {self.state.get('mode')!r}; "
                             "never switch modes mid-event (delete the file only for a fresh start)")
        self.weights: pd.Series | None = None
        self.weights_bar: pd.Timestamp | None = None
        self._base_id: str | None = None
        self._last_target: TargetPortfolio | None = None
        self._last_emit_bar: pd.Timestamp | None = None
        self._lock_streak = 0
        self.events: list[dict[str, Any]] = []      # one entry per newly emitted signal (for demos/ops)

    # ------------------------------------------------------------------ public
    @property
    def locked(self) -> bool:
        return bool(self.state.get("locked", False))

    def __call__(self, snapshot: dict[str, Any]) -> TargetPortfolio:
        now = pd.Timestamp(self.clock.now()).tz_convert("UTC")
        equity = float(snapshot.get("equity_usd", 0.0))
        ref = self._ref_equity or self.state.get("start_equity")
        if equity <= 0 or (ref and abs(equity / float(ref) - 1.0) > self.max_equity_jump):
            log.error("snapshot rejected: equity %.2f vs reference %s (partial API read?)", equity, ref)
            raise SnapshotRejected(f"implausible equity {equity:.2f} (reference {ref})")
        self._ref_equity = equity
        self._update_lock(equity, now)
        self.buffer.update(now)
        self._refresh_weights(now)

        bar_now = now.floor("15min")
        if self.weights is None:
            return self._emit(self._hold_target(snapshot, now), f"{self.mode}-hold-{bar_now:%Y%m%dT%H%M}",
                              bar_now, "no signal yet (insufficient/stale data): hold current book")

        base = f"{self.mode}-{self.weights_bar:%Y%m%dT%H%M}" + ("-L" if self.locked else "")
        longs, shorts, traded, desired = self._build(snapshot)
        if base != self._base_id:
            prev, self._base_id = self._base_id, base
            if self._last_target is None or prev is None:
                reason = "first start"
            elif prev.split("-")[1] == base.split("-")[1]:
                reason = "lock-in: weights scaled"
            else:
                reason = "daily rebalance"
            return self._emit(self._portfolio(base, now, longs, shorts, reason, desired, traded), base, bar_now, reason)
        if self.requote and traded and bar_now != self._last_emit_bar:
            sid = f"{base}-r{bar_now:%Y%m%dT%H%M}"
            reason = f"re-quote off-target symbols {traded}"
            return self._emit(self._portfolio(sid, now, longs, shorts, reason, desired, traded), sid, bar_now, reason)
        return self._last_target   # unchanged: engine treats it as a duplicate signal

    # ------------------------------------------------------------------ signal
    def _refresh_weights(self, now: pd.Timestamp) -> None:
        last = self.buffer.last_bar()
        if last is None:
            return
        if last < last_closed_bar_open(now) - self.stale_after:
            log.warning("market data stale: last bar %s at %s; holding previous target", last, now)
            return
        rebalance_bar = last.floor(f"{int(self.params.get('rebalance_h', 24))}h")
        if self.weights_bar is not None and rebalance_bar <= self.weights_bar:
            return
        # Weights are computed once per rebalance, and a coin without its decision bar is ineligible
        # for that whole day. So give late feeds a short grace before deciding without them.
        late = self.buffer.lagging(rebalance_bar)
        if late and now < rebalance_bar + BAR + self.bar_grace:
            log.info("waiting for the %s bar of %s before computing weights", rebalance_bar, late)
            return
        if late:
            log.warning("computing weights for %s without %s (no bar after %s)", rebalance_bar, late, self.bar_grace)
        bar, row = compute_target_weights(self.buffer.data(), self.params)
        self.weights = row.fillna(0.0)
        self.weights_bar = rebalance_bar
        log.info("new weights for %s (data to %s): %s", rebalance_bar, bar,
                 {k: round(v, 4) for k, v in self.weights[self.weights != 0].items()})

    def scaled_weights(self) -> dict[str, float]:
        """Engine-symbol weights after lock-in scaling and the cash/fee gross cap."""
        if self.weights is None:
            return {}
        scale = self.lockin_scale if self.locked else 1.0
        w = {binance_to_coin(s): float(v) * scale for s, v in self.weights.items() if v != 0}
        gross = sum(abs(v) for v in w.values())
        if gross > self.gross_cap:
            w = {s: v * self.gross_cap / gross for s, v in w.items()}
        return w

    def _build(self, snapshot: dict[str, Any]):
        equity = float(snapshot.get("equity_usd", 0.0))
        prices = snapshot.get("prices", {}) or {}
        pend_l, pend_s = _pending_open_exposure(snapshot.get("pending_orders"))
        cur_l = {s: float(v) + pend_l.get(s, 0.0) for s, v in (snapshot.get("longs") or {}).items()}
        cur_s = {s: float(v) + pend_s.get(s, 0.0) for s, v in (snapshot.get("shorts") or {}).items()}
        for s, v in pend_l.items():
            cur_l.setdefault(s, v)
        for s, v in pend_s.items():
            cur_s.setdefault(s, v)
        desired = self.scaled_weights()
        missing = [s for s in desired if prices.get(s, 0.0) <= 0]
        if missing:
            log.warning("no Roostoo price for %s: not traded", missing)
            desired = {s: v for s, v in desired.items() if s not in missing}

        longs: list[LongTarget] = []
        shorts: list[ShortTarget] = []
        traded: list[str] = []
        for s in sorted(set(desired) | {k for k, v in cur_l.items() if v > 0} | {k for k, v in cur_s.items() if v > 0}):
            w = desired.get(s, 0.0)
            have_l, have_s = cur_l.get(s, 0.0), cur_s.get(s, 0.0)
            cur_w = (have_l - have_s) / equity if equity > 0 else 0.0
            dust = self.min_trade_usd
            exit_side = (w <= 0 and have_l > dust) or (w >= 0 and have_s > dust)
            drift = abs(w - cur_w) > self.band and abs(w - cur_w) * equity > dust
            if equity > 0 and (drift or exit_side):
                traded.append(s)
                price = prices.get(s, 0.0)
                if w > 0:
                    longs.append(LongTarget(symbol=s, weight=min(w, 1.0), limit_price=price * (1 - self.offset)))
                elif w < 0:
                    shorts.append(ShortTarget(symbol=s, collateral_usd=abs(w) * equity, limit_price=price * (1 + self.offset)))
            else:   # within band: freeze at current size so the engine doesn't touch it
                if have_l > 0:
                    longs.append(LongTarget(symbol=s, notional_usd=have_l))
                if have_s > 0:
                    shorts.append(ShortTarget(symbol=s, collateral_usd=have_s))
        return longs, shorts, traded, desired

    def _portfolio(self, sid, now, longs, shorts, reason, desired, traded) -> TargetPortfolio:
        gross = sum(abs(v) for v in desired.values())
        text = (f"{reason} | mode={self.mode} bar={self.weights_bar} locked={self.locked} "
                f"gross={gross:.3f} trade={traded} "
                f"w={ {k: round(v, 4) for k, v in sorted(desired.items(), key=lambda kv: -kv[1])} }")
        return TargetPortfolio(strategy_id=f"rxm_{self.mode}", strategy_version=STRATEGY_VERSION,
                               signal_id=sid, timestamp=now.to_pydatetime(), longs=longs, shorts=shorts, reason=text)

    def _hold_target(self, snapshot: dict[str, Any], now: pd.Timestamp) -> TargetPortfolio:
        longs = [LongTarget(symbol=s, notional_usd=float(v)) for s, v in (snapshot.get("longs") or {}).items() if v > 0]
        shorts = [ShortTarget(symbol=s, collateral_usd=float(v)) for s, v in (snapshot.get("shorts") or {}).items() if v > 0]
        return TargetPortfolio(strategy_id=f"rxm_{self.mode}", strategy_version=STRATEGY_VERSION,
                               signal_id="placeholder", timestamp=now.to_pydatetime(), longs=longs, shorts=shorts,
                               reason="hold")

    def _emit(self, target: TargetPortfolio, sid: str, bar_now: pd.Timestamp, reason: str) -> TargetPortfolio:
        if target.signal_id != sid:
            target = target.model_copy(update={"signal_id": sid})
        if self._last_target is None or self._last_target.signal_id != sid:
            log.info("signal %s: %s", sid, target.reason)
            self.events.append({"time": target.timestamp, "signal_id": sid, "reason": reason,
                                "weights": self.scaled_weights(), "locked": self.locked})
        self._last_target = target
        self._last_emit_bar = bar_now
        return target

    # ------------------------------------------------------------------ lock-in
    def _update_lock(self, equity: float, now: pd.Timestamp) -> None:
        if equity <= 0:
            return
        if self.state.get("start_equity") is None:
            self.state.update(mode=self.mode, start_equity=equity, start_time=now.isoformat(), locked=False)
            log.info("recorded start equity %.2f", equity)
            return
        if self.lockin_return <= 0 or self.locked:
            return
        ret = equity / float(self.state.get("start_equity")) - 1.0
        self._lock_streak = self._lock_streak + 1 if ret >= self.lockin_return else 0
        if self._lock_streak >= self.lock_confirmations:
            self.state.update(locked=True, lock_time=now.isoformat(), lock_equity=equity, lock_return=ret)
            log.warning("LOCK-IN: return %.2f%% >= %.2f%%, weights x%.2f from now on",
                        100 * ret, 100 * self.lockin_return, self.lockin_scale)

