# Live runbook: RXM on Roostoo

```text
ResidualMomentum (tradebot/strategy/library/rxm.py, frozen PRESETS "comp" / "neutral")
  -> tradebot/live/bridge.py   CompetitionStrategy(snapshot) -> TargetPortfolio
       BarBuffer (15m Binance klines, 50-day window)        strategy_state.json (start equity, lock-in)
  -> tradebot/engine           Engine.run_once: cancel stale -> snapshot -> plan -> risk -> orders
     (guard: portfolio checks on the whole plan before anything is sent)
  -> RepegPort (fresh price per limit) -> GuardedPort (per-order guard checks)
  -> ThrottledPort (<= 25 requests/min) -> RoostooExchangePort (live) | ReplayExchangePort (simulation)
supervised by tradebot/live/runner.py (LiveRunner): recovery with backoff, kill switch, heartbeat, status file
```

## 1. Simulate first (no network, no credentials)

```bash
python -m pytest -q
python -m tradebot --config config/competition.yaml replay --start 2026-09-01T00:16 --days 7
python -m tradebot --config config/competition.yaml replay --mode neutral --days 3 --out results/replay_neutral
```

The replay runs the **same `LiveRunner`** as the live command: same bridge, guard, re-pegging, rate limiter, engine and recovery loop. Only the exchange is `ReplayExchangePort`, which replays downloaded 15m candles on a simulated clock.

Outputs go to `results/replay/` (or `--out`): `engine_audit.jsonl`, `engine_state.db`, `strategy_state.json`, `status.json`, `snapshots.jsonl`, `fills.csv`. The summary compares the result with the backtest simulator over the same period. A 7-day replay takes about 6 minutes.

## 2. Run the bot

```bash
# Dry run on the TEST account: reads the account, computes targets, sends NO orders
python -m tradebot --config config/competition.yaml live --state-dir var/live_test_dry

# Real orders (TEST account first, then the competition account with its own .env and state dir)
ROOSTOO_CONFIRM_LIVE=YES python -m tradebot --config config/competition.yaml live --live --state-dir var/live_comp
```

- **Credentials:** `ROOSTOO_API_KEY`, `ROOSTOO_API_SECRET` and optional `BASE_URL` come from `.env` in the working directory. Never pass them on the command line.
- **`--live`** sends real orders, and only when `ROOSTOO_CONFIRM_LIVE=YES` is also set. Without it the bot runs a dry run.
- **Each account and mode needs its own `--state-dir`.** The engine journal makes signal ids one-shot, even in a dry run, and `strategy_state.json` holds the start equity and lock-in.
- **Stopping:** Ctrl-C or `kill <pid>` (SIGTERM) stops cleanly after the current loop.

### What happens when things go wrong

| Situation | What the bot does |
|---|---|
| A request fails (network, timeout, 5xx) | Read requests retry inside the client. If the loop still fails, it is logged with a traceback and the next loop waits 60 s → 120 s → 240 s …, capped at `live.max_backoff_seconds` (15 min). The first success resets the delay and logs "recovered". |
| An order request times out | Never retried automatically (it could double-fill). The engine marks it UNCERTAIN. The next loop re-reads the account and re-plans from what actually happened. |
| A snapshot looks broken (equity ≤ 0 or moved > 50%) | The loop is skipped before any order (`SnapshotRejected`). |
| Candles are late or missing | It waits up to 30 min for a late decision bar, then decides without that coin. With no fresh candles for 2 h, it holds the current book. It never sends an empty "flatten everything" target. |
| The process crashes or the server reboots | Restart it (systemd `Restart=always`). The journal makes the day's signal a DUPLICATE, so nothing is re-sent. Start equity and lock-in are restored from `strategy_state.json`. |
| A status or snapshot file can't be written | It retries, logs a warning and keeps trading. Bookkeeping never stops the bot. |

### Kill switch

```bash
touch KILL     # pause: no account reads, no orders, from the next loop on
rm KILL        # resume
```

The file path is `live.kill_file`, relative to the working directory. While it exists, the guard also blocks any order already in flight.

### The guard

- **On the whole plan, before anything is sent:** gross, net, per-coin weight, short collateral, cash and lock-in. A failing plan is rejected like a risk rejection (`REJECTED_RISK`, reason `guard:<check>`) and retried next loop. A plan that moves exposure *down* (for example de-risking after the lock-in) is never blocked by the exposure or lock-in checks.
- **On each order, as it is sent:** the kill switch, fat-finger price (> 3% from last), order size, self-cross, and order and API rate.
- **Every loop:** the account is checked, with warnings in `bot.log` (drawdown, drift, heartbeat). These are alerts only.

### Watching it

