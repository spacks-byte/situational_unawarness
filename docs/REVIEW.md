# Project review (October 2026)

A review of the whole repository against the hackathon rules (autonomous bot on Roostoo, $100k, spot long and 1x short, 0.05% maker / 0.1% taker fees, judged on return, Sharpe, Sortino and Calmar, deployed on AWS EC2, open source), followed by a refactor into one package (see `docs/ARCHITECTURE.md`). The trading strategy itself is out of scope.

## 1. Bugs fixed in the refactor

| Severity | Problem | Fix |
|---|---|---|
| **Critical** | The live engine read the wallet from a `Wallet` key, but Roostoo's `/v3/balance` returns `SpotWallet`. **On the real exchange the engine saw $0 equity and rejected every signal** (`invalid_equity`). Found by running a cycle against the mock exchange. | The snapshot reads `SpotWallet`, falling back to `Wallet`; the mock exchange returns the real shape; regression test added |
| High | The backtest charged the 0.05% maker fee on short opens. Roostoo charges 0.1% on short collateral, even for limit opens. Long/short backtests looked better than reality. | One `FeeSchedule` (spot maker/taker, short open/close) used everywhere. The long/short MA baseline moved from −28.8% to −30.8%; long-only results are unchanged. |
| High | Downloading a narrow date range **overwrote** a coin's stored full-year candle file with just that range | Downloads merge into the existing Parquet file |
| High | No HTTP timeouts anywhere, so one hung request could freeze the bot forever | Every request has a timeout (`exchange.request_timeout_seconds`) |
| High | Live sells were always market orders (0.1%), against the team's limit-only decision and the backtest | `order_policy: limit_only`: buys, sells and short opens are limit orders; short closes stay market (the API has no price for them) |
| High | `fill_timeout_seconds: 30` cancelled limit orders after 30 s, and the signal was then marked done, so they were never re-placed | Default changed to one bar (900 s); each new bar's signal re-places at the new price, as in the backtest |
| Medium | `exchangeInfo` downloaded on every order (twice for market sells) and `serverTime` fetched before every signed call: about 10+ requests per cycle, risky under the "no excessive requests" rule | `exchangeInfo` cached (1 h), server-time offset re-synced every 10 min, client throttle, and order prices taken from the snapshot. One engine cycle is now **5 requests**. |
| Medium | Error handlers printed `N/A` instead of the error body: `if e.response` is False for 4xx/5xx responses | `RoostooError` carries the status and body; every request is logged |
| Medium | `place_order` network errors weren't caught; other calls returned `None` and printed | Typed `RoostooError`. GET requests are retried with backoff; orders are **never** retried automatically. |
| Medium | `cancel_order()` with no arguments cancelled every order | Requires `cancel_all=True` |
| Medium | `close_short` accepted both `close_qty` and `close_pct` and sent both | Requires exactly one |
| Medium | The engine's YAML config was never loaded (`from_yaml` was unused) | `Engine` loads `config/default.yaml` (or `--config` / `$TRADEBOT_CONFIG`) |
| Medium | Risk cash check used the taker fee for limit orders | Uses the maker fee under `limit_only` |
| Low | `__all__` overwritten in `engine/__init__.py` and `state/__init__.py`, so `Engine` and `AuditLog` weren't exported | Fixed |
| Low | Short intents stored `side="short"` for both opens and closes | `SHORT_OPEN` / `SHORT_CLOSE` |
| Low | Mock exchange charged no spot fees and started with $50k | Fees from config; $100k |
| Low | A LIMIT price given as a string could crash the MiniOrder check | Decimal parsing with validation |

**Removed because the limit-only design replaces them:**
- `buy_coin_by_value` silently shrank orders down to 10% of the requested size.
- The market-sell "step size" loop made up to 12 attempts with a 1 s sleep. Quantities are now floored to the pair's `AmountPrecision` before sending.

## 2. Structural problems fixed

- **Three subsystems with no shared code:**
  - `crypto-roostoo-api/` (raw scripts), plus `roostoo_api/`, which loaded them through `sys.path` hacks (each module was loaded twice).
  - `src/engine/`.
  - `data_pipeline/` and `backtest/`.

  These are now **one package, `tradebot`**: `core` → `exchange` / `data` → `strategy` → `backtest` / `engine` → `cli`.
