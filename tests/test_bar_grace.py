"""A coin whose decision bar arrives late must not be dropped from the book for the whole day."""
from datetime import UTC, datetime

import pandas as pd

from tradebot.core.clock import SimClock
from tradebot.live.bridge import CompetitionStrategy
from tradebot.live.market_data import BarBuffer
from tests.test_strategy_bridge import _universe

DECISION_BAR = pd.Timestamp("2026-02-25T00:00", tz="UTC")


def _strategy(tmp_path, late_symbol):
    frames = _universe(n=10, days=70, end="2026-03-01")
    held_back = {"on": True}

    def fetch(symbol, start, end):
        df = frames[symbol]
        if symbol == late_symbol and held_back["on"]:
            df = df[df.index < DECISION_BAR]                 # this feed has not delivered the 00:00 bar yet
        return df[(df.index >= start) & (df.index < end)]

    clock = SimClock(datetime(2026, 2, 25, 0, 16, tzinfo=UTC))
    strat = CompetitionStrategy(BarBuffer(list(frames), fetch=fetch), mode="comp",
                                state_path=tmp_path / "s.json", clock=clock)
    return strat, held_back


def _refresh(strat, when):
    now = pd.Timestamp(when, tz="UTC")
    strat.buffer.update(now)
    strat._refresh_weights(now)


def _reference_weights(tmp_path):
    strat, held_back = _strategy(tmp_path / "ref", late_symbol=None)
    _refresh(strat, "2026-02-25T00:16")
    return strat.weights


def test_decision_waits_for_a_late_bar_then_includes_the_coin(tmp_path):
    reference = _reference_weights(tmp_path)
    late = reference[reference != 0].index[0]                # a coin the strategy wants to hold today
    strat, held_back = _strategy(tmp_path, late)
    _refresh(strat, "2026-02-25T00:16")
    assert strat.weights is None                             # no decision without the late coin yet
    held_back["on"] = False
    _refresh(strat, "2026-02-25T00:18")
    pd.testing.assert_series_equal(strat.weights, reference)


def test_decision_goes_ahead_without_the_coin_after_the_grace_period(tmp_path):
    reference = _reference_weights(tmp_path)
    late = reference[reference != 0].index[0]
    strat, _ = _strategy(tmp_path, late)
    _refresh(strat, "2026-02-25T00:16")
    _refresh(strat, "2026-02-25T00:46")                      # 30 min after the bar closed
    assert strat.weights is not None and strat.weights[late] == 0
