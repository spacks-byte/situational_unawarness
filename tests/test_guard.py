from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot.live.guard import (
    Guard,
    GuardConfig,
    ProposedOrder,
    Status,
    check_api_budget,
    check_cash_buffer,
    check_drawdown,
    check_drift,
    check_gross_exposure,
    check_heartbeat,
    check_kill_switch,
    check_lockin,
    check_net_exposure,
    check_order_notional,
    check_order_rate,
    check_price_band,
    check_self_cross,
    check_short_collateral,
    check_stale_data,
    check_symbol_weights,
    exposures,
    normalize_symbol,
    project_snapshot,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
PRICES = {"BTC": 80_000.0, "ETH": 3_000.0, "SOL": 150.0, "DOGE": 0.2, "ADA": 0.5, "XRP": 2.0}


def snap(cash=0.0, longs=None, shorts=None, equity=100_000.0, pending=None):
    return {"cash_usd": cash, "longs": longs or {}, "shorts": shorts or {}, "equity_usd": equity,
            "prices": dict(PRICES), "entry_prices": {}, "pending_orders": pending or []}


def comp_book():
    # 65/35 comp-mode book at gross 1.0, net +0.30
    return snap(cash=300.0, longs={"BTC": 25_000, "ETH": 20_000, "SOL": 20_000},
                shorts={"DOGE": 12_000, "ADA": 12_000, "XRP": 11_000})


def fresh_times(age_s=10):
    return {s: NOW - timedelta(seconds=age_s) for s in PRICES}


def guard(tmp_path, **kw):
    cfg = GuardConfig(kill_file=str(tmp_path / "KILL"), **kw)
    g = Guard(cfg, now=lambda: NOW)
    g.record_fill(NOW)
    return g


# ------------------------------------------------------------------ result shape
def test_check_result_is_a_five_tuple():
    r = check_drawdown(95.0, 100.0)
    name, status, value, limit, message = r
    assert name == "drawdown" and status is Status.WARN and value == pytest.approx(-0.05)
    assert r.to_dict()["status"] == "WARN"


def test_normalize_symbol():
    assert normalize_symbol("btc/usd") == "BTC"
    assert normalize_symbol("ETHUSDT") == "ETH"
    assert normalize_symbol("SOL") == "SOL"


# ------------------------------------------------------------------ exposure checks
def test_gross_exposure_blocks_leverage_only_when_batch_adds_it():
    assert check_gross_exposure(1.0).status is Status.OK
    assert check_gross_exposure(1.05, before=0.9).status is Status.BLOCK
    # a de-risking batch that still leaves gross > 1 is never blocked
    assert check_gross_exposure(1.05, before=1.08).status is Status.WARN
    # on a poll: drift above 1 is a WARN, far above the hard limit a BLOCK
    assert check_gross_exposure(1.03).status is Status.WARN
    assert check_gross_exposure(1.2).status is Status.BLOCK


def test_symbol_weight_cap():
    assert check_symbol_weights({"BTC": 0.25, "DOGE": -0.2}).status is Status.OK
    assert check_symbol_weights({"BTC": 0.33}).status is Status.WARN
    r = check_symbol_weights({"BTC": 0.1, "DOGE": -0.45})
    assert r.status is Status.BLOCK and "DOGE" in r.message
    assert check_symbol_weights({"DOGE": -0.45}, before={"DOGE": -0.5}).status is Status.WARN


def test_net_exposure_bounds():
    assert check_net_exposure(0.30).status is Status.OK
    assert check_net_exposure(0.50).status is Status.WARN
    assert check_net_exposure(0.70).status is Status.BLOCK
    assert check_net_exposure(-0.30).status is Status.BLOCK
    assert check_net_exposure(0.65, before=0.75).status is Status.WARN  # moving back inside


def test_short_collateral_cap():
    assert check_short_collateral(35_000, 100_000).status is Status.OK
    assert check_short_collateral(42_000, 100_000).status is Status.WARN
    assert check_short_collateral(50_000, 100_000).status is Status.BLOCK
    assert check_short_collateral(20_000, 100_000, max_usd=10_000).status is Status.BLOCK
    assert check_short_collateral(50_000, 100_000, before_usd=60_000).status is Status.WARN


def test_cash_buffer():
    assert check_cash_buffer(500).status is Status.OK
    assert check_cash_buffer(20).status is Status.WARN
    assert check_cash_buffer(-100, before=5_000).status is Status.BLOCK


# ------------------------------------------------------------------ order sanity
def test_price_band_fat_finger():
    ok = [ProposedOrder("BTC", "BUY", 0.1, 80_000 * 0.9995)]
    assert check_price_band(ok, PRICES).status is Status.OK
    warn = [ProposedOrder("BTC", "BUY", 0.1, 80_000 * 0.985)]
    assert check_price_band(warn, PRICES).status is Status.WARN
    fat = [ProposedOrder("ETH", "SELL", 1, 300.0)]  # dropped a zero
    assert check_price_band(fat, PRICES).status is Status.BLOCK
    market = [ProposedOrder("ETH", "SELL", 1)]
    assert check_price_band(market, PRICES).status is Status.OK
    assert check_price_band([ProposedOrder("XYZ", "BUY", 1, 1.0)], PRICES).status is Status.BLOCK


def test_order_notional_caps():
    assert check_order_notional([ProposedOrder("BTC", "BUY", 0.25)], PRICES, 100_000).status is Status.OK
    big = check_order_notional([ProposedOrder("BTC", "BUY", 1.0)], PRICES, 100_000)
    assert big.status is Status.BLOCK and big.limit == 40_000
    tiny = check_order_notional([ProposedOrder("DOGE", "BUY", 10)], PRICES, 100_000)
    assert tiny.status is Status.WARN


def test_self_cross_blocks_both_sides_on_one_symbol():
    assert check_self_cross([ProposedOrder("BTC", "BUY", 0.1), ProposedOrder("ETH", "SELL", 1)]).status is Status.OK
    r = check_self_cross([ProposedOrder("BTC", "BUY", 0.1), ProposedOrder("BTC/USD", "SELL", 0.05)])
    assert r.status is Status.BLOCK and r.value == ["BTC"]
    # against a resting order on the other side
    pending = [{"Pair": "SOL/USD", "Side": "SELL"}]
    assert check_self_cross([ProposedOrder("SOL", "BUY", 1)], pending).status is Status.BLOCK
    # short open (sell side) + spot buy on one coin is also two-sided
    assert check_self_cross([ProposedOrder("SOL", "SHORT", 1), ProposedOrder("SOL", "BUY", 1)]).status is Status.BLOCK


def test_order_rate_and_api_budget():
    assert check_order_rate(0, 6).status is Status.OK
    assert check_order_rate(10, 6).status is Status.WARN
    assert check_order_rate(18, 6).status is Status.BLOCK
    assert check_api_budget(5, 6).status is Status.OK
    assert check_api_budget(15, 6).status is Status.WARN
    assert check_api_budget(25, 6).status is Status.BLOCK


def test_stale_data():
    assert check_stale_data(fresh_times(), NOW).status is Status.OK
    assert check_stale_data(fresh_times(200), NOW).status is Status.WARN
    assert check_stale_data(fresh_times(600), NOW).status is Status.BLOCK
    assert check_stale_data(None, NOW).status is Status.WARN
    assert check_stale_data({"BTC": NOW}, NOW, symbols=["ETH"]).status is Status.BLOCK
    assert check_stale_data({"BTC": NOW.isoformat()}, NOW).status is Status.OK


# ------------------------------------------------------------------ monitors
def test_drawdown_is_warn_only():
    assert check_drawdown(100, 100).status is Status.OK
    assert check_drawdown(94, 100).status is Status.WARN
    deep = check_drawdown(70, 100)
    assert deep.status is Status.WARN and "ALERT" in deep.message  # never BLOCK


def test_lockin_consistency():
    assert check_lockin(0.02, False, 1.0, 1.0).status is Status.OK
    assert check_lockin(0.06, True, 0.3, 0.3).status is Status.OK
    assert check_lockin(0.06, True, 1.0, 1.0).status is Status.BLOCK      # strategy ignored the lock
    assert check_lockin(0.06, True, 0.3, 0.9).status is Status.WARN       # de-risking in progress
    assert check_lockin(0.06, True, 0.3, 0.3, bot_locked=False).status is Status.WARN
    assert check_lockin(0.01, False, 0.3, 0.3, bot_locked=True).status is Status.WARN


def test_drift():
    assert check_drift({"BTC": 0.2}, {"BTC": 0.21}).status is Status.OK
    assert check_drift({"BTC": 0.2}, {"BTC": 0.25}).status is Status.WARN
    r = check_drift({"BTC": 0.2}, {"ETH": 0.2})
    assert r.status is Status.WARN and "ALERT" in r.message
    assert check_drift({"BTC": 0.2}, None).status is Status.OK


def test_heartbeat():
    assert check_heartbeat({NOW.date()}, NOW).status is Status.OK
    assert check_heartbeat(set(), NOW).status is Status.OK  # noon, plenty of time
    assert check_heartbeat({date(2026, 10, 4)}, NOW.replace(hour=19)).status is Status.WARN
    late = check_heartbeat(set(), NOW.replace(hour=23))
    assert late.status is Status.WARN and "URGENT" in late.message
    assert check_heartbeat(["2026-10-05T01:00:00+00:00"], NOW).status is Status.OK


def test_kill_switch_file(tmp_path):
    kill = tmp_path / "KILL"
    assert check_kill_switch(kill).status is Status.OK
    kill.write_text("stop")
    assert check_kill_switch(kill).status is Status.BLOCK
    assert check_kill_switch(None, engaged=True).status is Status.BLOCK


# ------------------------------------------------------------------ projection
def test_project_snapshot_applies_orders():
    s = snap(cash=10_000, longs={"BTC": 40_000}, shorts={"DOGE": 10_000})
    p = project_snapshot(s, [ProposedOrder("ETH", "BUY", 1, 3_000), ProposedOrder("DOGE", "COVER", 25_000),
                             ProposedOrder("BTC", "SELL", 0.125, 80_000)])
    assert p["longs"]["ETH"] == pytest.approx(3_000)
    assert p["longs"]["BTC"] == pytest.approx(30_000)
    assert p["shorts"]["DOGE"] == pytest.approx(5_000)
    assert p["cash_usd"] == pytest.approx(10_000 - 3_000 * 1.0005 + 5_000 * 0.999 + 10_000 * 0.9995)  # limit sell: maker fee
    e = exposures(p)
    assert e["gross"] == pytest.approx(38_000 / p["equity_usd"])


# ------------------------------------------------------------------ Guard integration
def test_guard_clean_comp_book_passes(tmp_path):
    g = guard(tmp_path)
    target = {"BTC": 0.25, "ETH": 0.20, "SOL": 0.20, "DOGE": -0.12, "ADA": -0.12, "XRP": -0.11}
    rep = g.poll(comp_book(), target_weights=target, price_times=fresh_times())
    assert rep.allowed and rep.status is Status.OK, rep.summary()
    assert len(rep.results) == 16
    assert {r.name for r in rep.results} >= {"kill_switch", "gross_exposure", "self_cross", "lockin", "daily_heartbeat"}


def test_guard_pre_trade_blocks_leveraging_batch(tmp_path):
    g = guard(tmp_path)
    orders = [ProposedOrder("SOL", "BUY", 100, 150 * 0.9995)]  # +$15k on a fully invested book
    rep = g.pre_trade(comp_book(), orders, price_times=fresh_times())
    assert not rep.allowed
    assert {r.name for r in rep.blocks} >= {"gross_exposure", "cash_buffer"}


def test_guard_allows_rebalance_that_reduces_risk(tmp_path):
    g = guard(tmp_path)
    over = snap(cash=0, longs={"BTC": 60_000, "ETH": 30_000}, shorts={"DOGE": 20_000}, equity=100_000)
    orders = [ProposedOrder("BTC", "SELL", 0.25, 80_000 * 1.0005)]
    rep = g.pre_trade(over, orders, price_times=fresh_times())
    assert rep.allowed, rep.summary()


def test_guard_kill_file_blocks_everything(tmp_path):
    g = guard(tmp_path)
    (tmp_path / "KILL").touch()
    rep = g.pre_trade(comp_book(), [ProposedOrder("BTC", "SELL", 0.01, 80_040)], price_times=fresh_times())
    assert not rep.allowed and rep["kill_switch"].status is Status.BLOCK
    (tmp_path / "KILL").unlink()
    assert g.pre_trade(comp_book(), [ProposedOrder("BTC", "SELL", 0.01, 80_040)], price_times=fresh_times()).allowed


def test_guard_rate_limit_uses_recorded_calls(tmp_path):
    g = guard(tmp_path)
    g.record_api_call(NOW - timedelta(seconds=30), n=20)
    g.record_api_call(NOW - timedelta(seconds=120), n=50)  # outside the window
    orders = [ProposedOrder("BTC", "SELL", 0.01, 80_040) for _ in range(5)]
    assert g.pre_trade(comp_book(), orders, price_times=fresh_times())["api_budget"].status is Status.WARN
    g.record_orders_sent(5, NOW - timedelta(seconds=5))
    rep = g.pre_trade(comp_book(), orders, price_times=fresh_times())
    assert rep["api_budget"].status is Status.BLOCK


def test_guard_lockin_latches_and_blocks_full_size_target(tmp_path):
    g = guard(tmp_path)
    up = comp_book()
    up["equity_usd"] = 106_000
    full = {"BTC": 0.25, "ETH": 0.20, "SOL": 0.20, "DOGE": -0.12, "ADA": -0.12, "XRP": -0.11}
    rep = g.poll(up, target_weights=full, price_times=fresh_times())
    assert g.locked and rep["lockin"].status is Status.BLOCK
    # the latch is sticky even after equity falls back below +5%
    down = comp_book()
    down["equity_usd"] = 103_000
    scaled = {k: v * 0.3 for k, v in full.items()}
    rep = g.poll(down, target_weights=scaled, price_times=fresh_times())
    assert g.locked and rep["lockin"].status is Status.WARN  # actual book still full size: de-risking
    assert rep["drawdown"].status is Status.OK  # 106k -> 103k is -2.8%


def test_guard_report_serializes(tmp_path):
    rep = guard(tmp_path).poll(comp_book(), price_times=fresh_times())
    d = rep.to_dict()
    assert d["allowed"] is True and len(d["results"]) == 16
    assert "guard poll" in rep.summary()


# ------------------------------------------------------------------ dashboard (engine source smoke test)
def test_dashboard_builds_from_mock_engine_run(tmp_path):
    from tradebot.core.config import ExecutionConfig
    from tradebot.dashboard import build, sources
    from tradebot.engine import Engine, LongTarget, TargetPortfolio
    from tradebot.engine.state.snapshot import read_exchange_snapshot
    from tradebot.exchange import MockExchangePort

    port = MockExchangePort(initial_wallet={"USD": 100_000.0})
    engine = Engine(port, config=ExecutionConfig(dry_run=False), state_path=tmp_path / "engine_state.db",
                    audit_path=tmp_path / "engine_audit.jsonl")
    engine.runner.execute(
        TargetPortfolio(strategy_id="t", strategy_version="v1", signal_id="s1", timestamp=NOW,
                        longs=[LongTarget(symbol="BTC", weight=0.2)]),
        read_exchange_snapshot(port), 100_000.0)
    engine.close()
    import json
    (tmp_path / "latest_snapshot.json").write_text(json.dumps(read_exchange_snapshot(port)))

    data = sources.load_engine(tmp_path, kill_file=str(tmp_path / "KILL"))
    assert data["source"] == "engine"
    assert any(t["symbol"] == "BTC" for t in data["trades"])
    assert data["positions"] and data["positions"][0]["symbol"] == "BTC"
    out = tmp_path / "dash.html"
    build.write_html(data, out)
    html = out.read_text()
    assert "<html" in html and "guard" in html.lower() and len(html) > 5_000


def test_lockin_never_blocks_a_sell_that_reduces_gross():
    """Oct 3 replay: after the lock the guard blocked the de-risking sells, freezing a full-size book."""
    g = Guard(GuardConfig(initial_equity_usd=100_000, lockin_return=0.06, lockin_scale=0.3), now=lambda: NOW)
    snap = {"cash_usd": 8_000, "longs": {"ARB": 60_000, "ETH": 40_000}, "shorts": {}, "equity_usd": 108_000,
            "prices": {"ARB": 1.0, "ETH": 2000.0}, "pending_orders": []}
    sell = g.pre_trade(snap, [ProposedOrder("ARB", "SELL", 30_000, 1.0005)])
    buy = g.pre_trade(snap, [ProposedOrder("ETH", "BUY", 1.0, 1999.0)])
    assert g.locked and sell["lockin"].status is Status.WARN and sell.allowed
    assert buy["lockin"].status is Status.BLOCK
