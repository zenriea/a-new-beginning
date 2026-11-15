"""Research scheduling: each venue has its own clock, and failures never steal another venue's turn."""
import numpy as np
import pytest

from conftest import NASDAQ_OPEN_TS, put_bars

H = 3600.0


@pytest.fixture()
def desk(env, monkeypatch):
    """Both venues have enough history; research_cycle is replaced by a recorder."""
    from quantdesk import clock, worker
    from quantdesk.agent import research_loop
    now = clock.now()
    put_bars("BTC-USD", list(100 + np.arange(400.0)), "1h", start=int(now // 3600) * 3600 - 400 * 3600)
    put_bars("QQQ", list(100 + np.arange(400.0)), "1d", start=int(now // 86400) * 86400 - 400 * 86400)
    calls = []
    outcome = {"status": "done"}

    def fake(venue, analyst=None):
        calls.append(venue)
        if isinstance(outcome.get("status"), Exception):
            raise outcome["status"]
        return dict(outcome)
    monkeypatch.setattr(research_loop, "research_cycle", fake)
    import threading
    thread = worker.AnalystThread(threading.Event(), threading.Event())
    return thread, calls, outcome


def _advance(seconds):
    """Move time forward; hourly crypto bars keep arriving as they do live."""
    from quantdesk import clock, db
    clock.advance(seconds)
    last = db.q1("SELECT MAX(ts) t FROM bars WHERE symbol='BTC-USD'")["t"]
    n = int((clock.now() - 3600 - last) // 3600)
    if n > 0:
        put_bars("BTC-USD", [500.0] * n, "1h", start=last + 3600)


def _new_qqq_bar():
    from quantdesk import db
    last = db.q1("SELECT MAX(ts) t FROM bars WHERE symbol='QQQ'")["t"]
    put_bars("QQQ", [500.0], "1d", start=last + 86400)


def test_crypto_researches_on_its_own_faster_clock(desk):
    from quantdesk import clock
    thread, calls, _ = desk
    thread.tick()
    thread.tick()
    assert sorted(calls) == ["CRYPTO", "NASDAQ"]       # both due at start, one cycle each
    _advance(4 * H + 60)
    thread.tick()
    assert calls[-1] == "CRYPTO" and len(calls) == 3    # crypto again after 4h
    thread.tick()
    assert len(calls) == 3                              # NASDAQ is not due yet


def test_nasdaq_waits_for_a_new_daily_bar(desk):
    from quantdesk import clock
    thread, calls, _ = desk
    thread.tick(); thread.tick()
    _advance(21 * H)
    calls.clear()
    thread.tick(); thread.tick()
    assert calls == ["CRYPTO"]                          # interval passed, but no new NASDAQ data
    _new_qqq_bar()
    thread.tick()
    assert calls == ["CRYPTO", "NASDAQ"]


def test_failed_ai_call_retries_the_same_venue(desk):
    from quantdesk import clock
    thread, calls, outcome = desk
    thread.tick(); thread.tick()                        # both venues done
    _advance(4 * H + 60)
    outcome["status"] = "llm_error"
    thread.tick()
    assert calls[-1] == "CRYPTO"
    thread.tick()
    assert len(calls) == 3                              # waits before retrying
    _advance(11 * 60)
    outcome["status"] = "done"
    thread.tick()
    assert calls[-1] == "CRYPTO" and len(calls) == 4    # crypto keeps its turn


def test_daily_cap_is_per_venue(desk):
    from quantdesk import clock, db, settings
    thread, calls, _ = desk
    settings.operator_update("schedule", {"max_research_per_day": 1})
    day0 = clock.now() - clock.now() % 86400
    db.ex("INSERT INTO hypotheses(created_at, venue, title, rationale, prediction, status, author) "
          "VALUES(?,?,?,?,?,?,?)", (day0 + 1, "NASDAQ", "t", "r", "p", "rejected", "rules"))
    db.ex("INSERT INTO experiments(hypothesis_id, created_at, venue, kind, spec, spec_hash, status) VALUES(1,?,?,?,?,?,?)",
          (day0 + 1, "NASDAQ", "dsl", "{}", "h", "failed"))
    thread.tick(); thread.tick()
    assert calls == ["CRYPTO"]                          # NASDAQ used its one slot; crypto still runs


def test_unexpected_research_error_is_journaled_and_not_retried_in_a_loop(desk):
    from quantdesk import db
    thread, calls, outcome = desk
    outcome["status"] = KeyError("sizing")
    thread.tick()
    j = db.q1("SELECT * FROM journal WHERE kind='system' AND title LIKE '%research%failed%'")
    assert j is not None and "KeyError" in j["body"]
    n = len(calls)
    thread.tick()
    assert calls.count(calls[0]) == 1 and len(calls) == n + 1  # the other venue runs; the failed one waits


def test_next_actions_lists_each_venue(desk):
    from quantdesk import worker
    thread, _, _ = desk
    thread.tick(); thread.tick()
    whats = [a["what"] for a in worker.next_actions()]
    assert any("CRYPTO" in w and "research" in w.lower() for w in whats)
    assert any("NASDAQ" in w and "research" in w.lower() for w in whats)


def test_requested_research_runs_even_when_not_due(desk):
    from quantdesk import db
    thread, calls, _ = desk
    thread.tick(); thread.tick()
    db.set_meta("research_requested", "NASDAQ")
    thread.tick()
    assert calls[-1] == "NASDAQ" and len(calls) == 3


def test_crash_mid_experiment_closes_it_out(env, monkeypatch):
    import threading
    from quantdesk import db, worker
    from quantdesk.research import gates
    from test_agent import _seed_nasdaq
    _seed_nasdaq()

    def boom(*a, **k):
        raise ZeroDivisionError("bad metric")
    monkeypatch.setattr(gates, "evaluate", boom)
    worker.run_research("NASDAQ")
    e = db.q1("SELECT status, error FROM experiments")
    assert e["status"] == "error" and "ZeroDivisionError" in e["error"]
    assert db.q1("SELECT status FROM hypotheses")["status"] == "abandoned"
    assert db.q1("SELECT last_status FROM jobs WHERE name='research:NASDAQ'")["last_status"] == "error"
    assert db.q1("SELECT 1 FROM journal WHERE title='NASDAQ research cycle failed unexpectedly'")


def test_review_failure_is_journaled_and_other_strategies_still_reviewed(env, monkeypatch):
    from conftest import make_strategy
    from quantdesk import db
    from quantdesk.agent import review
    a, b = make_strategy(), make_strategy()
    seen = []
    real = review.review_strategy

    def flaky(strat, analyst=None):
        seen.append(strat["id"])
        if strat["id"] == a:
            raise ValueError("bad expectations")
        return real(strat, analyst)
    monkeypatch.setattr(review, "review_strategy", flaky)
    out = review.review_all()
    assert seen == [a, b] and len(out) == 1
    assert db.q1("SELECT 1 FROM journal WHERE kind='system' AND title LIKE 'Review of%failed%'")
