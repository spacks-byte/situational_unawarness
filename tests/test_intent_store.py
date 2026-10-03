from tradebot.engine.state.intent_store import IntentJournal, IntentRecord


def test_intent_record_has_deterministic_id():
    a = IntentRecord.build("sig-1", "BTC/USD", "spot", "BUY", 0, {"qty": 1.0})
    b = IntentRecord.build("sig-1", "BTC/USD", "spot", "BUY", 0, {"qty": 1.0})
    assert a.intent_id == b.intent_id


def test_intent_journal_persists_and_updates_status():
    journal = IntentJournal(memory=True)
    intent = IntentRecord.build("sig-2", "ETH/USD", "spot", "SELL", 2, {"qty": 2.0})
    journal.add(intent)
    stored = journal.get(intent.intent_id)
    assert stored is not None
    assert stored.status == "PENDING_SEND"

    journal.mark_sent(intent.intent_id, response_id="abc")
    stored = journal.get(intent.intent_id)
    assert stored is not None and stored.status == "SENT"
    assert stored.response_id == "abc"

    journal.close()