- **Duplication removed:**

  | Was | Now |
  |---|---|
  | 2 metrics modules (different units) | `core.metrics` |
  | 2 config systems; fees defined in 5 places | One `config/default.yaml` with a shared `fees` section |
  | 3 symbol formats with inline `f"{s}/USD"` | `core.symbols`; coin internally |
  | HMAC signing copy-pasted 5 times | `RoostooClient._sign` |
  | Argparse defaults duplicating the backtest dataclass | CLI defaults read from config |

- **Hygiene:**
  - Added `pyproject.toml`, which declares all dependencies; `pydantic` and `pyyaml` were missing before.
  - Removed the 62 tracked `.pyc` files and the broken `Roostoo-API-Documents` gitlink.
  - Wider `.gitignore`.
  - One root `.env.example` with placeholders.

## 3. Missing features (in priority order)

**Update Oct 3:** the quant's RXM strategy was ported into the package, and items 1, 2 and 7 below are now built (see section 6).

### P0: needed to compete
1. ~~**Live runner / entry point.**~~ **Done:** `python -m tradebot live`, which runs `tradebot.live.runner.LiveRunner`.
   - Failed loops back off exponentially instead of stopping the bot.
   - Restarts are safe, via the journal and the persisted strategy state.
   - It supports a kill switch, SIGINT/SIGTERM shutdown, a heartbeat and `status.json`.
2. ~~**Strategy adapter and live candle feed.**~~ **Done:** `tradebot.live.bridge.LiveStrategy` runs any backtested `Strategy` on live Binance candles (`BarBuffer`). Tests assert live weights equal backtest weights on real data.
   - Not built: a Roostoo-ticker fallback when Binance is unreachable. The bot holds its book on stale candles instead.
3. **AWS EC2 deployment.**
   - There is nothing for it yet.
   - **Needed:** a setup script (Python 3.11+, venv, `pip install .`), a systemd unit with `Restart=always`, log rotation, `.env` provisioning and a short runbook.
4. **Rotate the Roostoo API keys** before the repo goes public. They were pasted into an uncommitted `.env.example` and into chats. Git history is clean.

### P1: risk and observability
5. **Risk state is static.**
   - `RiskState` (kill switch, daily loss, drawdown) is never computed or updated, so `max_daily_loss_usd` and `max_drawdown_pct` do nothing.
   - **Needed:** track the equity high-water mark and daily start equity, plus a kill-switch file or flag.
6. **Order follow-up.**
   - SENT limit orders are never checked for fills; they are only cancelled when stale.
   - `reconcile_uncertain_intent` exists but nothing calls it.
   - Short opens with status `OPEN` are treated as resolved.
   - There are no fills table and no realised-P&L record.
7. ~~**Live performance records.**~~ **Mostly done:**
   - The runner appends one account snapshot per 15m bar to `snapshots.jsonl`, logs a heartbeat, and keeps the engine audit log.
   - The dashboard (`tradebot dashboard --source engine`) computes Sharpe, Sortino and Calmar from these.
   - The engine's own `equity_snapshot_interval_seconds` / `heart_beat_interval_seconds` settings remain reserved.
8. **Risk defaults don't fit a $100k portfolio.**
   - These would reject typical targets: `max_total_short_collateral_usd: 10000`, `max_per_symbol_exposure: 0.35`, and `max_order_value_usd: 25000`. The last is checked *before* child-order splitting.
   - A breach rejects the **whole** signal instead of scaling it down.
   - **Tuned for RXM** in `config/competition.yaml`. The defaults still don't fit, and whole-signal rejection remains.

### P2: correctness gaps
9. **Exchange rules aren't checked before sending.** The engine doesn't check `MiniOrder` or precision before sending. A rejected child order becomes `UNCERTAIN` and **aborts the rest of the plan**.
10. **Pending sells don't count as exposure** in the plan (pending buys and short opens do).
11. **Coins outside the strategy get sold.** Any wallet coin the strategy didn't mention is treated as a target of 0 and sold.
12. **Long stop-loss and take-profit never fire.** The snapshot only has entry prices for shorts.
13. **Configured guards aren't implemented:** `stale_data_seconds`, `spread_guard_pct`, `price_deviation_pct`, `fat_finger_limit_pct`, `pending_short_ttl_seconds`, `min_trade_interval_seconds` and `TargetPortfolio.ttl_seconds`. They are marked *reserved* in `config/default.yaml`.

