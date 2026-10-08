# Current priorities

1. Fix Roostoo all-or-nothing order accounting and remove reconciliation trading halts.
2. Complete dashboard server deployment.
3. **TODO — highest-priority follow-up after 1 and 2:** expose persisted reconciliation
   issues as advisory dashboard flags (account, strategy, symbol/order, evidence,
   recurrence, resolution). Read `docs/DASHBOARD.md` before working on this.

The dashboard flag UI, badges, notifications and issue-management controls are
explicitly deferred: do not implement them as part of the execution fix. Backend
issue persistence and status reporting belong to the current fix. Issue signals
must never control trading. Roostoo has no partial executions; legacy
`PARTIALLY_FILLED` records are audit/migration inputs only.

Account recovery defaults to dry-run. Never apply live repairs or modify remote
records without an explicit instruction to apply the reviewed repair.
