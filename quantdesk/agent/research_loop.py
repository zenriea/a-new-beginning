"""Observe -> Investigate -> Test -> Challenge -> Decide.

Each stage is persisted before the next starts, so the record shows what was believed
and decided *before* outcomes were known:
  hypothesis row (rationale, prediction, falsification) -> experiment row (frozen spec)
  -> test results -> decision row (+ lessons) -> strategy row (thesis, exit plan,
  invalidation) if capital is allocated.
"""
from __future__ import annotations

import logging
import traceback

from .. import clock, db, settings
from ..config import load_config
from ..research import gates as G
from ..research.experiment import ExperimentError, run_experiment, validate_research_spec
from ..research.observe import observe
from ..research.strategy import SpecError, describe_rules, spec_hash
from ..trading import ledger
from . import journal
from .analyst import RulesAnalyst, get_analyst, memory, spec_from_llm
from .llm import BudgetExceeded, LLMError

log = logging.getLogger(__name__)


def _fmt_obs(obs: dict) -> str:
    if obs.get("error"):
        return obs["error"]
    n = obs.get("notable") or []
    return "; ".join(x["text"] for x in n[:6]) if n else "no unusual moves, volatility shifts or regime changes"


def record_observation(venue: str) -> dict:
    u = settings.get("universe")[venue]
    bench = settings.get("benchmarks")[venue]
    obs = observe(venue, u, bench)
    oid = db.ex("INSERT INTO observations(ts, venue, data_end, summary, notable) VALUES(?,?,?,?,?)",
                (clock.now(), venue, obs.get("as_of"), db.dumps(obs), db.dumps(obs.get("notable"))))
    obs["observation_id"] = oid
    if obs.get("notable"):
        journal.write("observation", f"{venue}: what changed",
                      "From stored bars: " + _fmt_obs(obs) + ".", refs={"observation_id": oid},
                      author="rules", dedupe_key=f"obs:{venue}:{obs.get('as_of')}")
    return obs


def portfolio_summary() -> dict:
    eb = ledger.equity_breakdown()
    strats = [dict(r) for r in db.q("SELECT id, name, version, venue, status, allocation, approved_allocation FROM strategies "
                                    "WHERE status IN ('active','incubating')")]
    return {"equity": round(eb["equity"], 2), "cash": round(eb["cash"], 2), "gross_exposure": round(eb["gross"], 2),
            "positions": {s: {"qty": v["qty"], "value": round(v["value"], 2)} for s, v in eb["positions"].items()},
            "active_strategies": strats}


def _prior_test_windows(venue: str) -> list[tuple[int, int]]:
    out = []
    for r in db.q("SELECT windows FROM experiments WHERE venue=? AND frozen_at IS NOT NULL AND test IS NOT NULL", (venue,)):
        w = db.loads(r["windows"], {})
        if w and "test" in w:
            out.append((w["test"]["start"], w["test"]["end"]))
    return out


def record_unexpected_failure(venue: str, exc: BaseException) -> None:
    """A research cycle crashed (a bug, not a provider outage): make it visible in the journal
    and close out any experiment it left half-done, so nothing fails silently."""
    now = clock.now()
    for e in db.q("SELECT id, hypothesis_id FROM experiments WHERE status='running' AND venue=?", (venue,)):
        db.ex("UPDATE experiments SET status='error', error=?, completed_at=? WHERE id=?",
              (f"{type(exc).__name__}: {exc}"[:1000], now, e["id"]))
        db.ex("UPDATE hypotheses SET status='abandoned', updated_at=? WHERE id=? AND status='testing'", (now, e["hypothesis_id"]))
    journal.write("system", f"{venue} research cycle failed unexpectedly",
                  f"{type(exc).__name__}: {exc}"[:1500] + "\n\nThis is a software error, not a market or AI-provider "
                  "problem. The cycle was abandoned and the next one runs on schedule. Details are in the worker log "
                  "and Limits & system > Jobs.", author="system")


def _record_no_idea(venue: str, prop: dict, author: str, call_id, observation_id: int) -> dict:
    journal.write("research", f"{venue}: nothing worth testing right now", prop["why"], author=author,
                  llm_call_id=call_id, refs={"observation_id": observation_id})
    db.ex("INSERT INTO decisions(ts, kind, subject_type, subject_id, rationale, details, author, llm_call_id) "
          "VALUES(?,?,?,?,?,?,?,?)", (clock.now(), "no_trade", "venue", None, prop["why"],
                                    db.dumps({"venue": venue}), author, call_id))
    return {"status": "no_idea"}


