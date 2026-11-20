"""Paper exchange: fills submitted orders against live quotes. Deterministic.

Fill rules
  * not before ``eligible_at`` (latency; NASDAQ orders wait for the regular session),
  * only against a quote OBSERVED at/after eligible_at and younger than max_quote_age,
  * buy at ask / sell at bid; if the quote is one-sided/too wide (common on the free IEX
    feed) use a fresh last trade +/- the modelled half-spread and label the fill,
  * plus square-root market impact, plus venue fees,
  * per-order size capped at ``max_participation`` x ADV; remainder cancelled,
  * all exposure/cash/loss limits re-checked at the fee-inclusive fill price;
    the current order is excluded from its own pending reservation.
"""
from __future__ import annotations

import logging
import math

from .. import clock, db, settings, venues
from ..agent import journal
from ..marketdata import store
from ..venues import VENUES, qty_decimals
from . import costs as C
from . import ledger, oms, risk
from .liquidity import estimates, lookback_bars

log = logging.getLogger(__name__)


def liquidity(symbol: str, venue: str) -> tuple[float | None, float | None]:
    """(ADV in USD, daily sigma) from the last 20 days of stored bars."""
    v = VENUES[venue]
    # Only completed bars available at this fill can contribute.
    b = store.load_bars(symbol, v.timeframe, end=int(clock.now() - v.bar_seconds),
                        limit=lookback_bars(v.bars_per_day) + 1)
    adv, sigma = estimates(b["close"], b["volume"], v.bars_per_day)
    a, s = float(adv[-1]), float(sigma[-1])
    return a if math.isfinite(a) else None, s if math.isfinite(s) else None


def _price_basis(order: dict, q: dict | None, now: float, lim: dict, cost_cfg: dict):
    """Return (ref_price, ref_dict) or (None, reason)."""
    if not q:
        return None, "no quote"
    if q["received_at"] is None or q["received_at"] < order["eligible_at"]:
        return None, "waiting for a quote observed after the order became eligible"
    age = now - q["ts"]
    bid, ask = q["bid"], q["ask"]
    if bid and ask and math.isfinite(bid) and math.isfinite(ask) and ask >= bid > 0 and 0 <= age <= lim["max_quote_age_sec"]:
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 1e4
        if spread_bps <= lim["max_spread_bps"]:
            ref = ask if order["side"] == "buy" else bid
            return ref, {"bid": bid, "ask": ask, "ts": q["ts"], "basis": f"{q['source']} quote",
                         "spread_bps": round(spread_bps, 2)}
    last, lts = q["last"], q["last_ts"]
    if last and lts and math.isfinite(last) and last > 0 and 0 <= now - lts <= min(60, lim["max_quote_age_sec"]):
        hs = cost_cfg["backtest_half_spread_bps"]
        ref = last * (1 + hs / 1e4) if order["side"] == "buy" else last * (1 - hs / 1e4)
        return ref, {"bid": bid, "ask": ask, "ts": lts, "basis": f"{q['source']} last trade + modelled {hs}bp half-spread",
                     "spread_bps": 2 * hs, "notes": "quote unusable (one-sided, wide or stale); used fresh last trade"}
    return None, f"quote unusable (age {age:.0f}s) and no fresh last trade"


def process_orders() -> list[dict]:
    # No network calls here. Keep final checks and booking atomic with operator writes,
    # so an order cannot be filled from a stale pre-cancellation object.
    with db.tx():
        return _process_orders()


def _process_orders() -> list[dict]:
    """Try to fill every open order. Returns fill events."""
    now = clock.now()
    agent = settings.get("agent")
    lim = settings.get("risk")
    costs_all = settings.get("costs")
    events = []
    conn = db.connect()
    for o in oms.open_orders():
        # One bad order must not roll back every other fill/cancellation in this tick,
        # nor stop the mark-to-market and drawdown monitor that run after it.
        conn.execute("SAVEPOINT process_order")
        try:
            ev = _process_order(o, now, agent, lim, costs_all)
        except Exception as e:
            conn.execute("ROLLBACK TO process_order")
            conn.execute("RELEASE process_order")
            log.error("order %s could not be processed: %s", o["id"], e, exc_info=True)
            _cancel_failed(o["id"], e)
            continue
        conn.execute("RELEASE process_order")
        if ev is not None:
            events.append(ev)
    return events


def _cancel_failed(order_id: int, err: Exception) -> None:
    row = db.q1("SELECT * FROM orders WHERE id=?", (order_id,))
    if row is None or row["status"] not in ("intent", "submitted", "partially_filled"):
        return
    try:
        oms.close_order(dict(row), "cancelled", f"Could not be processed ({type(err).__name__}: {err}); "
                                                "no fill booked. The next decision cycle re-evaluates it.")
    except Exception:
        log.error("could not cancel order %s: %s", order_id, err, exc_info=True)


