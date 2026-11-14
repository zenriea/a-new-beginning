"""Independent pre-trade risk engine. Deterministic; no LLM involvement.

``evaluate`` is a pure function of (order request, account snapshot, limits). It may
approve, shrink (resize a buy to fit limits) or reject. Risk-reducing orders (sells) are
allowed even when loss limits are hit; only the emergency stop blocks them (except the
operator's own flatten orders).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .. import clock, db, settings, venues
from ..marketdata import store
from ..venues import VENUES, venue_of
from . import ledger


@dataclass
class OrderRequest:
    symbol: str
    side: str            # buy | sell
    qty: float
    price_hint: float    # current reference price for notional checks
    strategy_id: int | None
    origin: str = "agent"  # agent | operator


@dataclass
class Snapshot:
    now: float
    equity: float
    cash: float
    gross: float
    crypto_gross: float
    symbol_value: dict
    sleeve_qty: float
    strategy_allocation: float | None
    strategy_invested: float
    peak_equity: float
    day_start_equity: float
    orders_last_hour: int
    quote_age: float | None
    quote_ok: bool
    market_open: bool
    paused: bool
    emergency_stop: bool
    drawdown_halt: bool
    bars_ok: bool = True
    open_buy_notional: float = 0.0


@dataclass
class Decision:
    approved: bool
    qty: float
    checks: list = field(default_factory=list)
    reason: str = ""
    resized: bool = False

    def as_dict(self):
        return {"approved": self.approved, "qty": self.qty, "checks": self.checks,
                "reason": self.reason, "resized": self.resized}


def evaluate(req: OrderRequest, s: Snapshot, lim: dict) -> Decision:
    checks = []
    venue = venue_of(req.symbol)
    decimals = VENUES[venue].qty_decimals

    def note(name, ok, detail):
        checks.append({"check": name, "pass": bool(ok), "detail": detail})
        return ok

    def reject(reason):
        return Decision(False, 0.0, checks, reason)

    reducing = req.side == "sell"
    if req.qty <= 0 or not math.isfinite(req.qty):
        note("qty_positive", False, f"qty={req.qty}")
        return reject("quantity must be positive")
    # 1. kill switches
    if s.emergency_stop and not (req.origin == "operator" and reducing):
        note("emergency_stop", False, "emergency stop is active")
        return reject("emergency stop active: all agent trading halted")
    note("emergency_stop", True, "off")
    if not reducing and s.paused:
        note("paused", False, "agent paused by operator")
        return reject("agent is paused: no new risk")
    if not reducing and s.drawdown_halt:
        note("drawdown_halt", False, "max drawdown breached; operator must reset")
        return reject("drawdown halt active")
    # 2. market + data
    if not note("market_open", s.market_open, "session open" if s.market_open else "venue closed"):
        return reject(f"{venue} is closed")
    if not note("quote_fresh", s.quote_ok and s.quote_age is not None and s.quote_age <= lim["max_quote_age_sec"],
                f"quote age {s.quote_age if s.quote_age is None else round(s.quote_age, 1)}s "
                f"(max {lim['max_quote_age_sec']}s)"):
        return reject("market data stale or unusable")
    if not reducing and not note("bars_current", s.bars_ok, "signal bars current" if s.bars_ok else "bars stale"):
        return reject("bar data stale")
    # 3. order rate
    if not note("order_rate", s.orders_last_hour < lim["max_orders_per_hour"],
                f"{s.orders_last_hour} orders in the last hour (max {lim['max_orders_per_hour']})"):
        return reject("order rate limit")
    px = req.price_hint
    if not px or px <= 0:
        note("price", False, "no reference price")
        return reject("no reference price")
    qty = req.qty
    if reducing:
        if not note("no_short", qty <= s.sleeve_qty + 1e-9, f"sell {qty:g} vs sleeve {s.sleeve_qty:g}"):
            qty = s.sleeve_qty
            if qty <= 0:
                return reject("nothing to sell (shorting not allowed)")
        if not note("min_notional", qty * px >= min(lim["min_order_usd"], s.sleeve_qty * px) - 1e-9,
                    f"${qty * px:,.2f}"):
            return reject("below minimum order size")
        return Decision(True, qty, checks, "risk-reducing order approved", qty != req.qty)

    # ---- buys: compute the largest notional every limit allows
    eq = s.equity
    caps = {
        "max_order_usd": lim["max_order_usd"],
        "cash": max(0.0, s.cash - s.open_buy_notional) * 0.995,
        "max_position_pct": lim["max_position_pct"] * eq - s.symbol_value.get(req.symbol, 0.0),
        "max_gross_pct": lim["max_gross_pct"] * eq - s.gross - s.open_buy_notional,
    }
    if venue == "CRYPTO":
        caps["max_crypto_pct"] = lim["max_crypto_pct"] * eq - s.crypto_gross
    if s.strategy_allocation is not None:
        caps["strategy_allocation"] = s.strategy_allocation - s.strategy_invested
    # 4. loss limits block new risk
    day_loss = (s.day_start_equity - eq) / s.day_start_equity if s.day_start_equity else 0.0
    if not note("daily_loss", day_loss < lim["daily_loss_limit_pct"],
                f"day P&L {-day_loss:+.2%} (limit -{lim['daily_loss_limit_pct']:.2%})"):
        return reject("daily loss limit reached: no new risk today")
    dd = (s.peak_equity - eq) / s.peak_equity if s.peak_equity else 0.0
    if not note("drawdown", dd < lim["max_drawdown_pct"],
                f"drawdown {dd:.2%} (limit {lim['max_drawdown_pct']:.2%})"):
        return reject("max drawdown reached")
    want = qty * px
    allowed = min(want, *caps.values())
    for k, v in caps.items():
        note(k, v >= want - 1e-6, f"room ${max(v, 0):,.2f} vs order ${want:,.2f}")
    if allowed < lim["min_order_usd"]:
        binding = min(caps, key=caps.get)
        return reject(f"no room under {binding} (room ${max(caps[binding], 0):,.2f})")
    new_qty = allowed / px
    new_qty = math.floor(new_qty * 10 ** decimals) / 10 ** decimals
    if new_qty <= 0 or new_qty * px < lim["min_order_usd"]:
        return reject("resized order below minimum size")
    resized = new_qty < qty - 1e-12
    if resized:
        binding = min(caps, key=caps.get)
        reason = f"resized from {qty:g} to {new_qty:g} by {binding}"
    else:
        reason = "approved"
    return Decision(True, min(new_qty, qty), checks, reason, resized)


# ------------------------------------------------------------------ state gathering
def peak_and_day_start(now: float, equity: float) -> tuple[float, float]:
    r = db.q1("SELECT MAX(peak_equity) p FROM equity_snapshots")
    peak = max(float(r["p"]) if r and r["p"] else equity, equity)
    day0 = now - (now % 86400)
    d = db.q1("SELECT equity FROM equity_snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1", (day0,))
    if d is None:
        d = db.q1("SELECT equity FROM equity_snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT 1", (day0,))
    return peak, float(d["equity"]) if d else equity


def snapshot_for(req: OrderRequest, bars_ok: bool = True) -> Snapshot:
    now = clock.now()
    eb = ledger.equity_breakdown()
    agent = settings.get("agent")
    peak, day0 = peak_and_day_start(now, eb["equity"])
    q = store.get_quote(req.symbol)
    age = (now - q["ts"]) if q else None
    quote_ok = bool(q and ((q["bid"] and q["ask"] and q["ask"] >= q["bid"] > 0) or q["last"]))
    alloc, invested = None, 0.0
    if req.strategy_id is not None:
        st = db.q1("SELECT allocation FROM strategies WHERE id=?", (req.strategy_id,))
        alloc = float(st["allocation"]) if st else 0.0
        for p in ledger.sleeve_positions(req.strategy_id):
            m, _, _ = store.mark_price(p["symbol"])
            invested += p["qty"] * (m or p["avg_cost"])
        for pend in db.q("SELECT symbol, qty-filled_qty AS rem FROM orders WHERE strategy_id=? AND side='buy' "
                         "AND status IN ('submitted','partially_filled')", (req.strategy_id,)):
            invested += pend["rem"] * (store.mark_price(pend["symbol"])[0] or 0)
    open_buys = 0.0
    for o in db.q("SELECT symbol, qty-filled_qty AS rem FROM orders WHERE side='buy' AND status IN ('submitted','partially_filled')"):
        m, _, _ = store.mark_price(o["symbol"])
        open_buys += (m or 0) * o["rem"]
    n_hour = db.q1("SELECT COUNT(*) n FROM orders WHERE created_at > ? AND status != 'rejected'", (now - 3600,))["n"]
    return Snapshot(
        now=now, equity=eb["equity"], cash=eb["cash"], gross=eb["gross"], crypto_gross=eb["crypto_gross"],
        symbol_value={s: v["value"] for s, v in eb["positions"].items()},
        sleeve_qty=ledger.sleeve_qty(req.strategy_id, req.symbol) if req.strategy_id is not None else 0.0,
        strategy_allocation=alloc, strategy_invested=invested, peak_equity=peak, day_start_equity=day0,
        orders_last_hour=int(n_hour), quote_age=age, quote_ok=quote_ok,
        market_open=venues.is_open(venue_of(req.symbol), now),
        paused=bool(agent.get("paused")) or db.get_meta("system_halt") == "1",
        emergency_stop=bool(agent.get("emergency_stop")), drawdown_halt=db.get_meta("drawdown_halt") == "1",
        bars_ok=bars_ok, open_buy_notional=open_buys)


def check(req: OrderRequest, bars_ok: bool = True, at_submission_time: bool = False) -> Decision:
    """``at_submission_time``: NASDAQ orders decided after the close are checked again at
    fill time; at decision time we skip the market-open and quote-age checks."""
    snap = snapshot_for(req, bars_ok)
    lim = settings.get("risk")
    if at_submission_time:
        snap.market_open = True
        snap.quote_ok = True
        snap.quote_age = 0.0
    return evaluate(req, snap, lim)


def monitor() -> list[dict]:
    """Account-level watch run every tick: sets the drawdown halt and reports breaches."""
    events = []
    now = clock.now()
    eb = ledger.equity_breakdown()
    lim = settings.get("risk")
    peak, day0 = peak_and_day_start(now, eb["equity"])
    dd = (peak - eb["equity"]) / peak if peak else 0
    if dd >= lim["max_drawdown_pct"] and db.get_meta("drawdown_halt") != "1":
        db.set_meta("drawdown_halt", "1")
        db.ex("INSERT INTO risk_events(ts, kind, detail) VALUES(?,?,?)",
              (now, "drawdown_halt", db.dumps({"drawdown": dd, "peak": peak, "equity": eb["equity"]})))
        events.append({"kind": "drawdown_halt", "drawdown": dd, "equity": eb["equity"], "peak": peak})
    day_key = f"daily_loss_flag:{int(now // 86400)}"
    loss = (day0 - eb["equity"]) / day0 if day0 else 0
    if loss >= lim["daily_loss_limit_pct"] and db.get_meta(day_key) != "1":
        db.set_meta(day_key, "1")
        db.ex("INSERT INTO risk_events(ts, kind, detail) VALUES(?,?,?)",
              (now, "daily_loss_limit", db.dumps({"loss": loss, "equity": eb["equity"]})))
        events.append({"kind": "daily_loss_limit", "loss": loss, "equity": eb["equity"]})
    return events
