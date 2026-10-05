"""Red-team findings (Oct 5 2026 review of 95e0763). Each test fails on 95e0763 and passes with the fix.
Nothing here touches the network."""
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from tests.test_live_runner import _runner
from tests.test_strategy_bridge import _snapshot, _strategy, _universe, load_competition_config
from tradebot.core.clock import SimClock
from tradebot.core.symbols import to_coin
from tradebot.engine.execution.runner import ExecutionRunner
from tradebot.engine.reconcile.plan import compute_rebalance_plan
from tradebot.engine.state.intent_store import IntentJournal, IntentRecord
from tradebot.exchange.mock import MockExchangePort
from tradebot.live.bridge import SnapshotRejected
from tradebot.live.guard import Guard, GuardConfig
from tradebot.live.runner import plan_orders

NOW = datetime(2026, 2, 25, 0, 20, tzinfo=UTC)
LONG_W, SHORT_W = 0.65 / 3 * 0.98, 0.35 / 3 * 0.98      # comp mode, equal vol, gross capped at 0.98


def _book_after_short_profit(equity0=97_000.0, long_move=-0.03, short_pnl=5_000.0):
    """A full RXM comp book bought at equity0; since then longs moved `long_move` and shorts made `short_pnl`."""
    longs = {s: LONG_W * equity0 * (1 + long_move) for s in ("A", "B", "C")}
    shorts = {s: SHORT_W * equity0 for s in ("D", "E", "F")}            # collateral = notional at entry
    cash = equity0 - LONG_W * equity0 * 3 - SHORT_W * equity0 * 3
    equity = cash + sum(longs.values()) + sum(shorts.values()) + short_pnl
    prices = {s: 100.0 for s in "ABCDEFGH"}
    return {"cash_usd": cash, "cash_free_usd": cash, "longs": longs, "shorts": shorts, "equity_usd": equity,
            "prices": prices, "entry_prices": {}, "pending_orders": []}


# ------------------------------------------------------------------ 1. cash_reserve freezes the book
def test_profitable_shorts_do_not_freeze_the_daily_rebalance(tmp_path):
    """Short P&L is in equity (so in every target) but not in cash until the short is closed. With a full
    book and shorts up ~15%, the plan needs more cash than exists and the RiskManager rejected the WHOLE
    plan - exits and short closes included - on every loop (replay: Jul 19-21 2026, 48 h, 2 rebalances lost)."""
    strat, _ = _strategy(tmp_path, _universe(), NOW)
    snap = _book_after_short_profit()
    # new day: C leaves the long book for G, F leaves the short book for H; A, B, D, E stay
    strat.weights = pd.Series({"A": LONG_W, "B": LONG_W, "G": LONG_W, "D": -SHORT_W, "E": -SHORT_W, "H": -SHORT_W}) / 0.98
    longs, shorts, traded, desired, _ = strat._build(snap)
    target = strat._portfolio("day2", pd.Timestamp(NOW), longs, shorts, "daily rebalance", desired, traded)

    runner = ExecutionRunner(MockExchangePort(), IntentJournal(memory=True), load_competition_config(dry_run=True))
    guard = Guard(GuardConfig(kill_file=str(tmp_path / "KILL"), max_symbol_weight=0.6, lockin_return=0.0),
                  now=lambda: NOW)

    def plan_guard(plan, actual, equity):          # LiveRunner.plan_guard, minus the order-only checks
        report = guard.pre_trade(actual, plan_orders(plan, actual["prices"]), price_times={s: NOW for s in actual["prices"]})
        return [f"guard:{r.name}" for r in report.blocks if r.name in ("cash_buffer", "gross_exposure", "net_exposure")]

    runner.plan_check = plan_guard
    result = runner.execute(target, snap, snap["equity_usd"])
    assert result["status"] == "EXECUTED", result.get("reasons")
    done = {(op["kind"], op["symbol"]) for op in result["operations"]}
    assert {("close_long", "C"), ("close_short", "F")} <= done      # the exits are never held hostage


