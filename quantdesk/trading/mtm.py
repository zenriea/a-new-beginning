"""Mark-to-market snapshots and benchmark tracking."""
from __future__ import annotations

from .. import clock, db, settings
from ..config import load_config
from ..marketdata import store
from . import ledger


def _bench_equity(symbol: str) -> float | None:
    key = f"bench_start:{symbol}"
    start = db.get_meta(key)
    px, _, _ = store.mark_price(symbol)
    if px is None:
        return None
    if start is None:
        db.set_meta(key, str(px))
        start = px
    return load_config().initial_capital * px / float(start)


def snapshot() -> dict:
    now = clock.now()
    ts = now - (now % 60)
    eb = ledger.equity_breakdown()
    b = settings.get("benchmarks")
    prev = db.q1("SELECT MAX(peak_equity) p FROM equity_snapshots")
    peak = max(eb["equity"], prev["p"] or 0) if prev else eb["equity"]
    detail = {s: {"qty": v["qty"], "mark": v["mark"], "basis": v["basis"]} for s, v in eb["positions"].items()}
    db.ex("INSERT INTO equity_snapshots(ts, equity, cash, gross, crypto_gross, benchmark_equity, crypto_benchmark_equity, "
          "peak_equity, detail) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity, "
          "cash=excluded.cash, gross=excluded.gross, crypto_gross=excluded.crypto_gross, "
          "benchmark_equity=excluded.benchmark_equity, crypto_benchmark_equity=excluded.crypto_benchmark_equity, "
          "peak_equity=excluded.peak_equity, detail=excluded.detail",
          (ts, eb["equity"], eb["cash"], eb["gross"], eb["crypto_gross"], _bench_equity(b["account"]),
           _bench_equity(b["CRYPTO"]), peak, db.dumps(detail)))
    return eb
