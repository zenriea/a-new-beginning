"""A strategy approved while the total-allocation limit is full waits, then activates when room frees up."""
from conftest import make_strategy


def _approve(pct=0.2):
    from quantdesk.agent.research_loop import create_strategy
    spec = {"name": "late", "venue": "CRYPTO", "universe": ["BTC-USD"], "kind": "dsl", "entry": "close > 0",
            "exit": "close < 0", "sizing": {"method": "fixed", "weight": 0.5}, "params": {}}
    res = {"test": {"sharpe": 1.0}, "validation": {}, "windows": {}, "chosen_params": {}}
    dec = {"allocation_pct": pct, "thesis": "t", "exit_plan": "e", "invalidation": {}}
    prop = {"rationale": "r"}
    return create_strategy(1, 1, prop, spec, res, dec, "llm", None)


def test_strategy_waits_for_room_then_activates(env):
    from quantdesk import db, settings
    from quantdesk.trading import live
    settings.operator_update("risk", {"max_total_allocation_pct": 0.3})
    big = make_strategy(allocation=30000.0)                  # uses all the room
    sid = _approve(0.2)
    s = db.q1("SELECT * FROM strategies WHERE id=?", (sid,))
    assert s["status"] == "incubating" and s["allocation"] == 0 and s["approved_allocation"] == 20000
    assert live.activate_waiting() == []                     # still no room
    db.ex("UPDATE strategies SET status='retired' WHERE id=?", (big,))
    assert live.activate_waiting() == [sid]
    s = db.q1("SELECT * FROM strategies WHERE id=?", (sid,))
    assert s["status"] == "active" and s["allocation"] == 10000  # incubation size (50%)
    assert db.q1("SELECT 1 FROM journal WHERE title LIKE 'Activated%'")


def test_partial_room_starts_smaller(env):
    from quantdesk import db, settings
    settings.operator_update("risk", {"max_total_allocation_pct": 0.3})
    make_strategy(allocation=26000.0)
    sid = _approve(0.2)
    s = db.q1("SELECT * FROM strategies WHERE id=?", (sid,))
    assert s["status"] == "active" and s["allocation"] == 4000 and s["approved_allocation"] == 20000


def test_passing_test_with_zero_percent_gets_the_minimum_allocation(env, monkeypatch):
    from quantdesk import db
    from quantdesk.agent import research_loop
    from quantdesk.agent.analyst import RulesAnalyst
    from quantdesk.research import gates
    from test_agent import _seed_nasdaq
    _seed_nasdaq()
    monkeypatch.setattr(gates, "evaluate", lambda res, g: {"passed": True, "checks": [], "failed": []})
    real = RulesAnalyst.decide

    def zero(self, ctx):
        d, c = real(self, ctx)
        return dict(d, decision="allocate", allocation_pct=0.0), c
    monkeypatch.setattr(RulesAnalyst, "decide", zero)
    out = research_loop.research_cycle("NASDAQ", analyst=RulesAnalyst())
    assert out["decision"] == "allocate"   # the gates admit; the analyst only sizes
    s = db.q1("SELECT * FROM strategies")
    assert s["approved_allocation"] == 5000 and s["probe"] == 0


def test_review_increase_respects_total_allocation_limit(env):
    from quantdesk import clock, db, settings
    from quantdesk.agent.analyst import RulesAnalyst
    from quantdesk.agent.review import review_strategy
    settings.operator_update("risk", {"max_total_allocation_pct": 0.3})
    make_strategy(allocation=25000.0)
    sid = make_strategy(allocation=4000.0)
    db.ex("UPDATE strategies SET approved_allocation=20000, activated_at=?, created_at=? WHERE id=?",
          (clock.now() - 30 * 86400, clock.now() - 30 * 86400, sid))

    class Wants(RulesAnalyst):
        def review(self, ctx):
            r, c = super().review(ctx)
            return dict(r, action="increase", new_allocation_pct=0.2), c
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    out = review_strategy(s, Wants())
    assert out["action"] == "increase"
    assert db.q1("SELECT allocation FROM strategies WHERE id=?", (sid,))["allocation"] <= 5000 + 1e-6  # 30k - 25k
