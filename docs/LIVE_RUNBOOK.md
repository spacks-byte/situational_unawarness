# Live runbook: residual momentum (competition mode) on the Roostoo engine

```text
ResidualMomentum (backtest/strategies/rxm.py, frozen PRESETS "comp" / "neutral")
  -> src/strategy_bridge/live_strategy.py  CompetitionStrategy(snapshot) -> TargetPortfolio
       BarBuffer (15m Binance klines, 50-day window)        lock-in state JSON
  -> src/engine  Engine.run_once: cancel stale -> snapshot -> plan -> risk -> orders
  -> ThrottledPort (<= 25 HTTP/min) -> LibraryExchangePort (live) | ReplayExchangePort (sim)
```

## 1. Run the mock demo (no network, no credentials)

```bash
# from the repo root
python3 -m pytest -q                          # all tests, incl. tests/test_strategy_bridge.py
python3 scripts/run_comp_mock.py              # 2026-09-01 00:16 -> 09-08, comp mode, 60 s polls, ~2.5 min
python3 scripts/run_comp_mock.py --mode neutral --days 3 -v
```

The demo replays real 15m bars from `data/binance/klines/15m` on a `SimClock`, with
`dry_run=False` on a **simulated** port only. The port is `ReplayExchangePort`, because
`MockExchangePort` has static prices, fills limit buys instantly, never fills limit shorts and
charges no fees (`--port mock` runs that one anyway). Outputs go to `results/live_demo/`:
`engine_audit.jsonl`, `engine_state.db`, `strategy_state.json`, `fills.csv` and `equity.csv`.

Reference run, 2026-09-01..09-08, comp mode, $100k:

- 8 daily rebalances, plus 1 lock-in re-target (09-03 10:16, +5.1%) and 26 re-quotes.
- 90 orders sent; 75 fills: 34 passive limits and 41 market exits.
- 12 limits expired unfilled and were re-quoted.
- Final equity **$104,437 (+4.44%)**. The backtest engine gives +4.58% over the same period.

## 2. How the live strategy behaves

- **Signal.** Identical to the backtest. `generate_weights(buffer).iloc[-1]` equals the row of the
  latest 00:00 UTC bar, which the test asserts to 1e-10 on real data. The signal is computed once
  the 00:00 bar closes (00:15 UTC) and recomputed immediately on (re)start.
- **Target.**
  - A positive weight becomes `LongTarget(weight, limit=last*(1-5bp))`.
  - A negative weight becomes `ShortTarget(|w|*equity, limit=last*(1+5bp))`.
  - Gross is capped at 0.98, so the engine's cash-reserve check (which includes fees) can't reject a gross-1.0 plan.
- **Band (same as the backtest).** Only symbols more than 1% off target, or with a side to exit, are
  traded. The others are frozen at their current size (`notional_usd` / `collateral_usd`), so the
  engine doesn't touch them.
- **signal_id.**
  - `comp-YYYYMMDDT0000` for the daily target. A `-L` suffix is added after lock-in.
  - `...-rYYYYMMDDTHHMM` for re-quotes: at most one per 15m bar, and only when a symbol is still
    more than 1% off. A typical case is a passive limit that the engine cancelled after
    `fill_timeout_seconds=900`.
  - Otherwise the same target is returned, and the engine logs `DUPLICATE` (no churn).
- **Lock-in.**
  - Equity ≥ start × 1.06 on 2 consecutive polls locks the book. From then on all weights are ×0.3, for good.
  - `start_equity` and `locked` are kept in `strategy_state.json`, which survives restarts.
  - The bot refuses to start in a different mode with an existing state file.
- **Safety.**
  - A snapshot with equity ≤ 0, or more than 50% away from the last one (a partial API read), raises `SnapshotRejected` before any order is sent.
  - No data or stale data means the bot holds the current book. It never sends an empty, flatten-everything target.

## 3. Config (`src/strategy_bridge/competition_config.yaml`)

`config/default.yaml` is unchanged. Every key below is checked against `ExecutionConfig`.

