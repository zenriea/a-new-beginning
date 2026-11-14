"""Which bar is 'the latest closed bar' right now, and is our data current?

A bar is only ever used after it has closed. For NASDAQ daily bars we additionally wait
for the free-plan SIP delay (16 minutes) after the session close.
"""
from __future__ import annotations

import pandas as pd

from .. import venues
from ..venues import VENUES

SIP_DELAY = 16 * 60


def daily_bar_ts(session: pd.Timestamp) -> int:
    """Alpaca daily bars are stamped at 00:00 America/New_York of the session date."""
    return int(pd.Timestamp(session.date()).tz_localize("America/New_York").timestamp())


def bar_closed(venue: str, ts: int, now: float) -> bool:
    if venue == "CRYPTO":
        return ts + VENUES[venue].bar_seconds <= now
    session = pd.Timestamp(pd.Timestamp(ts, unit="s", tz="UTC").tz_convert("America/New_York").date())
    try:
        _, close = venues.session_bounds(session)
    except Exception:
        return False
    return close + SIP_DELAY <= now


def expected_last_bar(venue: str, now: float) -> int:
    """Open-time of the most recent bar that should be closed (and published) by ``now``."""
    if venue == "CRYPTO":
        step = VENUES[venue].bar_seconds
        return int(now // step) * step - step
    close = venues.last_close_before(now - SIP_DELAY)
    session = pd.Timestamp(pd.Timestamp(close, unit="s", tz="UTC").tz_convert("America/New_York").date())
    return daily_bar_ts(session)


def bars_current(venue: str, last_ts: int | None, now: float, tolerance_bars: int = 0) -> bool:
    if last_ts is None:
        return False
    exp = expected_last_bar(venue, now)
    if venue == "CRYPTO":
        return last_ts >= exp - tolerance_bars * VENUES[venue].bar_seconds
    if tolerance_bars == 0:
        return last_ts >= exp
    # NASDAQ tolerance counted in sessions (weekends/holidays skipped)
    return last_ts >= exp - (tolerance_bars + 3) * 86400


def quote_age(quote: dict | None, now: float) -> float | None:
    if not quote:
        return None
    return now - quote["ts"]
