"""Strategy bridge: backtest signal -> live engine. No test touches the network."""
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from pathlib import Path

from tradebot.core.clock import SimClock
from tradebot.core.config import Settings
from tradebot.core.symbols import to_binance, to_coin, to_pair
from tradebot.data.binance_vision import klines_path
from tradebot.engine import Engine, TargetPortfolio
from tradebot.exchange.replay import ReplayExchangePort
from tradebot.live.bridge import CompetitionStrategy, SnapshotRejected, rxm_spec as mode_spec
from tradebot.live.market_data import BarBuffer, klines_rows_to_frame, parquet_fetch
from tradebot.live.throttle import ThrottledPort
from tradebot.strategy.library.rxm import UNIVERSE, ResidualMomentum, preset

COMPETITION_YAML = Path(__file__).resolve().parents[1] / "config" / "competition.yaml"
DATA_DIR = Settings.load().data.dir


def load_competition_config(**overrides):
    """Execution settings of config/competition.yaml, with test overrides."""
    return Settings.load(COMPETITION_YAML).execution.model_copy(update=overrides)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any HTTP attempt fails the test."""
    import requests
    import socket

    def boom(*a, **k):
        raise AssertionError("network access attempted in a test")

    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)


def _universe(n=10, days=50, seed=1, end="2026-03-01"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=pd.Timestamp(end, tz="UTC") - pd.Timedelta(minutes=15), periods=days * 96, freq="15min")
    out = {}
    for i in range(n):
        r = rng.normal((i - n / 2) * 3e-5, 0.004 + 0.001 * i, len(idx))
        close = 100 * np.exp(np.cumsum(r))
        vol = rng.uniform(50, 150, len(idx))
        out["BTC" if i == 0 else f"C{i}"] = pd.DataFrame({
            "open": close, "high": close * 1.002, "low": close * 0.998, "close": close, "volume": vol,
            "quote_volume": vol * close, "trades": 10, "taker_buy_base": vol * 0.5, "taker_buy_quote": vol * close * 0.5,
        }, index=idx.rename("open_time"))
    return out


def _frame_fetch(frames, calls=None):
    def fetch(symbol, start, end):
        if calls is not None:
            calls.append((symbol, start, end))
        df = frames.get(symbol, pd.DataFrame())
        return df[(df.index >= start) & (df.index < end)]
    return fetch


def _snapshot(equity=100_000.0, prices=None, longs=None, shorts=None, pending=None):
    return {"cash_usd": equity, "longs": longs or {}, "shorts": shorts or {}, "equity_usd": equity,
            "prices": prices or {}, "entry_prices": {}, "pending_orders": pending or []}


def _strategy(tmp_path, frames, now, mode="comp", **kw):
    clock = SimClock(now)
    buf = BarBuffer(list(frames), fetch=_frame_fetch(frames), window_days=50)
    return CompetitionStrategy(buf, mode=mode, state_path=tmp_path / "state.json", clock=clock, **kw), clock


# ---------------------------------------------------------------------------- symbols
def test_symbol_mapping_round_trip():
    for pair in ["BTC/USD", "PEPE/USD", "1000CHEEMS/USD", "ZEC/USD"]:
        assert to_pair(to_binance(pair)) == pair
    assert to_binance("eth") == "ETHUSDT"
    assert to_binance("ETHUSDT") == "ETHUSDT"
    assert to_coin("SOLUSDT") == "SOL"


# ---------------------------------------------------------------------------- buffer
def test_buffer_bootstraps_and_updates_incrementally_without_open_bar():
    frames = _universe(n=3, days=60, end="2026-03-01")
    calls = []
    buf = BarBuffer(list(frames), fetch=_frame_fetch(frames, calls), window_days=45)
    now = datetime(2026, 2, 20, 12, 7, tzinfo=UTC)
    buf.bootstrap(now)
    assert buf.last_bar() == pd.Timestamp("2026-02-20 11:45", tz="UTC")        # 12:00 bar still open
    assert all(df.index[0] >= pd.Timestamp(now) - pd.Timedelta(days=45, minutes=30) for df in buf.data().values())
    calls.clear()
    assert buf.update(now + timedelta(minutes=5)) == 0 and not calls          # nothing new closed: no fetch
    added = buf.update(now + timedelta(minutes=35))                            # 12:42: 12:00, 12:15 closed
    assert added == 2 * 3 and buf.last_bar() == pd.Timestamp("2026-02-20 12:15", tz="UTC")
    assert all(start == pd.Timestamp("2026-02-20 12:00", tz="UTC") for _, start, _ in calls)


def test_buffer_survives_fetch_errors():
    def bad(symbol, start, end):
        raise ConnectionError("down")
    buf = BarBuffer(["BTC/USD"], fetch=bad)
    buf.bootstrap(datetime(2026, 2, 20, tzinfo=UTC))
    assert buf.data() == {} and buf.last_bar() is None


def test_klines_rows_parse():
    rows = [[1767225600000, "1", "2", "0.5", "1.5", "10", 1767226499999, "15", 7, "4", "6", "0"]]
    df = klines_rows_to_frame(rows)
    assert df.index[0] == pd.Timestamp("2026-01-01", tz="UTC") and df["close"].iloc[0] == 1.5
    assert df["trades"].dtype == "int64" and df["taker_buy_base"].iloc[0] == 4.0


# ---------------------------------------------------------------------------- signal equality
def test_live_weights_equal_backtest_weights_synthetic(tmp_path):
    frames = _universe(n=10, days=70, end="2026-03-01")
    now = datetime(2026, 2, 25, 0, 20, tzinfo=UTC)                 # just after the 00:00 bar closed
    strat, _ = _strategy(tmp_path, frames, now)
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    strat(_snapshot(prices=prices))
    full = ResidualMomentum(**preset("comp")[0]).generate_weights(frames)
    ref = full.loc[pd.Timestamp("2026-02-25 00:00", tz="UTC")]
    assert strat.weights_bar == pd.Timestamp("2026-02-25 00:00", tz="UTC")
    pd.testing.assert_series_equal(strat.weights.sort_index(), ref.sort_index(), check_names=False, atol=1e-10, rtol=0)
    assert (ref != 0).sum() == 6                                   # k=3 per side


@pytest.mark.skipif(not klines_path(DATA_DIR, "BTC", "15m").exists(), reason="local parquet data not downloaded")
@pytest.mark.parametrize("mode", ["comp", "neutral"])
def test_live_weights_equal_backtest_weights_real_data(tmp_path, mode):
    from tradebot.data.loader import load_universe
    data = load_universe(list(UNIVERSE), "15m", "2026-06-01", "2026-10-01")       # backtest hold-out load
    full = ResidualMomentum(**preset(mode)[0]).generate_weights(data)
    t = pd.Timestamp("2026-09-05 00:00", tz="UTC")
    buf = BarBuffer(list(UNIVERSE), fetch=parquet_fetch(DATA_DIR), window_days=50)
    clock = SimClock(datetime(2026, 9, 5, 0, 16, tzinfo=UTC))
    strat = CompetitionStrategy(buf, mode=mode, state_path=tmp_path / "s.json", clock=clock)
    prices = {to_coin(s): float(df.loc[:t, "close"].iloc[-1]) for s, df in data.items()}
    strat(_snapshot(prices=prices))
    assert strat.weights_bar == t
    pd.testing.assert_series_equal(strat.weights.sort_index(), full.loc[t].sort_index(), check_names=False, atol=1e-10, rtol=0)


# ---------------------------------------------------------------------------- target portfolio
def test_target_portfolio_valid_and_priced(tmp_path):
    frames = _universe()
    strat, _ = _strategy(tmp_path, frames, datetime(2026, 2, 25, 0, 20, tzinfo=UTC))
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    tgt = strat(_snapshot(prices=prices))
    assert isinstance(tgt, TargetPortfolio)
    TargetPortfolio.model_validate(tgt.model_dump())                # re-validates (no duplicates, tz-aware)
    assert len({t.symbol for t in tgt.longs}) == len(tgt.longs) and len({t.symbol for t in tgt.shorts}) == len(tgt.shorts)
    gross = sum(t.weight for t in tgt.longs) + sum(t.collateral_usd for t in tgt.shorts) / 100_000
    assert 0.9 < gross <= 0.98 + 1e-9                              # gross 1.0 capped to 0.98 for cash/fees
    for t in tgt.longs:
        assert t.limit_price == pytest.approx(prices[t.symbol] * (1 - 0.0005))
    for t in tgt.shorts:
        assert t.limit_price == pytest.approx(prices[t.symbol] * (1 + 0.0005))
    assert tgt.signal_id == "comp-20260225T0000"
    assert "first start" in tgt.reason


def test_signal_id_idempotent_within_day_and_new_next_day(tmp_path):
    frames = _universe(days=60, end="2026-03-01")
    strat, clock = _strategy(tmp_path, frames, datetime(2026, 2, 25, 0, 20, tzinfo=UTC), requote=False)
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    first = strat(_snapshot(prices=prices))
    ids = set()
    for _ in range(23 * 4):                    # every 15 min until 23:20
        clock.advance(15 * 60)
        ids.add(strat(_snapshot(prices=prices)).signal_id)
    assert ids == {first.signal_id}
    clock.advance(60 * 60)                     # 00:20 next day: the 00:00 bar has closed
    nxt = strat(_snapshot(prices=prices))
    assert nxt.signal_id == "comp-20260226T0000" and "daily rebalance" in nxt.reason


def test_within_band_symbols_frozen_and_requote_only_when_off_target(tmp_path):
    frames = _universe()
    strat, clock = _strategy(tmp_path, frames, datetime(2026, 2, 25, 0, 20, tzinfo=UTC))
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    first = strat(_snapshot(prices=prices))
    w = strat.scaled_weights()
    eq = 100_000.0
    # everything filled exactly on target -> later bars return the same signal
    on_target = _snapshot(equity=eq, prices=prices, longs={s: v * eq for s, v in w.items() if v > 0},
                          shorts={s: -v * eq for s, v in w.items() if v < 0})
    clock.advance(15 * 60)
    same = strat(on_target)
    assert same.signal_id == first.signal_id
    # one long missing (its limit expired unfilled) -> re-quote at the next bar, others frozen
    miss = max((s for s in w if w[s] > 0), key=lambda s: w[s])
    off = dict(on_target, longs={s: v for s, v in on_target["longs"].items() if s != miss})
    req = strat(off)                                              # first bar it is seen off target
    assert req.signal_id.startswith(first.signal_id + "-r") and miss in req.reason
    by_sym = {t.symbol: t for t in req.longs}
    assert by_sym[miss].weight == pytest.approx(w[miss])
    assert all(by_sym[s].notional_usd == pytest.approx(v) for s, v in off["longs"].items())   # frozen
    assert strat(off).signal_id == req.signal_id                 # same bar again: idempotent


# ---------------------------------------------------------------------------- lock-in
def test_lockin_triggers_scales_and_persists_across_restart(tmp_path):
    frames = _universe()
    now = datetime(2026, 2, 25, 0, 20, tzinfo=UTC)
    strat, clock = _strategy(tmp_path, frames, now)
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    strat(_snapshot(equity=100_000, prices=prices))
    base = strat.scaled_weights()
    strat(_snapshot(equity=105_900, prices=prices))
    assert not strat.locked
    strat(_snapshot(equity=106_100, prices=prices))               # 1st confirmation
    assert not strat.locked
    tgt = strat(_snapshot(equity=106_200, prices=prices))         # 2nd confirmation -> locked
    assert strat.locked and tgt.signal_id.endswith("-L")
    for s, v in strat.scaled_weights().items():           # base was capped 1.0 -> 0.98; locked = raw x 0.3
        assert v == pytest.approx(base[s] / 0.98 * 0.3, rel=1e-9)
    assert sum(abs(v) for v in strat.scaled_weights().values()) == pytest.approx(0.3, rel=1e-6)

    # restart: new process, same state file; equity has fallen back below +6% -> still locked
    strat2, _ = _strategy(tmp_path, frames, now + timedelta(hours=1))
    assert strat2.locked
    strat2(_snapshot(equity=101_000, prices=prices))
    assert strat2.locked and strat2.state.get("start_equity") == 100_000
    assert sum(abs(v) for v in strat2.scaled_weights().values()) == pytest.approx(0.3, rel=1e-6)


def test_neutral_mode_never_locks_and_mode_switch_refused(tmp_path):
    frames = _universe()
    strat, _ = _strategy(tmp_path, frames, datetime(2026, 2, 25, 0, 20, tzinfo=UTC), mode="neutral")
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    for eq in (100_000, 120_000, 130_000):
        strat(_snapshot(equity=eq, prices=prices))
    assert not strat.locked and mode_spec("neutral")["params"]["k"] == 5
    with pytest.raises(ValueError, match="never switch modes"):
        _strategy(tmp_path, frames, datetime(2026, 2, 25, 1, 0, tzinfo=UTC), mode="comp")


def test_no_data_holds_current_book(tmp_path):
    strat = CompetitionStrategy(BarBuffer(["BTC/USD"], fetch=lambda *a: pd.DataFrame()), mode="comp",
                                state_path=tmp_path / "s.json", clock=SimClock(datetime(2026, 2, 25, tzinfo=UTC)))
    tgt = strat(_snapshot(prices={"BTC": 1.0}, longs={"BTC": 500.0}))
    assert [t.notional_usd for t in tgt.longs] == [500.0] and "-hold-" in tgt.signal_id   # never an empty (flatten) target


def test_broken_snapshot_raises_instead_of_trading(tmp_path):
    frames = _universe()
    strat, _ = _strategy(tmp_path, frames, datetime(2026, 2, 25, 0, 20, tzinfo=UTC))
    prices = {to_coin(s): float(df["close"].iloc[-1]) for s, df in frames.items()}
    strat(_snapshot(equity=100_000, prices=prices))
    with pytest.raises(SnapshotRejected):          # e.g. get_balance failed: only short collateral left
        strat(_snapshot(equity=20_000, prices=prices, shorts={"C1": 20_000}))
    with pytest.raises(SnapshotRejected):
        strat(_snapshot(equity=0.0, prices=prices))
    assert strat(_snapshot(equity=99_000, prices=prices)).signal_id == "comp-20260225T0000"


# ---------------------------------------------------------------------------- config + engine end-to-end
def test_competition_config_values():
    cfg = load_competition_config()
    assert cfg.live_mode is False and cfg.dry_run is True
    assert cfg.strategy_poll_interval_seconds >= 60 and cfg.fill_timeout_seconds == 900
    assert cfg.max_gross_exposure == 1.0 and cfg.max_effective_leverage == 1.0
    assert cfg.max_total_short_collateral_usd >= 50_000 and cfg.max_per_symbol_exposure >= 0.56
    assert cfg.max_order_value_usd >= 60_000 and cfg.max_daily_loss_usd > 1e6


def test_engine_end_to_end_on_replay_port(tmp_path):
    frames = _universe(n=10, days=70, end="2026-03-01")
    clock = SimClock(datetime(2026, 2, 25, 0, 16, tzinfo=UTC))
    sim = ReplayExchangePort(frames, clock, initial_usd=100_000)
    port = ThrottledPort(sim, clock, max_per_minute=25)
    engine = Engine(port, config=load_competition_config(dry_run=False), state_path=tmp_path / "e.db",
                    audit_path=tmp_path / "a.jsonl", clock=clock)
    buf = BarBuffer(list(frames), fetch=_frame_fetch(frames))
    strat = CompetitionStrategy(buf, mode="comp", state_path=tmp_path / "s.json", clock=clock)
    results = engine.run(strat, max_iterations=180)                # 3 hours at 60 s
    engine.close()
    statuses = [r["status"] for r in results]
    assert statuses[0] == "EXECUTED" and "REJECTED_RISK" not in statuses
    assert {op["kind"] for op in results[0]["operations"]} == {"open_long", "open_short"}
    assert statuses.count("DUPLICATE") > 150                        # no churn between bars
    assert sim.fills and port.peak <= 25
    assert sim.equity() == pytest.approx(100_000, rel=0.05)


def test_runner_full_short_exit_closes_losing_short_completely():
    from tradebot.core.config import ExecutionConfig
    from tradebot.engine.execution.runner import ExecutionRunner
    from tradebot.engine.state.intent_store import IntentJournal
    from tradebot.engine.state.snapshot import read_exchange_snapshot
    from tradebot.exchange import MockExchangePort

    port = MockExchangePort(initial_wallet={"USD": 10_000.0}, tickers={"ETH/USD": 2000.0})
    port.open_short("ETH", 1000.0)                       # 0.5 ETH at 2000
    port.tickers["ETH/USD"] = 2500.0                      # short is losing: collateral/price = 0.4 ETH only
    runner = ExecutionRunner(port, IntentJournal(memory=True), ExecutionConfig(dry_run=False))
    target = TargetPortfolio(strategy_id="t", strategy_version="v", signal_id="exit", timestamp=datetime.now(UTC))
    snap = read_exchange_snapshot(port)
    result = runner.execute(target, snap, snap["equity_usd"])
    assert [op["kind"] for op in result["operations"]] == ["close_short"]
    assert port.short_positions == []                     # no residual 0.1 ETH short left behind
