from tradebot.engine.state.snapshot import normalize_exchange_snapshot


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


def test_snapshot_reads_roostoo_spot_wallet_key():
    """Real /v3/balance responses use "SpotWallet"; reading only "Wallet" made live equity $0."""
    from tradebot.engine.state.snapshot import normalize_exchange_snapshot

    snapshot = normalize_exchange_snapshot(
        {"Success": True, "SpotWallet": {"USD": {"Free": 50000, "Lock": 0}, "BTC": {"Free": 0.5, "Lock": 0}}},
        {"Positions": []},
        {"Data": {"BTC/USD": {"LastPrice": 80000}}},
    )

    assert snapshot["cash_usd"] == 50000
    assert snapshot["equity_usd"] == 90000
