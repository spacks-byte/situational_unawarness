from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

from src.engine.clock import Clock, RealClock


@dataclass
class IntentRecord:
    intent_id: str
    signal_id: str
    symbol: str
    kind: str
    side: str
    child_index: int
    payload: dict[str, Any]
    status: str = "PENDING_SEND"
    created_at: str | None = None
    response_id: str | None = None
    updated_at: str | None = None

    @classmethod
    def build(cls, signal_id: str, symbol: str, kind: str, side: str, child_index: int, payload: dict[str, Any], clock: Clock | None = None) -> "IntentRecord":
        intent_id = hashlib.sha256(
            json.dumps(
                {
                    "signal_id": signal_id,
                    "symbol": symbol,
                    "kind": kind,
                    "side": side,
                    "child_index": child_index,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        now = (clock or RealClock()).now().astimezone(timezone.utc).isoformat()
        return cls(
            intent_id=intent_id,
            signal_id=signal_id,
            symbol=symbol,
            kind=kind,
            side=side,
            child_index=child_index,
            payload=payload,
            status="PENDING_SEND",
            created_at=now,
            updated_at=now,
        )


class IntentJournal:
    """SQLite-backed journal for order intent tracking and idempotency."""

    def __init__(self, db_path: str | Path | None = None, *, memory: bool = False, clock: Clock | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else (":memory:" if memory else "engine_state.db")
        self.clock = clock or RealClock()
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                kind TEXT NOT NULL,
                side TEXT NOT NULL,
                child_index INTEGER NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                response_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_receipts (
                signal_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def add(self, intent: IntentRecord) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO intents (
                intent_id, signal_id, symbol, kind, side, child_index, payload, status,
                created_at, response_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent.intent_id,
                intent.signal_id,
                intent.symbol,
                intent.kind,
                intent.side,
                intent.child_index,
                json.dumps(intent.payload, sort_keys=True),
                intent.status,
                intent.created_at or self.clock.now().astimezone(timezone.utc).isoformat(),
                intent.response_id,
                intent.updated_at or self.clock.now().astimezone(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def get(self, intent_id: str) -> IntentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None:
            return None
        return IntentRecord(
            intent_id=row["intent_id"],
            signal_id=row["signal_id"],
            symbol=row["symbol"],
            kind=row["kind"],
            side=row["side"],
            child_index=row["child_index"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            created_at=row["created_at"],
            response_id=row["response_id"],
            updated_at=row["updated_at"],
        )

    def mark_sent(self, intent_id: str, response_id: str | None = None) -> None:
        self._conn.execute(
            "UPDATE intents SET status = 'SENT', response_id = ?, updated_at = ? WHERE intent_id = ?",
            (response_id, self.clock.now().astimezone(timezone.utc).isoformat(), intent_id),
        )
        self._conn.commit()

    def mark_resolved(self, intent_id: str) -> None:
        self._conn.execute(
            "UPDATE intents SET status = 'RESOLVED', updated_at = ? WHERE intent_id = ?",
            (self.clock.now().astimezone(timezone.utc).isoformat(), intent_id),
        )
        self._conn.commit()

    def mark_uncertain(self, intent_id: str) -> None:
        self._conn.execute(
            "UPDATE intents SET status = 'UNCERTAIN', updated_at = ? WHERE intent_id = ?",
            (self.clock.now().astimezone(timezone.utc).isoformat(), intent_id),
        )
        self._conn.commit()

    def exists_for_signal(self, signal_id: str, symbol: str, kind: str, side: str, child_index: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM intents WHERE signal_id = ? AND symbol = ? AND kind = ? AND side = ? AND child_index = ?",
            (signal_id, symbol, kind, side, child_index),
        ).fetchone()
        return row is not None

    def exists_signal(self, signal_id: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM signal_receipts WHERE signal_id = ?
            UNION ALL
            SELECT 1 FROM intents WHERE signal_id = ?
            LIMIT 1
            """,
            (signal_id, signal_id),
        ).fetchone()
        return row is not None

    def claim_signal(self, signal_id: str) -> bool:
        if self.exists_signal(signal_id):
            return False
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO signal_receipts (signal_id, received_at) VALUES (?, ?)",
            (signal_id, self.clock.now().astimezone(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def close(self) -> None:
        self._conn.close()