# ------------------------------------------------------------------ 2. a missing ticker closes shorts
def test_missing_roostoo_price_never_closes_a_short(tmp_path):
    """bridge._build drops a coin without a Roostoo price from `desired`, but a short in it is still 'held',
    so its weight reads as 0 -> exit -> close_short(close_pct=100) at market. One ticker gap = a full
    market close (0.1% fee + spread) of a short the strategy still wants, re-opened next bar."""
    frames = _universe()
    strat, _ = _strategy(tmp_path, frames, NOW)
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    strat(_snapshot(prices=prices))
    w = strat.scaled_weights()
    short_sym = min(w, key=w.get)
    eq = 100_000.0
    book = _snapshot(equity=eq, prices=prices, longs={s: v * eq for s, v in w.items() if v > 0},
                     shorts={s: -v * eq for s, v in w.items() if v < 0})
    gap = dict(book, prices={s: p for s, p in prices.items() if s != short_sym})   # ticker misses one pair
    longs, shorts, traded, desired, _ = strat._build(gap)
    target = strat._portfolio("gap", pd.Timestamp(NOW), longs, shorts, "re-quote", desired, traded)
    plan = compute_rebalance_plan(target, gap, eq)
    assert short_sym not in plan["close_shorts"], f"would market-close the {short_sym} short"


# ------------------------------------------------------------------ 3. one bad snapshot poisons the reference
def test_one_partial_snapshot_does_not_reject_every_later_loop(tmp_path):
    """The jump check compares with the LAST ACCEPTED equity. A partial read that drops equity by 33-50%
    (e.g. ticker without the pairs of a ~40% long book) passes, becomes the reference, and every correct
    snapshot after it is then 'a +67% jump': SnapshotRejected on every loop until someone restarts the bot
    (status.json keeps updating, so the documented watchdog does not fire)."""
    frames = _universe()
    strat, _ = _strategy(tmp_path, frames, NOW)
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    strat(_snapshot(equity=100_000, prices=prices))
    with pytest.raises(SnapshotRejected):                      # the partial read itself is refused now:
        strat(_snapshot(equity=60_000, prices=prices))        # -40% needs +67% to undo, outside the band
    accepted = 0
    for _ in range(10):                                        # ten minutes of correct snapshots
        try:
            strat(_snapshot(equity=100_000, prices=prices))
            accepted += 1
        except SnapshotRejected:
            pass
    assert accepted >= 5, "bot stays frozen after a single partial snapshot"


def test_a_ticker_gap_on_a_held_coin_is_not_traded_on(tmp_path):
    """A held coin missing from one ticker read leaves equity (it is simply not counted): every other weight
    looks inflated and the next re-quote SELLS the rest of the book down to the smaller equity. A ticker
    that answers Success:false empties all prices the same way. Such a snapshot must be rejected."""
    from tradebot.engine.state.snapshot import normalize_exchange_snapshot

    frames = _universe()
    strat, clock = _strategy(tmp_path, frames, NOW)
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    strat(_snapshot(prices=prices))
    w = strat.scaled_weights()
    big = max(w, key=w.get)
    wallet = {"USD": {"Free": 2_000.0, "Lock": 0.0}}
    wallet.update({s: {"Free": v * 100_000 / prices[s], "Lock": 0.0} for s, v in w.items() if v > 0})
    positions = {"Success": True, "Positions": [
        {"Pair": f"{s}/USD", "Collateral": -v * 100_000, "UnrealizedPNL": 0.0} for s, v in w.items() if v < 0]}
    ticker = {"Success": True, "Data": {f"{s}/USD": {"LastPrice": p} for s, p in prices.items()}}
    full = normalize_exchange_snapshot({"Success": True, "SpotWallet": wallet}, positions, ticker, [])
    strat(full)
    clock.advance(15 * 60)                                       # next bar: a re-quote may go out
    gap = dict(ticker, Data={k: v for k, v in ticker["Data"].items() if k != f"{big}/USD"})
    with pytest.raises(SnapshotRejected):
        strat(normalize_exchange_snapshot({"Success": True, "SpotWallet": wallet}, positions, gap, []))
    with pytest.raises(SnapshotRejected):
        strat(normalize_exchange_snapshot({"Success": True, "SpotWallet": wallet}, positions,
                                          {"Success": False, "ErrMsg": "timestamp invalid"}, []))
    assert strat(full).signal_id.startswith("comp-")             # and the next good read trades normally