### P3
14. **File or queue intake watcher** for strategy signals (described in `DESIGN.md`).
15. **Roostoo API docs in the repo.** The submodule was never committed, so the verified behaviours exist only in `DESIGN.md`.
16. **No CI.** Running `pytest` on every pull request would catch regressions.

## 4. Remaining backtest vs live differences

These should be closed, or at least accounted for, before relying on backtest numbers:

| Topic | Backtest | Live engine |
|---|---|---|
| Rebalance threshold | Net weight change > band, for buys and sells | `no_trade_band_pct` applies to **opens only**; any reduction is sent |
| Risk breach | Rows are scaled to ≤ 100% of capital | The whole signal is rejected |
| Large orders | One order per bar | Split into child orders of ≤ 25% of equity |
| Fill model | All or nothing if the bar trades through the limit | Real exchange; partial fills possible |
| Cash for buys | Only cash free before the bar | Sells first, then buys in the same cycle; the exchange rejects buys if a sell hasn't filled |
| Prices | Binance candles | Roostoo last price (tracked within about 1% in spot checks) |
| Short size | Sized by current notional | Tracked by collateral USD |

## 5. Verification of the refactor

- **Tests:** 60 pass (engine; client against a fake HTTP session; backtest simulator rules; core config and symbols).
- **Regression:** the long-only MA crossover (80/400) reproduces its pre-refactor results exactly: −30.27% full year, 651 trades, 95.74% fill rate, −1.47% median over 328 seven-day windows.
- **Live read-only check** against the Roostoo mock exchange:
  - Every client endpoint responds.
  - `exchangeInfo` is fetched once.
  - A full engine cycle on the real account, with orders intercepted, produced a LIMIT buy at the last price and a LIMIT short open.

## 6. RXM port and live runner (Oct 3)

The quant's branch (`strategy/noise-cancelling-momentum`) was built on the pre-refactor layout. It was ported into the package as follows:

| Quant's file | Now |
|---|---|
| `backtest/strategies/rxm.py` | `tradebot/strategy/library/rxm.py` (logic unchanged; golden weights reproduce to 1e-12) |
| `backtest/engine.py` changes (lock-in, latency stress, gap toggle, dust fixes) | `tradebot/backtest/simulator.py`, `BacktestConfig` |
| `backtest/experiments.py`, `tune.py` | `tradebot/research/` |
| `src/strategy_bridge/*` | `tradebot/live/` (bridge, market_data, repeg, throttle); `ReplayExchangePort` → `tradebot/exchange/replay.py` |
| `src/guard/checks.py` | `tradebot/live/guard.py` |
| `dashboard/` | `tradebot/dashboard/` (`python -m tradebot dashboard`) |
| `scripts/run_live.py`, `run_comp_mock.py` | `python -m tradebot live`, `python -m tradebot replay` (both use `LiveRunner`) |
| `competition_config.yaml` | `config/competition.yaml` |

**Problems found and fixed during the port:**

