"""Live (paper) execution of approved strategies on each newly closed bar."""
from __future__ import annotations

import ast
import datetime as dt
import math

import numpy as np

from .. import clock, db, settings
from ..agent import journal
from ..marketdata import freshness
from ..research import dsl
from ..research.backtest import StepView
from ..research.experiment import build_panel
from ..research.strategy import PosState, compute_signals, entry_weight, step
from ..venues import VENUES
from . import ledger, oms


def _bar_label(venue: str, ts: int) -> str:
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return d.strftime("%Y-%m-%d %H:%M UTC") + " 15-minute bar"


def explain(expr: str, panel: dict, params: dict, t: int, j: int, ppy: float, market_col) -> list[str]:
    """Values of each comparison inside an expression at bar t for symbol column j."""
    out = []
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = ast.unparse(node.left), ast.unparse(node.comparators[0])
            try:
                lv = dsl.evaluate(left, panel, params, ppy, market_col)
                rv = dsl.evaluate(right, panel, params, ppy, market_col)
                lval = lv[t, j] if isinstance(lv, np.ndarray) else lv
                rval = rv[t, j] if isinstance(rv, np.ndarray) else rv
                op = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}[type(node.ops[0])]
                truth = {"<": lval < rval, "<=": lval <= rval, ">": lval > rval, ">=": lval >= rval}[op]
                out.append(f"{left} = {lval:.6g} {op} {right} = {rval:.6g} -> {'true' if truth else 'false'}")
            except Exception:
                continue
            if len(out) >= 6:
                break
    return out


def _load_states(sid: int, universe: list[str]) -> dict[str, PosState]:
    rows = {r["symbol"]: r for r in db.q("SELECT * FROM strategy_state WHERE strategy_id=?", (sid,))}
    out = {}
    for s in universe:
        r = rows.get(s)
        out[s] = PosState(bool(r["in_position"]), r["entry_price"], r["entry_bar"], r["bars_held"]) if r else PosState()
    return out


def _save_states(sid: int, states: dict[str, PosState], bar: int) -> None:
    with db.tx():
        for s, st in states.items():
            db.ex("INSERT INTO strategy_state(strategy_id, symbol, in_position, entry_price, entry_bar, bars_held, updated_bar) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(strategy_id, symbol) DO UPDATE SET in_position=excluded.in_position, "
                  "entry_price=excluded.entry_price, entry_bar=excluded.entry_bar, bars_held=excluded.bars_held, "
                  "updated_bar=excluded.updated_bar",
                  (sid, s, int(st.in_position), st.entry_price, st.entry_bar, st.bars_held, bar))


