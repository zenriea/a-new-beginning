"""Venues and trading sessions.

NASDAQ uses the exchange_calendars XNAS calendar (holidays, early closes); only the
regular session is tradable. CRYPTO trades 24/7. New venues (e.g. NYSE) only need a
Venue entry with a calendar.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache

import pandas as pd

UTC = dt.timezone.utc


@dataclass(frozen=True)
class Venue:
    name: str
    asset_class: str          # "equity" | "crypto"
    timeframe: str            # research/trading bar size: "1d" or "1h"
    bar_seconds: int
    periods_per_year: float   # for annualisation
    qty_decimals: int         # 0 = whole shares


VENUES = {
    "NASDAQ": Venue("NASDAQ", "equity", "1d", 86400, 252.0, 0),
    "CRYPTO": Venue("CRYPTO", "crypto", "1h", 3600, 24 * 365.0, 8),
}


def venue_of(symbol: str) -> str:
    return "CRYPTO" if "-" in symbol or "/" in symbol else "NASDAQ"


@lru_cache(maxsize=1)
def _xnas():
    import exchange_calendars as xc
    return xc.get_calendar("XNAS")


def _to_ts(epoch: float) -> pd.Timestamp:
    return pd.Timestamp(epoch, unit="s", tz="UTC")


def is_open(venue: str, epoch: float) -> bool:
    if venue == "CRYPTO":
        return True
    cal = _xnas()
    ts = _to_ts(epoch).floor("min")
    try:
        return bool(cal.is_trading_minute(ts))
    except Exception:
        return False


def session_for(epoch: float) -> pd.Timestamp | None:
    """The NASDAQ session date (midnight-naive) that contains epoch, else None."""
    cal = _xnas()
    date = _to_ts(epoch).tz_convert("America/New_York").normalize().tz_localize(None)
    if not cal.is_session(date):
        return None
    return date


def session_bounds(session: pd.Timestamp) -> tuple[float, float]:
    cal = _xnas()
    return (cal.session_open(session).timestamp(), cal.session_close(session).timestamp())


def next_open(epoch: float) -> float:
    """Epoch of the next NASDAQ regular-session open strictly after ``epoch`` (or now if open)."""
    cal = _xnas()
    ts = _to_ts(epoch)
    if is_open("NASDAQ", epoch):
        return epoch
    return cal.next_open(ts).timestamp()


def last_close_before(epoch: float) -> float:
    cal = _xnas()
    return cal.previous_close(_to_ts(epoch)).timestamp()


def session_label(bar_ts: int) -> str:
    return dt.datetime.fromtimestamp(bar_ts, UTC).strftime("%Y-%m-%d")


def describe_session(epoch: float) -> dict:
    """Human-facing status for the UI."""
    o = is_open("NASDAQ", epoch)
    info = {"NASDAQ": {"open": o}, "CRYPTO": {"open": True}}
    try:
        if o:
            sess = session_for(epoch)
            info["NASDAQ"]["closes_at"] = session_bounds(sess)[1]
        else:
            info["NASDAQ"]["opens_at"] = next_open(epoch)
    except Exception as e:  # calendar beyond range etc.
        info["NASDAQ"]["error"] = str(e)
    return info
