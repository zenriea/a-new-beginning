"""AI analyst client with budget enforcement and a full audit trail.

Providers (QD_LLM_PROVIDER, inferred if unset):
  * ``openai_compatible`` - any OpenAI-style /chat/completions API: OpenAI, Google Gemini
    (OpenAI-compatible endpoint), Groq, OpenRouter, Mistral, DeepSeek, Together, or a local
    Ollama server. Plain HTTPS; set QD_LLM_BASE_URL, QD_LLM_API_KEY, QD_LLM_MODEL.
  * ``anthropic`` - Claude via the official Anthropic SDK.

Every HTTP attempt is stored in ``llm_calls`` (prompt, response, tokens, cost). A call is
refused *before* it is made if its worst-case cost would exceed the daily or monthly budget.
Responses must be JSON matching the task's schema; the schema is enforced by the provider
when it supports it, and always validated here (with one repair round-trip on mismatch).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time

from .. import clock, db, settings
from ..config import FALLBACK_PRICING, MODEL_PRICING, load_config

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


# ------------------------------------------------------------------ pricing & budget
def pricing(model: str):
    cfg = load_config()
    if cfg.llm_provider == "openai_compatible":
        return (cfg.llm_price_in, cfg.llm_price_out, cfg.llm_price_in, cfg.llm_price_in)
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
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    month0 = dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp()
    day0 = now - now % 86400
    b = settings.get("budget")
    m, dd = spent_since(month0), spent_since(day0)
    return {"month_spent": round(m, 4), "day_spent": round(dd, 4), "monthly_cap": b["monthly_usd"],
            "daily_cap": b["daily_usd"], "month_remaining": round(max(0, b["monthly_usd"] - m), 4),
            "day_remaining": round(max(0, b["daily_usd"] - dd), 4),
            "calls_today": db.q1("SELECT COUNT(*) n FROM llm_calls WHERE ts>=? AND status='ok'", (day0,))["n"]}


# ------------------------------------------------------------------ schema validation
def validate(obj, schema: dict, path: str = "$") -> list[str]:
    """Minimal JSON-schema check for the subset our schemas use. Returns error strings."""
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            if not validate(obj, sub, path):
                return []
        return [f"{path}: does not match any allowed type"]
    t = schema.get("type")
    errs: list[str] = []
    if t == "object":
        if not isinstance(obj, dict):
            return [f"{path}: expected object"]
        for k in schema.get("required", []):
            if k not in obj:
                errs.append(f"{path}.{k}: missing")
        for k, sub in (schema.get("properties") or {}).items():
            if k in obj:
                errs += validate(obj[k], sub, f"{path}.{k}")
    elif t == "array":
        if not isinstance(obj, list):
            return [f"{path}: expected array"]
        for i, item in enumerate(obj):
            errs += validate(item, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        if not isinstance(obj, str):
            errs.append(f"{path}: expected string")
    elif t == "number":
        if isinstance(obj, bool) or not isinstance(obj, (int, float)):
            errs.append(f"{path}: expected number")
    elif t == "integer":
        if isinstance(obj, bool) or not (isinstance(obj, int) or (isinstance(obj, float) and obj.is_integer())):
            errs.append(f"{path}: expected integer")
    elif t == "boolean":
        if not isinstance(obj, bool):
            errs.append(f"{path}: expected boolean")
    elif t == "null":
        if obj is not None:
            errs.append(f"{path}: expected null")
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']}")
    return errs


def _coerce_ints(obj, schema):
    """Providers sometimes return 20.0 for integers; normalise so downstream code is exact."""
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            if not validate(obj, sub):
                return _coerce_ints(obj, sub)
        return obj
    t = schema.get("type")
    if t == "integer" and isinstance(obj, float) and obj.is_integer():
        return int(obj)
    if t == "object" and isinstance(obj, dict):
        props = schema.get("properties") or {}
        return {k: _coerce_ints(v, props[k]) if k in props else v for k, v in obj.items()}
    if t == "array" and isinstance(obj, list):
        return [_coerce_ints(v, schema.get("items", {})) for v in obj]
    return obj


def extract_json(text: str):
    """Parse a JSON object from model output, tolerating ```json fences or surrounding prose."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


# ------------------------------------------------------------------ client
_compat_quirks: dict = {}  # learned per base_url: which request options the provider rejects


class LLM:
    def __init__(self, client=None, http_post=None):
        """``client``: injected Anthropic client (tests). ``http_post``: injected
        requests.post-compatible callable for the OpenAI-compatible path (tests)."""
        self.cfg = load_config()
        self._client = client
        self._post = http_post
        if client is not None:
            self.provider = "anthropic"
        elif http_post is not None:
            self.provider = "openai_compatible"
        else:
            self.provider = self.cfg.llm_provider

    @property
    def available(self) -> bool:
        return self._client is not None or self._post is not None or not self.cfg.demo_llm

    @property
    def model(self) -> str:
        return self.cfg.llm_model or ("claude-opus-5-5" if self.provider == "anthropic" else "unset")

    # -- shared bookkeeping
    def _precheck(self, purpose: str, system: str, user: str, max_tokens: int) -> None:
        pi, po, _, _ = pricing(self.model)
        worst = ((len(system) + len(user)) / 3.0 * pi + max_tokens * po) / 1e6
        b = budget_status()
        if worst > b["day_remaining"] or worst > b["month_remaining"]:
            raise BudgetExceeded(f"{purpose}: worst-case cost ${worst:.3f} exceeds remaining budget "
                                 f"(day ${b['day_remaining']:.2f}, month ${b['month_remaining']:.2f})")

    def _record_start(self, purpose: str, request: dict) -> int:
        return db.ex("INSERT INTO llm_calls(ts, purpose, model, status, request) VALUES(?,?,?,?,?)",
                     (clock.now(), purpose, self.model, "pending", json.dumps(request)))

    def _record_end(self, cid, status, err, served, usage, text) -> None:
        db.ex("UPDATE llm_calls SET status=?, error=?, served_model=?, input_tokens=?, output_tokens=?, "
              "cache_write_tokens=?, cache_read_tokens=?, cost_usd=?, response=? WHERE id=?",
              (status, err, served, usage["input_tokens"], usage["output_tokens"], usage.get("cache_write_tokens", 0),
               usage.get("cache_read_tokens", 0), cost_of(served or self.model, usage), (text or "")[:200000], cid))

    def call(self, purpose: str, system: str, user: str, schema: dict, max_tokens: int = 16000) -> tuple[dict, int]:
        if self.provider == "openai_compatible":
            return self._call_compat(purpose, system, user, schema, max_tokens)
        if self.provider == "anthropic":
            return self._call_anthropic(purpose, system, user, schema, max_tokens)
        raise LLMError("no AI provider configured")

    # -- OpenAI-compatible chat completions
    def _call_compat(self, purpose, system, user, schema, max_tokens):
        import requests
        post = self._post or requests.post
        base = self.cfg.llm_base_url
        quirks = _compat_quirks.setdefault(base, {"format": "json_schema", "tok": "max_tokens"})
        instruction = ("\n\nRespond with ONE JSON object and nothing else. It must match this JSON Schema "
                       "(all listed fields required; use null where allowed):\n" + json.dumps(schema))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user + instruction}]
        headers = {"Content-Type": "application/json"}
        if self.cfg.llm_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.llm_api_key}"
        repaired = False
        for _attempt in range(6):
            self._precheck(purpose, system, json.dumps(messages), max_tokens)
            body = {"model": self.model, "messages": messages, quirks["tok"]: max_tokens}
            if quirks["format"] == "json_schema":
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": purpose.replace("-", "_")[:60], "schema": schema, "strict": True}}
            elif quirks["format"] == "json_object":
                body["response_format"] = {"type": "json_object"}
            cid = self._record_start(purpose, {"provider": base, "model": self.model, "system": system,
                                               "user": messages[-1]["content"], "schema": schema,
                                               "max_tokens": max_tokens, "format": quirks["format"]})
            t0 = time.time()
            try:
                r = post(f"{base}/chat/completions", headers=headers, json=body, timeout=300)
            except Exception as e:
                self._record_end(cid, "error", f"{type(e).__name__}: {e}"[:1000], None, {"input_tokens": 0, "output_tokens": 0}, "")
                raise LLMError(f"{purpose}: request failed: {type(e).__name__}: {e}") from e
            if r.status_code == 400 and self._adapt(quirks, r.text):
                self._record_end(cid, "retry", f"400, adapting request: {r.text[:300]}", None,
                                 {"input_tokens": 0, "output_tokens": 0}, r.text)
                continue
            if r.status_code >= 400:
                self._record_end(cid, "error", f"HTTP {r.status_code}: {r.text[:500]}", None,
                                 {"input_tokens": 0, "output_tokens": 0}, r.text)
                raise LLMError(f"{purpose}: HTTP {r.status_code} from {base}: {r.text[:300]}")
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            u = data.get("usage") or {}
            usage = {"input_tokens": int(u.get("prompt_tokens") or (len(system) + len(messages[-1]["content"])) / 3),
                     "output_tokens": int(u.get("completion_tokens") or len(text) / 3)}
            served = data.get("model") or self.model
            finish = choice.get("finish_reason")
            if msg.get("refusal") or finish == "content_filter":
                self._record_end(cid, "refused", str(msg.get("refusal") or finish)[:500], served, usage, text)
                raise LLMError(f"{purpose}: model declined")
            if finish == "length":
                self._record_end(cid, "truncated", "hit max tokens", served, usage, text)
                raise LLMError(f"{purpose}: response hit max_tokens before completing")
            try:
                parsed = extract_json(text)
                errs = validate(parsed, schema)
            except ValueError as e:
                parsed, errs = None, [f"not valid JSON: {e}"]
            if not errs:
                self._record_end(cid, "ok", None, served, usage, text)
                log.info("llm %s ok %.1fs", purpose, time.time() - t0)
                return _coerce_ints(parsed, schema), cid
            self._record_end(cid, "invalid_json", "; ".join(errs)[:1000], served, usage, text)
            if repaired:
                raise LLMError(f"{purpose}: response did not match the schema: {'; '.join(errs[:5])}")
            repaired = True
            messages = messages + [{"role": "assistant", "content": text},
                                   {"role": "user", "content": "That response did not match the schema: "
                                    + "; ".join(errs[:10]) + ". Reply with the corrected JSON object only."}]
        raise LLMError(f"{purpose}: provider kept rejecting the request format")

    @staticmethod
    def _adapt(quirks: dict, err: str) -> bool:
        """Learn from a 400 which optional request feature the provider does not support."""
        e = err.lower()
        if "max_tokens" in e and quirks["tok"] == "max_tokens" and ("max_completion_tokens" in e or "unsupported" in e):
            quirks["tok"] = "max_completion_tokens"
            return True
        if ("response_format" in e or "json_schema" in e or "schema" in e) and quirks["format"] == "json_schema":
            quirks["format"] = "json_object"
            return True
        if ("response_format" in e or "json_object" in e) and quirks["format"] == "json_object":
            quirks["format"] = "none"
            return True
        return False

    # -- Anthropic (Claude)
    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key, max_retries=2, timeout=300.0)
        return self._client

    def _call_anthropic(self, purpose, system, user, schema, max_tokens):
        model = self.model
        self._precheck(purpose, system, user, max_tokens)
        cid = self._record_start(purpose, {"provider": "anthropic", "model": model, "system": system, "user": user,
                                           "schema": schema, "max_tokens": max_tokens, "effort": self.cfg.llm_effort})
        kwargs = dict(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.cfg.llm_effort, "format": {"type": "json_schema", "schema": schema}},
        )
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
        usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0,
                 "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}
        served = getattr(resp, "model", model) or model
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
                errs = validate(parsed, schema)
                if errs:
                    status, err, parsed = "invalid_json", "; ".join(errs)[:1000], None
            except ValueError as e:
                status, err = "invalid_json", str(e)
        self._record_end(cid, status, err, served, usage, text)
        if parsed is None:
            raise LLMError(f"{purpose}: {err}")
        return parsed, cid
