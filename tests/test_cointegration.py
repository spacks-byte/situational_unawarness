from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradebot.core.clock import SimClock
from tradebot.core.cointegration import CointegrationConfig
from tradebot.live.pairs import PairRuntime
from tradebot.strategy.library.cointegration import STEP

FIXTURES = Path(__file__).parent / "fixtures" / "cointegration"


def fixture(case="recent_24h"):
    folder = FIXTURES / case
    meta = json.loads((folder / "manifest.json").read_text())
    data = {}
    for asset, spec in meta["assets"].items():
        path = folder / spec["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == spec["sha256"]
        frame = pd.read_csv(path, index_col="open_time")
        frame.index = pd.to_datetime(frame.index, utc=True)
        data[asset] = frame
    for name, digest in meta["expected_sha256"].items():
        assert hashlib.sha256((folder / "expected" / name).read_bytes()).hexdigest() == digest
    config = CointegrationConfig(cycle_start=meta["test_start"], cycle_end=meta["test_end"])
    return config, data, folder


@pytest.mark.parametrize("case", ["recent_24h", "fortnight_43"])
def test_archived_equity_trades_weights_and_exposure(tmp_path, case):
    config, data, folder = fixture(case)
    clock = SimClock(config.cycle_start)
    path = tmp_path / "pairs.db"
    runtime = PairRuntime(config, path, clock=clock)
    runtime.initialize(data)
    assert runtime.pending() == []  # final training close is not an entry
    expected_weights = pd.read_csv(folder / "expected/weights.csv", index_col="pair")
    for pair, model in runtime.state["models"].items():
        assert model["weight"] == pytest.approx(expected_weights.loc[pair, "weight"], rel=1e-9, abs=1e-8)
        assert model["sigma"] == pytest.approx(expected_weights.loc[pair, "daily_pair_volatility"], rel=1e-9, abs=1e-8)
    marks = []
    for n, ts in enumerate(pd.date_range(config.cycle_start, config.cycle_end, freq=STEP, inclusive="left")):
        opens = {c: float(f.loc[ts, "open"]) for c, f in data.items()}
        closes = {c: float(f.loc[ts, "close"]) for c, f in data.items()}
        runtime.fill_reference(ts, opens)
        clock.advance(1800)
        obs = runtime.process_close(ts, closes)
        runtime.fill_reference(ts+STEP, closes, terminal=True)
        mark = runtime.ledger.mark(closes)
        mark["timestamp"] = ts+STEP
        marks.append(mark)
        # Repeat both a decision and a restart while positions/intents are active.
        assert runtime.process_close(ts, closes) == []
        if n in {2, 20, 300}:
            saved = deepcopy(runtime.state)
            runtime.close()
            runtime = PairRuntime(config, path, clock=clock)
            assert runtime.state == saved
    expected = pd.read_csv(folder / "expected/portfolio_equity.csv")
    for key in ("equity", "net_pnl", "fees", "slippage"):
        np.testing.assert_allclose([m[key] for m in marks], expected[key], rtol=1e-9, atol=1e-8, err_msg=key)
    actual_trades = [json.loads(r[0]) for r in runtime.db.execute("SELECT payload FROM pair_trades")]
    expected_trades = pd.read_csv(folder / "expected/trades.csv")
    assert len(actual_trades) == len(expected_trades)
    for trade in actual_trades:
        found = expected_trades[(expected_trades.pair == trade["pair"]) &
            (pd.to_datetime(expected_trades.entry_time, utc=True) == pd.Timestamp(trade["entry_time"]))]
        assert len(found) == 1
        e = found.iloc[0]
        assert pd.Timestamp(trade["exit_time"]) == pd.Timestamp(e.exit_time)
        assert trade["exit_reason"] == e.exit_reason
        assert trade["net_pnl"] == pytest.approx(e.pnl_net, rel=1e-9, abs=1e-8)
        for leg, col in (("A", "qty_a"), ("B", "qty_b")):
            p = trade["legs"][leg]
            qty = p["quantity"]*(1 if p["side"] == "long" else -1)
            assert qty == pytest.approx(e[col], rel=1e-9, abs=1e-8)
    # Venue marks and report exposures have the same closed-bar ordering.
    expected_exposure = pd.read_csv(folder / "expected/asset_net_exposure.csv")
    for asset in config.assets:
        np.testing.assert_allclose([m["exposure"].get(asset, {}).get("net_notional", 0.) for m in marks],
                                   expected_exposure[asset], rtol=1e-9, atol=1e-8)
    assert not runtime.pending() and not runtime.ledger.positions()
    if (folder / "expected/signals.csv.gz").exists():
        signals = pd.read_csv(folder / "expected/signals.csv.gz")
        signals["candle_open"] = pd.to_datetime(signals.candle_open, utc=True)
        signals = signals.set_index(["pair", "candle_open"])
        for row in runtime.db.execute("SELECT payload FROM pair_decisions"):
            obs = json.loads(row[0])
            expected_signal = signals.loc[(obs["pair"], pd.Timestamp(obs["candle"]))]
            for actual, reference in (("spread", "spread"), ("mean", "rolling_mean"), ("std", "rolling_std"), ("z", "z")):
                assert obs[actual] == pytest.approx(expected_signal[reference], rel=1e-9, abs=1e-8)
    runtime.close()


def test_defaults_equal_authoritative_handoff():
    reference = json.loads((FIXTURES / "strategy_config.json").read_text())
    assert [p.model_dump() for p in CointegrationConfig().pairs] == reference["pairs"]


def test_bad_history_and_future_candles_do_not_advance(tmp_path):
    config, data, _ = fixture()
    clock = SimClock(config.cycle_start)
    runtime = PairRuntime(config, tmp_path / "pairs.db", clock=clock)
    damaged = dict(data, FIL=data["FIL"].drop(data["FIL"].index[1]))
    with pytest.raises(ValueError, match="incomplete"):
        runtime.initialize(damaged)
    assert runtime.state is None and not runtime.ledger.accounts()
    runtime.initialize(data)
    state = deepcopy(runtime.state)
    with pytest.raises(ValueError, match="unfinished"):
        runtime.process_close(pd.Timestamp(config.cycle_start), {c: f.close.iloc[-1] for c, f in data.items()})
    assert runtime.state == state
    runtime.close()


def test_failed_decision_commit_and_restart_between_leg_fills(tmp_path):
    config, data, _ = fixture()
    clock = SimClock(config.cycle_start)
    path = tmp_path / "pairs.db"
    runtime = PairRuntime(config, path, clock=clock)
    runtime.initialize(data)
    ts = pd.Timestamp(config.cycle_start)
    clock.advance(1800)
    runtime.process_close(ts, {c: float(f.loc[ts, "close"]) for c, f in data.items()})
    saved = deepcopy(runtime.state)
    # FIL-MIRA creates an intent before the next pair causes the write to fail.
    runtime.db.execute("CREATE TRIGGER fail_pair BEFORE INSERT ON pair_decisions "
                       "WHEN NEW.pair_id='AVNT-MIRA' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    ts += STEP
    clock.advance(1800)
    closes = {c: float(f.loc[ts, "close"]) for c, f in data.items()}
    with pytest.raises(Exception, match="disk failure"):
        runtime.process_close(ts, closes)
    assert runtime.state == saved and not runtime.pending()
    assert runtime.db.execute("SELECT COUNT(*) FROM owner_reservations").fetchone()[0] == 0
    runtime.db.execute("DROP TRIGGER fail_pair")
    runtime.process_close(ts, closes)
    intent = next(i for i in runtime.pending() if i["pair"] == "FIL-MIRA")
    px = float(data["FIL"].loc[ts+STEP, "open"])
    fill = dict(fill_id="first-leg", quantity=intent["budget"]/2/px, price=px, timestamp=ts+STEP, fee=1.)
    runtime.record_fill(intent["intent_id"], "A", **fill)
    runtime.close()
    runtime = PairRuntime(config, path, clock=clock)
    assert len(runtime.ledger.positions()) == 1 and runtime.pending()[0]["completed_legs"] == ["A"]
    assert not runtime.record_fill(intent["intent_id"], "A", **fill)
    runtime.fill_reference(ts+STEP, {c: float(f.loc[ts+STEP, "open"]) for c, f in data.items()})
    assert len(runtime.ledger.positions()) == 2 and not runtime.pending()
    runtime.close()
