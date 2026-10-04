# Trading dashboard and Guard

The dashboard is a single static HTML file: no server, no network, everything inline. The Guard
(`tradebot/live/guard.py`) is a second line of defence. The bot calls it before every order batch and on
every poll. Neither one calls an exchange or reads credentials.

## Run

```bash
# Backtest source: frozen 'comp' preset over the last 14 days of data/binance/klines/15m
python -m tradebot dashboard --source backtest --out results/dashboard.html
python -m tradebot dashboard --source backtest --end 2026-09-15 --days 14   # another window

# Engine source: a live or mock engine run directory
python -m tradebot dashboard --source engine --engine-dir var/live_comp --out results/dashboard.html

# Rebuild every 30 s. The page reloads itself at the same rate.
python -m tradebot dashboard --source engine --engine-dir var/live_comp --watch 30
```

The backtest source calls `tradebot.backtest.run_backtest` with `ResidualMomentum` with the frozen `comp` preset (`src/tradebot/strategy/library/rxm.py`)
(k=3, tilt 0.3, gross 1.0, rank buffer 2, residual 3/7/14-day ensemble, daily rebalance) and
`BacktestConfig(limit_offset_bps=5, lockin_return=0.06, lockin_scale=0.3)`. It uses 45 days of
warm-up, and the window starts from cash. A build takes about 4 seconds.

The engine source reads these files from `--engine-dir`. You can also pass each one with
`--audit/--db/--snapshot`.

| File (newest match wins) | Used for |
|---|---|
| `*audit*.jsonl` (`AuditLog`) | blotter (`operation` events), risk rejections, any event with `equity_usd` |
| `*.db` (`IntentJournal`, opened read-only) | intent counts by status (PENDING_SEND/SENT/RESOLVED/UNCERTAIN) |
| `latest_snapshot.json` / `*snapshot*.json` | positions, prices, equity, guard state |
| `snapshots.jsonl` (optional) | equity, gross and net history (one `{"timestamp", "snapshot"}` per line) |

The snapshot can be a bare normalized snapshot (`read_exchange_snapshot` output). It can also be
wrapped with extras that the strategy bridge should write:

```json
{"timestamp": "2026-10-05T12:00:00+00:00",
 "snapshot": {"cash_usd": 0, "longs": {}, "shorts": {}, "prices": {}, "entry_prices": {}, "pending_orders": [], "equity_usd": 100000},
 "target_weights": {"SEI": 0.22, "ZEC": -0.12},
 "scores": {"SEI": 1.52, "ZEC": -1.31},
 "price_times": {"SEI": "2026-10-05T11:59:40+00:00"},
 "locked": false}
```

## Panels

The page is ordered for research first. The live-monitoring panels (Guard, Positions, Blotter,
Execution) sit in a collapsed **Monitoring** section at the bottom.

| Panel | Contents |
|---|---|
| Where the strategy trades | Backtest source only. One coin at a time (buttons show the number of fills): the 15m price with a marker for every order (buy, sell, short, cover; hollow = not filled) and shading while the coin is held long or short. Underneath, that coin's signal score against the score of the k-th best and k-th worst coin, i.e. the level a new coin must reach to enter each book. With the rank buffer, a held coin can stay a little past that line |
| Header | Source, as-of time, lock-in pill (ARMED or LOCKED 0.3×), overall guard status (worst check) |
| KPI strip | Equity, P&L $ and %, today's UTC P&L, return vs the 3.3%/5.2% qualification cut-offs and the lock-in level (progress bar), drawdown now and max, competition day, Sharpe, Sortino, Calmar, composite `0.4·Sortino + 0.3·Sharpe + 0.3·min(Calmar, 50)`, gross, net, lock-in state |
| Equity | Equity curve with dashed reference lines (start, +3.3%, +5.2%, lock-in) and the lock-in time. Drawdown is drawn underneath on its own axis. The crosshair is synced across both |
| Guard | Every check with OK/WARN/BLOCK, the current value and the limit. In backtest mode the order checks replay the last order batch |
| Positions | Symbol, side, qty, entry, last, value, weight (with a bar), target weight, drift (amber when ≥ 3%), uP&L $ and %, contribution to return (realized + unrealized − fees, as % of start equity) |
| Exposure by symbol | Signed weight bars (blue long, orange short) with a tick at the target. Gross and net over time are shown below, with the 100% cap |
| Blotter | Newest first: time, symbol, side, LIMIT/MARKET, qty, price, notional, fee, filled, status. Filter: all / filled / unfilled |
| Execution | Orders, fills, limit fill rate, maker and taker fees, fees in bp of notional, turnover, stats by side. A heartbeat strip shows fills per UTC day (red = a day with no fill). Intent-journal counts appear in engine mode |
| Signal | Today's ranking: ensemble residual-momentum score, annualized vol, longs and shorts, target weight. In backtest mode the dashboard recomputes the score independently. `MATCHES STRATEGY` confirms that its top/bottom-k equals the strategy's own selection |
| P&L attribution | Contribution by symbol across open and closed positions |

