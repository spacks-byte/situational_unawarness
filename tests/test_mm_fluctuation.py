from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradebot.core.config import MarketMakingConfig
from tradebot.strategy.library.mm_fluctuation import MMFluctuation, accepted_spread, combined_quotes


def frames(now="2026-09-21T01:00:01Z", future=20):
    now = pd.Timestamp(now)
    idx = pd.date_range(now-pd.Timedelta(seconds=3601), now+pd.Timedelta(seconds=future), freq="s")
    close = 100*np.exp(np.sin(np.arange(len(idx))*.005)*.002)
    df = pd.DataFrame({"open": close, "high": close*1.001, "low": close*.999, "close": close, "trades": 1}, index=idx)
    return now.to_pydatetime(), {c: df.copy() for c in ("PEPE", "BONK", "1000CHEEMS")}


def context():
    books = {c: {"capital": 10000., "cash": 10000., "quantity": 0.} for c in ("PEPE", "BONK", "1000CHEEMS")}
    rules = {f"{c}/USD": {"CanTrade": True, "PricePrecision": 2, "AmountPrecision": 8, "MiniOrder": 1} for c in books}
    return books, rules


def test_native_combined_formula_parity():
    golden = json.loads((Path(__file__).parent / "fixtures/mm_combined_native.json").read_text())
    for case in golden["cases"]:
        assert combined_quotes(*case["features"]) == pytest.approx(case["quotes"], rel=1e-13, abs=1e-20)


def test_strict_filter_is_not_relaxed():
    assert not accepted_spread(100, 100.1)
    assert not accepted_spread(100, 100.10001)  # fees still do not break even
    assert accepted_spread(100, 100.10006)


def test_causal_lag_fixed_anchor_lot_and_no_short_inventory():
    now, data = frames()
    books, rules = context()
    s = MMFluctuation()
    f = {}
    first = s.generate(data, now=now, books=books, features=f, rules=rules)
    assert first.quotes and all(q.side == "BUY" for q in first.quotes)
    assert all(v["feature_input"] == int(now.timestamp())-2 for v in first.observations.values())
    anchor = f["PEPE"]["anchor"]
    assert f["PEPE"]["lot"] == pytest.approx(500/anchor)
    changed = deepcopy(data)
    for df in changed.values():
        df.loc[pd.Timestamp(now):, "close"] *= 100
    second = s.generate(changed, now=now, books=books, features={}, rules=rules)
    assert first == second
    # The immediately preceding completed candle affects capacity/anchor but not
    # the lagged formula. Freeze an established anchor to isolate that decision.
    prev = deepcopy(f)
    later = pd.Timestamp(now)+pd.Timedelta(seconds=600)
    _, more = frames(later, future=20)
    s.generate(more, now=later.to_pydatetime(), books=books, features=f, rules=rules)
    assert f["PEPE"]["anchor"] == prev["PEPE"]["anchor"]
    assert f["PEPE"]["lot"] == prev["PEPE"]["lot"]


def test_missing_or_stale_seconds_block_quotes_empty_observed_seconds_are_allowed():
    now, data = frames()
    books, rules = context()
    strategy = MMFluctuation()
    empty = deepcopy(data)
    for df in empty.values():
        df["trades"] = 0
    assert strategy.generate(empty, now=now, books=books, features={}, rules=rules).quotes
    missing = {c: df.drop(df.index[100]) for c, df in data.items()}
    assert not strategy.generate(missing, now=now, books=books, features={}, rules=rules).quotes
    stale = {c: df.loc[:pd.Timestamp(now)-pd.Timedelta(seconds=3)] for c, df in data.items()}
    assert not strategy.generate(stale, now=now, books=books, features={}, rules=rules).quotes


def test_reductions_capped_and_cash_cannot_anticipate_sales():
    now, data = frames()
    books, rules = context()
    for b in books.values():
        b.update(quantity=1., cash=0.)
    quotes = MMFluctuation().generate(data, now=now, books=books, features={}, rules=rules).quotes
    assert quotes
    assert all(q.side == "SELL" and q.quantity <= 1 for q in quotes)


@pytest.mark.parametrize("enforce,expected", [(True, (.00414, .00417)), (False, (.00415, .00416))])
def test_midpoint_between_ticks_changes_quote_distance(enforce, expected):
    assert combined_quotes(.004155, 0, 0, 0, .004155, .00001,
                           enforce_one_tick_distance=enforce) == pytest.approx(expected)


def test_latest_midpoint_replaces_price_but_preserves_candle_features():
    now, data = frames()
    books, rules = context()
    config = MarketMakingConfig(reference_source="midpoint")
    markets = {coin: dict(bid=101., ask=101.01, received_at=now.isoformat()) for coin in books}
    batch = MMFluctuation(config).generate(data, now=now, books=books, features={}, rules=rules,
                                          market_quotes=markets)
    baseline = MMFluctuation().generate(data, now=now, books=books, features={}, rules=rules)
    assert batch.quotes  # A stale last-trade price below this midpoint must not suppress buys.
    for coin, observed in batch.observations.items():
        assert observed["reference"] == pytest.approx(101.005)
        assert observed["reference_source"] == "midpoint"
        for field in ("variance", "alpha", "feature_input", "anchor", "lot"):
            assert observed[field] == baseline.observations[coin][field]


@pytest.mark.parametrize("changes", [
    {"bid": 102.}, {"bid": float("nan")}, {"ask": float("inf")}, {"bid": 0},
    {"ask": 100.}, {"book_stale": True}, {"received_at": None},
    {"received_at": "2026-09-21T01:00:02Z"},  # future
    {"received_at": "2026-09-21T00:59:58Z"},  # too old
    {"received_at": "2026-09-21T01:00:01"},  # timezone missing
])
def test_invalid_midpoint_book_never_falls_back_to_candles(changes):
    now, data = frames()
    books, rules = context()
    quote = dict(bid=100., ask=100.01, received_at=now.isoformat())
    quote.update(changes)
    batch = MMFluctuation(MarketMakingConfig(reference_source="midpoint")).generate(
        data, now=now, books=books, features={}, rules=rules, market_quotes={"PEPE": quote})
    assert not batch.quotes
    assert all(o["reason"] == "missing_or_invalid_book" for o in batch.observations.values())
