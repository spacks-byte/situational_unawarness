from tradebot.core.config import ExecutionConfig
from tradebot.engine.execution.runner import ExecutionRunner
from tradebot.exchange.mock import MockExchangePort
from tradebot.engine.state.intent_store import IntentJournal, IntentRecord


def test_uncertain_intent_resolves_from_observed_exposure_delta():
    journal = IntentJournal(memory=True)
    intent = IntentRecord.build(
        signal_id="sig",
        symbol="BTC",
        kind="open_long",
        side="BUY",
        child_index=0,
        payload={"amount_usd": 1000.0},
    )
    journal.add(intent)
    journal.mark_uncertain(intent.intent_id)
    runner = ExecutionRunner(MockExchangePort(), journal, ExecutionConfig())

    status = runner.reconcile_uncertain_intent(
        intent.intent_id,
        {"longs": {"BTC": 0.0}, "shorts": {}},
        {"longs": {"BTC": 1000.0}, "shorts": {}},
    )

    assert status == "RESOLVED"
    assert journal.get(intent.intent_id).status == "RESOLVED"


def test_restart_reconciles_uncertain_intents_from_fresh_snapshot():
    journal = IntentJournal(memory=True)
    intent = IntentRecord.build(
        signal_id="restart-sig",
        symbol="BTC",
        kind="open_long",
        side="BUY",
        child_index=0,
        payload={"amount_usd": 1000.0, "before": {"longs": {"BTC": 0.0}}},
    )
    journal.add(intent)
    journal.mark_uncertain(intent.intent_id)
    runner = ExecutionRunner(MockExchangePort(), journal, ExecutionConfig())

    assert runner.reconcile_uncertain_intents(
        {"longs": {"BTC": 1000.0}, "shorts": {}}
    ) == [intent.intent_id]
    assert journal.get(intent.intent_id).status == "RESOLVED"
