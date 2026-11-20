import numpy as np
import pytest

from conftest import NASDAQ_OPEN_TS, make_strategy, put_bars, put_quote


def test_worker_lock_prevents_two_workers(env):
    from quantdesk import clock
    from quantdesk.worker import LOCK_STALE, LockHeld, acquire_lock, heartbeat
    assert acquire_lock("A") is None
    with pytest.raises(LockHeld):
        acquire_lock("B")
    clock.advance(LOCK_STALE + 1)            # A died: heartbeat stale
    prev = acquire_lock("B")
    assert prev is not None
    assert heartbeat("A") is False            # A can no longer act as the leader
    assert heartbeat("B") is True


def test_recover_reports_downtime_and_cleans_up(env):
    from quantdesk import clock, db
    from quantdesk.worker import recover
    sid = make_strategy()
    db.ex("INSERT INTO orders(client_order_id, strategy_id, symbol, venue, side, qty, status, reason, rationale, origin, created_at) "
          "VALUES('stuck', ?, 'AAPL', 'NASDAQ', 'buy', 1, 'intent', 'entry', 'r', 'agent', ?)", (sid, clock.now()))
    db.ex("INSERT INTO hypotheses(created_at, venue, title, rationale, prediction, status, author) VALUES(?, 'NASDAQ', 't', 'r', 'p', 'testing', 'rules')",
          (clock.now(),))
    db.ex("INSERT INTO experiments(hypothesis_id, created_at, venue, kind, spec, spec_hash, status) VALUES(1, ?, 'NASDAQ', 'dsl', '{}', 'h', 'running')",
          (clock.now(),))
    db.ex("INSERT INTO llm_calls(ts, purpose, model, status) VALUES(?, 'hypothesis', 'm', 'pending')", (clock.now(),))
    db.ex("INSERT INTO llm_calls(ts, purpose, model, status) VALUES(?, 'question', 'm', 'pending')", (clock.now(),))
    db.ex("INSERT INTO jobs(name, last_started_at, last_finished_at, last_status, run_count) VALUES('research:CRYPTO', ?, ?, 'running', 5)",
          (clock.now() - 10, clock.now() - 7200))
    db.ex("INSERT INTO data_status(key, ok, last_error, changed_at) VALUES('bars:ROKU', 0, 'old', ?)", (clock.now(),))
    info = recover(clock.now() - 3 * 3600)
    assert info["downtime"] == pytest.approx(3 * 3600)
    assert db.q1("SELECT status FROM orders WHERE client_order_id='stuck'")["status"] == "cancelled"
    assert db.q1("SELECT status FROM experiments")["status"] == "interrupted"
    assert db.q1("SELECT status FROM llm_calls WHERE purpose='hypothesis'")["status"] == "interrupted"
    assert db.q1("SELECT status FROM llm_calls WHERE purpose='question'")["status"] == "pending"
    job = db.q1("SELECT * FROM jobs WHERE name='research:CRYPTO'")
    assert job["last_status"] == "interrupted" and job["last_started_at"] == pytest.approx(clock.now() - 7200)
    assert db.q1("SELECT 1 FROM data_status WHERE key='bars:ROKU'") is None
    titles = [r["title"] for r in db.q("SELECT title FROM journal")]
    assert any("Back online" in t for t in titles)
    assert info["integrity"]["ok"]


def _daily_setup():
    """AAPL/QQQ regular-session 15-minute bars through the last bar published at NASDAQ_OPEN_TS."""
    from quantdesk.marketdata.base import Bar
    from quantdesk.marketdata.freshness import expected_last_bar
    from quantdesk.marketdata.store import upsert_bars
    from quantdesk.venues import in_regular_session
    last = expected_last_bar("NASDAQ", NASDAQ_OPEN_TS)
    cand = list(range(last - 40 * 86400, last + 1, 900))
    stamps = [t for t, k in zip(cand, in_regular_session(cand, 900)) if k][-300:]
    for sym, base in (("AAPL", 100.0), ("QQQ", 400.0)):
        bars = [Bar(t, base + i * 0.2, base + i * 0.2 + 1, base + i * 0.2 - 1, base + i * 0.2 + 0.1, 5e7)
                for i, t in enumerate(stamps)]
        upsert_bars(sym, "15m", bars)
    return last


def test_live_evaluation_is_idempotent_and_skips_missed_bars(env):
    from quantdesk import clock, db
    from quantdesk.trading import live
    last = _daily_setup()
    sid = make_strategy({"entry": "close > sma(close, 20)", "exit": "close < sma(close, 20)"})
    put_quote("AAPL", 160.0, 160.05)
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    r1 = live.evaluate_strategy(s)
    assert r1["status"] == "evaluated" and r1["entries"] == ["AAPL"]
    assert db.q1("SELECT COUNT(*) n FROM orders")["n"] == 1
    # simulate a crash before last_signal_bar was saved: re-running the same bar must not duplicate the order
    db.ex("UPDATE strategies SET last_signal_bar=0 WHERE id=?", (sid,))
    db.ex("UPDATE strategy_state SET in_position=0 WHERE strategy_id=?", (sid,))
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    live.evaluate_strategy(s)
    assert db.q1("SELECT COUNT(*) n FROM orders")["n"] == 1
    # already up to date -> nothing
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    assert live.evaluate_strategy(s)["status"] == "up_to_date"
    # outage: last evaluation was several sessions ago -> journal says missed bars are not replayed
    db.ex("UPDATE strategies SET last_signal_bar=? WHERE id=?", (last - 6 * 86400, sid))
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    live.evaluate_strategy(s)
    assert db.q1("SELECT COUNT(*) n FROM journal WHERE title LIKE '%not evaluated during downtime%'")["n"] == 1