# ------------------------------------------------------------------ 4. resting orders sit out every other bar
class _Latency:
    """Each order request takes 2 s (an HTTP round trip); the first short open of the day is refused once."""

    def __init__(self, port):
        self._port = port
        self.refused = False

    def __getattr__(self, name):
        return getattr(self._port, name)

    def place_order(self, *a, **k):
        self._port.clock.advance(2)
        return self._port.place_order(*a, **k)

    def open_short(self, *a, **k):
        self._port.clock.advance(2)
        if not self.refused:
            self.refused = True
            return {"Success": False, "ErrMsg": "insufficient balance"}
        return self._port.open_short(*a, **k)


def test_resting_orders_are_requoted_every_bar(tmp_path, monkeypatch):
    """fill_timeout_seconds = 900 = one bar. Orders sent at :15+d are 900-d s old at the next bar's first
    loop, so they are NOT cancelled yet. If any symbol is off target without an order then (a refused or
    under-funded order), the re-quote for it goes out at :30, the :15 orders are cancelled one loop later
    (:31) - after this bar's re-quote - and nothing is re-placed until :45: 14 of 15 minutes with an empty
    book. Live loops start every 60 s at a fixed phase, so this repeats systematically; the replay never
    shows it because its loops land exactly on :00/:15/:30/:45 with zero latency."""
    from tradebot.exchange.replay import ReplayExchangePort

    monkeypatch.setattr(ReplayExchangePort, "_try_fill", lambda self, order, bar, ts: False)   # nothing fills
    runner, sim, clock = _runner(tmp_path, port=_Latency)
    seen: dict[str, int] = {}
    loops = 0
    while clock.now() < datetime(2026, 2, 25, 1, 16, tzinfo=UTC):
        runner.run_once()
        clock.advance(60)
        if clock.now() >= datetime(2026, 2, 25, 0, 31, tzinfo=UTC):
            loops += 1
            for coin in {o["Pair"].split("/")[0] for o in sim.orders if o["Side"] == "BUY"}:
                seen[coin] = seen.get(coin, 0) + 1
    coverage = {c: n / loops for c, n in seen.items()}
    assert len(coverage) == 3 and min(coverage.values()) >= 0.85, coverage


# ------------------------------------------------------------------ 5. risk + guard ignore resting orders
def test_resting_sells_count_when_the_plan_is_judged(tmp_path):
    """The plan is computed on positions + resting orders, but the RiskManager and the guard judge it on
    positions alone. Rotation day: yesterday's longs (0.64) have resting SELLs, the short book has not
    filled (cash 0.36). The re-quote buys today's longs and shorts with that cash; judged without the
    resting sells it looks like net +0.75 and the guard rejects the whole plan (guard:net_exposure)
    until the sells fill. Counting the resting orders, the post-trade net is +0.11."""
    from tradebot.engine.schema.models import LongTarget, ShortTarget, TargetPortfolio

    runner, sim, clock = _runner(tmp_path)
    px = {c: sim.last_price(f"{c}/USD") for c in ("C1", "C2", "C3", "C4", "C5")}
    runner._price_times = {c: clock.now() for c in px}
    runner.last_snapshot = {"prices": px, "pending_orders": []}
    actual = {"cash_usd": 36_000.0, "cash_free_usd": 36_000.0, "longs": {"C1": 32_000.0, "C5": 32_000.0},
              "shorts": {}, "equity_usd": 100_000.0, "prices": px, "entry_prices": {},
              "pending_orders": [{"Pair": f"{c}/USD", "Side": "SELL", "Status": "PENDING", "OrderID": i,
                                  "Price": px[c], "Quantity": 32_000.0 / px[c]} for i, c in enumerate(("C1", "C5"))]}
    target = TargetPortfolio(strategy_id="t", strategy_version="v", signal_id="requote", timestamp=clock.now(),
                             longs=[LongTarget(symbol=c, weight=0.3185, limit_price=px[c] * 0.9995) for c in ("C2", "C4")],
                             shorts=[ShortTarget(symbol="C3", collateral_usd=34_300.0, limit_price=px["C3"] * 1.0005)])
    result = runner.engine.runner.execute(target, actual, 100_000.0)
    assert result["status"] != "REJECTED_RISK", result.get("reasons")


