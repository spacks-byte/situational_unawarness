from src.engine.state.snapshot import normalize_exchange_snapshot


def test_normalize_exchange_snapshot_to_engine_state():
    snapshot = normalize_exchange_snapshot(
        balance={
            "Wallet": {
                "USD": {"Free": 4000.0, "Lock": 100.0},
                "BTC": {"Free": 0.02, "Lock": 0.0},
            }
        },
        short_positions={
            "Positions": [
                {
                    "Pair": "ETH/USD",
                    "Collateral": 500.0,
                    "UnrealizedPNL": 25.0,
                    "EntryPrice": 2500.0,
                    "PositionStatus": "OPEN",
                }
            ]
        },
        tickers={"BTC/USD": {"LastPrice": 50000.0}},
    )

    assert snapshot["cash_usd"] == 4100.0
    assert snapshot["longs"] == {"BTC": 1000.0}
    assert snapshot["shorts"] == {"ETH": 500.0}
    assert snapshot["equity_usd"] == 5625.0
    assert snapshot["prices"] == {"BTC": 50000.0}
    assert snapshot["entry_prices"] == {"ETH": 2500.0}
