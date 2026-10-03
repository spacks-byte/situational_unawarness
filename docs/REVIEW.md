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

## 3. Missing features (not built, in priority order)

### P0: needed to compete
1. **Live runner / entry point.**
   - There is no command that runs the engine unattended. `EngineRuntime.run()` has no exception handling, so a single failed request (now a `RoostooError`) stops the bot.
   - **Needed:** a `tradebot live` command with crash recovery, restart safety (rebuild state from the exchange and journal), graceful shutdown and a heartbeat log.
2. **Strategy adapter and live candle feed.**
   - Backtested strategies (`Strategy.generate_weights`) can't run live yet.
   - **Needed:** a recent-candle buffer from Binance klines (REST), with Roostoo ticker polling as a fallback. Then a call on each completed bar that converts the last weight row into a `TargetPortfolio` (see `ARCHITECTURE.md`), with one `signal_id` per bar.
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
7. **Live performance records.** The rules recommend logging performance internally. Equity snapshots, a heartbeat and live Sharpe/Sortino/Calmar are configured (`equity_snapshot_interval_seconds`, `heart_beat_interval_seconds`) but not implemented. `core.metrics` can compute them once equity is recorded.
8. **Risk defaults don't fit a $100k portfolio.**
   - These would reject typical targets: `max_total_short_collateral_usd: 10000`, `max_per_symbol_exposure: 0.35`, and `max_order_value_usd: 25000`. The last is checked *before* child-order splitting.
   - A breach rejects the **whole** signal instead of scaling it down.
   - These limits need tuning to the strategy.

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
