from types import SimpleNamespace

import pytest

from conftest import make_strategy, put_quote
from quantdesk import db
from quantdesk.agent.review import live_stats, review_strategy
from quantdesk.trading import mtm


def sleeve_at_drawdown():
    sid = make_strategy(allocation=10000)
    db.ex("INSERT INTO positions(strategy_id,symbol,venue,qty,avg_cost) VALUES(?,'AAPL','NASDAQ',100,100)", (sid,))
    put_quote("AAPL", 120, 120)
    mtm.snapshot()
    db.close_thread_connection()  # the peak must survive a worker restart
    put_quote("AAPL", 105, 105)
    return sid


def strategy(sid):
    return dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))


def analyst(action):
    return SimpleNamespace(author="rules", review=lambda ctx: ({"action": action, "journal": "test"}, None))


def test_profitable_sleeve_retires_after_peak_loss(env):
    sid = sleeve_at_drawdown()
    db.ex("UPDATE strategies SET invalidation=? WHERE id=?", (db.dumps({"max_live_drawdown_pct": 0.10}), sid))
    assert live_stats(strategy(sid))["return"] == pytest.approx(0.05)
    result = review_strategy(strategy(sid), analyst("continue"))
    assert result["action"] == "retire"
    assert "-12.5%" in result["flags"][0]


def test_allocation_cut_preserves_drawdown(env):
    sid = sleeve_at_drawdown()
    review_strategy(strategy(sid), analyst("reduce"))
    assert strategy(sid)["allocation"] == 5000
    mtm.snapshot()
    assert live_stats(strategy(sid))["drawdown"] == pytest.approx(-0.125)
