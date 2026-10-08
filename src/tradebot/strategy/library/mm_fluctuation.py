"""Combined Guéant/alpha quotes ported from shared-backtest/native/strategies.hpp.

Volatility and alpha use completed Binance seconds with an additional feature lag.
The optional midpoint reference uses the latest fresh bid/ask at quote time.
Empty *observed* candles count; missing seconds never get fabricated.
"""
from __future__ import annotations

import math
import sys
from decimal import Decimal, ROUND_DOWN

import pandas as pd

from tradebot.core.config import MarketMakingConfig
from tradebot.engine.schema.models import LimitQuote, QuoteBatch
from tradebot.strategy.base import Strategy


def combined_quotes(reference, variance, alpha, inventory_units, anchor, tick, *, enforce_one_tick_distance=True):
    """Frozen zero-adverse PoC parameters; infinite live horizon uses a 30s signal."""
    if not all(math.isfinite(x) for x in (reference, variance, alpha, inventory_units, anchor, tick)) \
            or min(reference, anchor, tick) <= 0 or variance < 0:
        return 0.0, 0.0
    gamma, kappa, arrival = 0.1, 2000.0, 0.5
    scale = reference / anchor
    ratio = gamma / kappa
    premium = math.log1p(ratio) / gamma
    power_log = (1.0 / ratio + 1.0) * math.log1p(ratio)
    c2 = math.sqrt(gamma * (variance * scale * scale) / (2.0 * arrival * kappa) * math.exp(power_log))
    forecast = alpha * scale * 30.0 * (-math.expm1(-1.0))
    center = -inventory_units * c2 + forecast
    half = min(max(premium + 0.5 * c2, 2e-4 * scale), 0.01 * scale)
    distance = max(2e-4 * scale, tick / anchor if enforce_one_tick_distance else 0.0, 1e-12 * scale)
    bid = min(reference + anchor * (center - half), reference - anchor * distance)
    ask = max(reference + anchor * (center + half), reference + anchor * distance)

    def snap(price):
        coordinate = price / tick
        nearest = math.floor(coordinate + 0.5)
        tolerance = min(1e-7, 8 * sys.float_info.epsilon * max(1.0, abs(coordinate)))
        return nearest if abs(coordinate - nearest) <= tolerance else coordinate

    bid, ask = math.floor(snap(bid)) * tick, math.ceil(snap(ask)) * tick
    return (bid if 0 < bid < reference else 0.0, ask if ask > reference else 0.0)


def accepted_spread(bid, ask):
    minimum, proceeds, cost = bid * 1.001, ask * 0.9995, bid * 1.0005
    tolerance = 8 * sys.float_info.epsilon * max(abs(ask), abs(minimum), abs(proceeds), abs(cost))
    return bid > 0 and ask > bid and ask - minimum > tolerance and proceeds - cost > tolerance


def floor_quantity(qty, precision):
    return float(Decimal(str(max(qty, 0))).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN))