def research_cycle(venue: str, analyst=None) -> dict:
    cfg = load_config()
    analyst = analyst or get_analyst()
    rs = settings.get("research")
    allowed = settings.get("universe")
    bench = settings.get("benchmarks")[venue]
    journal.set_status("researching", f"{venue}: observing markets")
    obs = record_observation(venue)
    if obs.get("error"):
        journal.write("data", f"{venue}: research skipped",
                      f"Not enough stored market data to research ({obs['error']}). Waiting for data.",
                      author="rules", dedupe_key=f"noresearch:{venue}:{int(clock.now() // 3600)}")
        return {"status": "no_data"}
    blinded = observe(venue, allowed[venue], bench, blind_frac=rs["train_frac"] + rs["val_frac"]) \
        if rs.get("observation_blinding", True) else obs
    costs = settings.get("costs")[venue]
    round_trip = 2 * (costs.get("taker_fee_bps", 0) + costs["backtest_half_spread_bps"] + costs["base_slippage_bps"])
    ctx = {
        # without these the model proposes high-turnover ideas that lose to fees and can never pass the gates
        "trading_costs": dict(costs, approx_round_trip_bps=round_trip,
                              note="Every buy+sell pays about approx_round_trip_bps plus market impact."),
        "acceptance_gates": settings.get("gates"),
        "venue": venue, "approved_universe": allowed[venue], "benchmark": bench,
        "timeframe": obs["timeframe"], "market_context": blinded,
        "context_note": ("Blinded: statistics end at the earliest possible test-window start." if blinded is not obs
                         else "Unblinded current statistics (operator disabled blinding)."),
        "research_memory": memory(venue),
        "active_strategies": portfolio_summary()["active_strategies"],
        "limits": {"max_param_combos": rs["max_param_combos"]},
        "now": clock.now(),
    }
    journal.set_status("researching", f"{venue}: forming a hypothesis")
    try:
        prop, call_id = analyst.propose(ctx)
    except BudgetExceeded as e:
        journal.write("system", "Research paused: API budget", f"{e}. Deterministic trading and risk controls continue.",
                      author="system", dedupe_key=f"budget:{int(clock.now() // 3600)}")
        return {"status": "budget"}
    except LLMError as e:
        journal.write("system", "Research call failed", f"{e}. No hypothesis this cycle.", author="system")
        return {"status": "llm_error"}
    author = analyst.author
    if prop["decision"] == "no_idea" or not prop.get("spec"):
        return _record_no_idea(venue, prop, author, call_id, obs["observation_id"])
    spec = spec_from_llm(prop["spec"], venue) if author == "llm" else prop["spec"]
    for attempt in range(2):
        try:
            validate_research_spec(spec, allowed, rs["max_param_combos"])
            break
        except SpecError as e:
            if attempt == 0 and author == "llm":
                try:
                    prop, call_id = analyst.repair(ctx, prop, str(e))
                    if prop["decision"] == "no_idea" or not prop.get("spec"):
                        return _record_no_idea(venue, prop, author, call_id, obs["observation_id"])
                    spec = spec_from_llm(prop["spec"], venue)
                    continue
                except (LLMError, BudgetExceeded) as e2:
                    err = str(e2)
            else:
                err = str(e)
            journal.write("research", f"Discarded an invalid strategy spec ({venue})",
                          f"The proposed rules could not be validated: {err}. Nothing was tested.", author="rules",
                          llm_call_id=call_id)
            return {"status": "invalid_spec", "error": err}
    now = clock.now()
    hid = db.ex("INSERT INTO hypotheses(created_at, venue, title, rationale, prediction, falsification, status, parent_id, "
                "observation_id, author, llm_call_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, venue, prop["title"], prop["rationale"], prop["prediction"], prop["falsification"], "testing",
                 prop.get("parent_experiment_id"), obs["observation_id"], author, call_id, now))
    rules = describe_rules(spec, {})
    journal.write("hypothesis", f"Investigating: {prop['title']}",
                  f"{prop['why']}\n\nIdea: {prop['rationale']}\nPrediction: {prop['prediction']}\n"
                  f"I'll drop it if: {prop['falsification']}\nRules: entry `{rules['entry']}`, exit `{rules['exit']}`"
                  + (f", rank by `{rules['rank_by']}` top {rules['top_k']}" if rules.get("rank_by") else "")
                  + f"; universe {', '.join(spec['universe'])}; parameter grid {spec.get('params') or {}}.",
                  refs={"hypothesis_id": hid, "observation_id": obs["observation_id"]}, author=author, llm_call_id=call_id)
    eid = db.ex("INSERT INTO experiments(hypothesis_id, created_at, venue, kind, spec, spec_hash, status) VALUES(?,?,?,?,?,?,?)",
                (hid, now, venue, spec.get("kind", "dsl"), db.dumps(spec), spec_hash(spec), "running"))
    journal.set_status("testing", f"Experiment {eid}: {prop['title']}")

    def on_freeze(fr):
        db.ex("UPDATE experiments SET frozen_at=?, chosen_params=?, windows=? WHERE id=?",
              (clock.now(), db.dumps(fr["chosen_params"]), db.dumps(fr["windows"]), eid))

    try:
        res = run_experiment(spec, settings.get("costs")[venue], rs, bench, _prior_test_windows(venue),
                             cfg.llm_knowledge_cutoff, on_freeze=on_freeze, llm_authored=(author == "llm"))
    except (ExperimentError, Exception) as e:
        log.error("experiment %s failed: %s", eid, traceback.format_exc())
        db.ex("UPDATE experiments SET status='error', error=?, completed_at=? WHERE id=?", (str(e)[:1000], clock.now(), eid))
        db.ex("UPDATE hypotheses SET status='abandoned', updated_at=? WHERE id=?", (clock.now(), hid))
        journal.write("test", f"Experiment {eid} could not run", f"{type(e).__name__}: {e}", refs={"experiment_id": eid},
                      author="rules")
        return {"status": "error", "error": str(e)}
    gres = G.evaluate(res, settings.get("gates"))
    db.ex("UPDATE experiments SET grid_results=?, train=?, validation=?, test=?, challenge=?, gates=?, leakage=?, "
          "equity_curve=?, status=?, completed_at=? WHERE id=?",
          (db.dumps(res["grid_results"]), db.dumps(res["train"]), db.dumps(res["validation"]), db.dumps(res["test"]),
           db.dumps(res["challenge"]), db.dumps(gres), db.dumps(res["leakage"]), db.dumps(res["equity_curve"]),
           "passed" if gres["passed"] else "failed", clock.now(), eid))
    t = res["test"]
    bvenue = t.get("benchmark_venue") or {}
    journal.write("test", f"Test result: {prop['title']} ({'passed' if gres['passed'] else 'failed'} gates)",
                  f"Chosen on train: {res['chosen_params']}. Train Sharpe {res['train']['sharpe']:.2f}, validation "
                  f"{res['validation']['sharpe']:.2f}. Sealed test ({res['windows']['test']['bars']} bars): net return "
                  f"{t['total_return']:+.2%}, Sharpe {t['sharpe']:.2f}, max DD {t['max_drawdown']:.1%}, {t['n_trades']} trades, "
                  f"cost drag {t['cost_drag']:.2%}; {bench} buy-and-hold {bvenue.get('total_return', 0):+.2%}. "
                  f"Doubled costs: {res['challenge']['test_cost_x2']['total_return']:+.2%}; one-bar delay Sharpe "
                  f"{res['challenge']['test_delay_1bar']['sharpe']:.2f}. Deflated PSR {t['deflated_psr']:.2f} "
                  f"({res['leakage']['n_trials_for_deflation']} trial(s) on this holdout)."
                  + (f" Failed: {', '.join(gres['failed'])}." if gres["failed"] else ""),
                  refs={"experiment_id": eid, "hypothesis_id": hid}, author="rules")
    return _decide(venue, eid, hid, prop, spec, res, gres, obs, bench, rs, analyst)


