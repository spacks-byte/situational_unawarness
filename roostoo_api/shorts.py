from __future__ import annotations

from roostoo_api._loader import load_raw_module

_raw = load_raw_module("shorts")

open_short = _raw.open_short
close_short = _raw.close_short
get_short_positions = _raw.get_short_positions

__all__ = ["open_short", "close_short", "get_short_positions"]
