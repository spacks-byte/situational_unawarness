"""Fixed quote execution in the engine, independent of target-weight rebalancing."""
from tradebot.engine.schema.models import QuoteBatch
from tradebot.engine.state.portfolio import AccountBlocked, MM


class QuoteExecutor:
    def __init__(self, account, clock):
        self.account, self.clock = account, clock

    def cancel_owned(self):
        """Request cancellation of every MM order. Returns the (coin, side) pairs still awaiting the
        venue's confirmation: their reservations stay held and they get no replacement this refresh."""
        now = self.clock.now().timestamp()
        for order in list(self.account.active(MM)):
            if order.get("order_id") is None:
                continue
            retry_due = now - order.get("cancel_requested_at", 0) >= self.account.config.cancel_retry_seconds
            if order["status"] != "CANCELING" or retry_due:     # idempotent re-send when unconfirmed
                self.account.cancel(MM, order["order_id"])
        self.account.sync()
        return {(o["coin"], o["side"]) for o in self.account.active(MM)}

    def _stats(self):
        return self.account.state.setdefault("quote_stats", {"submitted": 0, "deadline_missed": 0,
                                                             "max_age_seconds": 0.0, "last_age_seconds": None})

    def run_once(self, strategy):
        a = self.account
        now = self.clock.now().timestamp()
        due = a.state["next_refresh"]
        if due is not None and now < due:
            return {"status": "HOLD", "signal_id": (a.state.get("batch") or {}).get("signal_id")}
        waiting = self.cancel_owned()
        # Cancel/fill reconciliation completes before the strategy sees its book.
        batch = strategy(a.report())
        if not isinstance(batch, QuoteBatch) or batch.strategy_id != MM:
            raise ValueError("quote strategy must return its own QuoteBatch")
        stats = self._stats()
        stats["decision_lag_seconds"] = (self.clock.now()-batch.timestamp).total_seconds()  # data age at decision
        if (self.clock.now()-batch.timestamp).total_seconds() >= batch.refresh_seconds:
            stats["deadline_missed"] += len(batch.quotes)
            a.store.save("quote_batch_expired", {"signal": batch.signal_id})
            raise AccountBlocked("quote batch expired before execution")
        a.state["next_refresh"] = batch.timestamp.timestamp() + batch.refresh_seconds
        a.state["batch"] = batch.model_dump(mode="json")
        prepared = a.prepare_quotes(batch)
        results, errors = [], []
        for order in prepared:
            age = self.clock.now().timestamp() - batch.timestamp.timestamp()
            if self.clock.now().timestamp() >= a.state["next_refresh"]:
                order["status"] = "REJECTED"
                stats["deadline_missed"] += 1
                a.store.save("quote_expired", {"intent": order["intent_id"], "age_seconds": age})
                continue
            try:
                result = a.submit(order)
            except AccountBlocked as exc:
                # Unknown outcome on this coin only (it is now restricted); quote the others
                errors.append(f"{order['coin']} {order['side']}: {exc}")
                continue
            results.append(result)
            if result.get("Expired"):
                stats["deadline_missed"] += 1
            if result.get("Success") is True:
                stats["submitted"] += 1
                stats["last_age_seconds"] = age
                stats["max_age_seconds"] = max(stats["max_age_seconds"], age)
        a.sync()
        out = {"status": "DRY_RUN" if a.dry_run else "QUOTED", "signal_id": batch.signal_id,
               "quotes": len(batch.quotes), "submitted": sum(r.get("Success") is True for r in results),
               "observations": batch.observations, "awaiting_cancel": sorted(f"{c} {s}" for c, s in waiting),
               "quote_age_seconds": stats["last_age_seconds"], "deadline_missed_total": stats["deadline_missed"],
               "decision_lag_seconds": stats["decision_lag_seconds"]}
        if errors:
            out["uncertain"] = errors
        return out
