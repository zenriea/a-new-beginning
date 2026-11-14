"""Claude API client with budget enforcement and full audit trail.

Every call is stored in ``llm_calls`` (prompt, response, token usage, cost). Calls are
refused *before* they are made if the worst-case cost would exceed the operator's daily or
monthly budget. Structured outputs (JSON schema) make responses machine-checkable.
"""
from __future__ import annotations

import json
import logging
import time

from .. import clock, db, settings
from ..config import FALLBACK_PRICING, MODEL_PRICING, load_config

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


def pricing(model: str):
    return MODEL_PRICING.get(model, FALLBACK_PRICING)


def cost_of(model: str, usage: dict) -> float:
    pi, po, pw, pr = pricing(model)
    return (usage.get("input_tokens", 0) * pi + usage.get("output_tokens", 0) * po
            + usage.get("cache_write_tokens", 0) * pw + usage.get("cache_read_tokens", 0) * pr) / 1e6


def spent_since(epoch: float) -> float:
    r = db.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM llm_calls WHERE ts >= ?", (epoch,))
    return float(r["c"])


def budget_status() -> dict:
    now = clock.now()
    import datetime as dt
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    month0 = dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp()
    day0 = now - now % 86400
    b = settings.get("budget")
    m, dd = spent_since(month0), spent_since(day0)
    return {"month_spent": round(m, 4), "day_spent": round(dd, 4), "monthly_cap": b["monthly_usd"],
            "daily_cap": b["daily_usd"], "month_remaining": round(max(0, b["monthly_usd"] - m), 4),
            "day_remaining": round(max(0, b["daily_usd"] - dd), 4),
            "calls_today": db.q1("SELECT COUNT(*) n FROM llm_calls WHERE ts>=? AND status='ok'", (day0,))["n"]}


class LLM:
    """Thin wrapper around the official Anthropic SDK."""

    def __init__(self, client=None):
        self.cfg = load_config()
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self._client) or not self.cfg.demo_llm

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key, max_retries=2, timeout=300.0)
        return self._client

    def call(self, purpose: str, system: str, user: str, schema: dict, max_tokens: int = 16000) -> tuple[dict, int]:
        model = self.cfg.llm_model
        est_in = (len(system) + len(user)) / 3.0
        pi, po, _, _ = pricing(model)
        worst = (est_in * pi + max_tokens * po) / 1e6
        b = budget_status()
        if worst > b["day_remaining"] or worst > b["month_remaining"]:
            raise BudgetExceeded(f"{purpose}: worst-case cost ${worst:.3f} exceeds remaining budget "
                                 f"(day ${b['day_remaining']:.2f}, month ${b['month_remaining']:.2f})")
        ts = clock.now()
        request = {"model": model, "system": system, "user": user, "schema": schema, "max_tokens": max_tokens,
                   "effort": self.cfg.llm_effort}
        cid = db.ex("INSERT INTO llm_calls(ts, purpose, model, status, request) VALUES(?,?,?,?,?)",
                    (ts, purpose, model, "pending", json.dumps(request)))
        kwargs = dict(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.cfg.llm_effort, "format": {"type": "json_schema", "schema": schema}},
        )
        t0 = time.time()
        try:
            import anthropic
            try:
                if self.cfg.llm_fallbacks:
                    resp = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                            fallbacks="default", **kwargs)
                else:
                    resp = self.client.messages.create(**kwargs)
            except anthropic.BadRequestError as e:
                if self.cfg.llm_fallbacks and "fallback" in str(e).lower():
                    log.warning("fallbacks rejected (%s); retrying without", e)
                    resp = self.client.messages.create(**kwargs)
                else:
                    raise
        except Exception as e:
            db.ex("UPDATE llm_calls SET status='error', error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:1000], cid))
            raise LLMError(f"{purpose}: API call failed: {type(e).__name__}: {e}") from e
        u = getattr(resp, "usage", None)
        usage = {
            "input_tokens": getattr(u, "input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        }
        served = getattr(resp, "model", model) or model
        cost = cost_of(served, usage)
        stop = getattr(resp, "stop_reason", None)
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        status, err, parsed = "ok", None, None
        if stop == "refusal":
            status, err = "refused", f"model declined: {getattr(getattr(resp, 'stop_details', None), 'category', None)}"
        elif stop == "max_tokens":
            status, err = "truncated", "response hit max_tokens before completing"
        else:
            try:
                parsed = json.loads(text)
            except ValueError as e:
                status, err = "invalid_json", str(e)
        db.ex("UPDATE llm_calls SET status=?, error=?, served_model=?, input_tokens=?, output_tokens=?, "
              "cache_write_tokens=?, cache_read_tokens=?, cost_usd=?, response=? WHERE id=?",
              (status, err, served, usage["input_tokens"], usage["output_tokens"], usage["cache_write_tokens"],
               usage["cache_read_tokens"], cost, text[:200000], cid))
        log.info("llm %s %s %.1fs $%.4f", purpose, status, time.time() - t0, cost)
        if parsed is None:
            raise LLMError(f"{purpose}: {err}")
        return parsed, cid
