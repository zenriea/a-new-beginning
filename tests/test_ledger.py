import pytest

from conftest import make_strategy


_n = [0]


def _order(sid, symbol, side, qty):
    from quantdesk import clock, db
    _n[0] += 1
    oid = db.ex("INSERT INTO orders(client_order_id, strategy_id, symbol, venue, side, qty, status, reason, rationale, origin, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"x{_n[0]}", sid, symbol, "NASDAQ", side, qty,
                                                  "submitted", "t", "t", "agent", clock.now()))
    return dict(db.q1("SELECT * FROM orders WHERE id=?", (oid,)))


def test_buy_sell_accounting(env):
    from quantdesk.trading import ledger
    sid = make_strategy()
    ledger.apply_fill(_order(sid, "AAPL", "buy", 10), 10, 100.0, 1.0, {})
    assert ledger.cash() == pytest.approx(100000 - 1000 - 1)
    ledger.apply_fill(_order(sid, "AAPL", "buy", 10), 10, 110.0, 1.0, {})
    pos = ledger.sleeve_positions(sid)[0]
    assert pos["qty"] == 20 and pos["avg_cost"] == pytest.approx(105.0)
    ledger.apply_fill(_order(sid, "AAPL", "sell", 5), 5, 120.0, 0.5, {})
    pos = ledger.sleeve_positions(sid)[0]
    assert pos["qty"] == 15
    # realised = 5*(120-105) - all fees (1 + 1 + 0.5)
    assert pos["realized_pnl"] == pytest.approx(75 - 2.5)
    assert ledger.cash() == pytest.approx(100000 - 1001 - 1101 + 600 - 0.5)
    assert ledger.integrity_check()["ok"]


def test_no_shorting(env):
    from quantdesk.trading import ledger
    sid = make_strategy()
    ledger.apply_fill(_order(sid, "AAPL", "buy", 1), 1, 100.0, 0.0, {})
    with pytest.raises(ValueError):
        ledger.apply_fill(_order(sid, "AAPL", "sell", 2), 2, 100.0, 0.0, {})
    # failed transaction left nothing behind
    assert ledger.sleeve_positions(sid)[0]["qty"] == 1
    assert ledger.integrity_check()["ok"]


def test_order_progress_and_entry_price(env):
    from quantdesk import db
    from quantdesk.trading import ledger
    sid = make_strategy()
    db.ex("INSERT INTO strategy_state(strategy_id, symbol, in_position, entry_price) VALUES(?, 'AAPL', 1, 99)", (sid,))
    o = _order(sid, "AAPL", "buy", 10)
    ledger.apply_fill(o, 4, 100.0, 0.0, {})
    o = dict(db.q1("SELECT * FROM orders WHERE id=?", (o["id"],)))
    assert o["status"] == "partially_filled" and o["filled_qty"] == 4
    ledger.apply_fill(o, 6, 101.0, 0.0, {})
    o = dict(db.q1("SELECT * FROM orders WHERE id=?", (o["id"],)))
    assert o["status"] == "filled" and o["avg_price"] == pytest.approx(100.6)
    st = db.q1("SELECT entry_price FROM strategy_state WHERE strategy_id=?", (sid,))
    assert st["entry_price"] == pytest.approx(100.6)


def test_integrity_detects_tampering(env):
    from quantdesk import db
    from quantdesk.trading import ledger
    sid = make_strategy()
    ledger.apply_fill(_order(sid, "AAPL", "buy", 1), 1, 100.0, 0.0, {})
    db.ex("UPDATE positions SET qty=5")
    assert not ledger.integrity_check()["ok"]
