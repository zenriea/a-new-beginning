"""Gates admit, the analyst sizes; probes give live evidence for ideas that were profitable on test."""
import json

import pytest

from conftest import make_strategy


def _experiment(ret=0.05, status="failed", venue="CRYPTO", trades=40, created_at=None, failed=("deflated_psr",),
                bar_seconds=None):
    from quantdesk import clock, db
    now = clock.now() if created_at is None else created_at
    hid = db.ex("INSERT INTO hypotheses(created_at, venue, title, rationale, prediction, falsification, status, author, "
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (now, venue, "Idea", "because", "p", "f", "rejected", "llm", now))
    spec = {"name": "idea", "venue": venue, "universe": ["BTC-USD", "ETH-USD"], "kind": "dsl", "entry": "close > 0",
            "exit": "close < 0", "sizing": {"method": "fixed", "weight": 0.5}, "params": {}}
    test = {"total_return": ret, "sharpe": 1.1, "n_trades": trades, "max_drawdown": -0.04}
    gates = {"passed": status == "passed", "checks": [{"name": f, "pass": False} for f in failed], "failed": list(failed)}
    bar_seconds = bar_seconds or (900 if venue == "CRYPTO" else 4800)   # calendar time per 15m bar
    w = {"test": {"start": int(now) - bar_seconds * 2000, "end": int(now), "bars": 2000}}
    return db.ex("INSERT INTO experiments(hypothesis_id, created_at, venue, kind, spec, spec_hash, status, frozen_at, windows, "
                 "chosen_params, train, validation, test, challenge, gates, leakage, completed_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (hid, now, venue, "dsl", json.dumps(spec), "h", status, now, json.dumps(w), "{}", "{}", "{}",
                  json.dumps(test), "{}", json.dumps(gates), "{}", now))


def test_gates_passing_cannot_be_vetoed_by_the_analyst(env, monkeypatch):
    from quantdesk import db
    from quantdesk.agent import research_loop
    from quantdesk.agent.analyst import RulesAnalyst
    from quantdesk.research import gates
    from test_agent import _seed_nasdaq
    _seed_nasdaq()
    monkeypatch.setattr(gates, "evaluate", lambda res, g: {"passed": True, "checks": [], "failed": []})
    real = RulesAnalyst.decide

    def nope(self, ctx):
        d, c = real(self, ctx)
        return dict(d, decision="reject", allocation_pct=0.0, thesis="", exit_plan=""), c
    monkeypatch.setattr(RulesAnalyst, "decide", nope)
    out = research_loop.research_cycle("NASDAQ", analyst=RulesAnalyst())
    assert out["decision"] == "allocate"
    from quantdesk import settings
    days = settings.get("research")["deflation_window_days"]
    settings.operator_update("research", {"deflation_window_days": days + 1})
    assert research_loop.ranked_candidates(0)[0]["deflation_window_days"] == days
    s = db.q1("SELECT * FROM strategies WHERE id=?", (out["strategy_id"],))
    assert s["approved_allocation"] == 5000 and s["thesis"] and s["exit_plan"]
    assert "cannot block a passing test" in db.q1("SELECT rationale FROM decisions ORDER BY id DESC")["rationale"]
    entry = db.q1("SELECT refs FROM journal WHERE title LIKE 'Allocating capital:%'")
    test = db.loads(db.q1("SELECT test FROM experiments WHERE id=?", (out["experiment_id"],))["test"])
    assert db.loads(entry["refs"])["decision_evidence"] == {
        "outcome": "allocate", "test_return": test["total_return"], "test_trades": test["n_trades"],
        "gates_passed": True, "failed_gates": [],
    }


def test_profitable_but_failed_experiment_starts_a_probe(env):
    from quantdesk import db
    from quantdesk.agent.research_loop import auto_probe
    eid = _experiment(ret=0.04)
    sid = auto_probe("CRYPTO", eid)
    s = db.q1("SELECT * FROM strategies WHERE id=?", (sid,))
    assert s["probe"] == 1 and s["status"] == "active"
    assert s["allocation"] == pytest.approx(3000)   # 3% of $100k, no incubation haircut
    assert s["thesis"].startswith("PROBE")
    assert db.q1("SELECT kind FROM decisions WHERE subject_id=? ORDER BY id DESC", (eid,))["kind"] == "probe"
    entry = db.q1("SELECT kind, refs FROM journal WHERE title LIKE 'Probe started:%'")
    assert entry["kind"] == "decision"
    assert db.loads(entry["refs"]) == {"strategy_id": sid, "experiment_id": eid, "probe": True}


