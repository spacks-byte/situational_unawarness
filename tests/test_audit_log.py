import json

from tradebot.core.clock import SimClock
from tradebot.engine.state.audit_log import AuditLog


def test_audit_log_writes_structured_events(tmp_path):
    path = tmp_path / "events.jsonl"
    audit = AuditLog(path, clock=SimClock())

    audit.record("execution", {"signal_id": "sig-1", "status": "SENT"})

    event = json.loads(path.read_text(encoding="utf-8").strip())
    assert event["event"] == "execution"
    assert event["payload"]["signal_id"] == "sig-1"
    assert event["timestamp"].endswith("+00:00")