| File in `--state-dir` | Contents |
|---|---|
| `bot.log` | Everything, rotating at 20 MB. A heartbeat line every 15 min: equity, return since start, positions, last signal, failures, requests/min. |
| `status.json` | Rewritten every loop: `state` (running / backoff / paused / stopped), `consecutive_failures`, `last_error`, `last_signal`, `equity_usd`, `return_since_start`, `locked`, `guard`. A watchdog only needs to check that `updated` is recent. |
| `latest_snapshot.json`, `snapshots.jsonl` | The latest account snapshot, plus one per 15m bar: the performance history. |
| `engine_audit.jsonl`, `engine_state.db` | Every decision, order and outcome (the trade log judges can audit). |

Live dashboard: `python -m tradebot dashboard --source engine --engine-dir var/live_comp --watch 60`.

## 3. Configuration (`config/competition.yaml`)

Only overrides of `config/default.yaml` are listed there. The reasons for each value:

| Key | Default | Competition | Why |
|---|---|---|---|
| `execution.strategy_poll_interval_seconds` | 5 | **60** | API budget |
| `execution.limit_offset_bps` | 0 | **5** | Exits rest 5 bp passive, like every RXM limit |
| `execution.max_per_symbol_exposure` | 0.35 | **0.60** | k = 3 inverse-vol: max single weight 0.552 in 2024–26. 0.40 would reject whole rebalances. |
| `execution.max_order_value_usd` | 25,000 | **100,000** | Per-symbol plan amount; a 0.55 weight is $55k |
| `execution.max_total_short_collateral_usd` | 10,000 | **100,000** | 10k rejects every plan with > 10% shorts |
| `execution.max_daily_loss_usd` / `max_drawdown_pct` | 2000 / 0.25 | **1e9 / 1.0** | No brakes (research: they reduce value) |
| `execution.max_child_order_pct` | 0.25 | **0.5** | Fewer child orders, fewer API calls |
| `execution.min_cash_reserve_usd` | 500 | **200** | Together with the 0.98 gross cap |
| `live.guard.max_symbol_weight` | 0.40 | **0.60** | Same cap as the engine. **The default would block RXM's own rebalances.** |
| `live.guard.max_order_frac_equity` | 0.40 | **0.51** | Child orders are at most 0.5 of equity |

## 4. API budget (limit: 30 requests/min)

`RoostooClient` caches the exchange rules (hourly) and the server-time offset (every 10 min), so each port call is one HTTP request.

| Per engine loop | Requests |
|---|---|
| `cancel_stale_orders` → pending orders | 1 |
| Snapshot: balance, short positions, ticker (all pairs), pending orders | 4 |
| **Idle loop (60 s)** | **5** → about 5 per minute |
| Each order, plus its re-peg ticker read | 2 |

A daily rebalance (6–10 orders) is spread over about a minute by `ThrottledPort(25/min)`. Binance klines for the candle buffer use a different host and don't count towards Roostoo's budget.

## 5. TEST account checklist (before Oct 4)

1. Put the **test** key in `.env` with `BASE_URL=https://mock-api.roostoo.com`.
2. Run the dry run for one 00:15 UTC rebalance. Check that `bot.log` shows a `comp-YYYYMMDDT0000` signal, that equity matches the Roostoo UI, and that `status.json` shows no failures.
3. Run with real orders on the test account through one rebalance. Then check:
   - limit fills against the "trades through" model;
   - the short-open fee (charged up front) and what a cancel refunds;
   - `Lock` balances while orders rest (equity must not dip);
   - the full short exit (`close_pct=100`, `FullyClosed: true`).

## 6. Go-live checklist (Oct 4)

- [ ] Rotate keys. Put the competition key **only** in `.env` on the server.
- [ ] **No manual API calls with competition keys:** no `tradebot api`, no curl.
- [ ] Use a fresh `--state-dir var/live_comp`, so start equity is recorded on the first loop.
- [ ] `--mode comp` (decided). Never change it mid-event: the state file refuses a different mode.
- [ ] Run `python -m pytest -q` on the server.
- [ ] Run under a supervisor with automatic restart (systemd `Restart=always`). Restarts are safe.
- [ ] Watchdog: alert if `status.json` `updated` is older than 5 minutes, or if no `comp-YYYYMMDDT0000` signal appears in `bot.log` by 02:00 UTC. A missed day costs about 25% of the edge.
- [ ] Check `engine_audit.jsonl` daily for `REJECTED_RISK` and `UNCERTAIN`.

## 7. Known gaps

1. **Short closes are market orders** (0.1%). Roostoo's `short_close` takes no price.
2. **Partial short trims** are sized at the current price rather than the entry price, which is slightly off once the price has moved. Full exits use `close_pct=100` and are exact.
3. **The risk manager rejects a whole plan** when any limit is broken, which is why the limits above are wide.
4. **Equity during resting orders** is correct only if Roostoo reports locked USD in `Lock`. Verify on the test account.
5. **Universe:** the 35 backtest coins. A coin missing from Roostoo's ticker is skipped and its weight is not redistributed.
6. **Lock-in timing:** live uses mark-to-market snapshot equity confirmed on 2 polls; the backtest uses the previous bar's close and a single check.
7. **No deployment files yet** (systemd unit, setup script). See docs/REVIEW.md.