def test_losing_experiment_does_not_probe(env):
    from quantdesk.agent.research_loop import auto_probe, probe_blocker
    from quantdesk import db
    eid = _experiment(ret=-0.02)
    assert auto_probe("CRYPTO", eid) is None
    assert "not above" in probe_blocker(dict(db.q1("SELECT * FROM experiments WHERE id=?", (eid,))))


def test_probe_limits_per_day_and_active(env):
    from quantdesk import settings
    from quantdesk.agent.research_loop import auto_probe
    assert auto_probe("CRYPTO", _experiment()) is not None
    assert auto_probe("CRYPTO", _experiment()) is None            # one per venue per day
    assert auto_probe("NASDAQ", _experiment(venue="NASDAQ")) is not None
    settings.operator_update("probe", {"per_venue_per_day": 5, "max_active": 2})
    assert auto_probe("CRYPTO", _experiment()) is None            # two already active
    settings.operator_update("probe", {"enabled": False, "max_active": 9})
    assert auto_probe("CRYPTO", _experiment()) is None


def test_operator_probe_overrides_return_rule_but_not_duplicates(env):
    from quantdesk import db
    from quantdesk.agent.research_loop import start_probe
    eid = _experiment(ret=-0.05)
    sid = start_probe(eid, origin="operator")
    assert db.q1("SELECT probe FROM strategies WHERE id=?", (sid,))["probe"] == 1
    assert start_probe(eid, origin="operator") is None
    assert db.q1("SELECT 1 FROM journal WHERE title LIKE '%probe not started'")


def test_operator_can_rerun_a_retired_probe_but_the_desk_cannot(env):
    from quantdesk import db
    from quantdesk.agent.research_loop import probe_blocker, start_probe
    eid = _experiment()
    sid = start_probe(eid, origin="operator")
    db.ex("UPDATE strategies SET status='retired' WHERE id=?", (sid,))
    e = dict(db.q1("SELECT * FROM experiments WHERE id=?", (eid,)))
    assert probe_blocker(e) == "already ran as a probe (retired)"
    again = start_probe(eid, origin="operator")
    assert again and db.q1("SELECT version FROM strategies WHERE id=?", (again,))["version"] == 2


def test_probes_expire(env):
    from quantdesk import clock, db
    from quantdesk.agent.research_loop import expire_probes, start_probe
    sid = start_probe(_experiment(), origin="operator")
    keep = make_strategy()
    assert expire_probes() == []
    db.ex("UPDATE strategies SET created_at=? WHERE id IN (?, ?)", (clock.now() - 15 * 86400, sid, keep))
    assert expire_probes() == [sid]
    assert db.q1("SELECT status FROM strategies WHERE id=?", (sid,))["status"] == "retired"
    assert db.q1("SELECT status FROM strategies WHERE id=?", (keep,))["status"] == "active"


def test_deflation_counts_only_recent_trials(env):
    from quantdesk import clock, settings
    from quantdesk.agent.research_loop import _prior_test_windows
    _experiment(created_at=clock.now() - 30 * 86400)
    _experiment(created_at=clock.now() - 2 * 86400)
    _experiment(created_at=clock.now() - 3600, venue="NASDAQ")
    assert len(_prior_test_windows("CRYPTO", settings.get("research")["deflation_window_days"])) == 1
    settings.operator_update("research", {"deflation_window_days": 60})
    assert len(_prior_test_windows("CRYPTO", settings.get("research")["deflation_window_days"])) == 2


def test_old_database_gets_probe_column(tmp_path):
    import sqlite3
    from quantdesk import db
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript(db.SCHEMA.replace(", probe INTEGER NOT NULL DEFAULT 0", ""))
    c.commit()
    c.close()
    db.close_thread_connection()
    db.init(path)
    assert "probe" in {r[1] for r in db.connect().execute("PRAGMA table_info(strategies)")}
    db.close_thread_connection()


