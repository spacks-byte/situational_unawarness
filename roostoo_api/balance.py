from __future__ import annotations

from roostoo_api._loader import load_raw_module

_raw = load_raw_module("balance")

get_balance = _raw.get_balance

__all__ = ["get_balance"]
