import pytest

from tradebot.live.repeg import RepegPort


class _Port:
    is_live = False

    def __init__(self, last):
        self.last, self.sent = last, []

    def get_ticker(self, pair=None):
        return {"Success": True, "Data": {pair: {"LastPrice": self.last}} if self.last else {}}

    def place_order(self, pair_or_coin, side, quantity, price=None, order_type=None):
        self.sent.append((side, quantity, price, order_type))
        return {"Success": True}

    def open_short(self, pair_or_coin, collateral, price=None):
        self.sent.append(("SHORT", collateral, price, None))
        return {"Success": True}


def test_limit_buy_is_pegged_to_the_fresh_price_and_keeps_its_usd_size():
    inner = _Port(last=99.0)                               # the strategy saw 100, the market is now 99
    RepegPort(inner, offset_bps=5).place_order("ETH/USD", "BUY", 10.0, price=100 * (1 - 5e-4), order_type="LIMIT")
    side, qty, price, _ = inner.sent[0]
    assert price == pytest.approx(99 * (1 - 5e-4))         # passive again, not crossed
    assert qty * price == pytest.approx(10.0 * 100 * (1 - 5e-4))


def test_limit_short_open_is_pegged_above_the_fresh_price():
    inner = _Port(last=101.0)
    RepegPort(inner, offset_bps=5).open_short("ETH/USD", 1000.0, price=100 * (1 + 5e-4))
    assert inner.sent[0][1:3] == (1000.0, pytest.approx(101 * (1 + 5e-4)))


def test_market_orders_pass_through():
    inner = _Port(last=99.0)
    port = RepegPort(inner, offset_bps=5)
    port.place_order("ETH/USD", "SELL", 1.0, price=None, order_type="MARKET")
    port.open_short("ETH/USD", 1000.0)
    assert inner.sent == [("SELL", 1.0, None, "MARKET"), ("SHORT", 1000.0, None, None)]


def test_limit_sell_is_pegged_above_the_fresh_price_and_keeps_its_quantity():
    inner = _Port(last=98.0)                               # exits are limits too: re-peg them
    RepegPort(inner, offset_bps=5).place_order("ETH", "SELL", 2.5, price=100 * (1 + 5e-4), order_type="LIMIT")
    side, qty, price, _ = inner.sent[0]
    assert side == "SELL" and qty == 2.5                   # sell exactly what is held
    assert price == pytest.approx(98 * (1 + 5e-4))


@pytest.mark.parametrize("last", [0.0, 110.0])            # no price, or a 10% jump since the snapshot
def test_order_is_not_sent_without_a_usable_fresh_price(last):
    inner = _Port(last=last)
    out = RepegPort(inner, offset_bps=5).place_order("ETH/USD", "BUY", 10.0, price=99.95, order_type="LIMIT")
    assert out["Success"] is False and inner.sent == []