| Severity | Problem | Fix |
|---|---|---|
| High | **The guard (16 checks, kill switch) never ran live.** The live script didn't call it; only the dashboard did, although the spec lists it as layer L6. | Portfolio checks run on the engine's whole plan before anything is sent (a new `plan_check` hook next to the risk manager). Per-order checks (fat finger, size, self-cross, rate, kill switch) run at send time in `GuardedPort`. Every loop runs the guard's poll, and the kill switch pauses the loop. |
| **Critical** | **After the lock-in, the guard blocked the de-risking sells.** Its `lockin` check blocked any order while the gross target exceeded 0.32, including sells that *reduce* gross. A blocked order stops the plan. In a 7-day replay, the book stayed at full size for 3 days after locking (223 blocked ARB sells). This would have defeated the competition lock-in. | Lock-in, like the exposure checks, now blocks only orders that move gross *up*. Regression test added. |
| High | Checking portfolio limits order by order rejects normal rebalances. The engine sends all buys before the shorts, so mid-batch net exposure looks too long. | Portfolio checks judge the full plan once, as the guard was designed for (`pre_trade` on a batch). |
| High | **With its default caps the guard would have blocked RXM's own rebalances.** Its per-coin cap of 0.40 is below RXM's ~0.55 and its per-order cap of 0.40 is below the 0.5 child orders. | Caps for RXM in `config/competition.yaml`; a test shows the rejection with tight caps. |
| High | Status-file writes could crash the runner. On Windows another process briefly holding `status.json` made `os.replace` fail outside the error handling. Found by the replay. | Ops-file writes retry and never raise, and the loop's bookkeeping can't stop trading. Regression test added. |
| Medium | Long exits were market orders (0.1%), against the backtest's 5 bp passive limits. This was the quant's own listed gap. | Exits are limits at `execution.limit_offset_bps` (5 bp) and are re-pegged before sending, like entries. |
| Medium | The rate limiter's request weights counted the old client's extra serverTime and exchangeInfo calls. | Weights match `RoostooClient`: one request per call. |
| Medium | The guard's starting equity defaulted to $100k, so a different account size showed a false drawdown warning every loop. | Anchored to the account's real starting equity. |
| Low | The guard warned "stale data" on every loop because it wasn't told when prices were read. | The guard receives the snapshot read time. |
| Low | The replay exchange returned balances under `Wallet`; real Roostoo uses `SpotWallet`. | Matches the real shape. |
| — | Kept from the quant's branch: full short exits with `close_pct=100` (no residual on a losing short); long exits sized at the snapshot price (no oversell after a down-tick); the Binance Vision S3 fallback. | |

**Verification:**
- 144 tests pass. These include golden RXM weights on real data, live-equals-backtest weights for both presets, the full live stack down to signed HTTP requests, and runner recovery, kill switch, restarts and guard.
- A 7-day replay through `LiveRunner` (Sept 1–8, comp) returned **+7.75%, against +7.87% from the backtest simulator**. The guard blocked nothing, and gross exposure fell from 0.97 to 0.31 after the Sept 5 lock-in. API use averaged 5 requests/min (peak 19).
- A dry run against the Roostoo mock exchange with live Binance candles computed the day's real target and sent nothing.

Still open for the competition: AWS deployment files (P0 3), key rotation (P0 4), and the test-account checks in `docs/LIVE_RUNBOOK.md` section 5.

## 7. Shared MM/RXM account remediation (Oct 7)

PR #7 added the long-only MM strategy and a shared-account coordinator. The review reproduced failures in the
client-interaction layer (probes P1-P4) and a Windows startup crash. The strategy itself (universe, quote formulas,
long-only rule, >10 bps filter, 70/30 allocation) is unchanged. The general strategy/symbol/quantity/price ownership
ledger is still a future build.

