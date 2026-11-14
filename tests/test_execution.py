import pytest

from conftest import NASDAQ_OPEN_TS, make_strategy, put_bars, put_quote


def _setup(symbol="AAPL", tf="1d"):
    put_bars(symbol, [100 + i * 0.1 for i in range(40)], tf, volume=5e7)


def test_order_waits_for_latency_and_fresh_quote(env):
    from quantdesk import clock, db
    from quantdesk.trading import execution, oms
    _setup()
    sid = make_strategy()
    put_quote("AAPL", 100.0, 100.02)
    o = oms.create_order(symbol="AAPL", side="buy", qty=10, reason="entry", rationale="why", idem_key="k1", strategy_id=sid)
    assert o["status"] == "submitted"
    assert execution.process_orders() == []           # latency not elapsed
    clock.advance(3)
    assert execution.process_orders() == []           # quote observed BEFORE eligibility: not usable
    put_quote("AAPL", 101.0, 101.02)                    # fresh quote after eligibility
    fills = execution.process_orders()
    assert len(fills) == 1
    f = db.q1("SELECT * FROM fills")
    assert f["price"] > 101.02                          # paid the ask plus impact
    assert f["ref_ask"] == 101.02
    # intent journal entry precedes fill entry
    kinds = [r["kind"] for r in db.q("SELECT kind FROM journal ORDER BY id")]
    assert kinds.index("intent") < kinds.index("fill")


def test_duplicate_order_prevention(env):
    from quantdesk import db
    from quantdesk.trading import oms
    _setup()
    sid = make_strategy()
    put_quote("AAPL", 100.0, 100.02)
    a = oms.create_order(symbol="AAPL", side="buy", qty=10, reason="entry", rationale="why", idem_key="same", strategy_id=sid)
    b = oms.create_order(symbol="AAPL", side="buy", qty=10, reason="entry", rationale="why", idem_key="same", strategy_id=sid)
    assert a["id"] == b["id"] and b.get("duplicate")
    assert db.q1("SELECT COUNT(*) n FROM orders")["n"] == 1


def test_nasdaq_order_after_close_waits_for_session(env):
    from quantdesk import clock, venues
    from quantdesk.trading import execution, oms
    _setup()
    sid = make_strategy()
    evening = NASDAQ_OPEN_TS + 8 * 3600          # 18:00 New York
    clock.set_fixed(evening)
    put_quote("AAPL", 100.0, 100.02)
    o = oms.create_order(symbol="AAPL", side="buy", qty=10, reason="entry", rationale="why", idem_key="eve", strategy_id=sid)
    assert o["status"] == "submitted"
    assert o["eligible_at"] >= venues.next_open(evening)
    clock.advance(3600)
    put_quote("AAPL", 100.0, 100.02)
    assert execution.process_orders() == []      # still closed
    clock.set_fixed(o["eligible_at"] + 1)
    put_quote("AAPL", 100.0, 100.02)
    assert len(execution.process_orders()) == 1


def test_ttl_expiry_resets_state(env):
    from quantdesk import clock, db
    from quantdesk.trading import execution, oms
    _setup()
    sid = make_strategy()
    db.ex("INSERT INTO strategy_state(strategy_id, symbol, in_position) VALUES(?, 'AAPL', 1)", (sid,))
    put_quote("AAPL", 100.0, 100.02)
    o = oms.create_order(symbol="AAPL", side="buy", qty=10, reason="entry", rationale="why", idem_key="ttl", strategy_id=sid)
    clock.advance(10_000)  # past TTL, no fresh quote
    execution.process_orders()
    assert db.q1("SELECT status FROM orders WHERE id=?", (o["id"],))["status"] == "expired"
    assert db.q1("SELECT in_position FROM strategy_state WHERE strategy_id=?", (sid,))["in_position"] == 0


def test_liquidity_cap_partial_fill(env):
    from quantdesk import clock, db
    from quantdesk.trading import execution, oms
    put_bars("AAPL", [100.0] * 40, "1d", volume=1000)       # ADV ~ $100k -> 1% = $1k
    sid = make_strategy(allocation=50000)
    put_quote("AAPL", 100.0, 100.02)
    o = oms.create_order(symbol="AAPL", side="buy", qty=50, reason="entry", rationale="why", idem_key="liq", strategy_id=sid)
    clock.advance(3)
    put_quote("AAPL", 100.0, 100.02)
    execution.process_orders()
    o = dict(db.q1("SELECT * FROM orders WHERE id=?", (o["id"],)))
    assert 0 < o["filled_qty"] <= 10 and o["status"] == "cancelled"


def test_unusable_quote_falls_back_to_fresh_last_trade(env):
    from quantdesk import clock, db
    from quantdesk.trading import execution, oms
    _setup()
    sid = make_strategy()
    put_quote("AAPL", 100.0, 100.02)
    oms.create_order(symbol="AAPL", side="buy", qty=5, reason="entry", rationale="why", idem_key="fb", strategy_id=sid)
    clock.advance(3)
    put_quote("AAPL", 90.0, 110.0, last=100.0)        # 2000bp wide IEX quote
    execution.process_orders()
    f = db.q1("SELECT * FROM fills")
    assert "last trade" in f["price_basis"] and f["price"] < 101


def test_emergency_stop_cancels_at_fill_time(env):
    from quantdesk import clock, db, settings
    from quantdesk.trading import execution, oms
    _setup()
    sid = make_strategy()
    put_quote("AAPL", 100.0, 100.02)
    o = oms.create_order(symbol="AAPL", side="buy", qty=5, reason="entry", rationale="why", idem_key="es", strategy_id=sid)
    settings.operator_update("agent", {"emergency_stop": True})
    clock.advance(3)
    put_quote("AAPL", 100.0, 100.02)
    execution.process_orders()
    assert db.q1("SELECT status FROM orders WHERE id=?", (o["id"],))["status"] == "cancelled"
    assert db.q1("SELECT COUNT(*) n FROM fills")["n"] == 0


def test_costs_model():
    from quantdesk.config import DEFAULT_SETTINGS
    from quantdesk.trading import costs
    c = DEFAULT_SETTINGS["costs"]["NASDAQ"]
    assert costs.fees("NASDAQ", "buy", 100, 100, c) == 0.0
    sell = costs.fees("NASDAQ", "sell", 100, 100, c)
    assert sell == pytest.approx(10000 * 27.8 / 1e6 + 100 * 0.000166)
    assert costs.fees("NASDAQ", "sell", 1e6, 1, c) >= 8.30  # TAF capped, SEC fee not
    k = DEFAULT_SETTINGS["costs"]["CRYPTO"]
    assert costs.fees("CRYPTO", "buy", 1, 10000, k) == pytest.approx(25.0)
    assert costs.impact_bps(1e6, 1e8, 0.02, c) > costs.impact_bps(1e4, 1e8, 0.02, c)
