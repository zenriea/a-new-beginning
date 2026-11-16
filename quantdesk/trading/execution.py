"""Paper exchange: fills submitted orders against live quotes. Deterministic.

Fill rules
  * not before ``eligible_at`` (latency; NASDAQ orders wait for the regular session),
  * only against a quote OBSERVED at/after eligible_at and younger than max_quote_age,
  * buy at ask / sell at bid; if the quote is one-sided/too wide (common on the free IEX
    feed) use a fresh last trade +/- the modelled half-spread and label the fill,
  * plus square-root market impact, plus venue fees,
  * per-order size capped at ``max_participation`` x ADV; remainder cancelled,
  * kill switches re-checked at fill time; buys also re-checked against cash.
"""
from __future__ import annotations

import math

import numpy as np

from .. import clock, db, settings, venues
from ..agent import journal
from ..marketdata import store
from ..venues import VENUES
from . import costs as C
from . import ledger, oms


def liquidity(symbol: str, venue: str) -> tuple[float | None, float | None]:
    """(ADV in USD, daily sigma) from the last 20 days of stored bars."""
    v = VENUES[venue]
    n = int(20 * v.bars_per_day)
    b = store.load_bars(symbol, v.timeframe, limit=n + 1)
    if len(b["close"]) < 10:
        return None, None
    dollar = b["close"] * b["volume"]
    bpd = v.bars_per_day
    adv = float(np.mean(dollar)) * bpd
    lr = np.diff(np.log(b["close"]))
    sigma = float(np.std(lr, ddof=1) * math.sqrt(bpd)) if lr.size > 2 else None
    return adv, sigma


def _price_basis(order: dict, q: dict | None, now: float, lim: dict, cost_cfg: dict):
    """Return (ref_price, ref_dict) or (None, reason)."""
    if not q:
        return None, "no quote"
    if q["received_at"] is None or q["received_at"] < order["eligible_at"]:
        return None, "waiting for a quote observed after the order became eligible"
    age = now - q["ts"]
    bid, ask = q["bid"], q["ask"]
    if bid and ask and ask >= bid > 0 and age <= lim["max_quote_age_sec"]:
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 1e4
        if spread_bps <= lim["max_spread_bps"]:
            ref = ask if order["side"] == "buy" else bid
            return ref, {"bid": bid, "ask": ask, "ts": q["ts"], "basis": f"{q['source']} quote",
                         "spread_bps": round(spread_bps, 2)}
    last, lts = q["last"], q["last_ts"]
    if last and lts and now - lts <= 60:
        hs = cost_cfg["backtest_half_spread_bps"]
        ref = last * (1 + hs / 1e4) if order["side"] == "buy" else last * (1 - hs / 1e4)
        return ref, {"bid": bid, "ask": ask, "ts": lts, "basis": f"{q['source']} last trade + modelled {hs}bp half-spread",
                     "spread_bps": 2 * hs, "notes": "quote unusable (one-sided, wide or stale); used fresh last trade"}
    return None, f"quote unusable (age {age:.0f}s) and no fresh last trade"


def process_orders() -> list[dict]:
    """Try to fill every open order. Returns fill events."""
    now = clock.now()
    agent = settings.get("agent")
    lim = settings.get("risk")
    costs_all = settings.get("costs")
    events = []
    for o in oms.open_orders():
        venue = o["venue"]
        if o["expires_at"] and now > o["expires_at"]:
            oms.close_order(o, "expired", "Not filled within its time-to-live (no usable quote or market closed). "
                                          "The next decision cycle re-evaluates with fresh data.")
            continue
        if now < (o["eligible_at"] or 0):
            continue
        if not venues.is_open(venue, now):
            continue
        if agent.get("emergency_stop") and o["origin"] != "operator":
            oms.close_order(o, "cancelled", "Emergency stop is active.")
            continue
        if o["side"] == "buy":
            day_flag = db.get_meta(f"daily_loss_flag:{int(now // 86400)}") == "1"
            if (agent.get("paused") or db.get_meta("drawdown_halt") == "1" or day_flag
                    or db.get_meta("system_halt") == "1"):
                oms.close_order(o, "cancelled", "New risk blocked at fill time (paused, drawdown halt or daily loss limit).")
                continue
        cost_cfg = costs_all[venue]
        q = store.get_quote(o["symbol"])
        ref_px, ref = _price_basis(o, q, now, lim, cost_cfg)
        if ref_px is None:
            continue  # wait for a usable quote until TTL
        remaining = o["qty"] - o["filled_qty"]
        adv, sigma = liquidity(o["symbol"], venue)
        cap_note = None
        if adv:
            max_notional = lim["max_participation"] * adv
            if remaining * ref_px > max_notional:
                capped = max_notional / ref_px
                dec = VENUES[venue].qty_decimals
                capped = math.floor(capped * 10 ** dec) / 10 ** dec
                cap_note = (f"Liquidity cap: order {remaining:g} > {lim['max_participation']:.2%} of ADV "
                            f"(${adv:,.0f}); filled {capped:g}, remainder cancelled.")
                remaining = capped
        if o["side"] == "sell":
            remaining = min(remaining, ledger.sleeve_qty(o["strategy_id"] or 0, o["symbol"]))
        if remaining <= 0:
            oms.close_order(o, "cancelled", cap_note or "Nothing left to fill.")
            continue
        imp = C.impact_bps(remaining * ref_px, adv, sigma, cost_cfg)
        px = C.apply_slippage(o["side"], ref_px, imp)
        fee = C.fees(venue, o["side"], remaining, px, cost_cfg)
        if o["side"] == "buy":
            avail = ledger.cash()
            if remaining * px + fee > avail:
                dec = VENUES[venue].qty_decimals
                remaining = math.floor((avail * 0.995 / px) * 10 ** dec) / 10 ** dec
                if remaining <= 0:
                    oms.close_order(o, "cancelled", "Insufficient cash at fill time.")
                    continue
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
        events.append({"order_id": o["id"], "fill_id": fid, "qty": remaining, "price": px, "fee": fee})
    return events