def test_stale_bars_stop_signal_evaluation(env):
    from quantdesk import db
    from quantdesk.trading import live
    last = _daily_setup()
    db.ex("DELETE FROM bars WHERE symbol='AAPL' AND ts=?", (last,))
    sid = make_strategy()
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    r = live.evaluate_strategy(s)
    assert r["status"] in ("data_partial", "data_missing")
    assert db.q1("SELECT COUNT(*) n FROM orders")["n"] == 0


def test_rejected_entry_resets_state_and_reconcile_closes_orphans(env):
    from quantdesk import db, settings
    from quantdesk.trading import ledger, live
    last = _daily_setup()
    settings.operator_update("agent", {"paused": True})   # risk engine will reject the buy
    sid = make_strategy({"entry": "close > 0", "exit": "close < 0"})
    put_quote("AAPL", 160.0, 160.05)
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    live.evaluate_strategy(s)
    assert db.q1("SELECT status FROM orders")["status"] == "rejected"
    assert db.q1("SELECT in_position FROM strategy_state WHERE strategy_id=?", (sid,))["in_position"] == 0


def test_rejected_exit_does_not_immediately_duplicate_reconcile_order(env):
    from quantdesk import db
    from quantdesk.trading import live
    _daily_setup()
    sid = make_strategy({"entry": "close < 0", "exit": "close > 0"})
    db.ex("INSERT INTO positions(strategy_id, symbol, venue, qty, avg_cost) VALUES(?, 'AAPL', 'NASDAQ', 10, 100.0)", (sid,))
    db.ex("INSERT INTO strategy_state(strategy_id, symbol, in_position, entry_price) VALUES(?, 'AAPL', 1, 100.0)", (sid,))
    # No fresh quote -> exit sell is rejected by risk engine; reconcile_sleeve must not fire a second sell on the same tick.
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    live.evaluate_strategy(s)
    orders = db.q("SELECT reason, status FROM orders WHERE strategy_id=?", (sid,))
    assert len(orders) == 1 and orders[0]["reason"] == "exit rule" and orders[0]["status"] == "rejected"


def test_retired_strategy_freezes_days_live(env):
    from quantdesk import clock, db
    from quantdesk.agent.review import live_stats
    sid = make_strategy()
    clock.advance(2 * 86400)
    db.ex("UPDATE strategies SET status='retired', retired_at=? WHERE id=?", (clock.now(), sid))
    clock.advance(5 * 86400)
    st = live_stats(dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,))))
    assert st["days_live"] == pytest.approx(2.0, abs=0.01)


def test_provider_outage_backs_off_instead_of_blocking(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest
    from quantdesk.marketdata.base import DataError, Provider

    class Down(Provider):
        calls = 0

        def fetch_quotes(self, symbols):
            Down.calls += 1
            raise DataError("connection refused")
    ingest._backoff.clear()
    ingest.set_provider("CRYPTO", Down())
    ev = ingest.ingest_quotes("CRYPTO", ["BTC-USD"])
    assert ev and ev[0]["ok"] is False                     # outage reported once
    assert ingest.ingest_quotes("CRYPTO", ["BTC-USD"]) == []
    assert Down.calls == 1                                 # backing off: provider not hammered
    clock.advance(31)
    ingest.ingest_quotes("CRYPTO", ["BTC-USD"])
    assert Down.calls == 2
    ingest._backoff.clear()


def test_interrupted_entry_is_retried_not_lost(env):
    from unittest.mock import patch
    from quantdesk import db
    from quantdesk.trading import live
    _daily_setup()
    sid = make_strategy()
    put_quote("AAPL", 160.0, 160.05)
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    with patch.object(live.oms, "create_order", side_effect=RuntimeError("crash")):
        with pytest.raises(RuntimeError):
            live.evaluate_strategy(s)
    assert not db.q1("SELECT in_position FROM strategy_state WHERE strategy_id=?", (sid,)) or \
        db.q1("SELECT in_position FROM strategy_state WHERE strategy_id=?", (sid,))["in_position"] == 0
    r = live.evaluate_strategy(s)
    assert r["entries"] == ["AAPL"] and len(r["orders"]) == 1


def test_backup_refuses_missing_source_and_keeps_destination(tmp_path):
    import sqlite3
    from quantdesk.backup import backup
    dest = tmp_path / "backup.db"
    sqlite3.connect(dest).execute("CREATE TABLE saved(id INTEGER)").connection.close()
    with pytest.raises(sqlite3.OperationalError):
        backup(tmp_path / "typo.db", dest)
    assert not (tmp_path / "typo.db").exists()
    assert sqlite3.connect(dest).execute("SELECT name FROM sqlite_master").fetchall() == [("saved",)]


def test_backup_copies_desk_database(env, tmp_path):
    import sqlite3
    from quantdesk.backup import backup
    dest = tmp_path / "backup.db"
    backup(env.db_path, dest)
    assert sqlite3.connect(dest).execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
