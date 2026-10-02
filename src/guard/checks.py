"""
Pre-trade and real-time guard for the competition bot.

The engine's RiskManager (src/engine/risk/manager.py) gates a *rebalance plan*. This module is a
second, independent line of defence that the bot calls

  * BEFORE sending any batch of orders:  ``guard.pre_trade(snapshot, orders, ...)``
  * on EVERY poll (e.g. each 15m bar):  ``guard.poll(snapshot, ...)``

Every check is a pure function returning a ``CheckResult`` = (name, status, value, limit, message)
with status OK / WARN / BLOCK. ``BLOCK`` means "do not send new orders"; ``WARN`` means "a human
should look". The ``Guard`` class only adds the small amount of state the pure checks need
(peak equity, lock-in latch, API-call timestamps, fill days) and aggregates results.

Design choices worth knowing:
  * Risk-reducing batches are never blocked by exposure limits: an exposure check only BLOCKs when
    the limit is breached AND the batch makes that metric worse. A guard that stops you de-risking
    is worse than no guard.
  * The drawdown monitor is WARN-only. Our research showed drawdown brakes destroy value for this
    strategy (they sell the bottom of mean-reverting dips), so the guard never flattens.
  * The kill switch is a file (default ``KILL`` in the working directory): ``touch KILL`` stops all
    new orders without restarting the bot; ``rm KILL`` resumes.

Snapshot format is the engine's normalized snapshot (src/engine/state/snapshot.py):
``cash_usd``, ``longs`` {sym: USD notional}, ``shorts`` {sym: USD collateral}, ``pending_orders``,
``prices`` {sym: last}, ``entry_prices``, ``equity_usd``. Shorts are 1x, so collateral ~ notional.
"""
from __future__ import annotations

import math
import os
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence


# --------------------------------------------------------------------------------------- types
class Status(str, Enum):
    OK = "OK"
    WARN = "WARN"
    BLOCK = "BLOCK"


_SEVERITY = {Status.OK: 0, Status.WARN: 1, Status.BLOCK: 2}


class CheckResult(NamedTuple):
    name: str
    status: Status
    value: Any
    limit: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "value": _jsonable(self.value),
                "limit": _jsonable(self.limit), "message": self.message}


@dataclass(frozen=True)
class ProposedOrder:
    """One order the bot is about to send.

    side: BUY / SELL (spot), SHORT (open/increase short), COVER (close/reduce short).
    price: limit price, or None for a market order (valued at the last price).
    """

    symbol: str
    side: str
    quantity: float
    price: float | None = None
    order_type: str | None = None

    @property
    def sym(self) -> str:
        return normalize_symbol(self.symbol)

    @property
    def kind(self) -> str:
        return (self.order_type or ("LIMIT" if self.price is not None else "MARKET")).upper()

    def notional(self, last_price: float | None = None) -> float:
        px = self.price if self.price is not None else (last_price or 0.0)
        return abs(float(self.quantity)) * float(px or 0.0)


