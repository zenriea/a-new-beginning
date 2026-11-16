import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 2026-11-11 15:00 UTC = Wednesday 10:00 New York (NASDAQ open)
NASDAQ_OPEN_TS = 1794409200.0


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("QD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("QD_MARKET_DATA", "synthetic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("QD_ADMIN_PASSWORD", "")
    monkeypatch.setenv("QD_ENV_FILE", str(tmp_path / "none.env"))
    from quantdesk import clock, db
    from quantdesk.config import load_config
    from quantdesk.marketdata import ingest
    cfg = load_config(reload=True)
    db.close_thread_connection()
    db.init(cfg.db_path)
    clock.set_fixed(NASDAQ_OPEN_TS)
    for v in ("NASDAQ", "CRYPTO"):
        ingest.set_provider(v, None)
    from quantdesk.trading import ledger
    ledger.init_account()
    yield cfg
    clock.set_fixed(None)
    db.close_thread_connection()


def put_quote(symbol, bid, ask, ts=None, last=None, received=None):
    from quantdesk import clock, db
    from quantdesk.venues import venue_of
    ts = clock.now() if ts is None else ts
    db.ex("INSERT INTO quotes(symbol, venue, ts, bid, ask, bid_size, ask_size, last, last_ts, source, received_at) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET ts=excluded.ts, bid=excluded.bid, ask=excluded.ask, "
          "last=excluded.last, last_ts=excluded.last_ts, received_at=excluded.received_at",
          (symbol, venue_of(symbol), ts, bid, ask, 100, 100, last or (bid + ask) / 2 if bid and ask else last, ts, "test",
           received if received is not None else clock.now()))


def put_bars(symbol, closes, timeframe="15m", start=None, volume=1e7):
    from quantdesk.marketdata.base import Bar
    from quantdesk.marketdata.store import upsert_bars
    step = {"15m": 900, "1h": 3600, "1d": 86400}[timeframe]
    start = start if start is not None else 1700000000 - (1700000000 % step)
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        bars.append(Bar(start + i * step, o, max(o, c) * 1.001, min(o, c) * 0.999, c, volume))
    upsert_bars(symbol, timeframe, bars)
    return bars


def make_strategy(spec_overrides=None, allocation=20000.0, venue="NASDAQ"):
    from quantdesk import clock, db
    spec = {"name": "t", "venue": venue, "universe": ["AAPL"], "kind": "dsl", "entry": "close > 0", "exit": "close < 0",
            "sizing": {"method": "fixed", "weight": 0.5}, "params": {}, "fixed_params": {}}
    spec.update(spec_overrides or {})
    sid = db.ex("INSERT INTO strategies(hypothesis_id, experiment_id, name, version, venue, spec, spec_hash, status, "
                "approved_allocation, allocation, thesis, exit_plan, expectations, invalidation, created_at, activated_at) "
                "VALUES(1,1,'t',1,?,?,?,'active',?,?,'test thesis','test exit','{}','{}',?,?)",
                (venue, db.dumps(spec), "h", allocation, allocation, clock.now(), clock.now()))
    return sid