def _process_order(o: dict, now: float, agent: dict, lim: dict, costs_all: dict) -> dict | None:
    """Expire, cancel or fill one open order. Returns a fill event or None."""
    venue = o["venue"]
    if o["expires_at"] and now > o["expires_at"]:
        oms.close_order(o, "expired", "Not filled within its time-to-live (no usable quote or market closed). "
                                      "The next decision cycle re-evaluates with fresh data.")
        return None
    if now < (o["eligible_at"] or 0):
        return None
    if not venues.is_open(venue, now):
        return None
    if agent.get("emergency_stop") and o["origin"] != "operator":
        oms.close_order(o, "cancelled", "Emergency stop is active.")
        return None
    if o["side"] == "buy":
        st = db.q1("SELECT status FROM strategies WHERE id=?", (o["strategy_id"],))
        if o["strategy_id"] is not None and (not st or st["status"] != "active"
                or db.get_meta(f"flatten_pending:{o['strategy_id']}")):
            oms.close_order(o, "cancelled", "Strategy is no longer active or is being flattened.")
            return None
        day_flag = db.get_meta(f"daily_loss_flag:{int(now // 86400)}") == "1"
        if (agent.get("paused") or db.get_meta("drawdown_halt") == "1" or day_flag
                or db.get_meta("system_halt") == "1"):
            oms.close_order(o, "cancelled", "New risk blocked at fill time (paused, drawdown halt or daily loss limit).")
            return None
    cost_cfg = costs_all[venue]
    frac = bool(settings.get("execution").get("fractional_shares"))
    q = store.get_quote(o["symbol"])
    ref_px, ref = _price_basis(o, q, now, lim, cost_cfg)
    if ref_px is None:
        return None  # wait for a usable quote until TTL
    remaining = o["qty"] - o["filled_qty"]
    adv, sigma = liquidity(o["symbol"], venue)
    cap_note = None
    if adv:
        max_notional = lim["max_participation"] * adv
        if remaining * ref_px > max_notional:
            capped = max_notional / ref_px
            dec = qty_decimals(venue, frac)
            capped = math.floor(capped * 10 ** dec) / 10 ** dec
            cap_note = (f"Liquidity cap: order {remaining:g} > {lim['max_participation']:.2%} of ADV "
                        f"(${adv:,.0f}); filled {capped:g}, remainder cancelled.")
            remaining = capped
    if o["side"] == "sell":
        remaining = min(remaining, ledger.sleeve_qty(o["strategy_id"] or 0, o["symbol"]))
    if remaining <= 0:
        oms.close_order(o, "cancelled", cap_note or "Nothing left to fill.")
        return None
    imp = C.impact_bps(remaining * ref_px, adv, sigma, cost_cfg)
    px = C.apply_slippage(o["side"], ref_px, imp)
    fee = C.fees(venue, o["side"], remaining, px, cost_cfg)
    if not math.isfinite(px) or px <= 0 or not math.isfinite(fee) or fee < 0:
        oms.close_order(o, "cancelled", "Cost model produced an invalid fill price or fee; no fill booked.")
        return None
    if o["side"] == "buy":
        # The initial impact price is retained if resized: this is conservative,
        # and avoids a sizing/impact feedback loop. Include fees in every cap.
        req = risk.OrderRequest(o["symbol"], "buy", remaining, px + fee / remaining,
                                o["strategy_id"], o["origin"])
        snap = risk.snapshot_for(req, exclude_order_id=o["id"])
        # _price_basis already validated this exact quote/last-trade fallback.
        snap.quote_ok, snap.quote_age = True, now - ref["ts"]
        decision = risk.evaluate(req, snap, lim, check_order_rate=False, fractional_shares=frac)
        db.ex("INSERT INTO risk_events(ts, kind, detail, order_id) VALUES(?,?,?,?)",
              (now, "fill_risk_check", db.dumps(decision.as_dict()), o["id"]))
        if not decision.approved:
            oms.close_order(o, "cancelled", f"Fill-time risk check: {decision.reason}")
            return None
        if decision.resized:
            cap_note = ((cap_note + " ") if cap_note else "") + f"Fill-time risk check: {decision.reason}; remainder cancelled."
            remaining = decision.qty
            fee = C.fees(venue, "buy", remaining, px, cost_cfg)
    ref["slippage_bps"] = round(imp, 3)
    if cap_note:
        ref["notes"] = ((ref.get("notes") or "") + " " + cap_note).strip()
    fid = ledger.apply_fill(o, remaining, px, fee, ref)
    o2 = dict(db.q1("SELECT * FROM orders WHERE id=?", (o["id"],)))
    if cap_note and o2["status"] != "filled":
        oms.close_order(o2, "cancelled", cap_note)
    lag = now - (o["submitted_at"] or now)
    mid_txt = f"bid {ref['bid']:.6g} / ask {ref['ask']:.6g}" if ref.get("bid") and ref.get("ask") else "no two-sided quote"
    journal.write(
        "fill", f"Filled: {o['side']} {oms._fmt_qty(remaining)} {o['symbol']} @ {px:,.4f}",
        f"Simulated fill at {px:,.4f} ({ref['basis']}; {mid_txt}). Impact/slippage {imp:.1f} bp, fees "
        f"${fee:,.2f}, {lag:,.0f}s after submission. Order {o['client_order_id']} is now {o2['status']}.",
        refs={"order_id": o["id"], "fill_id": fid, "strategy_id": o["strategy_id"]}, author="rules",
        dedupe_key=f"fill:{fid}")
    return {"order_id": o["id"], "fill_id": fid, "qty": remaining, "price": px, "fee": fee}