| Change | Failure it fixes | Expected behaviour | Acceptance test | Remaining limitation |
|---|---|---|---|---|
| Portable account lock (`core/locking.py`) | `import fcntl` crashed every Windows start; the relative lock path let two processes in different directories trade one account | one OS lock per account (`msvcrt`/`fcntl`), absolute path keyed to a hash of URL+key, taken by the coordinator and standalone RXM; holder pid/host/state in `.holder.json`, no key material | `tests/test_locking.py` (second holder refused, cwd bypass, RXM runner, no credentials) | an already-running older binary does not take the lock: stop it by hand (preflight detects its `status.json`) |
| Scoped restrictions instead of a global halt (`engine/state/restrictions.py`) | P1/P2/P3 froze the whole account permanently, including RXM | findings restrict only `coin:`/`cash:`/`short:`/`strategy:` scopes; sells, covers and cancels keep working; lift after 2 clean syncs | `test_unexplained_cash_and_unknown_order_restrict_only_their_scope`, `test_restrictions_stay_with_their_strategy` | an `unresolved` finding (5 syncs) needs a person; there is no `resolve` command yet |
| Cash tolerance tiers | P2: a $0.03 commission difference blocked the account forever | `max($0.05, 3 bps x fill notional)` absorbed and booked to the causing strategy; beyond it, restrict the traced owner | `test_p2_*` (rounding sweep; doubled/missing maker fee still restricted) | the real venue's rounding size is unmeasured (V4) |
| Docs-shaped PENDING rows | P1: a PENDING row with `FilledQuantity == Quantity` blocked the account | treated as unfilled, journaled once | `test_p1_pending_rows_reporting_full_filled_quantity_do_not_block` | a genuine partial fill reported in that shape would be missed until the order completes |
| Lost spot / short responses | P3: a lost short-open reply blocked the account forever; lost spot replies blocked until manual action | intent kept, matched to exactly one venue order (spot) or a position move (short); "not executed" only from a provably complete history after 2 evidence windows; never resubmitted | `test_lost_spot_response_*`, `test_p3_*`, `test_history_paging_order_*`, `test_restart_mid_submit_*` | history page order is undocumented: completeness only at a short page or a newest-first page (V3 checks it) |
| Pending cancels | P4: a cancel not yet settled raised and left the order unmanaged | `CANCELING` keeps the reservation, no replacement on that side, re-sent after 30 s until terminal | `test_p4_*`, `test_restart_mid_cancel_*` | none known |
| Recovered short close re-baseline (bug found while testing) | the re-baseline flag lived on an intent that left the active set at the next save, so recovered closes left a permanent cash gap | flag persisted in state and consumed by the next cash check | `test_p3_lost_short_close_*` | — |
| Fill notional across re-reads (bug found while testing) | the confirmation re-read reset the fill list, so a real gap lost its owner and fell to `cash:account` with the $0.05 floor | fills accumulate until a committed sync; a persisting gap keeps its owner | `test_p2_cash_gap_beyond_tolerance_restricts_only_the_owner` | — |
| Request budget | RXM order cost 5 and cancel 6 requests (a full sync each); MM could delay RXM | no per-order sync; RXM runs first each loop; cap stays 25/min; quote age and deadline misses reported | `tests/test_request_budget.py` | the 25/min cap is shared: a 6-quote MM refresh still takes ~40 s of budget |
| Read failures | one failed balance read stopped the loop as BLOCKED | `DEGRADED`: no strategy step on stale data, `reads` scope until a read succeeds, backoff | `test_failed_balance_read_reports_degraded_then_recovers` | — |
| Market data | 1 s + book-ticker streams opened for every RXM coin; single endpoint; TLS store missing on Windows | 1 s streams only for MM coins; fallback endpoint list; certifi CA bundle with verification on; RXM served by REST repair when the stream is down | `test_coarse_only_*`, `test_stream_verifies_tls_*`, `test_rxm_candles_keep_arriving_over_rest_*` | `data-stream.binance.vision` fallback not yet exercised from the deployment host |
| Preflight / explain (`live/preflight.py`) | takeover decisions required manual API calls with competition keys | read-only report of shortfall, positions, unknown orders, lock holder, legacy runner, allocation drift; exit 0 = ready | `tests/test_preflight.py` (no mutating call, nothing written) | detects a legacy runner only on the same host/state dir |

**Merge-ready:** yes. All tests pass on Windows (`python -m pytest -q`). The default standalone RXM profile is
unchanged apart from the shared lock path.

**Deploy-ready:** not yet. Before the shared coordinator trades the competition account:
1. run `docs/ACCOUNT_VALIDATION.md` (V1-V10) on a **separate test account** and record the results;
2. run `account preflight` against the competition account (read-only) and resolve blockers by hand;
3. follow the migration steps in `docs/MARKET_MAKING.md`. Stopping the running competition bot is an operator
   decision and needs explicit authorization.

**Open deploy blocker found by the live data smoke test (not changed here: it alters MM decision timing).**
`MMFluctuation.generate_quotes` only quotes a coin when the candle for `now - 1 s` is already cached
(`cursor == second - 1`). On the public stream, closed one-second candles arrive 2-3 s after their
open time (16 snapshots from this host: lag 2-3 s every time). So at decision time that candle is
almost never present: MM emits an empty batch and waits the full 600 s refresh, repeatedly. Replay
cannot show this because simulated data is never late. Proposed fix: the quote bridge decides at
`min(now, last complete second + 1 s)` when that is at most a few seconds behind (a replay no-op,
so policy parity is unchanged), and reports the data lag. Needs the quant's agreement.

**Binance access from the deployment host:** `stream.binance.com` answered HTTP 451 (restricted
location) from this machine; the fallback `data-stream.binance.vision` connected with verified TLS.
Check both from the AWS host before deployment.