@dataclass
class GuardConfig:
    """All thresholds in one place. Weights are fractions of equity."""

    initial_equity_usd: float = 100_000.0
    # exposure
    max_gross_exposure: float = 1.0          # no leverage; BLOCK above (+ tolerance) if the batch adds gross
    gross_tolerance: float = 0.005           # fees/rounding slack on the projected gross
    hard_gross_exposure: float = 1.10        # BLOCK on a poll even without orders (something is wrong)
    max_symbol_weight: float = 0.40          # |weight| per symbol, BLOCK
    warn_symbol_weight: float = 0.30
    net_min: float = -0.20                   # BLOCK outside [net_min, net_max]
    net_max: float = 0.60                    # comp mode targets +0.30 (65/35 tilt at gross 1.0)
    net_warn_min: float = -0.05
    net_warn_max: float = 0.45
    max_short_collateral_frac: float = 0.45  # comp mode targets 0.35
    warn_short_collateral_frac: float = 0.40
    max_short_collateral_usd: float | None = None
    # cash
    min_cash_usd: float = 0.0                # projected cash below this = overdraft -> BLOCK
    warn_cash_usd: float = 50.0              # enough for a rebalance's fees
    limit_fee: float = 0.0005
    market_fee: float = 0.001
    short_fee: float = 0.001
    # order sanity
    price_band_warn_pct: float = 0.01        # limit vs last price (strategy uses 5 bp passive)
    price_band_block_pct: float = 0.03       # fat finger
    max_order_notional_usd: float = 50_000.0
    max_order_frac_equity: float = 0.40
    min_order_notional_usd: float = 10.0     # Roostoo MiniOrder; below it the exchange rejects (WARN)
    max_orders_per_minute: int = 20
    warn_orders_per_minute: int = 12
    api_calls_per_minute: int = 30           # Roostoo budget
    api_warn_frac: float = 0.60
    api_block_frac: float = 0.90             # keep 10% headroom for cancels / status queries
    # market data
    stale_warn_seconds: float = 120.0
    stale_block_seconds: float = 300.0
    # monitors (WARN only)
    drawdown_warn: float = 0.05
    drawdown_alert: float = 0.10
    drift_warn: float = 0.03                 # rebalance band is 0.01
    drift_alert: float = 0.10
    # lock-in
    lockin_return: float = 0.06
    lockin_scale: float = 0.30
    base_gross: float = 1.0
    lockin_tolerance: float = 0.02
    # daily heartbeat (every UTC day must have >= 1 fill)
    heartbeat_warn_hour: int = 18
    heartbeat_urgent_hour: int = 22
    # kill switch
    kill_file: str = "KILL"


# ------------------------------------------------------------------------------- helpers
def normalize_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if "/" in s:
        return s.split("/", 1)[0]
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def _num(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _jsonable(v: Any) -> Any:
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (int, str, bool)) or v is None:
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def _worst(*statuses: Status) -> Status:
    return max(statuses, key=lambda s: _SEVERITY[s]) if statuses else Status.OK


def _as_utc(ts: datetime | float | int | str) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, tz=timezone.utc)
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _upper_map(m: Mapping[str, Any] | None) -> dict[str, float]:
    return {normalize_symbol(k): _num(v) for k, v in (m or {}).items()}


