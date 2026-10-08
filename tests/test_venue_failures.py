"""Failure behaviour of the shared account against venue-shaped responses (no network).

Each test names the failure it reproduces (P1-P4 are the probes from the PR #7 review) and the
contract: the coordinator never halts the whole account for it, never resubmits blindly, and
records advisory issues without refusing otherwise valid orders.
"""
import pytest

from tests.test_shared_portfolio import prepare
from tests.venue_shaped import fill_at, fixture, venue_account
from tradebot.engine.execution.quotes import QuoteExecutor
from tradebot.engine.state.portfolio import MM, AccountBlocked, order_rows
from tradebot.engine.state.snapshot import normalize_exchange_snapshot, remaining_qty


def issues(a):
    return a.report()["issues"]


def wallet_matches_ledger(a, coin="PEPE"):
    wallet = a.balance["SpotWallet"].get(coin, {"Free": 0, "Lock": 0})
    held = float(wallet["Free"]) + float(wallet["Lock"])
    return held == pytest.approx(a.state["mm"][coin]["quantity"] + a.state["rxm_quantity"].get(coin, 0.0))


# ------------------------------------------------------------------ fixtures parse as the venue sends them
def test_documented_shapes_parse():
    rows = order_rows(fixture("query_order_mixed.json"))
    assert remaining_qty(rows[0]) == 10.0                          # PENDING with Filled == Quantity is unfilled
    assert remaining_qty(rows[2]) == 0.0                           # canceled: no reservation
    assert order_rows(fixture("query_order_none.json")) == []
    snap = normalize_exchange_snapshot(fixture("balance.json"), fixture("short_positions.json"),
                                       fixture("ticker.json"), rows)
    assert snap["lock_pending_usd"] == pytest.approx(990.0)
    assert snap["lock_short_collateral_usd"] == pytest.approx(500.0)
    assert snap["lock_unexplained_usd"] == pytest.approx(2990.0 - 990.0 - 500.0)
    assert fixture("cancel_order.json")["CanceledList"] == [812]


# ------------------------------------------------------------------ P1: documented PENDING rows
def test_p1_pending_rows_reporting_full_filled_quantity_do_not_block(tmp_path):
    a, venue, clock, _ = venue_account(tmp_path)
    o, = prepare(a, clock)
    assert a.submit(o)["Success"]
    for _ in range(3):
        a.sync()
    assert a.blocked is None and issues(a) == {}
    assert a.state["mm"]["PEPE"]["quantity"] == 0             # not booked as a fill
    fill_at(venue, clock, 99)
    a.sync()
    assert a.state["mm"]["PEPE"]["quantity"] == pytest.approx(10)
    assert wallet_matches_ledger(a) and issues(a) == {}


# ------------------------------------------------------------------ P2: commission rounding
@pytest.mark.parametrize("delta", [0.0, 0.004, -0.03, 0.03, 0.25])
def test_p2_commission_rounding_within_tolerance_is_absorbed(tmp_path, delta):
    a, venue, clock, _ = venue_account(tmp_path, commission_delta=delta)
    o, = prepare(a, clock)
    a.submit(o)
    fill_at(venue, clock, 99)
    a.sync()
    assert issues(a) == {}
    adjustments = a.report()["adjustments"]
    assert adjustments["count"] == (1 if abs(delta) > 1e-9 else 0)
    assert adjustments["cash_rounding_usd"] == pytest.approx(delta, abs=1e-6)


@pytest.mark.parametrize("delta", [0.495, -0.495, 2.0])   # a whole 5 bps maker fee doubled / missing, or worse
def test_p2_cash_gap_reports_owner_without_refusing_orders(tmp_path, delta):
    # The 3 bps default sits below the smallest fee (5 bps maker): a doubled or missing fee is never
    # absorbed as "rounding", while sub-cent commission rounding always is (test above).
    a, venue, clock, _ = venue_account(tmp_path, commission_delta=delta)
    o, = prepare(a, clock)
    a.submit(o)
    fill_at(venue, clock, 99)
    a.sync()
    assert set(issues(a)) == {f"cash:{MM}"}
    a.sync(); a.sync()                                         # the gap persists: still traced to MM
    assert set(issues(a)) == {f"cash:{MM}"}
    assert a.blocked is None
    assert a.refusal(MM, "BONK", "BUY") is None and a.refusal(MM, "PEPE", "SELL") is None
    assert a.refusal("rxm", "BONK", "BUY") is None             # RXM keeps trading (was: whole account)
    assert a.scoped("rxm").place_order("BONK", "BUY", 1, price=99)["Success"]


# ------------------------------------------------------------------ P3: lost short responses
def test_p3_lost_market_short_open_is_booked_from_the_position(tmp_path):
    a, venue, clock, _ = venue_account(tmp_path)
    venue.lose.add("open_short")
    with pytest.raises(AccountBlocked, match="uncertain short_open"):
        a.scoped("rxm").open_short("BONK", 500)
    assert "short:BONK/USD" in issues(a)
    assert a.refusal("rxm", "PEPE", "BUY") is None
    a.sync()
    assert a.state["short_quantity"]["BONK/USD"] == pytest.approx(5.0)
    a.sync(); a.sync()
    assert issues(a) == {} and venue.calls["open_short"] == 1  # never re-sent


