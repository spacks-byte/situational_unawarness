from copy import deepcopy
from datetime import UTC, datetime
import json

import pandas as pd
import pytest

from tradebot.core.clock import SimClock
from tradebot.core.config import Settings
from tradebot.engine.schema.models import LimitQuote, QuoteBatch
from tradebot.engine.state.portfolio import AccountBlocked, AccountCoordinator, AccountLock, MM, PortfolioStore
from tradebot.exchange.replay import ReplayExchangePort

COINS = ["PEPE", "BONK", "1000CHEEMS"]


def setup_account(tmp_path, dry_run=False, initial=100_000):
    clock = SimClock(datetime(2026, 9, 21, 1, tzinfo=UTC))
    idx = pd.date_range("2026-09-21", periods=10_000, freq="s", tz="UTC")
    frames = {c: pd.DataFrame({"open": 100., "high": 100., "low": 100., "close": 100., "trades": 1}, index=idx) for c in COINS}
    port = ReplayExchangePort(frames, clock, initial_usd=initial, intervals={c: "1s" for c in COINS})
    settings = Settings()
    store = PortfolioStore(tmp_path / "portfolio.db", clock)
    a = AccountCoordinator(port, store, settings.market_making, settings.fees, clock, dry_run=dry_run)
    return a, port, clock


def prepare(a, clock, side="BUY", quantity=10, price=99, coin="PEPE"):
    batch = QuoteBatch(signal_id="test", timestamp=clock.now(), quotes=[
        LimitQuote(symbol=coin, side=side, quantity=quantity, price=price)])
    return a.prepare_quotes(batch)


def fill_next(a, sim, clock, price, coin="PEPE"):
    ts = pd.Timestamp(clock.now()).ceil("s")
    sim.bars[f"{coin}/USD"].loc[ts, ["low", "high", "close"]] = [price, price, price]
    clock.advance(1)
    a.sync()


def test_allocator_uses_current_equity_and_reserves_both_sides(tmp_path):
    a, sim, clock = setup_account(tmp_path, initial=200_000)
    assert a.state["rxm_capital"] == 20_000
    assert a.owner("PEPE")["capital"] == 153_000
    assert a.owner("BONK")["capital"] == 13_500
    o, = prepare(a, clock)
    assert a.reservations(MM)[0] == pytest.approx(990.495)
    assert a.owner("PEPE")["cash"] == 153_000
    assert a.submit(o)["Success"]
    assert a.view_balance("rxm")["SpotWallet"]["USD"]["Free"] == pytest.approx(20_000)
    fill_next(a, sim, clock, 98)
    b = a.owner("PEPE")
    assert b["quantity"] == 10
    assert b["cash"] == pytest.approx(153_000-990.495)
    assert b["fees"] == pytest.approx(.495)
    a.sync()
    assert b["fees"] == pytest.approx(.495)
    assert a.view_balance("rxm")["SpotWallet"]["PEPE"]["Free"] == 0
    assert a.view_balance("rxm")["SpotWallet"]["USD"]["Free"] == pytest.approx(20_000)