## Calling the Guard from the bot

```python
from src.guard import Guard, GuardConfig, ProposedOrder

guard = Guard(GuardConfig(initial_equity_usd=100_000, kill_file="KILL"))
# On restart, replay equity history so the peak and the lock-in latch are correct:
# for ts, eq in history: guard.observe_equity(eq, ts)

def on_poll(snapshot, target_weights, price_times):
    guard.record_api_call(n=3)                       # every REST call you make (ticker, balance, ...)
    report = guard.poll(snapshot, target_weights=target_weights, price_times=price_times,
                        bot_locked=strategy_is_locked)
    log.info(report.summary())                       # e.g. "[guard poll] WARN (WARN:target_drift)"
    audit.record("guard", report.to_dict())          # the dashboard can show it later
    return report

def before_send(snapshot, orders, target_weights, price_times):
    proposed = [ProposedOrder(o.symbol, o.side, o.qty, o.limit_price) for o in orders]  # side: BUY/SELL/SHORT/COVER
    report = guard.pre_trade(snapshot, proposed, target_weights=target_weights, price_times=price_times)
    if not report.allowed:                           # at least one BLOCK
        log.error(report.summary()); return False    # do not send; try again next poll
    send(orders)
    guard.record_orders_sent(len(orders))
    return True

def on_fill(fill_time):
    guard.record_fill(fill_time)                     # feeds the daily heartbeat
```

`touch KILL` in the bot's working directory blocks every new order. `rm KILL` resumes trading.
`guard.kill()` does the same thing from code.

Exposure checks run on the post-trade projection: every proposed order is assumed to fill, with
fees included. They BLOCK only when a limit is breached and the batch makes it worse. A batch that
reduces risk is never blocked.

## Checks and default thresholds (`GuardConfig`)

| Check | OK | WARN | BLOCK |
|---|---|---|---|
| `kill_switch` | no `KILL` file | — | `KILL` file present or `guard.kill()` |
| `gross_exposure` | ≤ 1.00 | > 1.00 (on a poll, or when the batch reduces it) | projected > 1.005 and the batch adds gross; any poll > 1.10 |
| `symbol_weight` | max \|w\| ≤ 0.30 | 0.30–0.40 | > 0.40 and the batch increases it |
| `net_exposure` | −0.05…+0.45 | outside the warn band | outside −0.20…+0.60 and the batch worsens it |
| `short_collateral` | ≤ 40% of equity | 40–45% | > 45% of equity (or `max_short_collateral_usd`) and the batch adds to it |
| `cash_buffer` | ≥ $50 | < $50 | projected cash < $0 (overdraft) |
| `price_band` (fat finger) | limit within 1% of last | 1–3% | > 3%, or no last price for the symbol |
| `order_notional` | ≥ $10 and ≤ cap | an order below the $10 exchange minimum | an order > min($50k, 40% of equity) |
| `self_cross` | one book side per symbol | — | buy and sell on the same symbol in the batch or against resting orders (market-making / wash optics) |
| `order_rate` | ≤ 12 orders/60 s | 13–20 | > 20 |
| `api_budget` | ≤ 18 calls/60 s | > 18 (60% of 30) | > 27 (90% of the 30/min budget) |
| `stale_data` | oldest price ≤ 120 s | 120–300 s, or no timestamps supplied | > 300 s, or a held/ordered symbol has no timestamp |
| `drawdown` | < 5% from peak | ≥ 5%, "ALERT" at ≥ 10% | never. Monitor-only by design: drawdown brakes destroyed value in research |
| `lockin` | armed below +6%; locked with target ≤ 0.32 gross | locked but book still de-risking; bot and guard latch disagree | locked (return reached +6%, sticky) but target gross > 0.3 + 0.02 |
| `target_drift` | max \|actual − target\| < 3% | ≥ 3%, "ALERT" at ≥ 10% | never |
| `daily_heartbeat` | a fill today (UTC), or before 18:00 UTC | no fill by 18:00 UTC, "URGENT" after 22:00 UTC | never |

Every check is also exported as a pure function (`check_*` in `tradebot/live/guard.py`) that returns
`CheckResult(name, status, value, limit, message)`. The tests are in `tests/test_guard.py`.

## Notes for go-live

- The engine's `ExecutionConfig` defaults are `max_order_value_usd = 25_000` and
  `max_total_short_collateral_usd = 10_000`. On a $100k comp-mode book these reject normal
  rebalances: one 30% long is a $30k parent order, and the short book is about $35k. The engine
  checks the parent amount before child slicing. Raise both limits in the live config and leave
  the per-order and short caps to the Guard. A mock run with the defaults returned
  `REJECTED_RISK: max_order_value`.
