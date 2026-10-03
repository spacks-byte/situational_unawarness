from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path
from typing import Any

from tradebot.core.clock import Clock, RealClock


class AuditLog:
    """Append-only structured event log for execution and risk decisions."""

    def __init__(self, path: str | Path, *, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or RealClock()

    def record(self, event: str, payload: dict[str, Any]) -> None:
        entry = {
            "timestamp": self.clock.now().astimezone(timezone.utc).isoformat(),
            "event": event,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")