"""Crypto keeps 3 years of hourly history; older installs extend backwards once."""
from quantdesk.marketdata.synthetic import SyntheticProvider


class Counting(SyntheticProvider):
    def __init__(self):
        super().__init__("CRYPTO")
        self.calls = []

    def fetch_bars(self, symbols, timeframe, start, end):
        self.calls.append((tuple(symbols), start, end))
        return super().fetch_bars(symbols, timeframe, start, end)


def _first(sym):
    from quantdesk import db
    return db.q1("SELECT MIN(ts) t FROM bars WHERE symbol=? AND timeframe='1h'", (sym,))["t"]


def test_new_install_backfills_three_years_of_crypto(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest
    ingest.set_provider("CRYPTO", Counting())
    ingest.ingest_bars("CRYPTO", ["BTC-USD"])
    assert clock.now() - _first("BTC-USD") > 3 * 365 * 86400 - 2 * 86400


def test_existing_one_year_history_is_extended_backwards_once_one_symbol_per_tick(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest
    p = Counting()
    ingest.set_provider("CRYPTO", p)
    ingest.BACKFILL_DAYS["CRYPTO"], real = 365, ingest.BACKFILL_DAYS["CRYPTO"]
    try:
        ingest.ingest_bars("CRYPTO", ["BTC-USD", "ETH-USD"])   # an install from before this change
    finally:
        ingest.BACKFILL_DAYS["CRYPTO"] = real
    one_year = _first("BTC-USD")
    assert clock.now() - one_year < 366 * 86400
    ingest.ingest_bars("CRYPTO", ["BTC-USD", "ETH-USD"])
    extended = [s for s in ("BTC-USD", "ETH-USD") if clock.now() - _first(s) > 3 * 365 * 86400 - 2 * 86400]
    assert len(extended) == 1                                  # one symbol per tick keeps the loop responsive
    ingest.ingest_bars("CRYPTO", ["BTC-USD", "ETH-USD"])
    assert all(clock.now() - _first(s) > 3 * 365 * 86400 - 2 * 86400 for s in ("BTC-USD", "ETH-USD"))
    n = len(p.calls)
    ingest.ingest_bars("CRYPTO", ["BTC-USD", "ETH-USD"])
    older = [c for c in p.calls[n:] if c[1] < one_year]
    assert older == []                                         # never re-downloads old history


def test_symbol_with_shorter_history_is_not_refetched_forever(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest

    class Young(Counting):
        def fetch_bars(self, symbols, timeframe, start, end):
            listed = int(clock.now() - 200 * 86400)            # listed 200 days ago
            out = super().fetch_bars(symbols, timeframe, max(start, listed), end)
            return {s: [b for b in v if b.ts >= listed] for s, v in out.items()}
    p = Young()
    ingest.set_provider("CRYPTO", p)
    ingest.ingest_bars("CRYPTO", ["SOL-USD"])
    ingest.ingest_bars("CRYPTO", ["SOL-USD"])
    n = len(p.calls)
    ingest.ingest_bars("CRYPTO", ["SOL-USD"])
    assert all(c[1] >= _first("SOL-USD") - 3 * 3600 for c in p.calls[n:])
