"""Unit tests for the 2026-10-04 execution fixes (book stuck at ~41% of target). No network."""
from datetime import UTC, datetime

import pytest

from tests.test_strategy_bridge import _snapshot, _strategy, _universe
from tradebot.core.config import ExecutionConfig
from tradebot.core.symbols import to_coin
from tradebot.engine.execution.runner import ExecutionRunner
from tradebot.engine.reconcile.plan import pending_open_exposure
from tradebot.engine.schema.models import ShortTarget, TargetPortfolio, Urgency
from tradebot.engine.state.intent_store import IntentJournal
from tradebot.engine.state.snapshot import SnapshotReadError, normalize_exchange_snapshot, read_exchange_snapshot
from tradebot.exchange.mock import MockExchangePort
from tradebot.live.bridge import SnapshotRejected
from tradebot.live.repeg import RepegPort

POSITIONS = {"Positions": [
    {"Pair": "ZEN/USD", "Collateral": 4654.0, "UnrealizedPNL": -10.0, "EntryPrice": 7.0},
    {"Pair": "ZEC/USD", "Collateral": 4335.0, "UnrealizedPNL": -11.0, "EntryPrice": 1330.0},
    {"Pair": "PENDLE/USD", "Collateral": 5083.0, "UnrealizedPNL": -10.0, "EntryPrice": 2.43},
]}
TICKERS = {"Data": {"SUI/USD": {"LastPrice": 1.0}, "ZEN/USD": {"LastPrice": 7.0}}}
COLLATERAL = 4654.0 + 4335.0 + 5083.0
BUY = {"Pair": "SUI/USD", "Side": "BUY", "Status": "PENDING", "Quantity": 10_000.0, "Price": 1.0, "FilledQuantity": 0}
SHORT = {"Pair": "ZEN/USD", "Side": "SHORT_OPEN", "Status": "PENDING", "Quantity": 1_000.0, "Price": 7.0}
RESERVED = 10_000.0 + 7_000.0 * 1.001                          # buy USD + short collateral and fee


def _wallet(free, lock):
    return {"Success": True, "SpotWallet": {"USD": {"Free": free, "Lock": lock}, "SUI": {"Free": 23_597.0, "Lock": 0.0}}}


# ---------------------------------------------------------------------------- equity: USD Lock semantics
def test_open_short_collateral_in_lock_is_not_counted_twice():
    """The live account: UI USD $62,518 + crypto $23,597 + collateral $14,072 - $31 = $100,156."""
    snap = normalize_exchange_snapshot(_wallet(62_518.0, COLLATERAL), POSITIONS, TICKERS)
    assert snap["cash_usd"] == pytest.approx(62_518.0)
    assert snap["equity_usd"] == pytest.approx(62_518.0 + 23_597.0 + COLLATERAL - 31.0)
    assert snap["lock_short_collateral_usd"] == pytest.approx(COLLATERAL) and snap["lock_unexplained_usd"] == 0


@pytest.mark.parametrize("collateral_in_lock", [True, False])
@pytest.mark.parametrize("orders_in_lock", [True, False])
def test_equity_is_the_same_whichever_way_roostoo_reports_lock(collateral_in_lock, orders_in_lock):
    free = 30_000.0
    lock = (RESERVED if orders_in_lock else 0.0) + (COLLATERAL if collateral_in_lock else 0.0)
    snap = normalize_exchange_snapshot(_wallet(free, lock), POSITIONS, TICKERS, [BUY, SHORT])
    assert snap["cash_usd"] == pytest.approx(free + RESERVED)
    assert snap["equity_usd"] == pytest.approx(free + RESERVED + 23_597.0 + COLLATERAL - 31.0)
    assert snap["lock_unexplained_usd"] == pytest.approx(0.0)


def test_lock_nobody_explains_stays_cash_and_is_reported():
    snap = normalize_exchange_snapshot(_wallet(1_000.0, 300.0), POSITIONS, TICKERS)   # 300 < any collateral
    assert snap["cash_usd"] == 1_300.0 and snap["lock_unexplained_usd"] == 300.0