class MMFluctuation(Strategy):
    name = "mm-10m-fluctuation"
    output_kind = "quotes"

    def __init__(self, config: MarketMakingConfig | None = None):
        super().__init__()
        self.config = config or MarketMakingConfig()

    def generate_quotes(self, data, *, now, books, features, rules, market_quotes=None, feature_now=None):
        """Updates serializable EWMA/anchor state; caller commits it with the batch.

        Each feature cursor advances once. A gap resets warmup; the startup anchor
        and base lot survive gaps and restarts. No current/future row is consumed.
        """
        cfg = self.config
        second = int(pd.Timestamp(feature_now if feature_now is not None else now).timestamp())
        quotes, observations = [], {}
        wv, wa = -math.expm1(-math.log(2) / 300), -math.expm1(-math.log(2) / 30)
        for coin, book in books.items():
            state = features.setdefault(coin, {})
            df = data.get(coin, pd.DataFrame())
            cursor = state.get("cursor", second - cfg.warmup_seconds - 1)
            recent = df[(df.index > pd.Timestamp(cursor, unit="s", tz="UTC")) &
                        (df.index < pd.Timestamp(second, unit="s", tz="UTC"))] if len(df) else df
            for ts, row in recent.iterrows():
                stamp, close = int(ts.timestamp()), float(row["close"])
                if stamp != state.get("cursor", stamp - 1) + 1 or not math.isfinite(close) or close <= 0:
                    anchor, lot = state.get("anchor"), state.get("lot")
                    state.clear()
                    if anchor:
                        state.update(anchor=anchor, lot=lot)
                if not math.isfinite(close) or close <= 0:
                    continue
                if "reference" in state:
                    r = close / state["reference"] - 1
                    d = r - state["mean"]
                    state["mean"] += wv * d
                    state["variance"] = (1-wv) * (state["variance"] + wv*d*d)
                    state["alpha"] += wa * (r-state["alpha"])
                else:
                    state.update(mean=0.0, variance=0.0, alpha=0.0, count=0, history=[])
                state.update(reference=close, cursor=stamp, count=state["count"]+1)
                state["history"].append([stamp, close, state["variance"], state["alpha"]])
                state["history"] = state["history"][-cfg.feature_lag_seconds-1:]
            observations[coin] = {"latest_observation": state.get("cursor"), "reason": "warming_or_gap"}
            if state.get("cursor") != second-1 or state.get("count", 0) < cfg.warmup_seconds:
                continue
            lagged = next((v for v in state["history"] if v[0] == second-1-cfg.feature_lag_seconds), None)
            if lagged is None:
                continue
            reference, capacity_reference = lagged[1], state["reference"]
            if cfg.reference_source == "midpoint":
                market = (market_quotes or {}).get(coin, {})
                try:
                    best_bid, best_ask = float(market["bid"]), float(market["ask"])
                    received = pd.Timestamp(market["received_at"])
                    age = (pd.Timestamp(now) - received).total_seconds()
                    valid = (received.tzinfo is not None and math.isfinite(age) and
                             0 <= age <= cfg.max_book_age_seconds and
                             not market.get("book_stale", False) and
                             math.isfinite(best_bid) and math.isfinite(best_ask) and
                             0 < best_bid < best_ask)
                except (KeyError, TypeError, ValueError, OverflowError):
                    valid = False
                if not valid:
                    observations[coin]["reason"] = "missing_or_invalid_book"
                    continue
                reference = capacity_reference = best_bid + (best_ask - best_bid) / 2
                observations[coin].update(best_bid=best_bid, best_ask=best_ask,
                                          book_received_at=received.isoformat(), book_age_seconds=age)
            # Freeze the startup price for lot sizing and normalized quote scaling.
            # The reference price continues updating; the anchor survives restarts.
            if "anchor" not in state:
                state["anchor"] = state["reference"]
                state["lot"] = book["capital"] * cfg.lot_fraction / state["anchor"]
            rule = rules.get(f"{coin}/USD")
            if not rule or not rule.get("CanTrade", False):
                observations[coin]["reason"] = "not_tradeable"
                continue
            tick = 10.0 ** -int(rule["PricePrecision"])
            bid, ask = combined_quotes(reference, max(lagged[2], 1e-12), lagged[3],
                                       book["quantity"] / state["lot"], state["anchor"], tick,
                                       enforce_one_tick_distance=cfg.enforce_one_tick_distance)
            observations[coin].update(feature_input=lagged[0], bid=bid, ask=ask, anchor=state["anchor"],
                                      lot=state["lot"], inventory_units=book["quantity"] / state["lot"], reference=reference, variance=max(lagged[2], 1e-12),
                                      reference_source=cfg.reference_source,
                                      alpha=lagged[3], reason="spread_filter")
            if not accepted_spread(bid, ask):
                continue
            observations[coin]["reason"] = "accepted"
            # Same current-reference capacity and fixed lot as the PoC. Sell proceeds
            # cannot finance this bid; resources are reserved by the engine before sends.
            capacity = max(0.0, book["capital"] * cfg.inventory_fraction / capacity_reference - book["quantity"])
            sizes = {"BUY": min(state["lot"], capacity, book["cash"] / (bid * 1.0005)),
                     "SELL": min(state["lot"], book["quantity"])}
            for side, price in (("BUY", bid), ("SELL", ask)):
                tolerance = min(8*sys.float_info.epsilon*max(abs(price), abs(capacity_reference)), tick*1e-7)
                if side == "BUY" and price >= capacity_reference-tolerance or side == "SELL" and price <= capacity_reference+tolerance:
                    continue
                qty = floor_quantity(sizes[side], int(rule["AmountPrecision"]))
                if qty > 0 and qty * price >= float(rule["MiniOrder"]):
                    quotes.append(LimitQuote(symbol=coin, side=side, quantity=qty, price=price))
        return QuoteBatch(signal_id=f"{self.name}:{second}", timestamp=now,
                          refresh_seconds=cfg.refresh_seconds, quotes=quotes, observations=observations)