def exposures(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Signed weights and gross/net/short figures from a normalized snapshot."""
    eq = _num(snapshot.get("equity_usd"))
    longs = _upper_map(snapshot.get("longs"))
    shorts = _upper_map(snapshot.get("shorts"))
    weights: dict[str, float] = {}
    if eq > 0:
        for s, v in longs.items():
            weights[s] = weights.get(s, 0.0) + max(v, 0.0) / eq
        for s, v in shorts.items():
            weights[s] = weights.get(s, 0.0) - max(v, 0.0) / eq
    long_usd = sum(max(v, 0.0) for v in longs.values())
    short_usd = sum(max(v, 0.0) for v in shorts.values())
    return {
        "equity": eq,
        "weights": weights,
        "gross": (long_usd + short_usd) / eq if eq > 0 else math.inf,
        "net": (long_usd - short_usd) / eq if eq > 0 else 0.0,
        "short_usd": short_usd,
        "cash": _num(snapshot.get("cash_usd")),
    }


def project_snapshot(snapshot: Mapping[str, Any], orders: Sequence[ProposedOrder],
                     config: GuardConfig | None = None) -> dict[str, Any]:
    """Apply proposed orders to the snapshot as if they all fill (worst case for exposure)."""
    cfg = config or GuardConfig()
    prices = _upper_map(snapshot.get("prices"))
    longs = _upper_map(snapshot.get("longs"))
    shorts = _upper_map(snapshot.get("shorts"))
    cash = _num(snapshot.get("cash_usd"))
    equity = _num(snapshot.get("equity_usd"))
    for o in orders:
        s, side = o.sym, o.side.upper()
        n = o.notional(prices.get(s))
        fee = cfg.limit_fee if o.kind == "LIMIT" else cfg.market_fee
        if side == "BUY":
            longs[s] = longs.get(s, 0.0) + n
            cash -= n * (1 + fee)
            equity -= n * fee
        elif side == "SELL":
            longs[s] = max(longs.get(s, 0.0) - n, 0.0)
            cash += n * (1 - fee)
            equity -= n * fee
        elif side == "SHORT":
            shorts[s] = shorts.get(s, 0.0) + n
            cash -= n * (1 + cfg.short_fee)
            equity -= n * cfg.short_fee
        elif side in ("COVER", "CLOSE_SHORT"):
            shorts[s] = max(shorts.get(s, 0.0) - n, 0.0)
            cash += n * (1 - cfg.market_fee)
            equity -= n * cfg.market_fee
    return {**dict(snapshot), "longs": longs, "shorts": shorts, "cash_usd": cash, "equity_usd": equity}


# ------------------------------------------------------------------------- pure checks
def check_kill_switch(kill_file: str | os.PathLike | None, engaged: bool = False) -> CheckResult:
    present = bool(kill_file) and Path(kill_file).exists()
    if present or engaged:
        why = f"kill file '{kill_file}' present" if present else "kill switch engaged programmatically"
        return CheckResult("kill_switch", Status.BLOCK, True, False, f"{why}: all new orders blocked")
    return CheckResult("kill_switch", Status.OK, False, False, f"no kill file ('{kill_file}')")


def check_gross_exposure(gross: float, limit: float = 1.0, tolerance: float = 0.005,
                         before: float | None = None, hard_limit: float = 1.10) -> CheckResult:
    """BLOCK if gross > limit(+tol) and the batch increases it (or above the hard limit on a poll)."""
    if not math.isfinite(gross):
        return CheckResult("gross_exposure", Status.BLOCK, gross, limit, "equity <= 0")
    worsening = before is None or gross > before + 1e-9
    if gross > limit + tolerance and before is not None and worsening:
        return CheckResult("gross_exposure", Status.BLOCK, gross, limit,
                           f"projected gross {gross:.3f} > {limit:.2f} (leverage) and batch adds {gross - before:+.3f}")
    if before is None and gross > hard_limit:
        return CheckResult("gross_exposure", Status.BLOCK, gross, limit,
                           f"gross {gross:.3f} above hard limit {hard_limit:.2f}: block new risk, reduce")
    if gross > limit:
        return CheckResult("gross_exposure", Status.WARN, gross, limit,
                           f"gross {gross:.3f} > {limit:.2f}" + ("" if worsening else " (batch reduces it)"))
    return CheckResult("gross_exposure", Status.OK, gross, limit, f"gross {gross:.3f} <= {limit:.2f}")


def check_symbol_weights(weights: Mapping[str, float], limit: float = 0.40, warn: float = 0.30,
                         before: Mapping[str, float] | None = None) -> CheckResult:
    if not weights:
        return CheckResult("symbol_weight", Status.OK, 0.0, limit, "no positions")
    sym, w = max(weights.items(), key=lambda kv: abs(kv[1]))
    a = abs(w)
    if a > limit:
        prev = abs((before or {}).get(sym, 0.0)) if before is not None else None
        if prev is None or a > prev + 1e-9:
            return CheckResult("symbol_weight", Status.BLOCK, a, limit, f"{sym} |w|={a:.3f} > cap {limit:.2f}")
        return CheckResult("symbol_weight", Status.WARN, a, limit, f"{sym} |w|={a:.3f} > cap, batch reduces it")
    if a > warn:
        return CheckResult("symbol_weight", Status.WARN, a, limit, f"{sym} |w|={a:.3f} > warn {warn:.2f}")
    return CheckResult("symbol_weight", Status.OK, a, limit, f"max {sym} |w|={a:.3f}")


def check_net_exposure(net: float, lo: float = -0.20, hi: float = 0.60, warn_lo: float = -0.05,
                       warn_hi: float = 0.45, before: float | None = None) -> CheckResult:
    limit = [lo, hi]
    if net < lo or net > hi:
        worsening = before is None or (net < lo and net < before - 1e-9) or (net > hi and net > before + 1e-9)
        if worsening:
            return CheckResult("net_exposure", Status.BLOCK, net, limit, f"net {net:+.3f} outside [{lo:+.2f}, {hi:+.2f}]")
        return CheckResult("net_exposure", Status.WARN, net, limit, f"net {net:+.3f} outside bounds, batch reduces it")
    if net < warn_lo or net > warn_hi:
        return CheckResult("net_exposure", Status.WARN, net, limit, f"net {net:+.3f} outside warn band [{warn_lo:+.2f}, {warn_hi:+.2f}]")
    return CheckResult("net_exposure", Status.OK, net, limit, f"net {net:+.3f}")


def check_short_collateral(short_usd: float, equity: float, max_frac: float = 0.45, warn_frac: float = 0.40,
                           max_usd: float | None = None, before_usd: float | None = None) -> CheckResult:
    frac = short_usd / equity if equity > 0 else math.inf
    cap_usd = min(max_frac * equity, max_usd) if max_usd is not None else max_frac * equity
    if short_usd > cap_usd + 1e-6:
        if before_usd is None or short_usd > before_usd + 1e-6:
            return CheckResult("short_collateral", Status.BLOCK, short_usd, cap_usd,
                               f"short collateral ${short_usd:,.0f} ({frac:.1%}) > cap ${cap_usd:,.0f}")
        return CheckResult("short_collateral", Status.WARN, short_usd, cap_usd, "above cap, batch reduces it")
    if frac > warn_frac:
        return CheckResult("short_collateral", Status.WARN, short_usd, cap_usd, f"short collateral {frac:.1%} > {warn_frac:.0%}")
    return CheckResult("short_collateral", Status.OK, short_usd, cap_usd, f"short collateral {frac:.1%} of equity")


def check_cash_buffer(cash: float, min_cash: float = 0.0, warn_cash: float = 50.0,
                      before: float | None = None) -> CheckResult:
    if cash < min_cash and (before is None or cash < before - 1e-9):
        return CheckResult("cash_buffer", Status.BLOCK, cash, min_cash, f"projected cash ${cash:,.2f} < ${min_cash:,.0f} (overdraft)")
    if cash < warn_cash:
        return CheckResult("cash_buffer", Status.WARN, cash, min_cash, f"cash ${cash:,.2f} < ${warn_cash:,.0f} fee buffer")
    return CheckResult("cash_buffer", Status.OK, cash, min_cash, f"cash ${cash:,.2f}")


def check_price_band(orders: Sequence[ProposedOrder], prices: Mapping[str, float],
                     warn_pct: float = 0.01, block_pct: float = 0.03) -> CheckResult:
    """Fat-finger: every limit price within block_pct of the last price."""
    px = _upper_map(prices)
    worst, worst_sym, missing = 0.0, "", []
    for o in orders:
        if o.price is None:
            continue
        last = px.get(o.sym, 0.0)
        if last <= 0:
            missing.append(o.sym)
            continue
        dev = abs(o.price / last - 1)
        if dev > worst:
            worst, worst_sym = dev, o.sym
    if missing:
        return CheckResult("price_band", Status.BLOCK, None, block_pct, f"no last price for {sorted(set(missing))}")
    if worst > block_pct:
        return CheckResult("price_band", Status.BLOCK, worst, block_pct, f"{worst_sym} limit {worst:.2%} from last (fat finger)")
    if worst > warn_pct:
        return CheckResult("price_band", Status.WARN, worst, block_pct, f"{worst_sym} limit {worst:.2%} from last")
    return CheckResult("price_band", Status.OK, worst, block_pct,
                       f"max deviation {worst:.2%}" if orders else "no orders")


def check_order_notional(orders: Sequence[ProposedOrder], prices: Mapping[str, float], equity: float,
                         max_usd: float = 50_000.0, max_frac: float = 0.40, min_usd: float = 10.0) -> CheckResult:
    px = _upper_map(prices)
    cap = min(max_usd, max_frac * equity) if equity > 0 else max_usd
    if not orders:
        return CheckResult("order_notional", Status.OK, 0.0, cap, "no orders")
    sized = [(o.notional(px.get(o.sym)), o) for o in orders]
    big, bo = max(sized, key=lambda t: t[0])
    small, so = min(sized, key=lambda t: t[0])
    if big > cap:
        return CheckResult("order_notional", Status.BLOCK, big, cap, f"{bo.side} {bo.sym} ${big:,.0f} > cap ${cap:,.0f}")
    if small < min_usd:
        return CheckResult("order_notional", Status.WARN, small, cap, f"{so.side} {so.sym} ${small:,.2f} < exchange min ${min_usd:,.0f}")
    return CheckResult("order_notional", Status.OK, big, cap, f"largest ${big:,.0f}")


def _pending_sides(pending: Iterable[Mapping[str, Any]] | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in pending or []:
        sym = normalize_symbol(p.get("Pair") or p.get("symbol") or "")
        side = str(p.get("Side") or p.get("side") or "").upper()
        if sym and side:
            out.setdefault(sym, set()).add(_book_side(side))
    return out


def _book_side(side: str) -> str:
    """Which side of the book an order sits on: buy-ish (BUY, COVER) vs sell-ish (SELL, SHORT)."""
    return "B" if side.upper() in ("BUY", "COVER", "CLOSE_SHORT") else "S"


def check_self_cross(orders: Sequence[ProposedOrder], pending_orders: Iterable[Mapping[str, Any]] | None = None) -> CheckResult:
    """No simultaneous buy and sell on one symbol (incl. resting orders): looks like market making / wash."""
    sides = _pending_sides(pending_orders)
    for o in orders:
        sides.setdefault(o.sym, set()).add(_book_side(o.side))
    both = sorted(s for s, v in sides.items() if len(v) > 1)
    if both:
        return CheckResult("self_cross", Status.BLOCK, both, [], f"buy and sell on the same symbol: {', '.join(both)}")
    return CheckResult("self_cross", Status.OK, [], [], "one side per symbol")


def check_order_rate(orders_last_minute: int, new_orders: int, limit: int = 20, warn: int = 12) -> CheckResult:
    n = orders_last_minute + new_orders
    if n > limit:
        return CheckResult("order_rate", Status.BLOCK, n, limit, f"{n} orders in 60s > {limit}: split the batch")
    if n > warn:
        return CheckResult("order_rate", Status.WARN, n, limit, f"{n} orders in 60s")
    return CheckResult("order_rate", Status.OK, n, limit, f"{n} orders in 60s")


def check_api_budget(calls_last_minute: int, new_calls: int, budget: int = 30,
                     warn_frac: float = 0.60, block_frac: float = 0.90) -> CheckResult:
    n = calls_last_minute + new_calls
    hard = int(budget * block_frac)
    if n > hard:
        return CheckResult("api_budget", Status.BLOCK, n, budget, f"{n} calls/min would exceed {hard} ({block_frac:.0%} of {budget})")
    if n > budget * warn_frac:
        return CheckResult("api_budget", Status.WARN, n, budget, f"{n}/{budget} calls/min")
    return CheckResult("api_budget", Status.OK, n, budget, f"{n}/{budget} calls/min")


def check_stale_data(price_times: Mapping[str, Any] | None, now: datetime, symbols: Iterable[str] | None = None,
                     warn_s: float = 120.0, block_s: float = 300.0) -> CheckResult:
    if not price_times:
        return CheckResult("stale_data", Status.WARN, None, block_s, "no price timestamps supplied")
    times = {normalize_symbol(k): _as_utc(v) for k, v in price_times.items()}
    wanted = {normalize_symbol(s) for s in symbols} if symbols else set(times)
    missing = sorted(s for s in wanted if s not in times)
    if missing:
        return CheckResult("stale_data", Status.BLOCK, None, block_s, f"no price timestamp for {missing}")
    if not wanted:
        return CheckResult("stale_data", Status.OK, 0.0, block_s, "no symbols to check")
    sym = max(wanted, key=lambda s: (now - times[s]).total_seconds())
    age = (now - times[sym]).total_seconds()
    if age > block_s:
        return CheckResult("stale_data", Status.BLOCK, age, block_s, f"{sym} last price {age:,.0f}s old")
    if age > warn_s:
        return CheckResult("stale_data", Status.WARN, age, block_s, f"{sym} last price {age:,.0f}s old")
    return CheckResult("stale_data", Status.OK, age, block_s, f"oldest price {age:,.0f}s ({sym})")


def check_drawdown(equity: float, peak: float, warn: float = 0.05, alert: float = 0.10) -> CheckResult:
    """WARN only, by design: no automatic de-risking on drawdown."""
    dd = equity / peak - 1 if peak > 0 else 0.0
    if -dd >= alert:
        return CheckResult("drawdown", Status.WARN, dd, -alert, f"ALERT drawdown {dd:.2%} from peak (monitor only, no auto-flatten)")
    if -dd >= warn:
        return CheckResult("drawdown", Status.WARN, dd, -alert, f"drawdown {dd:.2%} from peak (monitor only)")
    return CheckResult("drawdown", Status.OK, dd, -alert, f"drawdown {dd:.2%}")


def check_lockin(return_since_start: float, locked: bool, target_gross: float | None, actual_gross: float,
                 lockin_return: float = 0.06, scale: float = 0.30, base_gross: float = 1.0,
                 tolerance: float = 0.02, bot_locked: bool | None = None) -> CheckResult:
    cap = scale * base_gross + tolerance
    if locked:
        if target_gross is not None and target_gross > cap:
            return CheckResult("lockin", Status.BLOCK, target_gross, cap,
                               f"LOCKED but target gross {target_gross:.2f} > {cap:.2f}: strategy not scaling")
        if bot_locked is False:
            return CheckResult("lockin", Status.WARN, actual_gross, cap, "guard latched lock-in but bot reports unlocked")
        if actual_gross > cap:
            return CheckResult("lockin", Status.WARN, actual_gross, cap, f"LOCKED, de-risking: gross {actual_gross:.2f} -> {scale * base_gross:.2f}")
        return CheckResult("lockin", Status.OK, actual_gross, cap, f"LOCKED at {scale:.0%} (return {return_since_start:+.2%})")
    if bot_locked:
        return CheckResult("lockin", Status.WARN, return_since_start, lockin_return, "bot reports locked but return never reached threshold")
    return CheckResult("lockin", Status.OK, return_since_start, lockin_return,
                       f"armed: {return_since_start:+.2%} vs +{lockin_return:.0%} trigger")


def check_drift(actual: Mapping[str, float], target: Mapping[str, float] | None,
                warn: float = 0.03, alert: float = 0.10) -> CheckResult:
    if target is None:
        return CheckResult("target_drift", Status.OK, None, warn, "no target supplied")
    a, t = _upper_map(actual), _upper_map(target)
    drifts = {s: a.get(s, 0.0) - t.get(s, 0.0) for s in set(a) | set(t)}
    if not drifts:
        return CheckResult("target_drift", Status.OK, 0.0, warn, "flat and no target")
    sym, d = max(drifts.items(), key=lambda kv: abs(kv[1]))
    if abs(d) >= alert:
        return CheckResult("target_drift", Status.WARN, abs(d), warn, f"ALERT {sym} drift {d:+.3f} (orders unfilled?)")
    if abs(d) >= warn:
        return CheckResult("target_drift", Status.WARN, abs(d), warn, f"{sym} drift {d:+.3f}")
    return CheckResult("target_drift", Status.OK, abs(d), warn, f"max drift {abs(d):.3f} ({sym})")


def check_heartbeat(fill_days: Iterable[Any], now: datetime, warn_hour: int = 18, urgent_hour: int = 22) -> CheckResult:
    """At least one fill per UTC day so every competition day counts as trading activity."""
    today = now.astimezone(timezone.utc).date()
    days = {_as_utc(d).date() if isinstance(d, (datetime, str, int, float)) else d for d in fill_days}
    if today in days:
        return CheckResult("daily_heartbeat", Status.OK, True, 1, f"filled today ({today})")
    left = 24 - now.astimezone(timezone.utc).hour
    if now.astimezone(timezone.utc).hour >= urgent_hour:
        return CheckResult("daily_heartbeat", Status.WARN, False, 1, f"URGENT: no fill on {today}, <{left}h left: send a min-size trade")
    if now.astimezone(timezone.utc).hour >= warn_hour:
        return CheckResult("daily_heartbeat", Status.WARN, False, 1, f"no fill yet on {today} ({left}h left)")
    return CheckResult("daily_heartbeat", Status.OK, False, 1, f"no fill yet on {today}, {left}h left")


# --------------------------------------------------------------------------- the Guard
@dataclass
class GuardReport:
    results: list[CheckResult]
    timestamp: datetime
    mode: str = "poll"

    @property
    def status(self) -> Status:
        return _worst(*(r.status for r in self.results))

    @property
    def allowed(self) -> bool:
        return self.status is not Status.BLOCK

    @property
    def blocks(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.BLOCK]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.WARN]

    def __getitem__(self, name: str) -> CheckResult:
        for r in self.results:
            if r.name == name:
                return r
        raise KeyError(name)

    def summary(self) -> str:
        bad = [f"{r.status.value}:{r.name}" for r in self.results if r.status is not Status.OK]
        return f"[guard {self.mode}] {self.status.value}" + (f" ({', '.join(bad)})" if bad else "")

    def to_dict(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp.isoformat(), "mode": self.mode, "status": self.status.value,
                "allowed": self.allowed, "results": [r.to_dict() for r in self.results]}


class Guard:
    """Stateful wrapper: call ``pre_trade`` before every batch and ``poll`` on every loop."""

    def __init__(self, config: GuardConfig | None = None, *, now: Callable[[], datetime] | None = None) -> None:
        self.config = config or GuardConfig()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.peak_equity = self.config.initial_equity_usd
        self.locked = False
        self.lock_time: datetime | None = None
        self.killed = False
        self._api_calls: deque[datetime] = deque()
        self._orders: deque[datetime] = deque()
        self.fill_days: set = set()

    # ---- state the bot feeds in
    def record_api_call(self, ts: datetime | None = None, n: int = 1) -> None:
        for _ in range(n):
            self._api_calls.append(ts or self._now())

    def record_orders_sent(self, n: int = 1, ts: datetime | None = None) -> None:
        """Count n orders as sent (each is also one API call)."""
        t = ts or self._now()
        for _ in range(n):
            self._orders.append(t)
            self._api_calls.append(t)

    def record_fill(self, ts: datetime | None = None) -> None:
        self.fill_days.add((ts or self._now()).astimezone(timezone.utc).date())

    def kill(self) -> None:
        self.killed = True

    def reset_kill(self) -> None:
        self.killed = False

    def _in_last_minute(self, q: deque, now: datetime) -> int:
        cutoff = now - timedelta(seconds=60)
        kept = [t for t in q if t > cutoff]
        q.clear()
        q.extend(kept)
        return sum(1 for t in kept if t <= now)

    def observe_equity(self, equity: float, ts: datetime | None = None) -> float:
        """Feed an equity observation (e.g. history replay on restart): updates peak and lock-in latch."""
        return self._update_equity_state(float(equity), ts or self._now())

    def _update_equity_state(self, equity: float, now: datetime) -> float:
        self.peak_equity = max(self.peak_equity, equity)
        ret = equity / self.config.initial_equity_usd - 1 if self.config.initial_equity_usd > 0 else 0.0
        if not self.locked and self.config.lockin_return > 0 and ret >= self.config.lockin_return:
            self.locked, self.lock_time = True, now
        return ret

    # ---- entry points
    def pre_trade(self, snapshot: Mapping[str, Any], orders: Sequence[ProposedOrder], *,
                  target_weights: Mapping[str, float] | None = None,
                  price_times: Mapping[str, Any] | None = None,
                  bot_locked: bool | None = None, now: datetime | None = None) -> GuardReport:
        return self.evaluate(snapshot, orders, target_weights=target_weights, price_times=price_times,
                             bot_locked=bot_locked, now=now, project=True)

    def poll(self, snapshot: Mapping[str, Any], *, target_weights: Mapping[str, float] | None = None,
             price_times: Mapping[str, Any] | None = None, bot_locked: bool | None = None,
             now: datetime | None = None) -> GuardReport:
        return self.evaluate(snapshot, (), target_weights=target_weights, price_times=price_times,
                             bot_locked=bot_locked, now=now, project=False)

    def evaluate(self, snapshot: Mapping[str, Any], orders: Sequence[ProposedOrder] = (), *,
                 target_weights: Mapping[str, float] | None = None,
                 price_times: Mapping[str, Any] | None = None, bot_locked: bool | None = None,
                 now: datetime | None = None, project: bool = True) -> GuardReport:
        """Run every check. With project=True, exposure checks use the post-trade snapshot."""
        c = self.config
        now = now or self._now()
        orders = list(orders)
        cur = exposures(snapshot)
        post = exposures(project_snapshot(snapshot, orders, c)) if (project and orders) else cur
        prior = cur if (project and orders) else None
        ret = self._update_equity_state(cur["equity"], now)
        tgt_gross = sum(abs(v) for v in target_weights.values()) if target_weights is not None else (
            post["gross"] if (project and orders) else None)
        involved = {o.sym for o in orders} | set(cur["weights"])
        prices = snapshot.get("prices") or {}

        results = [
            check_kill_switch(c.kill_file, self.killed),
            check_gross_exposure(post["gross"], c.max_gross_exposure, c.gross_tolerance,
                                 before=prior["gross"] if prior else None, hard_limit=c.hard_gross_exposure),
            check_symbol_weights(post["weights"], c.max_symbol_weight, c.warn_symbol_weight,
                                 before=prior["weights"] if prior else None),
            check_net_exposure(post["net"], c.net_min, c.net_max, c.net_warn_min, c.net_warn_max,
                               before=prior["net"] if prior else None),
            check_short_collateral(post["short_usd"], post["equity"], c.max_short_collateral_frac,
                                   c.warn_short_collateral_frac, c.max_short_collateral_usd,
                                   before_usd=prior["short_usd"] if prior else None),
            check_cash_buffer(post["cash"], c.min_cash_usd, c.warn_cash_usd, before=prior["cash"] if prior else None),
            check_price_band(orders, prices, c.price_band_warn_pct, c.price_band_block_pct),
            check_order_notional(orders, prices, cur["equity"], c.max_order_notional_usd,
                                 c.max_order_frac_equity, c.min_order_notional_usd),
            check_self_cross(orders, snapshot.get("pending_orders")),
            check_order_rate(self._in_last_minute(self._orders, now), len(orders),
                             c.max_orders_per_minute, c.warn_orders_per_minute),
            check_api_budget(self._in_last_minute(self._api_calls, now), len(orders),
                             c.api_calls_per_minute, c.api_warn_frac, c.api_block_frac),
            check_stale_data(price_times, now, involved or None, c.stale_warn_seconds, c.stale_block_seconds),
            check_drawdown(cur["equity"], self.peak_equity, c.drawdown_warn, c.drawdown_alert),
            check_lockin(ret, self.locked, tgt_gross, cur["gross"], c.lockin_return, c.lockin_scale,
                         c.base_gross, c.lockin_tolerance, bot_locked),
            check_drift(cur["weights"], target_weights, c.drift_warn, c.drift_alert),
            check_heartbeat(self.fill_days, now, c.heartbeat_warn_hour, c.heartbeat_urgent_hour),
        ]
        return GuardReport(results, now, "pre_trade" if (project and orders) else "poll")


def config_table(config: GuardConfig | None = None) -> dict[str, Any]:
    return asdict(config or GuardConfig())