def test_p3_short_open_that_never_reached_the_venue_is_rejected_after_the_window(tmp_path):
    a, venue, clock, _ = venue_account(tmp_path)
    venue.fail.add("open_short")
    with pytest.raises(AccountBlocked):
        a.scoped("rxm").open_short("BONK", 500)
    a.sync()
    assert "short:BONK/USD" in issues(a)
    assert a.refusal("rxm", "BONK", "SHORT_OPEN")
    clock.advance(2 * a.config.evidence_window_seconds + 1)
    a.sync(); a.sync(); a.sync()
    assert issues(a) == {} and not a.active("rxm")
    assert not venue.sim.shorts


def test_p3_lost_short_close_is_booked_and_full_close_stays_allowed(tmp_path):
    a, venue, clock, _ = venue_account(tmp_path)
    rxm = a.scoped("rxm")
    assert rxm.open_short("BONK", 1000)["Success"]
    a.sync()
    venue.lose.add("close_short")
    with pytest.raises(AccountBlocked):
        rxm.close_short("BONK", close_qty=4)
    assert a.refusal("rxm", "BONK", "SHORT_CLOSE", full_close=True)  # do not duplicate an uncertain close
    a.sync()
    assert a.state["short_quantity"]["BONK/USD"] == pytest.approx(6.0)
    a.sync(); a.sync()
    assert issues(a) == {}


# ------------------------------------------------------------------ P4: delayed cancel settlement
def test_p4_delayed_cancel_keeps_the_reservation_and_defers_the_replacement(tmp_path):
    a, venue, clock, _ = venue_account(tmp_path)
    o, = prepare(a, clock)
    a.submit(o)
    venue.cancel_delay = 3                                     # the venue settles three reads later
    result = a.cancel(MM, o["order_id"])
    assert result.get("Pending") and o["status"] == "CANCELING"
    assert prepare(a, clock) == []                             # no duplicate quote on PEPE BUY
    a.sync()
    assert o["status"] == "CANCELING" and issues(a) == {}
    a.sync(); a.sync()
    assert not a.active(MM) and issues(a) == {}
    assert a.state["mm"]["PEPE"]["cash"] == pytest.approx(a.state["mm"]["PEPE"]["capital"])


def test_p4_unacknowledged_cancel_is_retried_until_confirmed(tmp_path):
    a, venue, clock, _ = venue_account(tmp_path)
    quotes = QuoteExecutor(a, clock)
    o, = prepare(a, clock)
    a.submit(o)
    venue.fail.add("cancel_order")
    assert quotes.cancel_owned() == {("PEPE", "BUY")}           # still awaiting the venue
    assert venue.calls.get("cancel_order", 0) == 0
    assert quotes.cancel_owned() == {("PEPE", "BUY")}           # not re-sent before the retry delay
    clock.advance(a.config.cancel_retry_seconds)
    assert quotes.cancel_owned() == set()
    assert venue.calls["cancel_order"] == 1 and not venue.sim.orders


# ------------------------------------------------------------------ restarts
def test_restart_mid_submit_recovers_the_order_from_history(tmp_path):
    a, venue, clock, reopen = venue_account(tmp_path)
    venue.lose.add("place_order")
    o, = prepare(a, clock)
    with pytest.raises(AccountBlocked):
        a.submit(o)
    b = reopen()                                               # crash after the request, before the reply
    adopted = [x for x in b.active(MM) if x["intent_id"] == o["intent_id"]]
    assert adopted and adopted[0]["order_id"] == str(venue.sim.history[-1]["OrderID"])
    b.sync()
    assert issues(b) == {} and venue.calls["place_order"] == 1


def test_restart_mid_cancel_confirms_the_cancel(tmp_path):
    a, venue, clock, reopen = venue_account(tmp_path)
    o, = prepare(a, clock)
    a.submit(o)
    venue.fail.add("cancel_order")
    a.cancel(MM, o["order_id"])                                # request never reached the venue
    b = reopen()
    assert [x["status"] for x in b.active(MM)] == ["CANCELING"]
    clock.advance(b.config.cancel_retry_seconds)
    assert QuoteExecutor(b, clock).cancel_owned() == set()
    assert not venue.sim.orders and issues(b) == {}


def test_history_paging_order_does_not_declare_a_lost_order_unexecuted(tmp_path):
    a, venue, clock, _ = venue_account(tmp_path)
    for _ in range(150):                                       # older history: oldest-first pages of 100
        r = venue.sim.place_order("BONK", "BUY", 1, price=50)
        venue.sim.cancel_order(order_id=r["OrderDetail"]["OrderID"])
    venue.lose.add("place_order")
    o, = prepare(a, clock)
    with pytest.raises(AccountBlocked):
        a.submit(o)
    clock.advance(3 * a.config.evidence_window_seconds)       # past the window: only proof may reject
    a.sync()
    assert o["status"] == "PENDING" and o["order_id"] == str(venue.sim.history[-1]["OrderID"])


# ------------------------------------------------------------------ independent strategies
def test_unresolved_short_does_not_prevent_other_operations(tmp_path):
    a, venue, clock, _ = venue_account(tmp_path)
    venue.fail.add("open_short")
    with pytest.raises(AccountBlocked):
        a.scoped("rxm").open_short("BONK", 500)
    a.sync()
    assert a.refusal(MM, "BONK", "BUY") is None                # a short-pair finding never stops MM
    assert [q["coin"] for q in prepare(a, clock, coin="BONK")] == ["BONK"]
    assert a.scoped("rxm").place_order("PEPE", "BUY", 1, price=99)["Success"]
