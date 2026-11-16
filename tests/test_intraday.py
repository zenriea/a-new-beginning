"""15-minute bars: regular-session filtering, which bar is 'latest', and the bar-size migration."""
import pandas as pd

from conftest import make_strategy


def _ts(s):
    return int(pd.Timestamp(s, tz="UTC").timestamp())


def test_only_regular_session_bars_are_kept():
    from quantdesk.venues import in_regular_session
    # 2026-11-11 Wed (EST: 14:30-21:00 UTC); 11-26 Thanksgiving; 11-27 early close 18:00 UTC; 11-14 Saturday
    cases = {"2026-11-11 14:15": False, "2026-11-11 14:30": True, "2026-11-11 20:45": True,
             "2026-11-11 21:00": False, "2026-11-26 16:00": False, "2026-11-27 17:45": True,
             "2026-11-27 18:00": False, "2026-11-14 16:00": False}
    got = in_regular_session([_ts(k) for k in cases], 900)
    assert dict(zip(cases, got.tolist())) == cases


def test_latest_stock_bar_waits_for_close_plus_sip_delay():
    from quantdesk.marketdata.freshness import expected_last_bar
    want = {"2026-11-11 15:00": "2026-11-10 20:45",   # first bar of the day not yet published
            "2026-11-11 15:01": "2026-11-11 14:30",
            "2026-11-11 21:10": "2026-11-11 20:30",
            "2026-11-12 03:00": "2026-11-11 20:45",   # overnight: last bar of the session
            "2026-11-30 14:50": "2026-11-27 17:45"}   # after a holiday weekend with an early close
    for now, bar in want.items():
        assert expected_last_bar("NASDAQ", _ts(now)) == _ts(bar), now
    assert expected_last_bar("CRYPTO", _ts("2026-11-11 15:07")) == _ts("2026-11-11 14:45")


def test_bar_size_change_retires_old_strategies_once(env):
    from quantdesk import db, worker
    sid = make_strategy()
    retired = worker.retire_on_timeframe_change()          # an install from before 15-minute bars
    assert retired == [sid]
    s = db.q1("SELECT status, retire_reason FROM strategies WHERE id=?", (sid,))
    assert (s["status"], s["retire_reason"]) == ("retired", "bar size changed")
    assert db.q1("SELECT 1 FROM journal WHERE title LIKE '%bar size changed%'")
    new = make_strategy()
    assert worker.retire_on_timeframe_change() == []       # recorded: later strategies are left alone
    assert db.q1("SELECT status FROM strategies WHERE id=?", (new,))["status"] == "active"


def test_alpaca_never_returns_a_silently_truncated_history(monkeypatch):
    import pytest
    from quantdesk.marketdata import alpaca
    from quantdesk.marketdata.base import DataError
    page = {"bars": {"AAL": [{"t": "2026-11-11T15:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]},
            "next_page_token": "more"}
    monkeypatch.setattr(alpaca, "get_json", lambda *a, **k: page)   # a history that never ends
    monkeypatch.setattr(alpaca, "MAX_PAGES", 3)
    monkeypatch.setattr(alpaca.time, "sleep", lambda s: None)
    with pytest.raises(DataError):
        alpaca.AlpacaProvider("k", "s").fetch_bars(["AAL", "AMC"], "15m", 0, 2_000_000_000)


def test_first_downloads_go_one_symbol_at_a_time_within_a_time_budget(env, monkeypatch):
    from quantdesk.marketdata import ingest
    from quantdesk.marketdata.synthetic import SyntheticProvider

    class Slow(SyntheticProvider):
        calls = []

        def fetch_bars(self, symbols, timeframe, start, end):
            Slow.calls.append(tuple(symbols))
            ticks["t"] += 100                                  # each full download "takes" 100 s
            return super().fetch_bars(symbols, timeframe, start, end)
    ticks = {"t": 0.0}
    monkeypatch.setattr(ingest.time, "monotonic", lambda: ticks["t"])
    ingest.set_provider("NASDAQ", Slow("NASDAQ"))
    syms = ["AAPL", "MSFT", "NVDA", "AMD"]
    ingest.ingest_bars("NASDAQ", syms)
    assert Slow.calls == [("AAPL",), ("MSFT",)]                # one per request; stops once over budget
    assert ingest.history_pending("NASDAQ", syms)              # research waits for NVDA and AMD
    ingest.ingest_bars("NASDAQ", syms)
    assert ("NVDA",) in Slow.calls and ("AMD",) in Slow.calls
    assert not ingest.history_pending("NASDAQ", syms)


def test_a_symbol_whose_download_failed_does_not_block_research_forever(env):
    from quantdesk.marketdata import ingest, store
    store.set_status("bars:DEAD", False, "no bars returned")
    assert not ingest.history_pending("NASDAQ", ["DEAD"])
    assert ingest.history_pending("NASDAQ", ["NEVERTRIED"])
