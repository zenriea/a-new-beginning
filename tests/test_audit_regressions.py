"""Regressions found during the end-to-end safety/reliability review."""
import pytest

from conftest import make_strategy, put_quote


@pytest.mark.parametrize("group,patch", [
    ("execution", {"latency_sec": -1}),
    ("execution", {"order_ttl_sec": "600"}),
    ("execution", {"fractional_shares": 1}),
    ("research", {"train_frac": 0.9, "val_frac": 0.2}),
    ("research", {"incubation_fraction": 2}),
    ("research", {"max_param_combos": 1.5}),
    ("schedule", {"bar_refresh_min": 0}),
    ("schedule", {"review_hour_utc": 1.5}),
    ("costs", {"CRYPTO": {"taker_fee_bps": -25}}),
    ("costs", {"NASDAQ": {"impact_coef": float("inf")}}),
    ("costs", {"CRYPTO": {"unknown": 1}}),
    ("costs", {"CRYPTO": []}),
    ("universe", {"CRYPTO": "BTC-USD"}),
    ("universe", {"CRYPTO": ["BTC-USD", "BTC-USD"]}),
    ("universe", {"NASDAQ": ["BTC-USD"]}),
    ("benchmarks", {"account": ""}),
])
def test_settings_reject_malformed_values_without_writing(env, group, patch):
    from quantdesk import db, settings
    with pytest.raises(settings.SettingsError):
        settings.operator_update(group, patch)
    assert db.q1("SELECT COUNT(*) n FROM audit_log")["n"] == 0


def test_nested_settings_preserve_unspecified_values(env):
    from quantdesk import settings
    before = settings.get("costs")
    after = settings.operator_update("costs", {"CRYPTO": {"taker_fee_bps": 30}})
    assert after["CRYPTO"]["taker_fee_bps"] == 30
    assert after["CRYPTO"]["impact_coef"] == before["CRYPTO"]["impact_coef"]
    assert after["NASDAQ"] == before["NASDAQ"]


@pytest.mark.parametrize("venue", ["CRYPTO", "NASDAQ"])
def test_default_demo_proposal_fits_strategy_limit(env, venue):
    from quantdesk.agent.analyst import RulesAnalyst
    from quantdesk.config import DEFAULT_SETTINGS
    from quantdesk.research.strategy import validate_spec
    ctx = {"venue": venue, "approved_universe": DEFAULT_SETTINGS["universe"][venue],
           "benchmark": DEFAULT_SETTINGS["benchmarks"][venue]}
    proposal, _ = RulesAnalyst().propose(ctx)
    validate_spec(proposal["spec"], DEFAULT_SETTINGS["universe"])


def test_retirement_cancels_queued_buys(env):
    from quantdesk import db
    from quantdesk.trading import live, oms
    sid = make_strategy()
    put_quote("AAPL", 100, 100)
    order = oms.create_order(symbol="AAPL", side="buy", qty=5, reason="entry", rationale="test",
                             idem_key="before-retirement", strategy_id=sid)
    live.retire_strategy(dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,))), "test", origin="operator")
    assert db.q1("SELECT status FROM orders WHERE id=?", (order["id"],))["status"] == "cancelled"
    assert db.q1("SELECT status FROM strategies WHERE id=?", (sid,))["status"] == "retired"
    blocked = oms.create_order(symbol="AAPL", side="buy", qty=5, reason="entry", rationale="stale signal",
                               idem_key="after-retirement", strategy_id=sid)
    assert blocked["status"] == "rejected"


def test_flatten_retries_expired_exits_without_new_bars_or_duplicates(env):
    from quantdesk import clock, db, settings, worker
    from quantdesk.trading import ledger, live, oms
    from test_ledger import _order
    sid = make_strategy()
    put_quote("AAPL", 100, 100)
    ledger.apply_fill(_order(sid, "AAPL", "buy", 5), 5, 100, 0, {})
    strat = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    live.retire_strategy(strat, "operator retirement", origin="operator")
    first = oms.open_orders()[0]
    oms.close_order(first, "expired", "no quote before TTL")
    settings.operator_update("agent", {"emergency_stop": True})
    clock.advance(61)
    put_quote("AAPL", 100, 100)
    worker.signals_tick()
    retry = oms.open_orders()
    assert len(retry) == 1 and retry[0]["id"] != first["id"]
    assert retry[0]["origin"] == "operator" and retry[0]["qty"] == 5
    worker.signals_tick()
    assert len(oms.open_orders()) == 1
    assert ledger.integrity_check()["ok"]


def test_market_dependencies_include_held_removed_and_benchmark_symbols(env):
    from quantdesk import settings, worker
    from quantdesk.trading import ledger
    from test_ledger import _order
    sid = make_strategy()
    ledger.apply_fill(_order(sid, "AAPL", "buy", 1), 1, 100, 0, {})
    settings.operator_update("universe", {"NASDAQ": ["MSFT"], "CRYPTO": []})
    watched = worker.market_symbols()
    assert {"MSFT", "AAPL", "QQQ"} <= set(watched["NASDAQ"])
    assert "BTC-USD" in watched["CRYPTO"]


def test_ledger_reloads_order_and_rejects_replay_or_cancelled_fill(env):
    from quantdesk import db
    from quantdesk.trading import ledger, oms
    from test_ledger import _order
    sid = make_strategy()
    order = _order(sid, "AAPL", "buy", 5)
    ledger.apply_fill(order, 2, 100, 0, {})
    ledger.apply_fill(order, 3, 101, 0, {})  # deliberately stale order object
    assert db.q1("SELECT filled_qty FROM orders WHERE id=?", (order["id"],))["filled_qty"] == 5
    with pytest.raises(ValueError):
        ledger.apply_fill(order, 1, 100, 0, {})
    cancelled = _order(sid, "AAPL", "buy", 1)
    oms.close_order(cancelled, "cancelled", "operator")
    with pytest.raises(ValueError):
        ledger.apply_fill(cancelled, 1, 100, 0, {})
    assert ledger.integrity_check()["ok"]


@pytest.mark.parametrize("qty,price,fee", [(0, 100, 0), (-1, 100, 0), (1, -1, 0),
                                         (1, float("inf"), 0), (1, 100, -1)])
def test_ledger_rejects_invalid_fill_values(env, qty, price, fee):
    from quantdesk.trading import ledger
    from test_ledger import _order
    sid = make_strategy()
    with pytest.raises(ValueError):
        ledger.apply_fill(_order(sid, "AAPL", "buy", 5), qty, price, fee, {})
    assert ledger.cash() == 100000
