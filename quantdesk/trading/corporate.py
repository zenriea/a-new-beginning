"""Corporate actions inferred from re-adjusted price history.

When Alpaca re-adjusts past bars, the ratio stored/fresh tells us what happened:
  * a "nice" ratio far from 1 (2, 3, 4, 3/2, 1/10 ...) = stock split / reverse split:
    every holding's share count is multiplied by it and its cost basis divided by it, so the
    position's value is unchanged and marks against the new raw quotes stay continuous;
  * a ratio within a few percent of 1 = dividend adjustment: history is replaced, positions
    are untouched (paper positions do not receive cash dividends - disclosed limitation);
  * anything else = unexplained: history is replaced, positions untouched, and the journal
    asks the operator to review.
"""
from __future__ import annotations

from fractions import Fraction

from .. import clock, db
from ..agent import journal

SPLIT_THRESHOLD = 0.15


def _split_ratios() -> set[Fraction]:
    """Ratios real splits use: small-integer fractions (3:2, 5:4, 2:1 ... 10:1) and round
    large ones (15:1 ... 50:1), plus their reverse-split inverses."""
    out = {Fraction(p, q) for q in range(1, 6) for p in range(1, 11) if Fraction(p, q) != 1}
    out |= {Fraction(n) for n in (12, 15, 20, 25, 30, 40, 50)}
    out |= {1 / f for f in list(out)}
    return out


SPLIT_RATIOS = _split_ratios()


def snap_split_ratio(r: float, tol: float = 0.01) -> Fraction | None:
    best = min(SPLIT_RATIOS, key=lambda f: abs(float(f) - r) / r)
    return best if abs(float(best) - r) / r <= tol else None


def apply_split(symbol: str, ratio: Fraction, note: str = "") -> int:
    """Multiply share counts by ratio, divide cost basis and entry prices by it; cancel open
    orders in the symbol (their quantities were sized on the old basis). Returns rows changed."""
    r = float(ratio)
    ts = clock.now()
    from . import oms
    with db.tx():
        n = db.connect().execute("UPDATE positions SET qty=qty*?, avg_cost=avg_cost/?, updated_at=? WHERE symbol=? AND ABS(qty)>1e-12",
                                 (r, r, ts, symbol)).rowcount
        db.ex("UPDATE strategy_state SET entry_price=entry_price/? WHERE symbol=? AND entry_price IS NOT NULL", (r, symbol))
        after = db.q1("SELECT COALESCE(MAX(id), 0) m FROM fills")["m"]  # replay order for integrity rebuilds
        db.ex("INSERT INTO corporate_actions(ts, symbol, kind, ratio, detail) VALUES(?,?,?,?,?)",
              (ts, symbol, "split", r, db.dumps({"ratio": str(ratio), "note": note, "positions_adjusted": n,
                                                 "after_fill_id": after})))
        # Cached quotes may still be on the old price basis. Wait for a new observation.
        db.ex("DELETE FROM quotes WHERE symbol=?", (symbol,))
        for row in db.q("SELECT * FROM orders WHERE symbol=? AND status IN ('intent','submitted','partially_filled')", (symbol,)):
            oms.close_order(dict(row), "cancelled", f"Cancelled: {symbol} split {ratio} changed the share basis; "
                                                  "the next decision cycle re-sizes it.")
    return n


def on_history_readjusted(symbol: str, venue: str, ratio: float, bars: int) -> dict:
    ev = {"key": f"adjust:{symbol}", "ok": True, "symbol": symbol, "ratio": ratio, "kind": "readjustment"}
    if venue == "NASDAQ" and abs(ratio - 1) > SPLIT_THRESHOLD:
        f = snap_split_ratio(ratio)
        if f is not None:
            n = apply_split(symbol, f, f"inferred from re-adjusted history (stored/fresh = {ratio:.4f})")
            journal.write("data", f"{symbol}: {f.numerator}-for-{f.denominator} split detected",
                          f"Alpaca re-adjusted {symbol}'s price history by a factor of {ratio:.4f}, consistent with a "
                          f"{f.numerator}-for-{f.denominator} stock split. I replaced the stored history ({bars} bars) and "
                          f"adjusted {n} holding(s): share count x{float(f):g}, cost basis /{float(f):g}, so position "
                          "value is unchanged. Open orders in the symbol were cancelled and will be re-sized.",
                          refs={"symbol": symbol}, author="system", dedupe_key=f"split:{symbol}:{int(clock.now() // 86400)}")
            ev["kind"] = "split"
            return ev
        journal.write("risk", f"{symbol}: unexplained price-history change ({ratio:.4f}x)",
                      f"Stored history for {symbol} no longer matches the provider by a factor of {ratio:.4f}, which is not "
                      "a recognisable split ratio. I replaced the history but did NOT change any holdings. Please review "
                      "(possible special dividend, spin-off or data error).", refs={"symbol": symbol}, author="system",
                      dedupe_key=f"unexplained:{symbol}:{int(clock.now() // 86400)}")
        ev["kind"] = "unexplained"
        return ev
    journal.write("data", f"{symbol}: price history re-adjusted ({(ratio - 1) * 100:+.2f}%)",
                  f"The provider re-adjusted {symbol}'s history (typically a dividend). Stored bars were replaced "
                  f"({bars}). Holdings are unchanged; paper positions do not receive cash dividends.",
                  refs={"symbol": symbol}, author="system", dedupe_key=f"readj:{symbol}:{int(clock.now() // 86400)}")
    return ev