def test_skipped_research_snapshots_cited_trade_checks(env):
    from quantdesk import db
    from quantdesk.agent import journal
    from quantdesk.agent.research_loop import _record_no_idea
    ids = []
    for count, venue in ((19, "CRYPTO"), (9, "CRYPTO"), (3, "CRYPTO"), (1, "NASDAQ")):
        eid = _experiment(trades=count, venue=venue)
        ids.append(eid)
        db.ex("UPDATE experiments SET gates=? WHERE id=?", (db.dumps({"checks": [
            {"name": "test_trades", "op": ">=", "value": count, "threshold": 8}
        ]}), eid))
    why = "All below minimum: " + ", ".join(f"experiment:{i}" for i in [*ids, ids[0], 99999])
    _record_no_idea("CRYPTO", {"why": why}, "llm", None, 1)
    entry = journal.recent(1)[0]
    assert entry["body"] == why
    assert entry["refs"]["trade_checks"] == [
        {"experiment_id": eid, "trades": count, "minimum": 8} for eid, count in zip(ids, [19, 9, 3])]
    # Plural, hash and comma-separated citations used by LLMs are captured too.
    plural = f"Failed in experiments {ids[0]}, {ids[1]} and experiments:{ids[2]}, :{ids[3]}."
    _record_no_idea("CRYPTO", {"why": plural}, "llm", None, 1)
    assert journal.recent(1)[0]["refs"]["trade_checks"] == [
        {"experiment_id": eid, "trades": count, "minimum": 8} for eid, count in zip(ids, [19, 9, 3])]


