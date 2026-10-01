from __future__ import annotations

from roostoo_api._loader import load_raw_module

_raw = load_raw_module("portfolio_worth")

get_portfolio_worth = _raw.get_portfolio_worth

__all__ = ["get_portfolio_worth"]