| key | default | competition | why |
|---|---|---|---|
| strategy_poll_interval_seconds | 5 | **60** | API budget |
| fill_timeout_seconds | 30 | **900** | passive limits rest ~1 bar |
| max_gross_exposure | 2.0 | **1.0** | no leverage |
| max_effective_leverage | 2.5 | **1.0** | (not read by the engine) |
| max_per_symbol_exposure | 0.35 | **0.60** | k=3 inverse-vol: max single weight 0.552 in 2024-26 (p99 0.506). **0.40 would reject whole rebalances** |
| max_order_value_usd | 25,000 | **100,000** | per-symbol plan amount; a 0.55 weight is $55k |
| max_total_short_collateral_usd | 10,000 | **100,000** | 10k rejects every plan with >10% shorts |
| max_daily_loss_usd / max_drawdown_pct | 2000 / 0.25 | **1e9 / 1.0** | no brakes (and the engine never fills `RiskState` anyway) |
| max_child_order_pct | 0.25 | **0.5** | fewer child orders, fewer API calls |
| min_cash_reserve_usd | 500 | **200** | together with the 0.98 gross cap |

## 4. API-call budget (limit: 30 calls/min)

Every signed call in `crypto-roostoo-api/` first GETs `/v3/serverTime`. `place_order`, limit
`open_short` and `close_short(close_qty)` also GET `/v3/exchangeInfo`. The worst case below counts
every HTTP request.

