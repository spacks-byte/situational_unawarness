from datetime import datetime, timezone

from tradebot.core.clock import SimClock


def test_sim_clock_advances_time():
    clock = SimClock(start=datetime(2025, 1, 1, tzinfo=timezone.utc))
    clock.advance(30)
    assert clock.now() == datetime(2025, 1, 1, 0, 0, 30, tzinfo=timezone.utc)
    assert clock.monotonic() >= 30.0


def test_sim_clock_sleep_moves_time_forward():
    clock = SimClock(start=datetime(2025, 1, 1, tzinfo=timezone.utc))
    clock.sleep(5)
    assert clock.now() == datetime(2025, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
