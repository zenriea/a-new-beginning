"""The desk says what its records say: passes allocate, rankings agree, beliefs dedupe, gates read the sealed test."""
import copy

from test_gate_presets import _result
from test_probes import _experiment


def test_analyst_revise_at_zero_still_allocates_a_passing_test(env, monkeypatch):
    from quantdesk import db
    from quantdesk.agent import research_loop
    from quantdesk.agent.analyst import RulesAnalyst
    from quantdesk.research import gates
    from test_agent import _seed_nasdaq
    _seed_nasdaq()
    monkeypatch.setattr(gates, "evaluate", lambda res, g: {"passed": True, "checks": [], "failed": []})
    real_decide, real_create = RulesAnalyst.decide, research_loop.create_strategy
    created = []

    def revise(self, ctx):
        d, c = real_decide(self, ctx)
        return dict(d, decision="revise", allocation_pct=0.0, revision_idea="tweak it"), c

    def spy(*a, **kw):
        created.append(a)
        return real_create(*a, **kw)
    monkeypatch.setattr(RulesAnalyst, "decide", revise)
    monkeypatch.setattr(research_loop, "create_strategy", spy)
    out = research_loop.research_cycle("NASDAQ", analyst=RulesAnalyst())
    assert out["decision"] == "allocate" and len(created) == 1
    d = db.q1("SELECT kind, details FROM decisions WHERE subject_type='experiment' ORDER BY id DESC")
    assert d["kind"] == "allocate"
    s = db.q1("SELECT * FROM strategies WHERE id=?", (out["strategy_id"],))
    assert s["approved_allocation"] == 5000 and s["status"] in ("active", "incubating")
    assert db.q1("SELECT bar_seconds FROM experiments WHERE id=?", (out["experiment_id"],))["bar_seconds"] == 900


def test_rephrased_belief_supersedes_the_same_claim_for_that_venue(env):
    from quantdesk import db
    from quantdesk.agent.research_loop import _save_beliefs, same_claim
    eid = _experiment()
    ev = [f"experiment:{eid}"]
    _save_beliefs([{"statement": "QQQ trend filters fail the deflated PSR gate on 15-minute bars.", "confidence": "low",
                    "evidence": ev}], "NASDAQ", "llm", None)
    _save_beliefs([{"statement": "On 15-minute bars, QQQ trend filters keep failing the deflated PSR gate.",
                    "confidence": "medium", "evidence": ev}], "NASDAQ", "llm", None)
    _save_beliefs([{"statement": "QQQ trend filters fail the deflated PSR gate on 15-minute bars.", "confidence": "low",
                    "evidence": ev}], "CRYPTO", "llm", None)
    _save_beliefs([{"statement": "Crypto breakouts lose to fees within a day.", "confidence": "low",
                    "evidence": ["experiment:99999"]}], "CRYPTO", "llm", None)  # unverifiable evidence: not stored
    active = db.q("SELECT venue, statement FROM beliefs WHERE status='active' ORDER BY id")
    assert [(r["venue"], r["statement"][:6]) for r in active] == [("NASDAQ", "On 15-"), ("CRYPTO", "QQQ tr")]
    assert not same_claim("BTC breakouts lose to fees", "ETH reversion beats QQQ after costs")


def test_top5_gate_reads_sealed_test_and_skips_only_when_nothing_to_concentrate():
    from quantdesk.config import GATE_PRESETS
    from quantdesk.research import gates
    g = dict(GATE_PRESETS["exploratory"], max_top5_concentration=1.25)
    r = copy.deepcopy(_result())
    r["challenge"]["top5_concentration_pre_test"] = 9.0          # pre-test no longer gates
    r["test"].update(total_return=0.05, top5_concentration=0.8)
    assert "top5_concentration" not in gates.evaluate(r, g)["failed"]
    r["test"]["top5_concentration"] = 1.5
    assert "top5_concentration" in gates.evaluate(r, g)["failed"]
    r["test"].update(top5_concentration=None)                      # profitable but unmeasured: fail closed
    assert "top5_concentration" in gates.evaluate(r, g)["failed"]
    r["test"]["total_return"] = -0.01                              # no profit to concentrate
    chk = next(c for c in gates.evaluate(r, g)["checks"] if c["name"] == "top5_concentration")
    assert chk["pass"] and "does not apply" in chk["why"]


def test_dead_bar_size_winner_is_not_first_when_a_current_bar_pass_exists(env):
    from quantdesk import clock
    from quantdesk.agent.research_loop import ranked_candidates
    from quantdesk.worker import idle_detail
    dead = _experiment(ret=0.21, venue="NASDAQ", bar_seconds=125_000)            # #11-style: daily-bar era
    passed = _experiment(ret=0.059, venue="NASDAQ", status="passed", failed=())  # #47-style: passed, no strategy
    eligible = _experiment(ret=0.03)
    ids = [c["id"] for c in ranked_candidates(clock.now() - 7 * 86400)]
    assert ids == [passed, eligible, dead]
    detail = idle_detail(clock.now())
    assert f"#{passed}" in detail and "not allocated" in detail and f"#{dead}" not in detail


def test_state_reports_last_fill_and_distinct_beliefs(env):
    import importlib
    from fastapi.testclient import TestClient
    from quantdesk import clock, db
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    c = TestClient(importlib.reload(appmod).app)
    s = c.get("/api/state").json()
    assert s["last_fill_ts"] is None   # no fills: the chart may call the account uninvested
    for text in ("BTC trend gates fail on fees.", "Trend gates on BTC fail on fees."):
        db.ex("INSERT INTO beliefs(ts, venue, statement, confidence, evidence, status, author) VALUES(?,?,?,?,?,?,?)",
              (clock.now(), "CRYPTO", text, "low", "[]", "active", "llm"))
    assert [b["statement"] for b in c.get("/api/state").json()["beliefs"]] == ["Trend gates on BTC fail on fees."]


def test_operator_can_replace_a_venue_universe_and_bad_symbols_are_refused(env):
    import importlib
    from fastapi.testclient import TestClient
    from quantdesk import settings
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    c = TestClient(importlib.reload(appmod).app)
    h = {"X-QD": "1"}
    crypto = settings.get("universe")["CRYPTO"]
    assert c.post("/api/settings/universe", json={"NASDAQ": ["QQQ", "AAPL"]}, headers=h).status_code == 200
    assert settings.get("universe") == {"NASDAQ": ["QQQ", "AAPL"], "CRYPTO": crypto}   # list replaced, other venue kept
    assert c.post("/api/settings/universe", json={"CRYPTO": ["not a symbol"]}, headers=h).status_code == 400
    assert settings.get("universe")["CRYPTO"] == crypto
