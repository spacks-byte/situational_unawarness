from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.engine.schema.models import LongTarget, TargetPortfolio


def make_target(**changes):
    payload = {
        "strategy_id": "s",
        "strategy_version": "v1",
        "signal_id": "sig",
        "timestamp": datetime.now(UTC),
        "longs": [LongTarget(symbol="btc", notional_usd=100.0)],
    }
    payload.update(changes)
    return payload


def test_schema_normalizes_symbols_and_flatten():
    target = TargetPortfolio(**make_target(flatten=[" eth ", "ETH", ""]))
    assert target.longs[0].symbol == "BTC"
    assert target.flatten == ["ETH"]


def test_schema_rejects_naive_timestamp_and_duplicate_symbols():
    with pytest.raises(ValidationError):
        TargetPortfolio(**make_target(timestamp=datetime.now()))
    with pytest.raises(ValidationError):
        TargetPortfolio(**make_target(longs=[LongTarget(symbol="BTC", notional_usd=100.0), LongTarget(symbol="btc", notional_usd=200.0)]))