def _decide(venue, eid, hid, prop, spec, res, gres, obs, bench, rs, analyst) -> dict:
    # ---- Decide (spec is frozen; unblinded context is now allowed)
    journal.set_status("deciding", f"Experiment {eid}")
    lim = settings.get("risk")
    dctx = {"experiment_id": eid, "hypothesis": {"id": hid, "title": prop["title"], "rationale": prop["rationale"],
                                                  "prediction": prop["prediction"], "falsification": prop["falsification"]},
            "spec": spec, "results": {k: res[k] for k in ("windows", "chosen_params", "train", "validation", "test",
                                                          "challenge", "leakage")},
            "gates": gres, "benchmark": bench, "current_market": obs, "portfolio": portfolio_summary(),
            "limits": {"max_strategy_pct": lim["max_strategy_pct"], "max_total_allocation_pct": lim["max_total_allocation_pct"],
                       "incubation_fraction": rs["incubation_fraction"]}}
    try:
        dec, dcall = analyst.decide(dctx)
        dauthor = analyst.author
    except (BudgetExceeded, LLMError) as e:
        dec, dcall = RulesAnalyst().decide(dctx)
        dauthor = "rules"
        journal.write("system", "Decision made by rules engine", f"LLM unavailable for the decision step ({e}); "
                      "the deterministic rules made it instead.", author="system")
    decision = dec["decision"]
    if decision == "allocate" and not gres["passed"]:
        decision = "reject"
        dec["journal"] += "\n\n[Risk/gate engine] Allocation refused: gates failed (" + ", ".join(gres["failed"]) + ")."
    elif decision == "allocate" and not float(dec.get("allocation_pct") or 0) > 0:
        decision = "reject"
        dec["journal"] += "\n\n[Risk/gate engine] Recorded as a rejection: the allocation requested was 0%."
    did = db.ex("INSERT INTO decisions(ts, kind, subject_type, subject_id, rationale, details, author, llm_call_id) "
                "VALUES(?,?,?,?,?,?,?,?)", (clock.now(), decision, "experiment", eid, dec["journal"],
                                            db.dumps({"lessons": dec.get("lessons", []), "revision_idea": dec.get("revision_idea"),
                                                      "requested_allocation_pct": dec.get("allocation_pct")}),
                                            dauthor, dcall))
    _save_beliefs(dec.get("beliefs", []), venue, dauthor, dcall)
    hstatus = {"reject": "rejected", "revise": "revise", "allocate": "allocated"}[decision]
    db.ex("UPDATE hypotheses SET status=?, updated_at=? WHERE id=?", (hstatus, clock.now(), hid))
    title = {"reject": f"Rejected: {prop['title']}", "revise": f"Revising: {prop['title']}",
             "allocate": f"Allocating capital: {prop['title']}"}[decision]
    body = dec["journal"]
    if dec.get("lessons"):
        body += "\n\nRecorded lessons: " + " | ".join(dec["lessons"])
    if decision == "revise" and dec.get("revision_idea"):
        body += f"\nNext: {dec['revision_idea']}"
    journal.write("decision", title, body, refs={"experiment_id": eid, "decision_id": did}, author=dauthor, llm_call_id=dcall)
    out = {"status": "done", "experiment_id": eid, "decision": decision}
    if decision == "allocate":
        out["strategy_id"] = create_strategy(hid, eid, prop, spec, res, dec, dauthor, dcall)
    return out


