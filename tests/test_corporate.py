from fractions import Fraction

import pytest

from conftest import NASDAQ_OPEN_TS, make_strategy


class AdjustingProvider:
    """Mimics Alpaca: returns full adjusted history; ``factor`` divides all past prices."""
    name = "fake"
    venue = "NASDAQ"
    synthetic = False

    def __init__(self, sessions_ts, base=400.0):
        self.ts = sessions_ts
        self.base = base
        self.factor = 1.0
        self.upto = len(sessions_ts) - 1  # newest session index currently published

    def fetch_bars(self, symbols, timeframe, start, end):
        from quantdesk.marketdata.base import Bar
        out = {}
        for s in symbols:
            bars = []
            for i, t in enumerate(self.ts[: self.upto + 1]):
                if start <= t <= end:
                    c = (self.base + i * 0.1) / self.factor
                    bars.append(Bar(t, c, c * 1.01, c * 0.99, c, 1e7 * self.factor))
            out[s] = bars
        return out

    def fetch_quotes(self, symbols):
        return {}


def _sessions(n=60):
    import exchange_calendars as xc
    import pandas as pd
    from quantdesk.marketdata.freshness import daily_bar_ts, expected_last_bar
    last = expected_last_bar("NASDAQ", NASDAQ_OPEN_TS)
    cal = xc.get_calendar("XNAS")
    sess = cal.sessions_window(pd.Timestamp(pd.Timestamp(last, unit="s").date()), -n)
    return [daily_bar_ts(s) for s in sess]


def _hold(sid, symbol, qty, price):
    from quantdesk import clock, db
    from quantdesk.trading import ledger
    oid = db.ex("INSERT INTO orders(client_order_id, strategy_id, symbol, venue, side, qty, status, reason, rationale, origin, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"h{qty}{price}", sid, symbol, "NASDAQ", "buy", qty, "submitted", "t", "t",
                                                  "agent", clock.now()))
    ledger.apply_fill(dict(db.q1("SELECT * FROM orders WHERE id=?", (oid,))), qty, price, 0.0, {})
    db.ex("INSERT INTO strategy_state(strategy_id, symbol, in_position, entry_price) VALUES(?,?,1,?)", (sid, symbol, price))


def _setup(env):
    from quantdesk.marketdata import ingest
    ts = _sessions()
    prov = AdjustingProvider(ts)
    prov.upto = len(ts) - 2          # yesterday's bar not published yet
    ingest.set_provider("NASDAQ", prov)
    ingest._backoff.clear()
    ingest.ingest_bars("NASDAQ", ["AAPL"])
    return prov, ts


def test_snap_split_ratio():
    from quantdesk.trading.corporate import snap_split_ratio
    assert snap_split_ratio(4.0) == 4
    assert snap_split_ratio(1.5004) == Fraction(3, 2)
    assert snap_split_ratio(0.1) == Fraction(1, 10)
    assert snap_split_ratio(10.02) == 10
    assert snap_split_ratio(1.004) is None
    assert snap_split_ratio(1.37) is None


def test_split_rewrites_history_and_adjusts_positions(env):
    from quantdesk import db
    from quantdesk.marketdata import ingest, store
    from quantdesk.trading import ledger
    prov, ts = _setup(env)
    sid = make_strategy()
    _hold(sid, "AAPL", 10, 405.0)
    before = ledger.sleeve_positions(sid)[0]
    prov.factor = 4.0                 # 4-for-1 split: provider re-adjusts all history
    prov.upto = len(ts) - 1           # and publishes the newest bar
    ev = ingest.ingest_bars("NASDAQ", ["AAPL"])
    assert any(e.get("kind") == "split" for e in ev)
    closes = store.load_bars("AAPL", "1d")["close"]
    assert len(closes) == len(ts)
    assert closes.max() < 120          # no pre-split prices left: no fake cliff in history
    pos = ledger.sleeve_positions(sid)[0]
    assert pos["qty"] == pytest.approx(40) and pos["avg_cost"] == pytest.approx(405.0 / 4)
    assert pos["qty"] * pos["avg_cost"] == pytest.approx(before["qty"] * before["avg_cost"])
    assert db.q1("SELECT entry_price FROM strategy_state WHERE strategy_id=?", (sid,))["entry_price"] == pytest.approx(101.25)
    assert ledger.integrity_check()["ok"]              # rebuild replays the split
    assert db.q1("SELECT COUNT(*) n FROM journal WHERE title LIKE '%4-for-1 split%'")["n"] == 1
    # a second pass sees consistent history: no second split
    assert not any(e.get("kind") == "split" for e in ingest.ingest_bars("NASDAQ", ["AAPL"]))
    assert ledger.sleeve_positions(sid)[0]["qty"] == pytest.approx(40)


def test_dividend_readjustment_keeps_positions(env):
    from quantdesk.marketdata import ingest
    from quantdesk.trading import ledger
    prov, ts = _setup(env)
    sid = make_strategy()
    _hold(sid, "AAPL", 10, 405.0)
    prov.factor = 1.004
    ev = ingest.ingest_bars("NASDAQ", ["AAPL"])
    assert ev and ev[0]["kind"] == "readjustment"
    assert ledger.sleeve_positions(sid)[0]["qty"] == pytest.approx(10)
    assert ledger.integrity_check()["ok"]


def test_unexplained_jump_is_flagged_not_applied(env):
    from quantdesk import db
    from quantdesk.marketdata import ingest
    from quantdesk.trading import ledger
    prov, ts = _setup(env)
    sid = make_strategy()
    _hold(sid, "AAPL", 10, 405.0)
    prov.factor = 1.37
    ev = ingest.ingest_bars("NASDAQ", ["AAPL"])
    assert ev[0]["kind"] == "unexplained"
    assert ledger.sleeve_positions(sid)[0]["qty"] == pytest.approx(10)
    assert db.q1("SELECT COUNT(*) n FROM journal WHERE kind='risk' AND title LIKE '%unexplained%'")["n"] == 1
