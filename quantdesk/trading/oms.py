"""Order management: idempotent order creation, risk gate, expiry and cancellation.

Lifecycle:  intent -> submitted -> (partially_filled) -> filled
                   \\-> rejected            \\-> expired | cancelled
The intent row (with its rationale) is written BEFORE risk checks and before any fill
exists, so entry reasons are recorded before outcomes are known. ``client_order_id`` is
derived from (strategy, symbol, signal bar, side): replaying a cycle after a crash cannot
create a second order.
"""
from __future__ import annotations

import hashlib

from .. import clock, db, settings, venues
from ..agent import journal
from ..marketdata import store
from ..venues import venue_of
from . import risk

OPEN = ("submitted", "partially_filled")


def client_order_id(idem_key: str) -> str:
    return "qd-" + hashlib.sha256(idem_key.encode()).hexdigest()[:24]


def _fmt_qty(q: float) -> str:
    return f"{q:,.0f}" if float(q).is_integer() else f"{q:,.6f}".rstrip("0").rstrip(".")


def create_order(*, symbol: str, side: str, qty: float, reason: str, rationale: str,
                 idem_key: str, strategy_id: int | None, signal_snapshot: dict | None = None,
                 origin: str = "agent", bars_ok: bool = True) -> dict:
    """Create (or return the existing) order for this idempotency key."""
    venue = venue_of(symbol)
    coid = client_order_id(idem_key)
    existing = db.q1("SELECT * FROM orders WHERE client_order_id=?", (coid,))
    if existing:
        return dict(existing) | {"duplicate": True}
    now = clock.now()
    with db.tx():
        if db.q1("SELECT 1 FROM orders WHERE client_order_id=?", (coid,)):
            return dict(db.q1("SELECT * FROM orders WHERE client_order_id=?", (coid,))) | {"duplicate": True}
        oid = db.ex("INSERT INTO orders(client_order_id, strategy_id, symbol, venue, side, qty, status, reason, rationale, "
                    "signal_snapshot, origin, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (coid, strategy_id, symbol, venue, side, qty, "intent", reason, rationale,
                     db.dumps(signal_snapshot or {}), origin, now))
    name = _strategy_name(strategy_id)
    journal.write("intent", f"Intent: {side} {_fmt_qty(qty)} {symbol}" + (f" ({name})" if name else ""),
                  f"{rationale}\n\nThis is an intended order, not a fill. It still has to pass the risk engine "
                  f"and execute against live quotes.",
                  refs={"order_id": oid, "strategy_id": strategy_id}, author="rules", dedupe_key=f"intent:{coid}")
    px, _, _ = store.mark_price(symbol)
    open_now = venues.is_open(venue, now)
    dec = risk.check(risk.OrderRequest(symbol, side, qty, px or 0.0, strategy_id, origin), bars_ok=bars_ok,
                     at_submission_time=not open_now)
    ex = settings.get("execution")
    if not dec.approved:
        db.ex("UPDATE orders SET status='rejected', risk_check=?, closed_at=?, status_note=? WHERE id=?",
              (db.dumps(dec.as_dict()), now, dec.reason, oid))
        db.ex("INSERT INTO risk_events(ts, kind, detail, order_id) VALUES(?,?,?,?)",
              (now, "order_rejected", db.dumps(dec.as_dict()), oid))
        journal.write("risk", f"Risk engine rejected {side} {symbol}",
                      f"Order {coid} was rejected: {dec.reason}. No trade was made.",
                      refs={"order_id": oid}, author="rules", dedupe_key=f"reject:{coid}")
        _reset_state_if_entry(strategy_id, symbol, side)
    else:
        eligible = now + ex["latency_sec"]
        if not open_now and venue == "NASDAQ":
            eligible = venues.next_open(now) + ex["nasdaq_trade_delay_min"] * 60 + ex["latency_sec"]
        db.ex("UPDATE orders SET status='submitted', qty=?, risk_check=?, submitted_at=?, eligible_at=?, expires_at=?, "
              "status_note=? WHERE id=?", (dec.qty, db.dumps(dec.as_dict()), now, eligible, eligible + ex["order_ttl_sec"],
                                           dec.reason, oid))
        if dec.resized:
            journal.write("risk", f"Risk engine resized {side} {symbol}",
                          f"{dec.reason}. Submitted {_fmt_qty(dec.qty)} instead of {_fmt_qty(qty)}.",
                          refs={"order_id": oid}, author="rules", dedupe_key=f"resize:{coid}")
    return dict(db.q1("SELECT * FROM orders WHERE id=?", (oid,)))


def _strategy_name(sid):
    if sid is None:
        return None
    r = db.q1("SELECT name, version FROM strategies WHERE id=?", (sid,))
    return f"{r['name']} v{r['version']}" if r else None


def _reset_state_if_entry(strategy_id, symbol, side):
    """A buy that never filled must not leave the state machine thinking it is long."""
    if strategy_id is None or side != "buy":
        return
    from . import ledger
    if ledger.sleeve_qty(strategy_id, symbol) <= 0:
        db.ex("UPDATE strategy_state SET in_position=0, entry_price=NULL, entry_bar=NULL, bars_held=0 "
              "WHERE strategy_id=? AND symbol=?", (strategy_id, symbol))


def close_order(order: dict, status: str, note: str) -> None:
    db.ex("UPDATE orders SET status=?, closed_at=?, status_note=? WHERE id=? AND status IN ('intent','submitted','partially_filled')",
          (status, clock.now(), note, order["id"]))
    if order["filled_qty"] <= 0:
        _reset_state_if_entry(order["strategy_id"], order["symbol"], order["side"])
    verb = {"expired": "expired", "cancelled": "was cancelled"}.get(status, status)
    filled = f" after filling {_fmt_qty(order['filled_qty'])} of {_fmt_qty(order['qty'])}" if order["filled_qty"] else " unfilled"
    journal.write("order", f"Order {verb}: {order['side']} {order['symbol']}",
                  f"{order['client_order_id']} {verb}{filled}. {note}",
                  refs={"order_id": order["id"]}, author="rules", dedupe_key=f"close:{order['id']}")


def open_orders() -> list[dict]:
    return [dict(r) for r in db.q("SELECT * FROM orders WHERE status IN ('submitted','partially_filled') ORDER BY id")]


def cancel_all(note: str, origin_filter: str | None = None) -> int:
    n = 0
    for o in open_orders():
        if origin_filter and o["origin"] != origin_filter:
            continue
        close_order(o, "cancelled", note)
        n += 1
    # intents stuck mid-creation (crash between insert and risk check)
    for o in db.q("SELECT * FROM orders WHERE status='intent'"):
        close_order(dict(o), "cancelled", note)
        n += 1
    return n


def recover_stuck_intents(max_age: float = 120) -> int:
    """After a crash an order may be left in 'intent' (risk check never ran). Cancel it;
    the next trading cycle re-derives what to do from current data."""
    now = clock.now()
    n = 0
    for o in db.q("SELECT * FROM orders WHERE status='intent' AND created_at <= ?", (now - max_age,)):
        close_order(dict(o), "cancelled", "Interrupted before the risk check (process restart); not re-sent.")
        n += 1
    return n
