"""Regression for the 2026-10-04 live incident: the book sat at ~41% of target with 62% cash.

Roostoo keeps an OPEN short's collateral in USD Lock ("the remaining collateral stays locked until
the position is fully closed"). The snapshot added the whole Lock to cash AND the collateral as the
short position, so equity read ~$134k on a $100k account once the shorts filled, the +6% lock-in
fired on two polls, and every weight was cut x0.3 for good: 0.3 x 134k / (0.98 x 100k) = 41%.

Runs the real LiveRunner on the replay exchange with that wallet behaviour. Nothing touches the network.
"""
from datetime import UTC, datetime

import pandas as pd

from tests.test_live_runner import _settings
from tests.test_strategy_bridge import _frame_fetch, _universe
from tradebot.core.clock import SimClock
from tradebot.exchange.replay import ReplayExchangePort
from tradebot.live.runner import LiveRunner

START = datetime(2026, 2, 25, 0, 16, tzinfo=UTC)


class CollateralInLockPort(ReplayExchangePort):
    """USD Lock = resting buys + resting short opens (collateral + fee) + OPEN short collateral."""

    def get_balance(self):
        out = super().get_balance()
        usd = out["SpotWallet"]["USD"]
        usd["Lock"] += sum(o["OpenFee"] for o in self.orders if o["Side"] == "SHORT_OPEN")
        usd["Lock"] += sum(p["Collateral"] for p in self.shorts.values())
        return out


class NeverFillsPassivePort(CollateralInLockPort):
    """Worst case for passive execution: resting limits are never hit; a buy at/above the last price
    fills at once (taker), market short opens fill at once."""

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        last = self.last_price(pair_or_coin if "/" in str(pair_or_coin) else f"{pair_or_coin}/USD")
        if str(side).upper() == "BUY" and price is not None and float(price) >= last:
            return super().place_order(pair_or_coin, side, float(quantity) * float(price) / last, None, "MARKET")
        return super().place_order(pair_or_coin, side, quantity, price, order_type)

    def _try_fill(self, order, bar, ts):
        return False


def _run(tmp_path, port_cls, loops):
    frames = _universe(n=10, days=70, end="2026-03-01")
    clock = SimClock(START)
    sim = port_cls(frames, clock, initial_usd=100_000)
    runner = LiveRunner(_settings(tmp_path, universe=list(frames)), mode="simulate", port=sim,
                        fetch=_frame_fetch(frames), clock=clock)
    history = []
    for _ in range(loops):
        runner.run_once()
        history.append((pd.Timestamp(clock.now()), sim.equity(), dict(runner.last_snapshot or {}), runner.strategy.locked))
        clock.sleep(60)
    return runner, sim, history


def test_open_short_collateral_in_lock_never_trips_the_lockin(tmp_path):
    runner, sim, history = _run(tmp_path, CollateralInLockPort, loops=120)
    assert sim.shorts, "the shorts must have filled for the double count to show"
    assert not runner.strategy.locked and not runner.strategy.state.get("locked")
    for _, true_equity, snap, _ in history[5:]:
        assert abs(snap["equity_usd"] / true_equity - 1) < 0.005      # bot equity == exchange equity
    weights = runner.strategy.scaled_weights()
    assert sum(abs(v) for v in weights.values()) > 0.9                  # full gross, not x0.3


def test_book_reaches_target_within_an_hour_even_if_passive_limits_never_fill(tmp_path):
    runner, sim, history = _run(tmp_path, NeverFillsPassivePort, loops=90)
    w = runner.strategy.scaled_weights()
    first_order = min(o["CreateTimestamp"] for o in sim.history)
    t0 = pd.Timestamp(first_order, unit="ms", tz="UTC")
    target = sum(abs(v) for v in w.values())
    reached = None
    for t, equity, snap, _ in history:
        invested = (sum(snap.get("longs", {}).values()) + sum(snap.get("shorts", {}).values())) / snap["equity_usd"]
        if invested >= 0.95 * target:
            reached = t
            break
    assert reached is not None and reached - t0 <= pd.Timedelta(minutes=60), (reached, t0)
    assert not runner.strategy.locked
