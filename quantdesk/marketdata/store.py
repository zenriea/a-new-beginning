"""Bar/quote storage and data-health tracking."""
from __future__ import annotations

import math

import numpy as np

from .. import clock, db
from ..venues import VENUES, venue_of
from .base import Bar, DataError, Quote


def _validate_bars(bars: list[Bar]) -> None:
    for b in bars:
        prices = (b.open, b.high, b.low, b.close)
        if (not all(math.isfinite(v) and v > 0 for v in prices)
                or not math.isfinite(b.volume) or b.volume < 0
                or b.low > min(b.open, b.close) or b.high < max(b.open, b.close)
                or not isinstance(b.ts, int) or isinstance(b.ts, bool) or b.ts <= 0):
            raise DataError("invalid OHLCV bar: require finite positive prices, valid range, volume and timestamp")


def upsert_bars(symbol: str, timeframe: str, bars: list[Bar]) -> int:
    if not bars:
        return 0
    _validate_bars(bars)
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO bars(symbol, timeframe, ts, open, high, low, close, volume) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET open=excluded.open, high=excluded.high, "
            "low=excluded.low, close=excluded.close, volume=excluded.volume",
            [(symbol, timeframe, b.ts, b.open, b.high, b.low, b.close, b.volume) for b in bars])
    return len(bars)


def replace_bars(symbol: str, timeframe: str, bars: list[Bar]) -> int:
    """Atomically replace a symbol's stored history (used when the provider re-adjusts it)."""
    _validate_bars(bars)
    with db.tx() as conn:
        conn.execute("DELETE FROM bars WHERE symbol=? AND timeframe=?", (symbol, timeframe))
        conn.executemany(
            "INSERT INTO bars(symbol, timeframe, ts, open, high, low, close, volume) VALUES(?,?,?,?,?,?,?,?)",
            [(symbol, timeframe, b.ts, b.open, b.high, b.low, b.close, b.volume) for b in bars])
    return len(bars)


def stored_closes(symbol: str, timeframe: str, ts_list: list[int]) -> dict[int, float]:
    if not ts_list:
        return {}
    # a range query: an IN list of every timestamp exceeds SQLite's parameter limit on a full backfill
    want = set(ts_list)
    rows = db.q("SELECT ts, close FROM bars WHERE symbol=? AND timeframe=? AND ts BETWEEN ? AND ?",
                (symbol, timeframe, min(want), max(want)))
    return {r["ts"]: r["close"] for r in rows if r["ts"] in want}


def last_bar_ts(symbol: str, timeframe: str) -> int | None:
    r = db.q1("SELECT MAX(ts) AS t FROM bars WHERE symbol=? AND timeframe=?", (symbol, timeframe))
    return r["t"] if r and r["t"] is not None else None


def load_bars(symbol: str, timeframe: str, start: int | None = None, end: int | None = None,
              limit: int | None = None) -> dict[str, np.ndarray]:
    """Bars with ts in [start, end] (inclusive), oldest first."""
    sql = "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? AND timeframe=?"
    params: list = [symbol, timeframe]
    if start is not None:
        sql += " AND ts>=?"
        params.append(start)
    if end is not None:
        sql += " AND ts<=?"
        params.append(end)
    if limit:
        sql = f"SELECT * FROM ({sql} ORDER BY ts DESC LIMIT {int(limit)}) ORDER BY ts"
    else:
        sql += " ORDER BY ts"
    rows = db.q(sql, params)
    if not rows:
        empty = np.array([], dtype=float)
        return {"ts": np.array([], dtype=np.int64), "open": empty, "high": empty, "low": empty,
                "close": empty, "volume": empty}
    arr = np.array([tuple(r) for r in rows], dtype=float)
    return {"ts": arr[:, 0].astype(np.int64), "open": arr[:, 1], "high": arr[:, 2],
            "low": arr[:, 3], "close": arr[:, 4], "volume": arr[:, 5]}


