"""Coinbase Exchange public market data (no key required)."""
from __future__ import annotations

import datetime as dt
import logging
import time

from .base import Bar, DataError, Provider, Quote
from .http import get_json

BASE = "https://api.exchange.coinbase.com"
GRAN = {"15m": 900, "1h": 3600, "1d": 86400}
log = logging.getLogger(__name__)


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s: str) -> float:
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%dT%H:%M:%S"
    return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc).timestamp()


class CoinbaseProvider(Provider):
    name = "coinbase"
    venue = "CRYPTO"

    def fetch_bars(self, symbols, timeframe, start, end):
        g = GRAN[timeframe]
        out: dict[str, list[Bar]] = {}
        for sym in symbols:
            bars: dict[int, Bar] = {}
            cursor = start
            while cursor < end:
                chunk_end = min(end, cursor + g * 299)
                rows = get_json(f"{BASE}/products/{sym}/candles",
                                params={"granularity": g, "start": _iso(cursor), "end": _iso(chunk_end)})
                if not isinstance(rows, list):
                    raise DataError(f"unexpected candles payload for {sym}: {str(rows)[:200]}")
                for t, low, high, op, close, vol in rows:
                    bars[int(t)] = Bar(int(t), float(op), float(high), float(low), float(close), float(vol))
                cursor = chunk_end
                time.sleep(0.15)  # stay well under the public rate limit
            out[sym] = [bars[k] for k in sorted(bars)]
        return out

    def fetch_quotes(self, symbols):
        out = {}
        errors = []
        for sym in symbols:
            try:
                d = get_json(f"{BASE}/products/{sym}/ticker", timeout=5.0, retries=1)
                received = time.time()
                last_ts = _parse_time(d["time"]) if d.get("time") else None
                out[sym] = Quote(sym, received, float(d["bid"]), float(d["ask"]), None, None,
                                 float(d["price"]), last_ts, "coinbase")
            except (DataError, KeyError, TypeError, ValueError) as e:
                errors.append(DataError(f"ticker unavailable for {sym}: {e}"))
                log.warning("%s", errors[-1])
        if not out and errors:
            raise errors[0]
        return out