def evaluate_strategy(strat: dict) -> dict:
    sid = strat["id"]
    current = db.q1("SELECT status FROM strategies WHERE id=?", (sid,))
    if not current or current["status"] != "active" or db.get_meta(f"flatten_pending:{sid}"):
        return {"status": "inactive"}
    spec = db.loads(strat["spec"])
    params = spec.get("fixed_params") or {}
    venue = strat["venue"]
    v = VENUES[venue]
    now = clock.now()
    expected = freshness.expected_last_bar(venue, now)
    last_done = strat["last_signal_bar"] or 0
    if last_done >= expected:
        return {"status": "up_to_date"}
    bench = settings.get("benchmarks")[venue]
    panel = build_panel(venue, spec["universe"], bench, end_ts=expected)
    if len(panel["ts"]) == 0 or int(panel["ts"][-1]) != expected:
        journal.write("data", f"{strat['name']}: waiting for the {_bar_label(venue, expected)}",
                      f"The bar needed for today's decision is not stored yet (latest: "
                      f"{_bar_label(venue, int(panel['ts'][-1])) if len(panel['ts']) else 'none'}). "
                      "No signals are evaluated and no orders are sent until it arrives.",
                      refs={"strategy_id": sid}, author="rules", dedupe_key=f"wait:{sid}:{expected}")
        return {"status": "data_missing"}
    t = len(panel["ts"]) - 1
    syms = panel["symbols"]
    missing = [s for s in spec["universe"] if not np.isfinite(panel["close"][t, syms.index(s)])]
    if missing:
        journal.write("data", f"{strat['name']}: incomplete data for {_bar_label(venue, expected)}",
                      f"No closing price for {', '.join(missing)}. Decisions for this bar are skipped for the whole "
                      "strategy rather than acting on partial data.", refs={"strategy_id": sid}, author="rules",
                      dedupe_key=f"partial:{sid}:{expected}")
        return {"status": "data_partial", "missing": missing}
    if last_done:
        missed = int(((panel["ts"] > last_done) & (panel["ts"] < expected)).sum())
        if missed > 0:
            journal.write("system", f"{strat['name']}: {missed} bar(s) not evaluated during downtime",
                          f"The worker was not running for {missed} bar close(s) since {_bar_label(venue, last_done)}. "
                          "Those decisions are not replayed (no back-dated trades); position states are carried forward "
                          "and the latest bar is evaluated now.", refs={"strategy_id": sid}, author="rules",
                          dedupe_key=f"missed:{sid}:{expected}")
    sig = compute_signals(spec, params, panel)
    cols = [syms.index(s) for s in spec["universe"]]
    states = _load_states(sid, spec["universe"])
    st_list = [states[s] for s in spec["universe"]]
    close_row = panel["close"][t, cols]
    res = step(spec, st_list, t, StepView(sig, cols), close_row)
    for k, s in enumerate(spec["universe"]):
        if st_list[k].in_position and st_list[k].entry_bar == t:
            st_list[k].entry_bar = expected  # store bar timestamp, not panel index
    _save_states(sid, states, expected)
    market_col = syms.index(bench) if bench in syms else None
    sleeve = ledger.sleeve_equity(sid, strat["allocation"])
    created = []
    label = _bar_label(venue, expected)
    for k, reason in res.exit:
        s = spec["universe"][k]
        qty = ledger.sleeve_qty(sid, s)
        if qty <= 0:
            continue
        ev = explain(spec.get("exit") or "", panel, params, t, cols[k], v.periods_per_year, market_col) \
            if reason == "exit rule" else []
        pos = db.q1("SELECT avg_cost FROM positions WHERE strategy_id=? AND symbol=?", (sid, s))
        pnl = (close_row[k] / pos["avg_cost"] - 1) if pos and pos["avg_cost"] else None
        rationale = (f"Exit signal for {s} at the {label} close ({close_row[k]:.6g}): {reason}."
                     + (f" Position is {pnl:+.1%} vs average cost {pos['avg_cost']:.6g}." if pnl is not None else "")
                     + ("\nRule values: " + "; ".join(ev) if ev else ""))
        o = oms.create_order(symbol=s, side="sell", qty=qty, reason=reason, rationale=rationale,
                             idem_key=f"{sid}:{s}:{expected}:sell", strategy_id=sid,
                             signal_snapshot={"bar": expected, "close": float(close_row[k]), "rule_values": ev})
        created.append(o["id"])
    thesis = strat["thesis"]
    for k in res.enter:
        s = spec["universe"][k]
        col = cols[k]
        w = entry_weight(spec, sig.vol[t, col])
        notional = w * sleeve["equity"]
        px = close_row[k]
        qty = notional / px
        qty = math.floor(qty) if v.qty_decimals == 0 else math.floor(qty * 1e8) / 1e8
        if qty <= 0 or notional < 1:
            states[s].in_position = False
            _save_states(sid, {s: states[s]}, expected)
            journal.write("decision", f"{strat['name']}: entry signal in {s} too small to trade",
                          f"Target weight {w:.1%} of ${sleeve['equity']:,.0f} sleeve is below one share/unit at {px:.6g}.",
                          refs={"strategy_id": sid}, author="rules", dedupe_key=f"small:{sid}:{s}:{expected}")
            continue
        ev = explain(spec.get("entry") or "", panel, params, t, col, v.periods_per_year, market_col)
        exits = []
        if spec.get("exit"):
            exits.append(f"exit rule `{spec['exit']}` with {params}")
        if spec.get("stop_loss_pct"):
            exits.append(f"stop-loss at -{spec['stop_loss_pct']:.0%} from fill (on a bar close)")
        if spec.get("take_profit_pct"):
            exits.append(f"take-profit at +{spec['take_profit_pct']:.0%}")
        if spec.get("max_hold_bars"):
            exits.append(f"time stop after {spec['max_hold_bars']} bars")
        rationale = (f"Entry signal for {s} at the {label} close ({px:.6g}). Sizing {w:.1%} of the strategy's "
                     f"${sleeve['equity']:,.0f} capital (~${notional:,.0f}).\n"
                     + ("Rule values: " + "; ".join(ev) + "\n" if ev else "")
                     + f"Thesis (recorded at approval): {thesis}\nWould exit on: " + "; ".join(exits) + ".")
        o = oms.create_order(symbol=s, side="buy", qty=qty, reason="entry", rationale=rationale,
                             idem_key=f"{sid}:{s}:{expected}:buy", strategy_id=sid,
                             signal_snapshot={"bar": expected, "close": float(px), "weight": w, "rule_values": ev})
        created.append(o["id"])
    db.ex("UPDATE strategies SET last_signal_bar=? WHERE id=?", (expected, sid))
    reconcile_sleeve(strat, expected)
    return {"status": "evaluated", "bar": expected, "orders": created,
            "entries": [spec["universe"][k] for k in res.enter], "exits": [spec["universe"][k] for k, _ in res.exit]}


def allocation_room(equity: float | None = None) -> float:
    """Capital still allocatable under the operator's total-allocation limit."""
    eq = ledger.equity_breakdown()["equity"] if equity is None else equity
    used = db.q1("SELECT COALESCE(SUM(allocation),0) a FROM strategies WHERE status IN ('active','incubating')")["a"]
    return max(0.0, settings.get("risk")["max_total_allocation_pct"] * eq - used)


