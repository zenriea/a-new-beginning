import pytest

from quantdesk.marketdata.base import Bar, DataError, Quote


@pytest.mark.parametrize("bar", [
    Bar(100, 100, 101, 99, float("inf"), 1),
    Bar(100, 100, 90, 99, 100, 1),
    Bar(100, 100, 101, 99, 100, -1),
    Bar(100, 0, 101, 0, 100, 1),
])
def test_invalid_bars_cannot_replace_valid_history(env, bar):
    from quantdesk.marketdata import store
    store.upsert_bars("AAPL", "15m", [Bar(100, 100, 101, 99, 100, 1)])
    with pytest.raises(DataError):
        store.replace_bars("AAPL", "15m", [bar])
    assert store.load_bars("AAPL", "15m")["close"].tolist() == [100]


@pytest.mark.parametrize("change", [{"bid": float("inf")}, {"ask": -1}, {"ts": 99999999999},
                                   {"last": float("nan")}])
def test_invalid_quotes_are_not_persisted(env, change):
    from quantdesk import clock
    from quantdesk.marketdata import store
    fields = dict(symbol="AAPL", ts=clock.now(), bid=100, ask=101, last=100, last_ts=clock.now())
    fields.update(change)
    with pytest.raises(DataError):
        store.save_quote("NASDAQ", Quote(**fields))
    assert store.get_quote("AAPL") is None


def test_invalid_quote_is_flagged_without_stopping_other_symbols(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest, store
    from quantdesk.marketdata.base import Provider

    class Mixed(Provider):
        def fetch_quotes(self, symbols):
            return {"AAPL": Quote("AAPL", clock.now(), float("inf"), 101),
                    "MSFT": Quote("MSFT", clock.now(), 100, 101)}

    ingest.set_provider("NASDAQ", Mixed())
    events = ingest.ingest_quotes("NASDAQ", ["AAPL", "MSFT"])
    assert any(e["symbol"] == "AAPL" and not e["ok"] for e in events)
    assert store.get_quote("MSFT")["bid"] == 100


def test_one_unavailable_coinbase_product_does_not_discard_valid_quotes(monkeypatch):
    from quantdesk.marketdata import coinbase

    def ticker(url, **kwargs):
        if "BAD-USD" in url:
            raise DataError("HTTP 404")
        return {"bid": "100", "ask": "101", "price": "100", "time": "2026-11-17T00:00:00Z"}

    monkeypatch.setattr(coinbase, "get_json", ticker)
    assert set(coinbase.CoinbaseProvider().fetch_quotes(["BTC-USD", "BAD-USD", "ETH-USD"])) == {"BTC-USD", "ETH-USD"}
    with pytest.raises(DataError, match="BAD-USD"):
        coinbase.CoinbaseProvider().fetch_quotes(["BAD-USD"])


@pytest.mark.parametrize("length", [49, 50, 99, 1500])
def test_synthetic_history_does_not_change_when_more_bars_arrive(length):
    import numpy as np
    from quantdesk.marketdata.synthetic import _path
    short = _path("AAPL", 900, length)
    longer = _path("AAPL", 900, length + 101)
    for before, after in zip(short, longer):
        np.testing.assert_array_equal(before, after[:length])


def test_one_invalid_bar_is_dropped_without_freezing_the_symbol(env):
    from dataclasses import replace

    from quantdesk import clock
    from quantdesk.marketdata import ingest, store
    from quantdesk.marketdata.synthetic import SyntheticProvider

    real = SyntheticProvider("CRYPTO")
    bad_ts = {}

    class OneBadBar(SyntheticProvider):
        def fetch_bars(self, symbols, timeframe, start, end):
            data = real.fetch_bars(symbols, timeframe, start, end)
            bars = data["BTC-USD"]
            # Corrupt the same bar on every download, as a real provider would keep returning it.
            bad_ts.setdefault("ts", bars[-5].ts)
            data["BTC-USD"] = [replace(b, high=b.close * 0.5) if b.ts == bad_ts["ts"] else b for b in bars]
            return data

    ingest.set_provider("CRYPTO", OneBadBar("CRYPTO"))
    ingest.ingest_bars("CRYPTO", ["BTC-USD"])
    clock.advance(3600)  # a refresh re-downloads the overlap window, bad bar included
    ingest.ingest_bars("CRYPTO", ["BTC-USD"])

    stored = set(store.load_bars("BTC-USD", "15m")["ts"].tolist())
    assert bad_ts["ts"] not in stored                      # the bad bar was not stored...
    assert store.last_bar_ts("BTC-USD", "15m") > bad_ts["ts"] + 3000  # ...newer bars still arrive
    status = {r["key"]: r for r in store.status_all()}
    assert status["bars:BTC-USD"]["ok"]                      # and the symbol is not flagged stale