def test_api_pipeline_and_probe_control(env, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    from quantdesk import db
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    c = TestClient(importlib.reload(appmod).app)
    good, bad = _experiment(ret=0.03), _experiment(ret=-0.01, failed=("test_sharpe", "deflated_psr"))
    db.ex("UPDATE experiments SET leakage=? WHERE id=?", (json.dumps({"holdout_reuse_count": 29}), good))
    p = c.get("/api/state").json()["pipeline"]
    assert p["experiments"] == 2 and p["profitable"] == 1
    assert {r["id"]: r["holdout_reuse_count"] for r in p["review_candidates"]} == {good: 29, bad: None}
    assert p["gate_failures"][0] == {"gate": "deflated_psr", "n": 2}
    assert p["candidates"][0]["id"] == good and p["candidates"][0]["probe_blocker"] is None
    h = {"X-QD": "1"}
    assert c.post(f"/api/control/probe_{bad}", headers=h).status_code == 200
    assert db.get_meta("probe_requested") == str(bad)
    make_strategy()
    db.ex("UPDATE strategies SET experiment_id=? WHERE id=1", (good,))
    # A running probe stays visible even after its experiment leaves the research window;
    # it never consumes one of the three available review slots.
    db.ex("UPDATE strategies SET probe=1 WHERE id=1")
    others = [_experiment(ret=ret) for ret in (0.02, 0.01)]
    state = c.get("/api/state").json()
    assert [s["id"] for s in state["strategies"] if s["probe"]] == [1]
    assert {r["id"] for r in state["pipeline"]["review_candidates"]} == {bad, *others}
    db.ex("UPDATE experiments SET created_at=0 WHERE id=?", (good,))
    assert c.get("/api/state").json()["pipeline"]["live"]["probes"] == 1
    r = c.post(f"/api/control/probe_{good}", headers=h)
    assert r.status_code == 409 and "as a probe" in r.json()["detail"]


def test_strategy_api_lists_symbols_with_holdings(env):
    import importlib
    from fastapi.testclient import TestClient
    from quantdesk import db
    from quantdesk.config import load_config
    from conftest import put_quote
    load_config(reload=True)
    import quantdesk.api.app as appmod
    c = TestClient(importlib.reload(appmod).app)
    sid = make_strategy({"universe": ["AAPL", "MSFT"]})
    db.ex("INSERT INTO positions(strategy_id, symbol, venue, qty, avg_cost) VALUES(?,?,?,?,?)", (sid, "AAPL", "NASDAQ", 10, 100.0))
    put_quote("AAPL", 109.9, 110.1)
    syms = {x["symbol"]: x for x in c.get(f"/api/strategies/{sid}").json()["symbols"]}
    assert list(syms) == ["AAPL", "MSFT"]
    assert syms["AAPL"]["qty"] == 10 and syms["AAPL"]["value"] == 1100 and syms["AAPL"]["unrealized"] == 100
    assert syms["MSFT"]["qty"] == 0 and syms["MSFT"]["pending"] is None
    assert c.get("/api/strategies").json()[0]["symbols"][0]["symbol"] == "AAPL"


def test_experiment_tested_on_another_bar_size_cannot_probe(env):
    """An idea tested on daily bars means something else on 15-minute bars (sma(close, 100) = 100 days
    vs ~4 days), so neither the desk nor the operator may paper-trade it."""
    from quantdesk import db
    from quantdesk.agent.research_loop import auto_probe, probe_blocker, start_probe
    eid = _experiment(ret=0.2, venue="NASDAQ", bar_seconds=125_000)          # daily-bar era experiment
    e = dict(db.q1("SELECT * FROM experiments WHERE id=?", (eid,)))
    assert "bar size" in probe_blocker(e) and "bar size" in probe_blocker(e, operator=True)
    assert auto_probe("NASDAQ", eid) is None and start_probe(eid) is None
    ok = _experiment(ret=0.2, venue="NASDAQ", bar_seconds=4800)               # 15-minute NASDAQ bars
    assert probe_blocker(dict(db.q1("SELECT * FROM experiments WHERE id=?", (ok,)))) is None


def test_worker_retires_live_strategy_from_another_bar_size(env):
    from quantdesk import db
    from quantdesk.agent.research_loop import start_probe
    from quantdesk.worker import retire_stale_bar_size_strategies
    eid = _experiment(ret=0.2, venue="NASDAQ", bar_seconds=4800)
    sid = start_probe(eid)
    db.ex("UPDATE experiments SET windows=? WHERE id=?",
          (json.dumps({"test": {"start": 0, "end": 125_000 * 300, "bars": 300}}), eid))
    assert retire_stale_bar_size_strategies() == [sid]
    assert db.q1("SELECT status FROM strategies WHERE id=?", (sid,))["status"] == "retired"
    assert retire_stale_bar_size_strategies() == []


def test_rerun_retired_probe_does_not_duplicate_experiments_and_allows_rejudge(env):
    import importlib
    from fastapi.testclient import TestClient
    from quantdesk import clock, db
    from quantdesk.agent.research_loop import ranked_candidates, rejudge_experiment, start_probe
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    c = TestClient(importlib.reload(appmod).app)
    eid = _experiment(ret=0.05, status="passed", failed=())
    db.ex("UPDATE experiments SET validation=?, test=?, challenge=? WHERE id=?", (
        json.dumps({"sharpe": 1.0}),
        json.dumps({"total_return": 0.05, "sharpe": 1.1, "n_trades": 40, "max_drawdown": -0.04,
                    "top5_concentration": 0.4, "deflated_psr": 0.8}),
        json.dumps({"test_cost_x2": {"total_return": 0.02}, "test_delay_1bar": {"sharpe": 0.5},
                    "neighbor_positive_frac": 0.8, "subperiod_positive_frac": 0.75}),
        eid,
    ))
    first = start_probe(eid, origin="operator")
    db.ex("UPDATE strategies SET status='retired', retired_at=? WHERE id=?", (clock.now(), first))
    # Detail endpoint exposes that a retired current-bar probe can be re-run, and re-judge is allowed.
    detail = c.get(f"/api/experiments/{eid}").json()
    assert detail["operator_probe_blocker"] is None
    rejudged = rejudge_experiment(eid)
    assert rejudged["status"] == "done" and rejudged["decision"] == "allocate"
    assert start_probe(eid, origin="operator") is None  # already has an active regular strategy from re-judge

    eid2 = _experiment(ret=0.05, status="passed", failed=())
    p1 = start_probe(eid2, origin="operator")
    db.ex("UPDATE strategies SET status='retired', retired_at=? WHERE id=?", (clock.now(), p1))
    p2 = start_probe(eid2, origin="operator")
    assert p2 and p2 != p1
    rows = [r for r in c.get("/api/experiments").json() if r["id"] == eid2]
    assert len(rows) == 1 and rows[0]["strategy_id"] == p2 and rows[0]["strategy_status"] == "active"
    # Running only as a probe does not mark a gate-passing experiment as allocated.
    cand = next(x for x in ranked_candidates(0) if x["id"] == eid2)
    assert cand["passed"] is True and cand["allocated"] is False


def test_manual_probe_does_not_consume_automatic_daily_quota(env):
    from quantdesk.agent.research_loop import auto_probe, start_probe
    assert start_probe(_experiment(venue="NASDAQ"), origin="operator") is not None
    assert auto_probe("NASDAQ", _experiment(venue="NASDAQ")) is not None
    assert auto_probe("NASDAQ", _experiment(venue="NASDAQ")) is None  # automatic daily cap still enforced
