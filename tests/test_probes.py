"""Gates admit, the analyst sizes; probes give live evidence for ideas that were profitable on test."""
import json

import pytest

from conftest import make_strategy


def _experiment(ret=0.05, status="failed", venue="CRYPTO", trades=40, created_at=None, failed=("deflated_psr",)):
    from quantdesk import clock, db
    now = clock.now() if created_at is None else created_at
    hid = db.ex("INSERT INTO hypotheses(created_at, venue, title, rationale, prediction, falsification, status, author, "
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (now, venue, "Idea", "because", "p", "f", "rejected", "llm", now))
    spec = {"name": "idea", "venue": venue, "universe": ["BTC-USD", "ETH-USD"], "kind": "dsl", "entry": "close > 0",
            "exit": "close < 0", "sizing": {"method": "fixed", "weight": 0.5}, "params": {}}
    test = {"total_return": ret, "sharpe": 1.1, "n_trades": trades, "max_drawdown": -0.04}
    gates = {"passed": status == "passed", "checks": [{"name": f, "pass": False} for f in failed], "failed": list(failed)}
    w = {"test": {"start": int(now) - 86400 * 30, "end": int(now), "bars": 100}}
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
    s = db.q1("SELECT * FROM strategies WHERE id=?", (out["strategy_id"],))
    assert s["approved_allocation"] == 5000 and s["thesis"] and s["exit_plan"]
    assert "cannot block a passing test" in db.q1("SELECT rationale FROM decisions ORDER BY id DESC")["rationale"]


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
    assert db.q1("SELECT 1 FROM journal WHERE title LIKE 'New probe live%'")


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
    from quantdesk import clock
    from quantdesk.agent.research_loop import _prior_test_windows
    _experiment(created_at=clock.now() - 30 * 86400)
    _experiment(created_at=clock.now() - 2 * 86400)
    _experiment(created_at=clock.now() - 3600, venue="NASDAQ")
    assert len(_prior_test_windows("CRYPTO")) == 1
    from quantdesk import settings
    settings.operator_update("research", {"deflation_window_days": 60})
    assert len(_prior_test_windows("CRYPTO")) == 2


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


def test_api_pipeline_and_probe_control(env, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    from quantdesk import db
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    c = TestClient(importlib.reload(appmod).app)
    good, bad = _experiment(ret=0.03), _experiment(ret=-0.01, failed=("test_sharpe", "deflated_psr"))
    p = c.get("/api/state").json()["pipeline"]
    assert p["experiments"] == 2 and p["profitable"] == 1
    assert p["gate_failures"][0] == {"gate": "deflated_psr", "n": 2}
    assert p["candidates"][0]["id"] == good and p["candidates"][0]["probe_blocker"] is None
    h = {"X-QD": "1"}
    assert c.post(f"/api/control/probe_{bad}", headers=h).status_code == 200
    assert db.get_meta("probe_requested") == str(bad)
    make_strategy()
    db.ex("UPDATE strategies SET experiment_id=? WHERE id=1", (good,))
    r = c.post(f"/api/control/probe_{good}", headers=h)
    assert r.status_code == 409 and "already has a strategy" in r.json()["detail"]