| per engine loop | port calls | HTTP |
|---|---|---|
| `cancel_stale_orders` → `query_order(pending)` | 1 | 2 |
| snapshot: balance, short_positions, ticker (all pairs), pending orders | 4 | 8 |
| **idle loop** | **5** | **10** |
| each order (place_order / open_short / close_short) | 1 | 3 |
| each exit (extra `get_ticker` in the runner's `_price`) | 1 | 2 |
| each cancel | 1 | 2 |

- **Idle, 60 s poll:** 10 HTTP/min (5/min if serverTime isn't counted). The demo measured 10.0/min on average.
- **Rebalance loop:** 6–10 orders, about 40–48 HTTP in one loop. Unthrottled, that would break the
  limit for that minute. `ThrottledPort(max_per_minute=25)` therefore wraps the live port, delays
  the excess, and spreads a rebalance over about 2 minutes. In the demo the peak was 25/min and the
  throttle waited 540 s in total over 7 days.
- Binance klines for the buffer: 35 requests per 15 min to `data-api.binance.vision`. That's a
  different host and not part of the Roostoo budget.

## 5. Switching to the Roostoo TEST account first

1. Put the **test** key and secret in `crypto-roostoo-api/.env`, with
   `BASE_URL=https://mock-api.roostoo.com` (as in `.env.example`). Never commit `.env`.
2. Dry run: it reads the account, and the engine sends no orders.
   ```bash
   python3 scripts/run_live.py --state-dir results/live_test_dry
   ```
   Check `bot.log`:
   - a daily target appears;
   - equity matches the Roostoo UI;
   - the loop rate stays about 10 HTTP/min.
3. Real orders on the test account:
   ```bash
   ROOSTOO_CONFIRM_LIVE=YES python3 scripts/run_live.py --live --state-dir results/live_test
   ```
   Let it run for at least one 00:15 UTC rebalance. Then check:
   - limit fills against the "traded through" fill model;
   - the short open fee (charged up front) and what a cancel refunds;
   - the `Lock` balance field during pending orders;
   - partial closes.
4. Each dry or live run needs a **separate `--state-dir`**. The engine journal makes signal_ids
   one-shot, even in dry run, and `strategy_state.json` holds the start equity.

## 6. Go-live checklist (Oct 4)

- [ ] Rotate keys. Put the competition key **only** in `.env` on the server.
- [ ] **No manual API calls with competition keys.** No `manual_api_test.py`, no curl, no probes:
      every call counts towards the budget and any order counts as a trade.
- [ ] Use a fresh `--state-dir results/live_comp`, so start equity is recorded on the first loop.
- [ ] Choose `--mode comp` (decided) and never change it mid-event. The state file enforces this.
- [ ] Run `python3 -m pytest -q` on the server. Use a 50-day buffer, so the first start needs about
      35 × 5 Binance requests.
- [ ] Run under a supervisor (adapt `deploy/roostoo-bot.service` to `scripts/run_live.py`) with
      restart-on-failure. Restarts are safe: idempotent ids and persisted lock state.
- [ ] Watchdog: alert if no `comp-YYYYMMDDT0000` signal appears in `bot.log` by 02:00 UTC. A
      missed day costs about 25% of the edge.
- [ ] Check the audit log daily for `REJECTED_RISK` and `UNCERTAIN`.

## 7. Known gaps and engine limitations

1. **Exits are market orders.** `close_long` is always MARKET in `runner._dispatch`, and Roostoo's
   `short_close` has no price. The backtest sells long positions with passive limits, so live
   pays about 5 bp more per long exit (0.1% vs 0.05%). In the demo, 41 of 75 fills were market.
2. **Fixed (runner):** a full short exit sent `close_qty = collateral / current price`. That
   left a residual short whenever the short was losing (seen in the demo: `FullyClosed: false`).
   It now sends `close_pct=100`, unsliced. **Still open:** partial short trims use
   `amount / limit price` instead of `amount / entry`. This is slightly off when the price has moved.
3. **The risk manager rejects the whole plan,** not just the order that breaks a limit. This
   applies to per-symbol exposure, order value, short collateral and cash reserve, and it is why
   the limits above are wide.
4. Several config fields are **not read by the engine**: `max_effective_leverage`,
   `stale_data_seconds`, `spread_guard_pct`, `price_deviation_pct`, `fat_finger_limit_pct`,
   `min_trade_interval_seconds` and `pending_short_ttl_seconds`. `max_daily_loss_usd` and
   `max_drawdown_pct` only bind through a `RiskState`, which `Engine` never passes in.
5. **Stale limit prices.** The engine sends a batch one order at a time behind the rate limiter, so
   a limit priced off the snapshot can be a minute old. `RepegPort` (`src/strategy_bridge/repeg.py`,
   on by default in `scripts/run_live.py`, `--no-repeg` to disable) re-reads the ticker just before
   each limit buy / limit short-open and re-pegs the price. It costs one extra ticker call per limit
   order and skips the order if the price moved more than 3%. **Not yet run against the real API:**
   check it in step 5.3. Other than that, the strategy only checks bar freshness and equity plausibility.
   A coin whose 00:00 bar is late is waited for (up to 30 minutes) before the day's weights are computed.
6. **Fixed (runner):** a long exit sized its quantity from a re-fetched ticker, so after a
   down-tick it asked to sell more than the holding and Roostoo would reject it. It now uses the
   snapshot price the holding was valued at, which gives back the exact quantity.
7. **`MockExchangePort` gaps:**
   - limit buys fill instantly;
   - limit shorts never fill;
   - a cancel doesn't refund short collateral;
   - short P&L is never marked to market;
   - no fees are charged.

   `ReplayExchangePort` models all of these, but it is still a model. Validate it on the test account.
8. **No daily heartbeat trade** (RESEARCH.md hand-off #3). Rebalances and re-quotes trade on most
   days, but nothing forces a trade on a day without one.
9. **Pending-order equity.** Equity is correct only if Roostoo reports locked USD (pending buys and
   pending short collateral) in the `Lock` field. Otherwise equity dips while orders rest, and that
   would also delay the lock-in. Verify this on the test account (step 5.3).
10. **Universe.** The universe is the 35 backtest coins. A coin missing from Roostoo's ticker is
    skipped (logged) and its weight is not redistributed. Data comes from Binance spot USDT pairs;
    Roostoo prices can differ slightly.
11. **Lock-in timing.** Lock-in uses mark-to-market snapshot equity and needs 2 consecutive polls.
    The backtest uses the previous bar's close equity and a single check.