def rejudge_experiment(eid: int, analyst=None) -> dict:
    """Re-apply the CURRENT gates to a finished experiment's stored results.

    Nothing is re-backtested and the sealed test window is not opened again, so this
    adds no deflation trial. Used when the operator loosens gates after a rejection.
    """
    e = db.q1("SELECT * FROM experiments WHERE id=?", (eid,))
    if not e or e["status"] not in ("failed", "passed") or not e["test"]:
        journal.write("system", f"Experiment {eid} cannot be re-judged", "Only finished experiments with stored test "
                      "results can be re-judged.", author="system")
        return {"status": "not_found"}
    if db.q1("SELECT 1 FROM strategies WHERE experiment_id=?", (eid,)):
        journal.write("system", f"Experiment {eid} already has a strategy", "Nothing to re-judge.", author="system")
        return {"status": "exists"}
    h = dict(db.q1("SELECT * FROM hypotheses WHERE id=?", (e["hypothesis_id"],)))
    res = {k: db.loads(e[k]) for k in ("windows", "chosen_params", "train", "validation", "test", "challenge", "leakage")}
    res["strategy_kind"] = e["kind"]
    spec, venue = db.loads(e["spec"]), e["venue"]
    gres = G.evaluate(res, settings.get("gates"))
    db.ex("UPDATE experiments SET gates=?, status=? WHERE id=?",
          (db.dumps(gres), "passed" if gres["passed"] else "failed", eid))
    journal.write("test", f"Re-judged experiment {eid} under current gates ({'passed' if gres['passed'] else 'failed'})",
                  "Stored test results re-checked against the operator's current thresholds; no new backtest, "
                  "no new holdout look." + (f" Failed: {', '.join(gres['failed'])}." if gres["failed"] else ""),
                  refs={"experiment_id": eid, "hypothesis_id": h["id"]}, author="rules")
    if not gres["passed"]:
        return {"status": "done", "experiment_id": eid, "decision": "reject"}
    prop = {k: h[k] for k in ("title", "rationale", "prediction", "falsification")}
    obs = record_observation(venue)
    return _decide(venue, eid, h["id"], prop, spec, res, gres, obs, settings.get("benchmarks")[venue],
                   settings.get("research"), analyst or get_analyst())


