"""
Symbol conventions.

Inside the project a symbol is always the bare coin, e.g. "BTC". Convert at the edges:
  - Roostoo pairs:   "BTC/USD"  (to_pair)
  - Binance symbols: "BTCUSDT"  (to_binance, data layer only)
Every helper accepts any of the three forms.
"""

ROOSTOO_QUOTE = "USD"
BINANCE_QUOTE = "USDT"


def to_coin(symbol: str) -> str:
    """'BTC', 'btc', 'BTC/USD', 'BTCUSDT' -> 'BTC'"""
    s = str(symbol).strip().upper()
    if "/" in s:
        return s.split("/", 1)[0]
    if s.endswith(BINANCE_QUOTE) and len(s) > len(BINANCE_QUOTE):
        return s[: -len(BINANCE_QUOTE)]
    return s


def to_pair(symbol: str) -> str:
    """Any form -> Roostoo pair, e.g. 'BTC/USD'."""
    return f"{to_coin(symbol)}/{ROOSTOO_QUOTE}"


def to_binance(symbol: str) -> str:
    """Any form -> Binance spot symbol, e.g. 'BTCUSDT'."""
    return f"{to_coin(symbol)}{BINANCE_QUOTE}"
