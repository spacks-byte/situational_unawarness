# Real-order validation of the shared account (separate test account only)

The simulator and the venue-shaped fixtures (`tests/fixtures/roostoo`) only show how the
coordinator behaves against Roostoo as the documentation describes it. This protocol checks the
assumptions that only the real venue can confirm. It is for a **separate Roostoo test account**.

**Never run it with the competition keys, on the competition account, or while the competition bot
is running from the same machine and state directories.** Each step sends a small number of tiny
limit orders. Get explicit authorization before every run. Record results in the table at the end.

## Setup

1. Create `.env.test` with the test account's `ROOSTOO_API_KEY` / `ROOSTOO_API_SECRET`. Never commit it.
2. Use fresh directories: `TRADEBOT_LOCK_DIR=var/test-locks`, `--state-dir var/validation-<date>`.
3. Run `python -m tradebot --config config/market-making.yaml account preflight` and confirm it exits 0,
   reports the test account's equity (not the competition account's), and shows no unknown orders.
4. Pick one cheap pair with a coarse tick (for example PEPE/USD). Every order below is a passive
   limit at least 5% away from the touch, at the venue's minimum order value, unless the step says otherwise.

## Steps

Each step lists the assumption under test and the pass criterion. Steps V1-V4 use the interactive
API menu (`python -m tradebot api`) with the test keys. Steps V5-V10 run the coordinator
(`live --live`, `ROOSTOO_CONFIRM_LIVE=YES`) against the test account with `--max-loops`.

| # | Assumption | Procedure | Pass if |
|---|---|---|---|
| V1 | PENDING rows report `FilledQuantity == Quantity` while unfilled (P1) | place a far limit, `query_order` it | the row's FilledQuantity is either 0 or equal to Quantity, and `remaining_qty` = Quantity |
| V2 | Empty query = `Success:false`, "no order matched" | query a nonexistent order id | exact ErrMsg recorded; `order_rows` returns `[]` |
| V3 | History page order and paging (`offset`/`limit`) | place and cancel 3 far limits; query history with `limit=2`, offsets 0 and 2 | record page order (newest or oldest first); the union of pages contains all 3 ids exactly once |
| V4 | Commission rounding on a real fill (P2) | one minimum-size marketable limit buy, then a sell | `CommissionChargeValue` vs `quantity x price x 5 bps`, and the wallet's USD change vs the reported values: difference recorded; must be under 3 bps of notional |
| V5 | Cancel settles and releases the Lock (P4) | coordinator posts MM quotes, then `STOP_MM` | every quote reaches CANCELED; USD Lock returns to 0; status shows no `awaiting_cancel` and no open reconciliation issues |
| V6 | Lost spot response is recovered from history | run with a test-only fault hook that drops one `place_order` reply (timeout after send) | the next syncs adopt exactly that order id; no second order on the venue; advisory issue resolves after 2 clean syncs; other valid operations continue |
| V7 | Lost market short-open response (P3) | same fault hook on one minimum RXM `open_short` | position change booked; no second short; short issue resolves; cash reconciles inside tolerance |
| V8 | Lost short-close response | fault hook on a partial `close_short` | close booked from the position; RXM cash re-baselined once; no residual issue |
| V9 | Restart mid-cancel | `STOP_MM`, kill the process after the cancel request is sent, restart | the restarted coordinator confirms the cancel; no duplicate cancel error left unresolved |
| V10 | Request budget | run both strategies for 30 minutes | `http_last_minute` never above 25; no HTTP 429; `quote_stats.deadline_missed` recorded |

The fault hook for V6-V8 must live in a throwaway branch (a port wrapper like
`tests/venue_shaped.VenueShapedPort.lose`), never in the deployed code path.

## After the run

1. `STOP_MM`, wait for confirmed cancels, close the RXM short and spot positions by hand.
2. `account explain` must show no open reconciliation issues and no intents awaiting the venue.
3. Archive `var/validation-<date>/portfolio.db`, `status.json` and the bot log with the results.

## Results

| Date | Steps | Result | Notes (page order, rounding size, anything unexpected) |
|---|---|---|---|
| | | | |

Deployment of the shared coordinator on the competition account requires V1-V10 recorded as passing,
or each deviation reviewed and covered by a fix and a regression test.

## False-fill recovery (dry-run first)

Roostoo orders fill completely or execute nothing. Both PENDING and CANCELED may
report a placeholder FilledQuantity equal to Quantity with zero execution price,
asset movements and commission. Include canceled BUY and SELL responses in V1/V5.
No reconciliation finding may refuse otherwise valid orders, including after restart.

The recovery command reads authoritative order history and local state. If configured,
it also checks Supabase identity. It does not place/cancel orders or upload changes.

```bash
python -m tradebot --config config/market-making.yaml account repair-fills --output /tmp/fill-repair.json
# Only after explicit review/authorization, with all coordinators on this account stopped:
python -m tradebot --config config/market-making.yaml account repair-fills --apply /tmp/fill-repair.json
```

Apply takes the existing account lock, revalidates the manifest against current state
and venue evidence, makes a SQLite backup, and commits corrections plus upload-outbox
revisions atomically. The next live uploader sends the queued corrections. Verify the
Supabase status column accepts CANCELED before applying; incompatible constraints need
an explicit schema migration. Failed uploads stay queued and do not halt trading.

Repairs preserve allocations, features and unrelated books. MM accounting is replayed
from the opening allocation only if the journal reproduces the current book. RXM uses
its recorded opening baseline; legacy RXM permits a provable false-BUY reversal with
no later cost-basis dependency. Missing history or an unprovable baseline produces an
error in the dry-run; never reset ownership from the physical wallet. Cash rounding
shares are replayed when their attribution can be established from the journal.

Review every proposed cash, quantity, cost and realized-P&L change. Repeated application
of the same completed repair is a no-op. Reconciliation issues resolve through normal
observations and never control trading. The dashboard issue-flag UI is deferred; the
next agent must read root AGENTS.md for its priority after dashboard server deployment.
