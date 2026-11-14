"""Operator questions, answered from stored records only."""
from __future__ import annotations

import re

from .. import clock, db, settings
from ..trading import ledger
from . import journal
from .analyst import get_analyst
from .llm import BudgetExceeded, LLMError


def _symbols_in(question: str) -> list[str]:
    u = settings.get("universe")
    allsyms = u["NASDAQ"] + u["CRYPTO"]
    q = question.upper()
    found = [s for s in allsyms if re.search(rf"\b{re.escape(s)}\b", q) or
             ("-" in s and re.search(rf"\b{re.escape(s.split('-')[0])}\b", q))]
    return found


def gather(question: str) -> dict:
    syms = _symbols_in(question)
    rec: dict = {"now": clock.now(), "symbols_mentioned": syms}
    eb = ledger.equity_breakdown()
    rec["account"] = {"equity": round(eb["equity"], 2), "cash": round(eb["cash"], 2), "gross": round(eb["gross"], 2)}
    pos = ledger.sleeve_positions()
    if syms:
        pos = [p for p in pos if p["symbol"] in syms]
    rec["positions"] = [{"id": f"position:{p['strategy_id']}:{p['symbol']}", **p} for p in pos]
    sids = {p["strategy_id"] for p in pos}
    oq = "SELECT * FROM orders " + (f"WHERE symbol IN ({','.join('?' * len(syms))}) " if syms else "") + "ORDER BY id DESC LIMIT 12"
    orders = [dict(r) for r in db.q(oq, syms)]
    rec["orders"] = [{"id": f"order:{o['id']}", "symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
                      "status": o["status"], "reason": o["reason"], "rationale_recorded_before_fill": o["rationale"],
                      "created_at": o["created_at"], "filled_qty": o["filled_qty"], "avg_price": o["avg_price"],
                      "fees": o["fees"], "status_note": o["status_note"], "strategy_id": o["strategy_id"]} for o in orders]
    sids |= {o["strategy_id"] for o in orders if o["strategy_id"]}
    ids = [o["id"] for o in orders]
    rec["fills"] = [dict(r) for r in db.q(f"SELECT * FROM fills WHERE order_id IN ({','.join('?' * len(ids))}) ORDER BY id DESC",
                                          ids)] if ids else []
    for f in rec["fills"]:
        f["id"] = f"fill:{f['id']}"
    strat_rows = db.q(f"SELECT * FROM strategies WHERE id IN ({','.join('?' * len(sids))})", list(sids)) if sids else \
        db.q("SELECT * FROM strategies WHERE status IN ('active','incubating') ORDER BY id DESC LIMIT 5")
    rec["strategies"] = []
    for s in strat_rows:
        spec = db.loads(s["spec"], {})
        rec["strategies"].append({"id": f"strategy:{s['id']}", "name": s["name"], "version": s["version"],
                                  "status": s["status"], "allocation": s["allocation"], "thesis": s["thesis"],
                                  "exit_plan": s["exit_plan"], "invalidation": db.loads(s["invalidation"]),
                                  "entry_rule": spec.get("entry"), "exit_rule": spec.get("exit"),
                                  "params": spec.get("fixed_params"), "stop_loss_pct": spec.get("stop_loss_pct"),
                                  "take_profit_pct": spec.get("take_profit_pct"), "max_hold_bars": spec.get("max_hold_bars"),
                                  "experiment_id": s["experiment_id"]})
    exps = db.q("SELECT e.id, e.status, e.test, e.gates, h.title FROM experiments e JOIN hypotheses h ON h.id=e.hypothesis_id "
                "ORDER BY e.id DESC LIMIT 6")
    rec["recent_experiments"] = [{"id": f"experiment:{e['id']}", "title": e["title"], "status": e["status"],
                                  "test": db.loads(e["test"]), "failed_gates": (db.loads(e["gates"], {}) or {}).get("failed")}
                                 for e in exps]
    rec["beliefs"] = [{"id": f"belief:{b['id']}", "statement": b["statement"], "confidence": b["confidence"],
                       "evidence": db.loads(b["evidence"])} for b in db.q("SELECT * FROM beliefs WHERE status='active' "
                                                                          "ORDER BY id DESC LIMIT 8")]
    rec["recent_journal"] = [{"id": f"journal:{j['id']}", "kind": j["kind"], "title": j["title"], "body": j["body"][:600],
                              "ts": j["ts"]} for j in journal.recent(15)]
    st = db.q1("SELECT * FROM agent_status WHERE id=1")
    rec["agent_status"] = dict(st) if st else None
    rec["controls"] = settings.get("agent")
    return rec


def deterministic_answer(question: str, rec: dict) -> dict:
    q = question.lower()
    cites = []
    parts = []
    syms = rec.get("symbols_mentioned") or []
    if any(w in q for w in ("why", "buy", "bought", "enter", "own")) and rec["orders"]:
        buys = [o for o in rec["orders"] if o["side"] == "buy"]
        if buys:
            o = buys[0]
            parts.append(f"{o['id']} ({o['status']}): {o['rationale_recorded_before_fill']}")
            cites.append(o["id"])
    if any(w in q for w in ("sell", "exit", "close", "stop")):
        for s in rec["strategies"][:2]:
            rules = [f"exit rule `{s['exit_rule']}` with {s['params']}"] if s.get("exit_rule") else []
            if s.get("stop_loss_pct"):
                rules.append(f"stop-loss -{s['stop_loss_pct']:.0%}")
            if s.get("max_hold_bars"):
                rules.append(f"time stop {s['max_hold_bars']} bars")
            parts.append(f"{s['id']} {s['name']} exits on: {'; '.join(rules) or 'n/a'}. Exit plan: {s['exit_plan']}. "
                         f"Retirement conditions: {s['invalidation']}.")
            cites.append(s["id"])
    if not parts and any(w in q for w in ("research", "working", "doing", "next")):
        st = rec.get("agent_status") or {}
        parts.append(f"Current activity: {st.get('activity')} - {st.get('detail')}.")
        for e in rec["recent_experiments"][:3]:
            parts.append(f"{e['id']} '{e['title']}' is {e['status']}.")
            cites.append(e["id"])
    if not parts:
        parts.append("These records do not directly answer that. Most recent journal entries: " +
                     "; ".join(f"{j['id']} {j['title']}" for j in rec["recent_journal"][:4]))
        cites += [j["id"] for j in rec["recent_journal"][:4]]
    if syms and not rec["positions"]:
        parts.append(f"No current position in {', '.join(syms)}.")
    return {"answer": " ".join(parts), "citations": cites,
            "unknowns": "Demo mode: answer assembled from records by rules (no LLM)."}


def ask(question: str) -> dict:
    question = question.strip()[:1000]
    rec = gather(question)
    analyst = get_analyst()
    try:
        ans, call_id = analyst.answer(question, rec)
        author = analyst.author
    except (BudgetExceeded, LLMError) as e:
        ans, call_id, author = deterministic_answer(question, rec), None, "rules"
        ans["unknowns"] = f"LLM unavailable ({e}); answered from records by rules."
    known = set()
    for k in ("positions", "orders", "fills", "strategies", "recent_experiments", "beliefs", "recent_journal"):
        for r in rec.get(k, []):
            known.add(str(r.get("id")))
    ans["citations"] = [c for c in ans.get("citations", []) if c in known]  # drop citations to nonexistent records
    qid = db.ex("INSERT INTO questions(ts, question, answer, refs, author, llm_call_id) VALUES(?,?,?,?,?,?)",
                (clock.now(), question, ans["answer"], db.dumps(ans), author, call_id))
    return {"id": qid, "author": author, **ans}