# ---------------------------------------------------------------------------- partial fills
def test_a_partial_fill_counts_only_the_unfilled_rest_as_pending():
    part = dict(BUY, FilledQuantity=4_000.0)                    # 4k already in the wallet
    longs, _ = pending_open_exposure([part])
    assert longs["SUI"] == pytest.approx(6_000.0)
    quirk = dict(BUY, FilledQuantity=10_000.0)                  # API doc rows: PENDING with Filled == Quantity
    assert pending_open_exposure([quirk])[0]["SUI"] == pytest.approx(10_000.0)
    short_part = dict(SHORT, FilledQuantity=250.0, Collateral=7_000.0)
    assert pending_open_exposure([short_part])[1]["ZEN"] == pytest.approx(5_250.0)


# ---------------------------------------------------------------------------- partial reads
class _Port(MockExchangePort):
    def __init__(self, positions):
        super().__init__()
        self._positions = positions

    def get_short_positions(self):
        return self._positions


def test_a_failed_short_positions_read_aborts_the_loop_instead_of_dropping_the_shorts():
    with pytest.raises(SnapshotReadError):
        read_exchange_snapshot(_Port({"Success": False, "ErrMsg": "too many requests"}))
    assert read_exchange_snapshot(_Port({"Success": True, "Positions": []}))["shorts"] == {}


# ---------------------------------------------------------------------------- equity reference latch
def _prices(frames):
    return {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}


def test_a_low_reading_cannot_freeze_the_bot(tmp_path):
    """Before: 65.7k (-34%) was accepted, then the true 100k (+52%) was rejected on every loop."""
    frames = _universe()
    strat, clock = _strategy(tmp_path, frames, datetime(2026, 2, 25, 0, 20, tzinfo=UTC))
    p = _prices(frames)
    strat(_snapshot(equity=100_000, prices=p))
    with pytest.raises(SnapshotRejected):                       # a -34% step is judged like its +52% undo
        strat(_snapshot(equity=65_700, prices=p))
    assert strat(_snapshot(equity=100_100, prices=p))            # true value still accepted
    for _ in range(2):                                           # a level that persists is re-anchored
        with pytest.raises(SnapshotRejected):
            strat(_snapshot(equity=60_000, prices=p))
    assert strat(_snapshot(equity=60_100, prices=p))


def test_lockin_is_not_taken_on_unexplained_usd_lock(tmp_path):
    frames = _universe()
    strat, _ = _strategy(tmp_path, frames, datetime(2026, 2, 25, 0, 20, tzinfo=UTC))
    p = _prices(frames)
    strat(_snapshot(equity=100_000, prices=p))
    for _ in range(4):
        strat(dict(_snapshot(equity=110_000, prices=p), lock_unexplained_usd=10_000.0))
    assert not strat.locked


# ---------------------------------------------------------------------------- escalation ladder
def test_ladder_goes_passive_then_touch_then_cross_and_market_short(tmp_path):
    frames = _universe()
    strat, clock = _strategy(tmp_path, frames, datetime(2026, 2, 25, 0, 20, tzinfo=UTC), escalate=True)
    p = _prices(frames)
    first = strat(_snapshot(prices=p))                           # attempt 0: 5 bp passive
    assert all(t.limit_price == pytest.approx(p[t.symbol] * (1 - 5e-4)) for t in first.longs)
    assert all(t.limit_price == pytest.approx(p[t.symbol] * (1 + 5e-4)) for t in first.shorts)
    clock.advance(16 * 60)
    second = strat(_snapshot(prices=p))                          # nothing filled: attempt 1 at the last price
    assert second.signal_id.endswith("-r20260225T0030")
    assert all(t.limit_price == pytest.approx(p[t.symbol]) for t in second.longs + second.shorts)
    clock.advance(16 * 60)
    third = strat(_snapshot(prices=p))                           # attempt 2: cross
    assert all(t.limit_price == pytest.approx(p[t.symbol] * (1 + 1e-3)) for t in third.longs)
    assert all(t.limit_price is None and t.urgency == Urgency.HIGH for t in third.shorts)


