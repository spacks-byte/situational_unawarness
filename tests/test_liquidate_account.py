from scripts.liquidate_account import _rows


def test_no_order_matched_is_empty_open_order_list():
    assert _rows({"Success": False, "ErrMsg": "no order matched"}) == []
