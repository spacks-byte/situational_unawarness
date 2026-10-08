"""MM decision time with late live candles (Binance 1s candles arrive 2-3 s after their open time).

The policy needs the candle of second t-1 at decision second t. The quote bridge decides at
"last complete second + 1 s" when the data is at most `max_data_delay_seconds` behind, so a
late feed produces exactly the quotes an on-time feed would have produced at that second.
"""
from datetime import timedelta

import pandas as pd

from tests.test_mm_fluctuation import frames
from tests.test_shared_portfolio import setup_account
from tradebot.live.account import QuoteBridge

S = pd.Timedelta(seconds=1)


def bridge_for(tmp_path, lag, *, stale=()):
    """Bridge whose feed ends `lag` seconds before the current second (`stale` coins 30 s)."""
    account, _, clock = setup_account(tmp_path)
    _, data = frames(clock.now())
    for rule in account.rules.values():
        rule["PricePrecision"] = 2

    def fetch(coin, start, end):
        behind = 30 if coin in stale else lag
        return data[coin].loc[start:end - behind * S]          # last candle opens at end - behind
    return QuoteBridge(account, fetch, clock, account.config), clock


def quotes(batch):
    return [(q.symbol, q.side, q.price, q.quantity) for q in batch.quotes]


def test_late_feed_quotes_exactly_like_an_on_time_feed(tmp_path):
    on_time, clock = bridge_for(tmp_path / "a", lag=1)       # candle of t-1 present at t
    expected = on_time({})
    late, late_clock = bridge_for(tmp_path / "b", lag=3)     # live: candle of t-3 is the latest
    late_clock.advance(2)                                     # same data, read 2 s later
    batch = late({})
    assert expected.quotes and quotes(batch) == quotes(expected)
    assert batch.timestamp == expected.timestamp == clock.now()   # decided at last complete second + 1 s


def test_fresh_data_keeps_the_wall_clock_decision_time(tmp_path):
    bridge, clock = bridge_for(tmp_path, lag=1)
    clock.advance(0.4)                                        # mid-second: unchanged, fraction kept
    assert bridge({}).timestamp == clock.now()


def test_data_beyond_the_delay_limit_stays_stale_and_does_not_quote(tmp_path):
    bridge, clock = bridge_for(tmp_path, lag=10)              # max_data_delay_seconds = 5
    batch = bridge({})
    assert batch.timestamp == clock.now() and batch.quotes == []


def test_one_stale_coin_does_not_hold_back_the_others(tmp_path):
    bridge, clock = bridge_for(tmp_path, lag=3, stale={"BONK"})
    clock.advance(2)                                          # fixture history covers decisions from T on
    batch = bridge({})
    assert batch.timestamp == clock.now() - timedelta(seconds=2)
    symbols = {q.symbol for q in batch.quotes}
    assert "BONK" not in symbols and symbols


def test_midpoint_uses_wall_time_with_delayed_candle_features(tmp_path):
    bridge, clock = bridge_for(tmp_path, lag=3)
    clock.advance(2)
    bridge.config.reference_source = "midpoint"
    bridge.market_snapshot = lambda: {"quotes": {
        coin: {"bid": 101., "ask": 101.01, "received_at": clock.now().isoformat()}
        for coin in bridge.config.allocations}}
    batch = bridge({})
    assert batch.quotes
    assert batch.timestamp == clock.now()
    assert all(o["reference"] == 101.005 and o["book_age_seconds"] == 0
               for o in batch.observations.values())
    assert all(o["feature_input"] == int(clock.now().timestamp()) - 4
               for o in batch.observations.values())
