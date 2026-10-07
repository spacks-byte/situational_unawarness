"""Request budget of the shared account (Roostoo: 30 requests/minute, we schedule at most 25).

The costs below are the contract: a regression that adds a request per order or per cancel shows up
here before it shows up as RXM rebalances queuing behind MM quote refreshes on the live account.
"""
import json

import pytest

from tests.test_shared_portfolio import prepare, setup_account
from tradebot.engine.execution.quotes import QuoteExecutor
from tradebot.engine.schema.models import LimitQuote, QuoteBatch
from tests.test_shared_runner import shared
from tradebot.engine.state.portfolio import MM


def _calls(sim):
    return dict(sim.calls)


def _spent(before, sim):
    return sum(sim.calls.get(k, 0) - before.get(k, 0) for k in set(sim.calls) | set(before))


def test_per_operation_request_costs(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    b = _calls(sim); a.sync()
    assert _spent(b, sim) == 4                              # pending, balance, shorts, tickers
    quotes = [o for c in ("PEPE", "BONK", "1000CHEEMS") for o in prepare(a, clock, coin=c, price=99)]
    b = _calls(sim)
    for o in quotes:
        a.submit(o)
    assert _spent(b, sim) == 2 * len(quotes)                # fresh-ticker passivity check + order
    b = _calls(sim)
    for o in list(a.active(MM)):
        a.cancel(MM, o["order_id"])
    assert _spent(b, sim) == 2 * len(quotes)                # cancel + final-state query, no sync each
    rxm = a.scoped("rxm")
    b = _calls(sim)
    rxm.place_order("PEPE", "BUY", 5, price=99)
    assert _spent(b, sim) == 1                              # was 5: no full sync per order
    oid = next(o["order_id"] for o in a.active("rxm"))
    b = _calls(sim)
    rxm.cancel_order(order_id=oid)
    assert _spent(b, sim) == 3                              # was 6: cancel, final state, wallet only


def _record_tags(runner):
    seen = []
    acquire = runner.throttled._acquire

    def recording(weight):
        seen.append(runner.throttled.tag or "account")
        return acquire(weight)
    runner.throttled._acquire = recording
    return seen


def test_rxm_spends_its_requests_before_mm_in_every_loop(tmp_path):
    runner, sim, clock = shared(tmp_path)
    try:
        for _ in range(20):
            seen = _record_tags(runner)
            assert runner.run_once()["status"] == "OK"
            strategies = [t for t in seen if t != "account"]
            if "rxm" in strategies and MM in strategies:
                assert strategies.index("rxm") < strategies.index(MM)
            clock.advance(60)
        assert set(runner.throttled.by_tag) >= {"account", "rxm", MM}
    finally:
        runner.close()


def test_slow_venue_keeps_the_cap_and_reports_quote_age_and_deadline_misses(tmp_path):
    runner, sim, clock = shared(tmp_path)
    for name in ("get_ticker", "get_balance", "query_order", "get_short_positions",
                 "place_order", "cancel_order", "open_short", "close_short"):
        method = getattr(sim, name)

        def slow(*args, _method=method, **kwargs):
            clock.advance(1.5)                               # every response takes 1.5 s
            return _method(*args, **kwargs)
        setattr(sim, name, slow)
    try:
        statuses, rxm_step = [], []
        for _ in range(30):
            start = clock.now()
            result = runner.run_once()
            statuses.append(result["status"])
            rxm = result.get("strategies", {}).get("rxm", {})
            if rxm.get("status") not in {None, "HOLD"}:
                rxm_step.append((clock.now() - start).total_seconds())
            clock.advance(60)
        assert set(statuses) <= {"OK", "PARTIAL"}, statuses
        assert runner.throttled.peak <= 25                   # the cap is never raised to absorb contention
        status = json.loads((runner.state_dir / "status.json").read_text())
        assert set(status["account"]["quote_stats"]) >= {"submitted", "deadline_missed", "last_age_seconds"}
        assert set(status["http_by_strategy"]) >= {"account", "rxm", MM}
        assert status["http_last_minute"] <= 25
        # RXM goes first, so its step is never delayed by an MM refresh in the same loop
        assert all(seconds < 300 for seconds in rxm_step), rxm_step
    finally:
        runner.close()


def test_quotes_past_the_refresh_deadline_are_dropped_and_counted(tmp_path):
    a, sim, clock = setup_account(tmp_path)
    ticker = sim.get_ticker

    def slow_ticker(pair=None):
        clock.advance(250)                                   # a congested venue: 250 s per passivity check
        return ticker(pair)
    sim.get_ticker = slow_ticker
    quotes = [LimitQuote(symbol=c, side="BUY", quantity=10, price=99) for c in ("PEPE", "BONK", "1000CHEEMS")]
    result = QuoteExecutor(a, clock).run_once(lambda report: QuoteBatch(
        signal_id="slow", timestamp=clock.now(), refresh_seconds=600, quotes=quotes))
    stats = a.state["quote_stats"]
    assert result["submitted"] == 2 and stats["deadline_missed"] == 1   # never sent after its deadline
    assert result["deadline_missed_total"] == 1
    assert result["quote_age_seconds"] == pytest.approx(250) and stats["max_age_seconds"] == pytest.approx(250)
    assert len(sim.history) == 2
