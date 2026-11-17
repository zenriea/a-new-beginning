import json

import pytest

from test_agent import _seed_nasdaq


class Resp:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


def ok(content, finish="stop", model="test-model", usage=(1000, 500)):
    return Resp(200, {"model": model, "choices": [{"message": {"content": content}, "finish_reason": finish}],
                      "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})


class FakeServer:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "body": json})
        return self.responses.pop(0)


@pytest.fixture()
def compat(env, monkeypatch):
    monkeypatch.setenv("QD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("QD_LLM_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("QD_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("QD_LLM_MODEL", "test-model")
    monkeypatch.setenv("QD_LLM_PRICE_IN", "2")
    monkeypatch.setenv("QD_LLM_PRICE_OUT", "8")
    from quantdesk.agent import llm
    from quantdesk.config import load_config
    cfg = load_config(reload=True)
    llm._compat_quirks.clear()
    yield cfg
    llm._compat_quirks.clear()


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer", "n"],
          "properties": {"answer": {"type": "string"}, "n": {"type": "integer"}}}


def test_config_and_label(compat):
    assert compat.llm_provider == "openai_compatible" and not compat.demo_llm
    assert compat.llm_label == "test-model (llm.example.com)"


def test_strict_schema_call_and_cost(compat):
    from quantdesk import db
    from quantdesk.agent.llm import LLM
    srv = FakeServer([ok('{"answer": "hi", "n": 3.0}')])
    out, cid = LLM(http_post=srv).call("question", "sys", "user", SCHEMA)
    assert out == {"answer": "hi", "n": 3} and isinstance(out["n"], int)
    req = srv.requests[0]
    assert req["url"] == "https://llm.example.com/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer sk-test"
    assert req["body"]["response_format"]["type"] == "json_schema" and req["body"]["max_tokens"] == 16000
    row = db.q1("SELECT * FROM llm_calls WHERE id=?", (cid,))
    assert row["status"] == "ok" and row["cost_usd"] == pytest.approx((1000 * 2 + 500 * 8) / 1e6)


def test_adapts_to_provider_limitations_and_remembers(compat):
    from quantdesk.agent.llm import LLM
    srv = FakeServer([
        Resp(400, text='{"error": "Unsupported parameter: max_tokens; use max_completion_tokens"}'),
        Resp(400, text='{"error": "response_format json_schema is not supported by this model"}'),
        ok('{"answer": "a", "n": 1}'),
        ok('{"answer": "b", "n": 2}'),
    ])
    llm = LLM(http_post=srv)
    assert llm.call("q", "s", "u", SCHEMA)[0]["answer"] == "a"
    third = srv.requests[2]["body"]
    assert "max_completion_tokens" in third and "max_tokens" not in third
    assert third["response_format"] == {"type": "json_object"}
    llm.call("q", "s", "u", SCHEMA)                  # learned: no more failed attempts
    assert len(srv.requests) == 4


def test_fenced_and_invalid_json_gets_one_repair(compat):
    from quantdesk.agent.llm import LLM, LLMError
    srv = FakeServer([ok('Sure!\n```json\n{"answer": "x"}\n```'), ok('{"answer": "x", "n": 7}')])
    out, _ = LLM(http_post=srv).call("q", "s", "u", SCHEMA)
    assert out["n"] == 7
    repair_msgs = srv.requests[1]["body"]["messages"]
    assert repair_msgs[-1]["role"] == "user" and "$.n: missing" in repair_msgs[-1]["content"]
    srv = FakeServer([ok('{"answer": 1}'), ok("not json at all")])
    with pytest.raises(LLMError):
        LLM(http_post=srv).call("q", "s", "u", SCHEMA)


def test_truncation_and_budget(compat):
    from quantdesk import settings
    from quantdesk.agent.llm import LLM, BudgetExceeded, LLMError
    with pytest.raises(LLMError, match="max_tokens"):
        LLM(http_post=FakeServer([ok('{"answer"', finish="length")])).call("q", "s", "u", SCHEMA)
    settings.operator_update("budget", {"daily_usd": 0.001})
    srv = FakeServer([ok('{"answer": "x", "n": 1}')])
    with pytest.raises(BudgetExceeded):
        LLM(http_post=srv).call("q", "s", "u", SCHEMA)
    assert srv.requests == []


def test_research_cycle_via_openai_compatible_provider(compat):
    from quantdesk import db
    from quantdesk.agent.analyst import LLMAnalyst
    from quantdesk.agent.llm import LLM
    from quantdesk.agent.research_loop import research_cycle
    _seed_nasdaq()
    hyp = {"decision": "investigate", "why": "w", "title": "Trend", "rationale": "r", "prediction": "p",
           "falsification": "f", "parent_experiment_id": None,
           "spec": {"name": "trend", "universe": ["AAPL", "MSFT"], "kind": "dsl", "entry": "close > sma(close, n)",
                    "exit": "close < sma(close, n)", "code": None, "rank_by": None, "top_k": None,
                    "sizing": {"method": "fixed", "weight": 0.2, "max_weight": 0.2, "target_vol": 0.15, "vol_lookback": 20.0},
                    "stop_loss_pct": None, "take_profit_pct": None, "max_hold_bars": None,
                    "params": [{"name": "n", "values": [20, 50]}]}}
    dec = {"decision": "reject", "journal": "Not convincing.", "lessons": ["l"], "allocation_pct": 0, "thesis": "",
           "exit_plan": "", "invalidation": {"max_live_drawdown_pct": 0.1, "review_after_days": 7,
                                             "min_trades_before_judging": 3, "conditions": "c"},
           "revision_idea": None, "beliefs": []}
    srv = FakeServer([ok(json.dumps(hyp)), ok(json.dumps(dec))])
    res = research_cycle("NASDAQ", analyst=LLMAnalyst(LLM(http_post=srv)))
    assert res["status"] == "done" and res["decision"] == "reject"
    assert db.q1("SELECT author FROM hypotheses")["author"] == "llm"
    assert db.q1("SELECT COUNT(*) n FROM llm_calls WHERE status='ok'")["n"] == 2


def test_transient_errors_back_off_and_retry(compat, monkeypatch):
    from quantdesk.agent.llm import LLM, LLMError
    sleeps = []
    monkeypatch.setattr(LLM, "_sleep", staticmethod(lambda s: sleeps.append(s)))
    srv = FakeServer([Resp(503, text="high demand"), Resp(429, text="quota"), ok('{"answer": "x", "n": 1}')])
    assert LLM(http_post=srv).call("q", "s", "u", SCHEMA)[0]["n"] == 1
    assert sleeps == [4, 8]
    srv = FakeServer([Resp(503, text="high demand")] * 3)
    with pytest.raises(LLMError, match="503"):
        LLM(http_post=srv).call("q", "s", "u", SCHEMA)


def test_router_free_first_fallback_and_second_opinion(compat, monkeypatch):
    from quantdesk import db
    from quantdesk.agent import analyst as A
    from quantdesk.agent.llm import LLM, Router, free_helpers
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    assert [h.route["name"] for h in free_helpers()] == ["groq"]
    route = lambda n: {"name": n, "base_url": f"https://{n}.example/v1", "api_key": "k", "model": f"{n}-free"}
    free = FakeServer([Resp(401, text="bad key"), ok('{"answer": "free", "n": 2}', model="b-free")])
    paid = FakeServer([ok('{"answer": "paid", "n": 1}')])
    r = Router(LLM(http_post=paid), [LLM(http_post=free, route=route("a")), LLM(http_post=free, route=route("b"))])
    out, cid = r.call("question", "s", "u", SCHEMA)              # free-first: a fails, b answers
    assert out["answer"] == "free" and paid.requests == []
    assert db.q1("SELECT cost_usd FROM llm_calls WHERE id=?", (cid,))["cost_usd"] == 0
    assert r.call("decision", "s", "u", SCHEMA)[0]["answer"] == "paid"   # paid model first for decisions

    # an "allocate" survives only if a second model agrees
    monkeypatch.setattr(A.prompts, "decision_prompt", lambda ctx: "u")
    monkeypatch.setattr(A.prompts, "DECISION_SCHEMA", {"type": "object"})
    dec = lambda d: ok(json.dumps({"decision": d, "journal": "j"}))
    r = Router(LLM(http_post=FakeServer([dec("allocate")])), [LLM(http_post=FakeServer([dec("reject")]), route=route("c"))])
    got, _ = A.LLMAnalyst(r).decide({})
    assert got["decision"] == "revise" and "Second opinion: c-free said reject" in got["journal"]
    r = Router(LLM(http_post=FakeServer([dec("allocate")])), [LLM(http_post=FakeServer([dec("allocate")]), route=route("d"))])
    assert A.LLMAnalyst(r).decide({})[0]["decision"] == "allocate"
