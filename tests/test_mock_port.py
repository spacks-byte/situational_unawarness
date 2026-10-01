from src.engine.ports.mock_port import MockExchangePort


def test_mock_port_place_order_updates_wallet_and_order_state():
    port = MockExchangePort(initial_wallet={"USD": 10000.0})

    result = port.place_order("BTC/USD", "BUY", 0.1, price=50000.0)

    assert result["Success"] is True
    assert result["OrderDetail"]["Side"] == "BUY"
    assert port.wallet["USD"] < 10000.0
    assert port.wallet["BTC"] > 0.0
    assert len(port.orders) == 1


def test_mock_port_open_short_tracks_position_state():
    port = MockExchangePort(initial_wallet={"USD": 20000.0})
    result = port.open_short("BTC/USD", 1000.0)

    assert result["Success"] is True
    assert result["Collateral"] == 1000.0
    assert len(port.short_positions) == 1
    assert port.short_positions[0]["Pair"] == "BTC/USD"


def test_mock_port_limit_short_is_pending_and_charged_fee():
    port = MockExchangePort(initial_wallet={"USD": 20000.0})
    result = port.open_short("BTC/USD", 1000.0, price=49000.0)

    assert result["Status"] == "PENDING"
    assert result["OpenFee"] == 1.0
    assert port.get_short_positions()["Positions"] == []
    assert port.list_open_orders()[0]["Side"] == "SHORT_OPEN"
