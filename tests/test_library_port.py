from src.engine.ports.library_port import LibraryExchangePort


def test_library_port_forwards_short_close_and_open_orders():
    port = LibraryExchangePort()
    calls = []
    port._api["close_short"] = lambda pair, close_qty=None, close_pct=None: calls.append(
        (pair, close_qty, close_pct)
    ) or {"Success": True}
    port._api["query_order"] = lambda **kwargs: calls.append(kwargs) or {"OrderMatched": []}

    assert port.close_short("BTC", close_qty=0.25) == {"Success": True}
    assert port.list_open_orders() == {"OrderMatched": []}
    assert calls == [
        ("BTC", 0.25, None),
        {"pending_only": True},
    ]