def test_a_trim_rests_above_the_market(tmp_path):
    frames = _universe()
    strat, clock = _strategy(tmp_path, frames, datetime(2026, 2, 25, 0, 20, tzinfo=UTC))
    p = _prices(frames)
    strat(_snapshot(prices=p))
    w = strat.scaled_weights()
    big = max((s for s in w if w[s] > 0), key=lambda s: w[s])
    over = _snapshot(prices=p, longs={big: 2 * w[big] * 100_000})
    clock.advance(16 * 60)
    tgt = strat(over)
    trim = next(t for t in tgt.longs if t.symbol == big)
    assert trim.limit_price == pytest.approx(p[big] * (1 + 5e-4))


def test_high_urgency_short_without_price_goes_at_market_under_limit_only():
    port = MockExchangePort(initial_wallet={"USD": 10_000.0}, tickers={"ETH/USD": 2_000.0})
    runner = ExecutionRunner(port, IntentJournal(memory=True), ExecutionConfig(
        dry_run=False, min_cash_reserve_usd=0, max_total_short_collateral_usd=1e6, max_per_symbol_exposure=1.0))
    target = TargetPortfolio(strategy_id="t", strategy_version="v", signal_id="mkt", timestamp=datetime.now(UTC),
                             shorts=[ShortTarget(symbol="ETH", collateral_usd=1_000.0, urgency=Urgency.HIGH)])
    result = runner.execute(target, {"cash_usd": 10_000.0, "cash_free_usd": 10_000.0, "prices": {"ETH": 2_000.0}}, 10_000.0)
    assert result["operations"][0]["response"]["OrderType"] == "MARKET"
    assert port.short_positions and port.short_positions[0]["Collateral"] == 1_000.0


def test_repeg_keeps_each_orders_own_offset():
    port = MockExchangePort(tickers={"ETH/USD": 2_010.0})          # moved +0.5% since the snapshot
    sent = []
    port.place_order = lambda pair, side, qty, price=None, order_type=None: sent.append((side, qty, price)) or {"Success": True}
    repeg = RepegPort(port, 5.0, reference=lambda: {"ETH": 2_000.0})
    repeg.place_order("ETH/USD", "BUY", 1.0, price=2_002.0, order_type="LIMIT")    # crossing +10 bp
    repeg.place_order("ETH/USD", "SELL", 1.0, price=2_001.0, order_type="LIMIT")   # passive +5 bp
    assert sent[0][2] == pytest.approx(2_010.0 * 1.001) and sent[0][1] == pytest.approx(2_002.0 / (2_010.0 * 1.001))
    assert sent[1][2] == pytest.approx(2_010.0 * 1.0005)


# ---------------------------------------------------------------------------- plan guard sees resting orders
def test_plan_guard_counts_resting_short_opens_before_judging_net(tmp_path):
    from tests.test_live_runner import _runner

    runner, _, _ = _runner(tmp_path)
    prices = {"C1": 100.0, "C2": 100.0, "C3": 100.0}
    actual = {"equity_usd": 100_000.0, "cash_usd": 40_000.0, "cash_free_usd": 6_000.0, "prices": prices,
              "longs": {"C1": 30_000.0, "C3": 30_000.0}, "shorts": {},
              "pending_orders": [{"Pair": "C2/USD", "Side": "SHORT_OPEN", "Status": "PENDING", "Quantity": 340.0,
                                  "Price": 100.0}]}
    plan = {"open_longs": {"C1": 4_000.0}, "open_shorts": {}, "close_longs": {}, "close_shorts": {}}
    runner._price_times = {c: runner.clock.now() for c in prices}
    # without the resting short the book after the plan is net +0.64 > guard net_max 0.60
    assert runner.plan_guard(plan, actual, 100_000.0) == []
    assert runner.plan_guard(plan, dict(actual, pending_orders=[]), 100_000.0) == ["guard:net_exposure"]