def load_panel(symbols: list[str], timeframe: str, start: int | None = None,
               end: int | None = None) -> dict:
    """Aligned panel: common timestamp index; missing values are NaN (never forward-filled
    into the future - only carried from the past)."""
    data = {s: load_bars(s, timeframe, start, end) for s in symbols}
    all_ts = sorted(set().union(*[set(d["ts"].tolist()) for d in data.values()])) if data else []
    idx = np.array(all_ts, dtype=np.int64)
    pos = {t: i for i, t in enumerate(all_ts)}
    panel = {"ts": idx, "symbols": list(symbols)}
    for field in ("open", "high", "low", "close", "volume"):
        m = np.full((len(idx), len(symbols)), np.nan)
        for j, s in enumerate(symbols):
            d = data[s]
            if len(d["ts"]):
                rows = np.array([pos[t] for t in d["ts"].tolist()])
                m[rows, j] = d[field]
        panel[field] = m
    return panel


def save_quote(venue: str, q: Quote) -> None:
    values = (q.bid, q.ask, q.bid_size, q.ask_size, q.last)
    if (not math.isfinite(q.ts) or q.ts <= 0 or q.ts > clock.now() + 5
            or any(v is not None and (not math.isfinite(v) or v < 0) for v in values)
            or (q.last_ts is not None and (not math.isfinite(q.last_ts) or q.last_ts > clock.now() + 5 or q.last_ts <= 0))):
        raise DataError("invalid quote: require finite nonnegative values and nonfuture timestamps")
    db.ex("INSERT INTO quotes(symbol, venue, ts, bid, ask, bid_size, ask_size, last, last_ts, source, received_at) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET venue=excluded.venue, ts=excluded.ts, "
          "bid=excluded.bid, ask=excluded.ask, bid_size=excluded.bid_size, ask_size=excluded.ask_size, "
          "last=excluded.last, last_ts=excluded.last_ts, source=excluded.source, received_at=excluded.received_at",
          (q.symbol, venue, q.ts, q.bid, q.ask, q.bid_size, q.ask_size, q.last, q.last_ts, q.source, clock.now()))


def get_quote(symbol: str) -> dict | None:
    return db.row_dict(db.q1("SELECT * FROM quotes WHERE symbol=?", (symbol,)))


def mark_price(symbol: str) -> tuple[float | None, float | None, str]:
    """Best available mark: quote mid, else last trade, else last bar close. Returns
    (price, as_of_epoch, basis)."""
    q = get_quote(symbol)
    if q and q["bid"] and q["ask"] and q["ask"] >= q["bid"] > 0:
        return (q["bid"] + q["ask"]) / 2, q["ts"], "quote_mid"
    if q and q["last"]:
        return q["last"], q["last_ts"] or q["ts"], "last_trade"
    tf = VENUES[venue_of(symbol)].timeframe
    r = db.q1("SELECT ts, close FROM bars WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT 1", (symbol, tf))
    if r:
        return r["close"], r["ts"] + VENUES[venue_of(symbol)].bar_seconds, "bar_close"
    return None, None, "none"


def set_status(key: str, ok: bool, error: str | None = None) -> bool:
    """Record data health. Returns True when the ok/not-ok state CHANGED (used to journal
    transitions once instead of spamming)."""
    now = clock.now()
    prev = db.q1("SELECT ok FROM data_status WHERE key=?", (key,))
    changed = prev is None and not ok or (prev is not None and bool(prev["ok"]) != ok)
    if ok:
        db.ex("INSERT INTO data_status(key, ok, last_ok_at, last_error, changed_at) VALUES(?,1,?,NULL,?) "
              "ON CONFLICT(key) DO UPDATE SET ok=1, last_ok_at=excluded.last_ok_at, last_error=NULL, "
              "changed_at=CASE WHEN data_status.ok=0 THEN excluded.changed_at ELSE data_status.changed_at END",
              (key, now, now))
    else:
        db.ex("INSERT INTO data_status(key, ok, last_ok_at, last_error, changed_at) VALUES(?,0,NULL,?,?) "
              "ON CONFLICT(key) DO UPDATE SET ok=0, last_error=excluded.last_error, "
              "changed_at=CASE WHEN data_status.ok=1 THEN excluded.changed_at ELSE data_status.changed_at END",
              (key, error, now))
    return changed


def status_all() -> list[dict]:
    return [dict(r) for r in db.q("SELECT * FROM data_status ORDER BY key")]
