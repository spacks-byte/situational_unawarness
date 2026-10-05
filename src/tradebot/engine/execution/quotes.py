"""Fixed quote execution in the engine, independent of target-weight rebalancing."""
from tradebot.engine.schema.models import QuoteBatch
from tradebot.engine.state.portfolio import AccountBlocked, MM


class QuoteExecutor:
    def __init__(self, account, clock):
        self.account, self.clock = account, clock

    def cancel_owned(self):
        for order in list(self.account.active(MM)):
            if order.get("order_id") is not None:
                self.account.cancel(MM, order["order_id"])
        self.account.sync()
        if self.account.active(MM):
            raise AccountBlocked("MM cancellation is not confirmed")

    def run_once(self, strategy):
        a = self.account
        now = self.clock.now().timestamp()
        due = a.state["next_refresh"]
        if due is not None and now < due:
            return {"status": "HOLD", "signal_id": (a.state.get("batch") or {}).get("signal_id")}
        self.cancel_owned()
        # Cancel/fill reconciliation completes before the strategy sees its book.
        batch = strategy(a.report())
        if not isinstance(batch, QuoteBatch) or batch.strategy_id != MM:
            raise ValueError("quote strategy must return its own QuoteBatch")
        if (self.clock.now()-batch.timestamp).total_seconds() >= batch.refresh_seconds:
            raise AccountBlocked("quote batch expired before execution")
        a.state["next_refresh"] = batch.timestamp.timestamp() + batch.refresh_seconds
        a.state["batch"] = batch.model_dump(mode="json")
        prepared = a.prepare_quotes(batch)
        results = []
        for order in prepared:
            if self.clock.now().timestamp() >= a.state["next_refresh"]:
                order["status"] = "REJECTED"
                a.store.save("quote_expired", {"intent": order["intent_id"]})
                continue
            results.append(a.submit(order))
        a.sync()
        return {"status": "DRY_RUN" if a.dry_run else "QUOTED", "signal_id": batch.signal_id,
                "quotes": len(batch.quotes), "submitted": sum(r.get("Success") is True for r in results),
                "observations": batch.observations}