def activate_waiting() -> list[int]:
    """Start strategies that were approved while the total-allocation limit was full."""
    lim = settings.get("risk")
    frac = settings.get("research")["incubation_fraction"]
    out = []
    for s in db.q("SELECT * FROM strategies WHERE status='incubating' AND allocation <= 0 ORDER BY id"):
        start = min(s["approved_allocation"] * (1.0 if s["probe"] else frac), allocation_room())
        if start < lim["min_order_usd"]:
            continue
        now = clock.now()
        db.ex("UPDATE strategies SET status='active', allocation=?, activated_at=?, last_signal_bar=? WHERE id=?",
              (start, now, freshness.expected_last_bar(s["venue"], now), s["id"]))
        journal.write("decision", f"Activated {s['name']} v{s['version']}",
                      f"Capital freed up under the total-allocation limit, so this approved strategy now trades with "
                      f"${start:,.0f} (approved ${s['approved_allocation']:,.0f}), starting on the next bar close.",
                      refs={"strategy_id": s["id"]}, author="rules")
        out.append(s["id"])
    return out


def reconcile_sleeve(strat: dict, bar: int) -> None:
    """Keep state machine and holdings consistent (e.g. after rejected/expired orders)."""
    sid = strat["id"]
    states = {r["symbol"]: r for r in db.q("SELECT * FROM strategy_state WHERE strategy_id=?", (sid,))}
    open_syms = {(r["symbol"], r["side"]) for r in db.q(
        "SELECT symbol, side FROM orders WHERE strategy_id=? AND status IN ('intent','submitted','partially_filled')", (sid,))}
    for p in ledger.sleeve_positions(sid):
        st = states.get(p["symbol"])
        if (st is None or not st["in_position"]) and (p["symbol"], "sell") not in open_syms:
            oms.create_order(symbol=p["symbol"], side="sell", qty=p["qty"], reason="reconcile",
                             rationale=f"The strategy's rules are flat in {p['symbol']} but {p['qty']:g} is still held "
                                       "(an earlier exit did not complete). Closing it.",
                             idem_key=f"{sid}:{p['symbol']}:{bar}:reconcile", strategy_id=sid)


def flatten_strategy(strat: dict, reason: str, origin: str = "agent") -> list[int]:
    """Durable exit request: cancel entries, keep retrying until the sleeve is flat."""
    sid = strat["id"]
    with db.tx():
        for row in db.q("SELECT * FROM orders WHERE strategy_id=? "
                        "AND status IN ('intent','submitted','partially_filled')", (sid,)):
            oms.close_order(dict(row), "cancelled", "Superseded by flatten request: " + reason)
        db.set_meta(f"flatten_pending:{sid}", db.dumps({"reason": reason, "origin": origin}))
        db.ex("UPDATE strategy_state SET in_position=0, entry_price=NULL, entry_bar=NULL, bars_held=0 WHERE strategy_id=?", (sid,))
        return _submit_flatten(sid, reason, origin)


def _submit_flatten(sid: int, reason: str, origin: str) -> list[int]:
    out = []
    pending = {r["symbol"] for r in db.q(
        "SELECT symbol FROM orders WHERE strategy_id=? AND side='sell' "
        "AND status IN ('intent','submitted','partially_filled')", (sid,))}
    for p in ledger.sleeve_positions(sid):
        if p["symbol"] in pending:
            continue
        o = oms.create_order(symbol=p["symbol"], side="sell", qty=p["qty"], reason="flatten",
                             rationale=reason, idem_key=f"{sid}:{p['symbol']}:flatten:{int(clock.now() // 60)}",
                             strategy_id=sid, origin=origin)
        out.append(o["id"])
    return out


def reconcile_flattening() -> None:
    """Retry exits without needing another signal bar, including retired strategies."""
    with db.tx():
        # Recover retirees from older versions that did not persist an exit request.
        for s in db.q("SELECT * FROM strategies s WHERE status='retired' "
                      "AND EXISTS(SELECT 1 FROM positions p WHERE p.strategy_id=s.id AND p.qty>1e-12) "
                      "AND NOT EXISTS(SELECT 1 FROM meta m WHERE m.key='flatten_pending:' || s.id)"):
            flatten_strategy(dict(s), "Close remaining holdings of a retired strategy.")
        for r in db.q("SELECT key, value FROM meta WHERE key LIKE 'flatten_pending:%'"):
            sid = int(r["key"].split(":", 1)[1])
            req = db.loads(r["value"])
            if not ledger.sleeve_positions(sid):
                db.ex("DELETE FROM meta WHERE key=?", (r["key"],))
                continue
            _submit_flatten(sid, req["reason"], req["origin"])


def retire_strategy(strat: dict, reason: str, origin: str = "agent") -> list[int]:
    """Retirement and cancellation are one writer transaction, never a half-retired book."""
    with db.tx():
        db.ex("UPDATE strategies SET status='retired', retired_at=?, retire_reason=? WHERE id=?",
              (clock.now(), reason, strat["id"]))
        return flatten_strategy(strat, reason, origin)
