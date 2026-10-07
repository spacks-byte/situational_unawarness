"""Best-effort, idempotent upload of terminal trade intents to Supabase."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import find_dotenv, load_dotenv

log = logging.getLogger(__name__)


class SupabaseTradeUploader:
    def __init__(self, *, timeout: float = 10.0) -> None:
        load_dotenv(find_dotenv(usecwd=True))
        url = os.getenv("SUPABASE_URL", "").rstrip("/")
        if url.endswith("/rest/v1"):
            url = url[:-len("/rest/v1")].rstrip("/")
        key = os.getenv("SUPABASE_SERVICE_KEY", "")
        self.enabled = bool(url and key)
        self.endpoint = (f"{url}/rest/v1/trade_transactions"
                         "?on_conflict=bot_id%2Cintent_id") if self.enabled else ""
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        self.timeout = timeout

    def upload(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        response = requests.post(self.endpoint, headers=self.headers, json=payload, timeout=self.timeout)
        if not response.ok:
            raise RuntimeError(f"Supabase trade upload failed ({response.status_code}): {response.text[:500]}")

    @staticmethod
    def transaction(order: dict[str, Any], *, environment: str, bot_id: str) -> dict[str, Any] | None:
        row = order.get("row") or {}
        side = str(order.get("side", "UNKNOWN")).upper()
        operation = {
            "BUY": "open_long",
            "SELL": "close_long",
            "SHORT_OPEN": "open_short",
            "SHORT_CLOSE": "close_short",
        }.get(side, "close_long")
        status = str(order.get("status", "REJECTED")).upper()
        filled_quantity = float(order.get("filled", 0.0) or 0.0)
        if filled_quantity <= 0:
            return None
        average_price = float(row.get("FilledAverPrice") or order.get("price") or 0.0)
        filled_value = filled_quantity * average_price if filled_quantity else None
        submitted = order.get("submitted_at")
        submitted_at = (datetime.fromtimestamp(float(submitted), tz=timezone.utc).isoformat()
                        if submitted else None)
        return {
            "bot_id": bot_id,
            "environment": environment,
            "signal_id": str(order.get("signal_id") or order.get("intent_id")),
            "intent_id": str(order["intent_id"]),
            "child_index": 0,
            "symbol": str(order["coin"]),
            "pair": f"{order['coin']}/USD",
            "operation_kind": operation,
            "side": side,
            "order_type": str(row.get("Type") or "LIMIT").upper(),
            "strategy": str(order["strategy"]),
            "status": "FILLED" if status == "FILLED" else "PARTIALLY_FILLED",
            "requested_value_usd": float(order.get("quantity", 0.0)) * float(order.get("price", 0.0)),
            "requested_quantity": float(order.get("quantity", 0.0)),
            "requested_price": float(order.get("price", 0.0)),
            "filled": filled_quantity > 0,
            "filled_quantity": filled_quantity or None,
            "average_fill_price": average_price if filled_quantity else None,
            "filled_value_usd": filled_value,
            "fee_amount": max(0.0, float(order.get("fee", 0.0) or 0.0)),
            "fee_currency": "USD",
            "exchange_order_id": str(order["order_id"]) if order.get("order_id") else None,
            "exchange_status": str(row.get("Status")) if row.get("Status") else None,
            "submitted_at": submitted_at,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "rejection_reason": order.get("rejection_reason"),
            "uncertainty_reason": order.get("uncertainty_reason"),
            "exchange_response": row or None,
            "metadata": {},
        }