# ------------------------------------------------------------------ 6. 95e0763: UNCERTAIN resolution
def test_uncertain_buy_is_not_resolved_by_a_price_move():
    """`before` stores USD notional, so a 2% rally on a $30k position (+$600) 'proves' a $500 buy filled.
    Nothing in the engine acts on UNCERTAIN/RESOLVED today (it re-plans from the snapshot), so the damage
    is a wrong audit trail / dashboard - but anyone using RESOLVED to decide a re-send would double-buy."""
    journal = IntentJournal(memory=True)
    runner = ExecutionRunner(MockExchangePort(), journal, load_competition_config(dry_run=True))
    intent = IntentRecord.build(signal_id="s", symbol="BTC", kind="open_long", side="BUY", child_index=0,
                                payload={"amount_usd": 500.0, "before": {"longs": {"BTC": 30_000.0}, "price": 50_000.0}})
    journal.add(intent)
    journal.mark_uncertain(intent.intent_id)
    after = {"longs": {"BTC": 30_600.0}, "shorts": {}, "prices": {"BTC": 51_000.0}}   # same 0.6 BTC, price +2%
    assert runner.reconcile_uncertain_intents(after) == []
    assert journal.get(intent.intent_id).status == "UNCERTAIN"


# ------------------------------------------------------------------ 7. defensive: history rows in the pending list
def test_only_pending_rows_are_cancelled_or_counted_as_resting(tmp_path):
    """list_open_orders = query_order(pending_only=TRUE) with no pair. The docs only show pending_only
    together with `pair`; if the server ever answers with order history instead (up to 100 rows by default),
    cancel_stale_orders sends one cancel per FILLED/CANCELED row (each a request out of a 30/min budget)
    and the guard's self-cross check treats yesterday's filled SELL as a resting order and blocks today's BUY."""
    from tradebot.core.clock import SimClock
    from tradebot.engine.reconcile.pending import cancel_stale_orders
    from tradebot.engine.state.snapshot import normalize_exchange_snapshot
    from tradebot.live.guard import ProposedOrder, check_self_cross

    clock = SimClock(NOW)
    old = int((NOW - timedelta(hours=5)).timestamp() * 1000)
    history = {"Success": True, "OrderMatched": [
        {"OrderID": 7, "Pair": "BTC/USD", "Side": "SELL", "Status": "FILLED", "CreateTimestamp": old},
        {"OrderID": 8, "Pair": "BTC/USD", "Side": "BUY", "Status": "CANCELED", "CreateTimestamp": old}]}

    class Port:
        cancels = []

        def list_open_orders(self):
            return history

        def cancel_order(self, order_id=None, pair=None):
            self.cancels.append(order_id)
            return {"Success": False, "ErrMsg": "only pending order can be canceled"}

    cancel_stale_orders(Port(), clock, 900)
    assert Port.cancels == []
    snap = normalize_exchange_snapshot({"SpotWallet": {"USD": {"Free": 1.0}}}, {"Positions": []},
                                       {"Data": {"BTC/USD": {"LastPrice": 1.0}}}, history)
    assert check_self_cross([ProposedOrder("BTC", "BUY", 1.0, 1.0)], snap["pending_orders"]).status.value == "OK"


# ------------------------------------------------------------------ 8. hold mode vs resting orders
def test_hold_target_does_not_close_against_resting_orders(tmp_path):
    """With no candles (restart while Binance is unreachable) the bridge 'holds the current book', but the
    hold target is built from positions only while the plan adds resting orders: a resting short open turns
    into a market close_short of the real position, a resting buy into a sell."""
    from tradebot.live.market_data import BarBuffer
    from tradebot.live.bridge import CompetitionStrategy

    strat = CompetitionStrategy(BarBuffer(["BTC/USD"], fetch=lambda *a: pd.DataFrame()), mode="comp",
                                state_path=tmp_path / "s.json", clock=SimClock(NOW))
    snap = _snapshot(prices={"BTC": 100.0, "ETH": 10.0}, longs={"ETH": 5_000.0}, shorts={"BTC": 10_000.0}, pending=[
        {"Pair": "BTC/USD", "Side": "SHORT_OPEN", "Status": "PENDING", "Price": 100.0, "Quantity": 50.0},
        {"Pair": "ETH/USD", "Side": "BUY", "Status": "PENDING", "Price": 10.0, "Quantity": 100.0}])
    plan = compute_rebalance_plan(strat(snap), snap, snap["equity_usd"])
    assert not plan["close_shorts"] and not plan["close_longs"], plan