def _save_beliefs(beliefs: list, venue: str, author: str, call_id) -> None:
    valid_types = {"experiment": "experiments", "strategy": "strategies", "order": "orders", "fill": "fills",
                   "observation": "observations", "hypothesis": "hypotheses", "decision": "decisions"}
    for b in beliefs or []:
        ev = []
        for ref in b.get("evidence", []):
            typ, _, rid = str(ref).partition(":")
            if typ in valid_types and rid.isdigit() and db.q1(f"SELECT 1 FROM {valid_types[typ]} WHERE id=?", (int(rid),)):
                ev.append(f"{typ}:{rid}")
        if not ev:
            continue  # beliefs without verifiable evidence are not stored
        db.ex("UPDATE beliefs SET status='superseded' WHERE status='active' AND venue=? AND statement=?",
              (venue, b["statement"]))
        db.ex("INSERT INTO beliefs(ts, venue, statement, confidence, evidence, status, author, llm_call_id) "
              "VALUES(?,?,?,?,?,?,?,?)", (clock.now(), venue, b["statement"], b["confidence"], db.dumps(ev), "active",
                                          author, call_id))
    # keep the belief board short: newest 8 per venue stay active
    rows = db.q("SELECT id FROM beliefs WHERE status='active' AND venue=? ORDER BY id DESC", (venue,))
    for r in rows[8:]:
        db.ex("UPDATE beliefs SET status='superseded' WHERE id=?", (r["id"],))


def create_strategy(hid, eid, prop, spec, res, dec, author, call_id) -> int:
    lim = settings.get("risk")
    rs = settings.get("research")
    eq = ledger.equity_breakdown()["equity"]
    req_pct = max(0.0, float(dec.get("allocation_pct") or 0))
    approved_pct = min(req_pct, lim["max_strategy_pct"])
    approved = approved_pct * eq
    from ..trading.live import allocation_room
    start_alloc = min(approved * rs["incubation_fraction"], allocation_room(eq))  # never beyond total-allocation room
    if start_alloc < lim["min_order_usd"]:
        start_alloc = 0.0  # waits as 'incubating' until room frees up (live.activate_waiting)
    t = res["test"]
    v = 1 + db.q1("SELECT COUNT(*) n FROM strategies WHERE name=?", (spec["name"],))["n"]
    fixed = dict(spec, fixed_params=res["chosen_params"])
    expectations = {"test": t, "validation": res["validation"], "windows": res["windows"],
                    "bars_per_year": None, "chosen_params": res["chosen_params"]}
    inval = dec.get("invalidation") or {}
    now = clock.now()
    sid = db.ex("INSERT INTO strategies(hypothesis_id, experiment_id, name, version, venue, spec, spec_hash, status, "
                "approved_allocation, allocation, thesis, exit_plan, expectations, invalidation, created_at, activated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (hid, eid, spec["name"], v, spec["venue"], db.dumps(fixed), spec_hash(fixed),
                 "active" if start_alloc > 0 else "incubating", approved, start_alloc, dec.get("thesis") or prop["rationale"],
                 dec.get("exit_plan") or "", db.dumps(expectations), db.dumps(inval), now, now))
    from ..marketdata import freshness
    db.ex("UPDATE strategies SET last_signal_bar=? WHERE id=?", (freshness.expected_last_bar(spec["venue"], now), sid))
    note = ""
    if approved < req_pct * eq - 1:
        note = f" Requested {req_pct:.1%} of equity; operator limits capped approval at ${approved:,.0f}."
    full = approved * rs["incubation_fraction"]
    if start_alloc <= 0:
        title = f"Strategy approved, waiting for room: {spec['name']} v{v}"
        start = (f"No room under the total-allocation limit ({lim['max_total_allocation_pct']:.0%} of equity), so it "
                 "starts trading automatically when other strategies free up capital.")
    else:
        title = f"New strategy live: {spec['name']} v{v}"
        start = (f"Trading starts at the incubation size ${start_alloc:,.0f} on the next bar close"
                 + (f" (reduced from ${full:,.0f} by the total-allocation limit)." if start_alloc < full - 1 else
                    f" ({rs['incubation_fraction']:.0%})."))
    journal.write("decision", title,
                  f"Approved ${approved:,.0f}. {start}{note}\nThesis: {dec.get('thesis')}\n"
                  f"Exit plan: {dec.get('exit_plan')}\nInvalidation: {inval}",
                  refs={"strategy_id": sid, "experiment_id": eid}, author=author, llm_call_id=call_id)
    return sid
