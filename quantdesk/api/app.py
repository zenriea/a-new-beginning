"""HTTP API + static UI. Run: uvicorn quantdesk.api.app:app (or python -m quantdesk.api.app)

Security
  * Password login -> HMAC-signed, expiring session cookie (HttpOnly, SameSite=Strict,
    Secure when QD_COOKIE_SECURE=1). Without QD_ADMIN_PASSWORD only loopback clients are
    served.
  * State-changing requests must carry header ``X-QD: 1`` (blocks cross-site forms).
  * Login attempts are rate limited. Secrets never leave the server.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .. import clock, db, settings, venues
from ..agent import journal
from ..agent.llm import budget_status
from ..config import DEFAULT_SETTINGS, load_config
from ..marketdata import store
from ..research.sandbox import isolation_report
from ..trading import ledger, live, oms

WEB = Path(__file__).resolve().parents[2] / "web"
SESSION_TTL = 12 * 3600
cfg = load_config()
db.init(cfg.db_path)
_secret = (cfg.session_secret or db.get_meta("session_secret") or "").encode()
if not _secret:
    s = secrets.token_hex(32)
    db.set_meta("session_secret", s)
    _secret = s.encode()

app = FastAPI(title="QuantDesk", docs_url=None, redoc_url=None, openapi_url=None)
_attempts: dict[str, list[float]] = defaultdict(list)


# ------------------------------------------------------------------ auth
def _sign(exp: int) -> str:
    mac = hmac.new(_secret, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def _valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, mac = token.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(_sign(int(exp)), token)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def require_auth(request: Request):
    if not cfg.admin_password:
        if _client_ip(request) not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "QD_ADMIN_PASSWORD is not set: remote access disabled")
        return
    if not _valid(request.cookies.get("qd_session")):
        raise HTTPException(401, "login required")


def require_write(request: Request):
    require_auth(request)
    if request.headers.get("x-qd") != "1":
        raise HTTPException(403, "missing X-QD header")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self'; connect-src 'self'")
    return resp


@app.post("/api/login")
async def login(request: Request, response: Response):
    ip = _client_ip(request)
    now = time.time()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < 60]
    if len(_attempts[ip]) >= 5:
        raise HTTPException(429, "too many attempts; wait a minute")
    _attempts[ip].append(now)
    body = await request.json()
    pw = str(body.get("password", ""))
    if not cfg.admin_password:
        raise HTTPException(400, "no password configured; local access only")
    if not hmac.compare_digest(hashlib.sha256(pw.encode()).digest(), hashlib.sha256(cfg.admin_password.encode()).digest()):
        await asyncio.sleep(1.0)
        raise HTTPException(401, "wrong password")
    response.set_cookie("qd_session", _sign(int(now + SESSION_TTL)), max_age=SESSION_TTL, httponly=True,
                        samesite="strict", secure=cfg.cookie_secure, path="/")
    db.ex("INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)", (clock.now(), "operator", "login", db.dumps({"ip": ip})))
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("qd_session", path="/")
    return {"ok": True}


@app.get("/api/session")
async def session(request: Request):
    try:
        require_auth(request)
        return {"authenticated": True, "password_required": bool(cfg.admin_password)}
    except HTTPException:
        return {"authenticated": False, "password_required": bool(cfg.admin_password)}


@app.get("/api/health")
async def health():
    r = db.q1("SELECT heartbeat_at FROM worker_lock WHERE id=1")
    age = clock.now() - r["heartbeat_at"] if r else None
    ok = age is not None and age < 60
    return JSONResponse({"ok": ok, "worker_heartbeat_age_sec": age}, status_code=200 if ok else 503)


# ------------------------------------------------------------------ read models
def _state() -> dict:
    now = clock.now()
    eb = ledger.equity_breakdown()
    init = float(db.get_meta("initial_capital") or cfg.initial_capital)
    snap = db.q1("SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
    first = db.q1("SELECT * FROM equity_snapshots ORDER BY ts ASC LIMIT 1")
    peak = max(eb["equity"], (db.q1("SELECT MAX(peak_equity) p FROM equity_snapshots")["p"] or 0))
    fees = db.q1("SELECT COALESCE(SUM(fee),0) f FROM fills")["f"]
    pos_rows = []
    for p in ledger.sleeve_positions():
        px, as_of, basis = store.mark_price(p["symbol"])
        st = db.q1("SELECT * FROM strategies WHERE id=?", (p["strategy_id"],))
        entry_order = db.q1("SELECT id, rationale, created_at FROM orders WHERE strategy_id=? AND symbol=? AND side='buy' "
                            "AND filled_qty>0 ORDER BY id DESC LIMIT 1", (p["strategy_id"], p["symbol"]))
        spec = db.loads(st["spec"], {}) if st else {}
        exits = []
        if spec.get("exit"):
            exits.append(f"Rule: {spec['exit']} {spec.get('fixed_params') or ''}".strip())
        if spec.get("stop_loss_pct"):
            exits.append(f"Stop-loss -{spec['stop_loss_pct']:.0%} on a bar close (from ${p['avg_cost']:,.2f}: "
                         f"${p['avg_cost'] * (1 - spec['stop_loss_pct']):,.2f})")
        if spec.get("take_profit_pct"):
            exits.append(f"Take-profit +{spec['take_profit_pct']:.0%}")
        if spec.get("max_hold_bars"):
            exits.append(f"Time stop after {spec['max_hold_bars']} bars")
        if spec.get("rank_by"):
            exits.append(f"Rotated out if it drops out of the top {spec.get('top_k')} by {spec['rank_by']}")
        val = p["qty"] * (px or 0)
        pos_rows.append({
            "strategy_id": p["strategy_id"], "strategy": f"{st['name']} v{st['version']}" if st else "-",
            "symbol": p["symbol"], "venue": p["venue"], "qty": p["qty"], "avg_cost": p["avg_cost"], "mark": px,
            "mark_as_of": as_of, "mark_basis": basis, "value": val,
            "unrealized": p["qty"] * ((px or p["avg_cost"]) - p["avg_cost"]),
            "unrealized_pct": ((px or p["avg_cost"]) / p["avg_cost"] - 1) if p["avg_cost"] else 0,
            "weight": val / eb["equity"] if eb["equity"] else 0, "opened_at": p["opened_at"],
            "entry_rationale": entry_order["rationale"] if entry_order else None,
            "entry_order_id": entry_order["id"] if entry_order else None,
            "thesis": st["thesis"] if st else None, "exit_conditions": exits,
            "exit_plan": st["exit_plan"] if st else None,
        })
    agent_status = db.row_dict(db.q1("SELECT * FROM agent_status WHERE id=1"), ["next_actions"])
    hb = db.q1("SELECT * FROM worker_lock WHERE id=1")
    researching = db.q1("SELECT e.id, h.title, e.status, e.created_at FROM experiments e JOIN hypotheses h ON "
                        "h.id=e.hypothesis_id ORDER BY e.id DESC LIMIT 1")
    bench = snap["benchmark_equity"] if snap else None
    data_status = store.status_all()
    bad = [d for d in data_status if not d["ok"]]
    if cfg.market_data_mode != "synthetic" and not cfg.alpaca_configured:
        nasdaq = set(settings.get("universe")["NASDAQ"])   # already covered by the "not configured" alert
        bad = [d for d in bad if d["key"].split(":", 1)[-1] not in nasdaq]
    return {
        "now": now,
        "mode": {"paper": True, "llm": "demo" if cfg.demo_llm else cfg.llm_model,
                 "market_data": cfg.market_data_mode, "alpaca_configured": cfg.alpaca_configured,
                 "alpaca_feed": cfg.alpaca_feed},
        "worker": {"alive": bool(hb and now - hb["heartbeat_at"] < 60),
                   "heartbeat_age": now - hb["heartbeat_at"] if hb else None,
                   "started_at": hb["started_at"] if hb else None},
        "controls": settings.get("agent") | {"drawdown_halt": db.get_meta("drawdown_halt") == "1",
                                             "system_halt": db.get_meta("system_halt") == "1"},
        "account": {"equity": eb["equity"], "cash": eb["cash"], "gross": eb["gross"], "crypto_gross": eb["crypto_gross"],
                    "initial": init, "pnl": eb["equity"] - init, "return": eb["equity"] / init - 1,
                    "fees_paid": fees, "peak": peak, "drawdown": (eb["equity"] - peak) / peak if peak else 0,
                    "exposure": eb["gross"] / eb["equity"] if eb["equity"] else 0,
                    "benchmark_symbol": settings.get("benchmarks")["account"],
                    "benchmark_equity": bench,
                    "benchmark_return": (bench / init - 1) if bench else None,
                    "tracking_since": first["ts"] if first else None, "unpriced": eb["unpriced"]},
        "positions": sorted(pos_rows, key=lambda r: -r["value"]),
        "agent": agent_status,
        "latest_experiment": dict(researching) if researching else None,
        "sessions": venues.describe_session(now),
        "data": {"problems": bad, "count": len(data_status)},
        "budget": budget_status(),
        "limits": settings.get("risk"),
        "strategies": [dict(r) | {"spec": None} for r in db.q("SELECT id, name, version, venue, status, allocation, "
                                                                 "approved_allocation, created_at FROM strategies "
                                                                 "WHERE status IN ('active','incubating') ORDER BY id")],
        "beliefs": [db.row_dict(b, ["evidence"]) for b in db.q("SELECT * FROM beliefs WHERE status='active' ORDER BY id DESC LIMIT 10")],
        "counts": {k: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for k, t in
                   (("experiments", "experiments"), ("hypotheses", "hypotheses"), ("orders", "orders"),
                    ("fills", "fills"), ("strategies", "strategies"))},
        "open_orders": [dict(o) for o in db.q("SELECT id, symbol, side, qty, filled_qty, status, eligible_at, expires_at, "
                                              "reason FROM orders WHERE status IN ('intent','submitted','partially_filled') ORDER BY id")],
    }


@app.get("/api/state", dependencies=[Depends(require_auth)])
async def state():
    return await run_in_threadpool(_state)


@app.get("/api/equity", dependencies=[Depends(require_auth)])
async def equity(points: int = 600):
    def f():
        rows = db.q("SELECT ts, equity, benchmark_equity, crypto_benchmark_equity, gross, cash FROM equity_snapshots ORDER BY ts")
        if len(rows) > points:
            step = len(rows) / points
            rows = [rows[int(i * step)] for i in range(points)] + [rows[-1]]
        return [dict(r) for r in rows]
    return await run_in_threadpool(f)


@app.get("/api/journal", dependencies=[Depends(require_auth)])
async def get_journal(after_id: int = 0, limit: int = 100, kind: str | None = None):
    return await run_in_threadpool(journal.recent, min(limit, 500), after_id, kind.split(",") if kind else None)


@app.get("/api/stream", dependencies=[Depends(require_auth)])
async def stream(request: Request, after_id: int = 0):
    async def gen():
        last = after_id
        tick = 0
        while True:
            if await request.is_disconnected():
                break
            rows = await run_in_threadpool(lambda: list(reversed(journal.recent(50, last))))
            for r in rows:
                last = max(last, r["id"])
                yield f"event: journal\ndata: {json.dumps(r, default=str)}\n\n"
            tick += 1
            if tick % 5 == 0:
                yield f"event: ping\ndata: {int(time.time())}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _rows(sql, params=(), json_fields=()):
    return [db.row_dict(r, json_fields) for r in db.q(sql, params)]


@app.get("/api/experiments", dependencies=[Depends(require_auth)])
async def experiments(limit: int = 100):
    return await run_in_threadpool(_rows, "SELECT e.id, e.hypothesis_id, e.created_at, e.completed_at, e.venue, e.kind, e.status, "
                                   "e.chosen_params, e.test, e.gates, e.leakage, e.error, h.title, h.author FROM experiments e "
                                   "JOIN hypotheses h ON h.id=e.hypothesis_id ORDER BY e.id DESC LIMIT ?", (limit,),
                                   ("chosen_params", "test", "gates", "leakage"))


@app.get("/api/experiments/{eid}", dependencies=[Depends(require_auth)])
async def experiment(eid: int):
    def f():
        e = db.row_dict(db.q1("SELECT * FROM experiments WHERE id=?", (eid,)),
                        ["spec", "windows", "chosen_params", "grid_results", "train", "validation", "test", "challenge",
                         "gates", "leakage", "equity_curve"])
        if not e:
            raise HTTPException(404)
        e["hypothesis"] = db.row_dict(db.q1("SELECT * FROM hypotheses WHERE id=?", (e["hypothesis_id"],)))
        e["decisions"] = _rows("SELECT * FROM decisions WHERE subject_type='experiment' AND subject_id=? ORDER BY id",
                               (eid,), ("details",))
        e["strategy"] = db.row_dict(db.q1("SELECT id, name, version, status FROM strategies WHERE experiment_id=?", (eid,)))
        return e
    return await run_in_threadpool(f)


@app.get("/api/strategies", dependencies=[Depends(require_auth)])
async def strategies():
    def f():
        from ..agent.review import live_stats
        out = []
        for s in db.q("SELECT * FROM strategies ORDER BY id DESC"):
            d = db.row_dict(s, ["spec", "expectations", "invalidation"])
            d["live"] = live_stats(dict(s))
            d["reviews"] = _rows("SELECT * FROM decisions WHERE subject_type='strategy' AND subject_id=? ORDER BY id DESC LIMIT 5",
                                 (s["id"],), ("details",))
            out.append(d)
        return out
    return await run_in_threadpool(f)


@app.get("/api/orders", dependencies=[Depends(require_auth)])
async def orders(limit: int = 200):
    return await run_in_threadpool(_rows, "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,), ("signal_snapshot", "risk_check"))


@app.get("/api/fills", dependencies=[Depends(require_auth)])
async def fills(limit: int = 200, symbol: str | None = None):
    if symbol:
        return await run_in_threadpool(_rows, "SELECT * FROM fills WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))
    return await run_in_threadpool(_rows, "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/candles", dependencies=[Depends(require_auth)])
async def candles(symbol: str, limit: int = 400):
    def f():
        tf = venues.VENUES[venues.venue_of(symbol)].timeframe
        b = store.load_bars(symbol, tf, limit=min(limit, 3000))
        return {"symbol": symbol, "timeframe": tf, "bars": [
            {"time": int(t), "open": o, "high": h, "low": l, "close": c, "volume": v}
            for t, o, h, l, c, v in zip(b["ts"].tolist(), b["open"].tolist(), b["high"].tolist(), b["low"].tolist(),
                                        b["close"].tolist(), b["volume"].tolist())],
            "quote": store.get_quote(symbol)}
    return await run_in_threadpool(f)


@app.get("/api/universe", dependencies=[Depends(require_auth)])
async def universe():
    return settings.get("universe")


@app.get("/api/llm_calls", dependencies=[Depends(require_auth)])
async def llm_calls(limit: int = 50):
    return await run_in_threadpool(_rows, "SELECT id, ts, purpose, model, served_model, input_tokens, output_tokens, "
                                   "cache_read_tokens, cost_usd, status, error FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/llm_calls/{cid}", dependencies=[Depends(require_auth)])
async def llm_call(cid: int):
    r = await run_in_threadpool(db.q1, "SELECT * FROM llm_calls WHERE id=?", (cid,))
    if not r:
        raise HTTPException(404)
    d = dict(r)
    d["request"] = json.loads(d["request"]) if d["request"] else None
    return d


@app.get("/api/system", dependencies=[Depends(require_auth)])
async def system():
    def f():
        return {"settings": settings.all_settings(), "defaults": DEFAULT_SETTINGS,
                "ceilings": {k: getattr(cfg, k) for k in ("ceiling_max_position_pct", "ceiling_max_gross_pct",
                                                          "ceiling_max_strategy_pct", "ceiling_max_order_usd",
                                                          "ceiling_monthly_llm_usd")},
                "jobs": _rows("SELECT * FROM jobs ORDER BY name"), "data_status": store.status_all(),
                "audit": _rows("SELECT * FROM audit_log ORDER BY id DESC LIMIT 50", (), ("detail",)),
                "risk_events": _rows("SELECT * FROM risk_events ORDER BY id DESC LIMIT 50", (), ("detail",)),
                "integrity": ledger.integrity_check(), "sandbox": isolation_report(),
                "llm": {"model": cfg.llm_model, "effort": cfg.llm_effort, "knowledge_cutoff": cfg.llm_knowledge_cutoff,
                        "demo": cfg.demo_llm}}
    return await run_in_threadpool(f)


# ------------------------------------------------------------------ operator actions
def _audit(action: str, detail=None):
    db.ex("INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)", (clock.now(), "operator", action, db.dumps(detail)))


@app.post("/api/ask", dependencies=[Depends(require_write)])
async def ask(request: Request):
    body = await request.json()
    q = str(body.get("question", "")).strip()
    if not q:
        raise HTTPException(400, "empty question")
    from ..agent.qa import ask as do_ask
    return await run_in_threadpool(do_ask, q)


@app.post("/api/control/{action}", dependencies=[Depends(require_write)])
async def control(action: str):
    def f():
        if action == "pause":
            settings.operator_update("agent", {"paused": True})
            journal.write("system", "Operator paused the agent", "No new research or new positions. Exits and risk "
                          "monitoring continue.", author="operator")
        elif action == "resume":
            settings.operator_update("agent", {"paused": False})
            journal.write("system", "Operator resumed the agent", "Research and trading schedules are active again.",
                          author="operator")
        elif action == "emergency_stop":
            settings.operator_update("agent", {"emergency_stop": True, "paused": True})
            n = oms.cancel_all("Emergency stop by operator.")
            journal.write("risk", "EMERGENCY STOP", f"All agent trading halted; {n} open order(s) cancelled. Positions "
                          "are held. Use 'Flatten all' to close them.", author="operator")
        elif action == "clear_emergency":
            settings.operator_update("agent", {"emergency_stop": False})
            journal.write("system", "Emergency stop cleared", "The agent remains paused until resumed.", author="operator")
        elif action == "flatten_all":
            ids = []
            for s in db.q("SELECT * FROM strategies WHERE status IN ('active','incubating')"):
                ids += live.flatten_strategy(dict(s), "Operator requested flatten-all.", origin="operator")
            journal.write("risk", "Operator: flatten all positions", f"{len(ids)} sell order(s) created; they fill at the "
                          "next usable quotes (NASDAQ orders wait for the session).", author="operator")
        elif action == "reset_drawdown_halt":
            db.set_meta("drawdown_halt", "0")
            journal.write("system", "Drawdown halt reset by operator", "New positions are allowed again within limits.",
                          author="operator")
        elif action == "clear_system_halt":
            db.set_meta("system_halt", "0")
            journal.write("system", "System halt cleared by operator", "Ledger halt cleared.", author="operator")
        elif action.startswith("research_"):
            venue = action.split("_", 1)[1].upper()
            if venue not in settings.get("universe"):
                raise HTTPException(400, "unknown venue")
            db.set_meta("research_requested", venue)
            journal.write("system", f"Operator requested a {venue} research cycle", "It starts within ~15 seconds if the "
                          "worker is running and budget allows.", author="operator")
        else:
            raise HTTPException(404, "unknown action")
        _audit(f"control.{action}")
        return {"ok": True, "controls": settings.get("agent")}
    return await run_in_threadpool(f)


@app.post("/api/strategies/{sid}/retire", dependencies=[Depends(require_write)])
async def retire(sid: int):
    def f():
        s = db.q1("SELECT * FROM strategies WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404)
        live.flatten_strategy(dict(s), "Retired by operator.", origin="operator")
        db.ex("UPDATE strategies SET status='retired', retired_at=?, retire_reason='operator' WHERE id=?", (clock.now(), sid))
        journal.write("decision", f"Operator retired {s['name']} v{s['version']}", "Positions are being closed.",
                      refs={"strategy_id": sid}, author="operator")
        _audit("strategy.retire", {"id": sid})
        return {"ok": True}
    return await run_in_threadpool(f)


@app.post("/api/settings/{group}", dependencies=[Depends(require_write)])
async def update_settings(group: str, request: Request):
    body = await request.json()
    if group == "agent":
        raise HTTPException(400, "use /api/control for pause/stop")

    def f():
        try:
            merged = settings.operator_update(group, body)
        except settings.SettingsError as e:
            raise HTTPException(400, str(e))
        journal.write("system", f"Operator changed {group} settings", json.dumps(body), author="operator")
        return merged
    return await run_in_threadpool(f)


# ------------------------------------------------------------------ static UI
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


def main():
    import uvicorn
    uvicorn.run("quantdesk.api.app:app", host=cfg.bind_host, port=cfg.bind_port, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("QD_TRUSTED_PROXIES", "127.0.0.1"))


if __name__ == "__main__":
    main()