def test_sell_without_inventory_and_mm_short_calls_are_denied(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    assert prepare(a, clock, side="SELL", price=101) == []
    assert prepare(a, clock, quantity=1000) == []
    assert not a.scoped(MM).open_short("PEPE", 100)["Success"]
    assert not a.scoped(MM).close_short("PEPE", close_pct=100)["Success"]
    assert not a.scoped("rxm").place_order("PEPE", "SELL", 1, price=101)["Success"]
    assert not sim.history and not sim.shorts


def test_simultaneous_strategy_holdings_and_price_aware_cross_guard(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    o, = prepare(a, clock)
    a.submit(o)
    rxm = a.scoped("rxm")
    assert rxm.place_order("PEPE", "BUY", 5, price=99)["Success"]
    fill_next(a, sim, clock, 98)
    assert a.owner("PEPE")["quantity"] == 10
    assert a.state["rxm_quantity"]["PEPE"] == 5
    assert not rxm.place_order("PEPE", "SELL", 6, price=101)["Success"]
    sell, = prepare(a, clock, side="SELL", quantity=10, price=101)
    a.submit(sell)
    assert rxm.place_order("PEPE", "BUY", 1, price=100)["Success"]
    assert not rxm.place_order("PEPE", "BUY", 1, price=102)["Success"]
    assert not rxm.cancel_order(order_id=sell["order_id"])["Success"]
    assert len(sim.orders) == 2


def test_cancel_refund_and_realized_pnl(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    o, = prepare(a, clock)
    a.submit(o)
    a.cancel(MM, o["order_id"])
    a.sync()
    assert a.owner("PEPE")["cash"] == 76500
    assert a.reservations(MM)[0] == 0
    o, = prepare(a, clock)
    a.submit(o)
    fill_next(a, sim, clock, 98)
    sell, = prepare(a, clock, side="SELL", price=101)
    a.submit(sell)
    fill_next(a, sim, clock, 102)
    b = a.owner("PEPE")
    assert b["quantity"] == 0
    assert b["cash"] == pytest.approx(76519)
    assert b["fees"] == pytest.approx(1)
    assert b["realized_pnl"] == pytest.approx(19)
    assert a.report()["mm"]["PEPE"]["net_pnl"] == pytest.approx(19)


def test_full_fill_wins_cancel_race_and_duplicate_history(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    o, = prepare(a, clock)
    a.submit(o)
    def cancel(order_id=None, pair=None):
        fill_next(a, sim, clock, 98)
        return {"Success": True}
    sim.cancel_order = cancel
    a.cancel(MM, o['order_id'])
    a.sync()
    a.sync()
    assert a.owner('PEPE')['quantity'] == 10
    assert a.owner('PEPE')['cash'] == pytest.approx(76500-990.495)
    assert a.reservations(MM)[0] == 0


def test_uncertain_submit_recovers_unique_order_and_never_duplicates(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    actual = sim.place_order
    def timeout(*args, **kwargs):
        actual(*args, **kwargs)
        raise TimeoutError("response lost")
    sim.place_order = timeout
    o, = prepare(a, clock)
    with pytest.raises(AccountBlocked, match="uncertain"):
        a.submit(o)
    sim.place_order = actual
    a.sync()
    assert o["status"] == "PENDING"
    assert len(sim.history) == 1
    a.store.close()
    store = PortfolioStore(tmp_path / "portfolio.db", clock)
    restarted = AccountCoordinator(sim, store, a.config, a.fees, clock)
    assert len(restarted.active(MM)) == 1
    assert len(sim.history) == 1


def test_unexplained_cash_and_unknown_order_are_advisory(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    sim.free_usd += 100                     # untraceable: no fills this sync
    a.sync()
    assert a.blocked is None
    assert "cash mismatch" in a.report()["issues"]["cash:account"]["reason"]
    order, = prepare(a, clock)
    assert a.submit(order)['Success']
    assert a.refusal(MM, 'BONK', 'BUY') is None
    sim.free_usd -= 100
    a.sync()
    assert "cash:account" in a.report()["issues"]   # lifts only after consecutive clean syncs
    a.sync()
    assert a.report()["issues"] == {}
    sim.place_order("PEPE", "BUY", 1, price=99)          # an order this coordinator did not send
    a.sync()
    restrictions = a.report()["issues"]
    assert set(restrictions) == {"coin:PEPE"} and "unowned" in restrictions["coin:PEPE"]["reason"]
    assert a.refusal("rxm", "PEPE", "BUY") is None and a.refusal(MM, "BONK", "BUY") is None
    assert "cancel_order" not in sim.calls                # never adopted or cancelled automatically


def test_cash_roundoff_within_tolerance_is_absorbed_and_recorded(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    expected = a.state["expected_cash_assets"]
    sim.free_usd += 0.03                    # below the 0.05 USD floor
    a.sync()
    assert a.report()["issues"] == {}
    assert a.state["expected_cash_assets"] == pytest.approx(expected + 0.03)
    assert a.report()["adjustments"]["count"] == 1
    assert a.report()["adjustments"]["cash_rounding_usd"] == pytest.approx(0.03)


def test_dry_run_has_no_mutating_calls_even_cancellation(tmp_path):
    a, sim, clock = setup_account(tmp_path, dry_run=True)
    o, = prepare(a, clock)
    assert not a.submit(o)["Success"]
    assert not a.scoped("rxm").open_short("PEPE", 100)["Success"]
    assert not a.scoped("rxm").close_short("PEPE", close_pct=100)["Success"]
    o.update(status="PENDING", order_id="77")
    assert not a.cancel(MM, "77")["Success"]
    assert not {"place_order", "open_short", "close_short", "cancel_order"} & set(sim.calls)


def test_rxm_shorts_keep_own_policy_and_cannot_use_mm_cash(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    rxm = a.scoped("rxm")
    assert not rxm.open_short("PEPE", 40_000, price=101)["Success"]
    result = rxm.open_short("PEPE", 1010, price=101)
    assert result["Success"]
    assert a.rxm_free_cash() == pytest.approx(10_000-1011.01)
    rxm.cancel_order(order_id=result["ID"])
    assert a.rxm_free_cash() == pytest.approx(10_000)
    assert rxm.open_short("PEPE", 1010, price=101)["Success"]
    fill_next(a, sim, clock, 102)
    assert a.state["short_quantity"]["PEPE/USD"] == 10
    assert rxm.close_short("PEPE", close_qty=4)["Success"]
    assert a.state["short_quantity"]["PEPE/USD"] == 6
    assert all(b["fees"] == 0 for b in a.state["mm"].values())


def test_account_lock_and_corrupt_state(tmp_path):
    lock = AccountLock(tmp_path / "lock")
    with pytest.raises(AccountBlocked, match="already running"):
        AccountLock(tmp_path / "lock")
    lock.close()
    a, _, _ = setup_account(tmp_path)
    a.store.db.execute("UPDATE portfolio SET payload='invalid'")
    a.store.db.commit()
    with pytest.raises(json.JSONDecodeError):
        PortfolioStore(tmp_path / "portfolio.db", a.clock)


def test_deadline_and_pause_are_rechecked_after_ticker_wait(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    a.state["next_refresh"] = clock.now().timestamp()+600
    ticker = sim.get_ticker
    def delayed(pair=None):
        clock.advance(601)
        return ticker(pair)
    sim.get_ticker = delayed
    o, = prepare(a, clock)
    assert not a.submit(o)["Success"]
    assert not sim.history
    sim.get_ticker = ticker
    a.state["next_refresh"] = clock.now().timestamp()+600
    o, = prepare(a, clock)
    a.paused = lambda _: True               # paused between preparation and submission
    assert not a.submit(o)["Success"]
    assert not sim.history


def test_uncertain_submission_tracks_outcome_until_history_proves_no_execution(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    place = sim.place_order
    def timeout(*args, **kwargs):
        raise TimeoutError("outcome unknown")
    sim.place_order = timeout
    o, = prepare(a, clock)
    with pytest.raises(AccountBlocked, match="uncertain submission"):
        a.submit(o)
    sim.place_order = place
    for _ in range(3):                      # inside the evidence window: keep the reservation, wait
        a.sync()
        clock.advance(10)
    assert a.blocked is None and len(a.active(MM)) == 1 and not sim.history
    assert "coin:PEPE" in a.report()["issues"]
    assert a.refusal(MM, "BONK", "BUY") is None
    assert prepare(a, clock) == []          # never resubmitted while the outcome is unknown
    clock.advance(60)
    a.sync()                                # complete history, no such order: never executed
    assert not a.active(MM) and o["status"] == "REJECTED"
    a.sync()
    assert a.report()["issues"] == {}
    assert not sim.history


def test_lost_spot_response_is_adopted_from_history_without_resubmitting(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    place = sim.place_order
    def lost(*args, **kwargs):
        place(*args, **kwargs)
        raise TimeoutError("response lost")
    sim.place_order = lost
    o, = prepare(a, clock)
    with pytest.raises(AccountBlocked):
        a.submit(o)
    a.sync()
    assert o["order_id"] and o["status"] == "PENDING"
    assert len(sim.history) == 1
    a.sync()
    assert a.report()["issues"] == {}


def test_bootstrap_insufficient_unreserved_cash_does_not_liquidate(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    sim.coins["PEPE"] = 2000  # existing RXM positions cannot fit in its 30% allocation
    second = PortfolioStore(tmp_path / "other.db", clock)
    with pytest.raises(AccountBlocked, match="funding shortfall"):
        AccountCoordinator(sim, second, a.config, a.fees, clock)
    assert not sim.history


def test_terminal_state_and_ledger_events_reconcile(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    o, = prepare(a, clock)
    a.submit(o)
    fill_next(a, sim, clock, 98)
    events = [json.loads(r[0]) for r in a.store.db.execute("SELECT payload FROM events WHERE kind='fill'")]
    assert len(events) == 1
    assert events[0]["quantity"] == a.owner("PEPE")["quantity"]
    assert events[0]["fee_delta"] == a.owner("PEPE")["fees"]
    assert a.owner("PEPE")["capital"]-events[0]["value"]-events[0]["fee_delta"] == a.owner("PEPE")["cash"]
    assert not a.active(MM)
    assert len(a.store.completed(MM)) == 1


def test_database_failure_restores_durable_state(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    a.store.db.execute('CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, "disk failure"); END')
    a.store.db.commit()
    original_cash = a.owner("PEPE")["cash"]
    a.owner("PEPE")["cash"] = 42
    with pytest.raises(Exception, match="disk failure"):
        a.store.save("test")
    assert a.owner("PEPE")["cash"] == original_cash
    assert not sim.history


def test_mm_decimal_ticks_reach_the_real_client_without_extra_flooring(tmp_path):
    from tests.test_roostoo_wire import FakeRoostoo, SECRET
    from tradebot.core.config import ExchangeSettings
    from tradebot.exchange import RoostooClient, RoostooExchangePort
    a, sim, clock = setup_account(tmp_path)
    sim.bars['PEPE/USD'].loc[:, ['open', 'high', 'low', 'close']] = .000004
    sim._px_cache.clear()
    server = FakeRoostoo(sim, clock)
    client = RoostooClient(api_key='test-key', api_secret=SECRET, session=server,
                           settings=ExchangeSettings(min_request_interval_seconds=0))
    client.base_url = 'http://roostoo.invalid'
    a.port = RoostooExchangePort(client)
    a.sync()
    order, = prepare(a, clock, quantity=100_000_000, price=.00000398)
    assert a.submit(order)['Success']
    fill_next(a, sim, clock, .00000397)
    # This subtraction commonly leaves a float just below the intended tick.
    order, = prepare(a, clock, side='SELL', quantity=100_000_000, price=.00000402-.00000001)
    assert a.submit(order)['Success']
    requests = [r for r in server.requests if r['path'] == '/v3/place_order']
    assert requests[-1]['payload']['price'] == '0.00000401'
    assert not any(r['path'] in {'/v6/short_open', '/v6/short_close'} for r in server.requests)


def test_bad_best_price_is_rejected_and_metadata_is_refreshed(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    sim.get_ticker = lambda pair=None: {"Success": True, "Data": {"PEPE/USD": {"MinAsk": float('nan')}}}
    o, = prepare(a, clock)
    assert not a.submit(o)["Success"]
    assert not sim.history
    # Metadata is refreshed while the account engine is running, not just at launch.
    info = sim.get_exchange_info()
    info["TradePairs"]["PEPE/USD"]["AmountPrecision"] = 3
    sim.get_exchange_info = lambda: info
    sim.get_ticker = lambda pair=None: {"Success": True, "Data": {"PEPE/USD": {"LastPrice": 100.0}}}
    clock.advance(3600)
    a.sync()
    assert a.rules["PEPE/USD"]["AmountPrecision"] == 3


def test_rate_limit_wait_precedes_final_quote_checks(tmp_path):
    from tradebot.live.throttle import ThrottledPort
    a, sim, clock = setup_account(tmp_path)
    a.port = ThrottledPort(sim, clock, max_per_minute=2)
    a.port.get_ticker()  # Only one request remains in this minute.
    a.state["next_refresh"] = clock.now().timestamp()+30
    o, = prepare(a, clock)
    assert not a.submit(o)["Success"]
    assert clock.now().timestamp() >= a.state["next_refresh"]
    assert not sim.history
    a.state["next_refresh"] = clock.now().timestamp()+600
    o, = prepare(a, clock)
    assert a.submit(o)["Success"]
    assert a.port.peak <= 2


def test_near_flat_large_lots_reconcile_without_rewriting_cash_or_positions(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    lot = 815068493.1506848
    quantity = 0.
    for _ in range(7):
        quantity += lot
    for _ in range(7):
        quantity -= lot
    assert quantity == 9.5367431640625e-7  # The 28-day replay's observed residue.
    a.owner("PEPE")["quantity"] = quantity
    a.tickers["Data"]["PEPE/USD"]["LastPrice"] = 3.6e-6
    before = deepcopy(a.state)
    assert a._reconcile() == []
    assert a.state == before
    # A real coin discrepancy must still be found; this is not a blanket waiver for
    # assets with small unit prices, and other strategy holdings remain separate.
    a.state["rxm_quantity"]["PEPE"] = 1.
    assert [(scope, "inventory mismatch" in reason) for scope, reason, *_ in a._reconcile()] == [("coin:PEPE", True)]


@pytest.mark.parametrize("price", [0., -1., float("nan"), 100_000.])
def test_roundoff_allowance_needs_valid_price_and_negligible_value(tmp_path, price):
    a, sim, clock = setup_account(tmp_path)
    a.owner("PEPE")["quantity"] = 9.5367431640625e-7
    a.tickers["Data"]["PEPE/USD"]["LastPrice"] = price
    assert [(scope, "inventory mismatch" in reason) for scope, reason, *_ in a._reconcile()] == [("coin:PEPE", True)]
