"""SYNTHETIC market data for demo mode and tests. Never presented as real: every journal
entry, quote and the UI banner carry the 'synthetic' label when this provider is active.

Prices follow a seeded regime-switching random walk, so results are reproducible.
"""
from __future__ import annotations

import hashlib
import math
from functools import lru_cache

import numpy as np

from .. import clock
from .base import Bar, Provider, Quote


def _seed(sym: str) -> int:
    return int(hashlib.sha256(sym.encode()).hexdigest()[:8], 16)


class SyntheticProvider(Provider):
    name = "synthetic"
    synthetic = True

    def __init__(self, venue: str):
        self.venue = venue

    def _series(self, sym: str, step: int, start: int, end: int) -> list[Bar]:
        anchor = 1_500_000_000 - (1_500_000_000 % step)
        n_total = (end - anchor) // step + 1
        o, h, l, c, v = _path(sym, step, int(n_total))
        first = max(0, (start - anchor + step - 1) // step)
        return [Bar(anchor + i * step, float(o[i]), float(h[i]), float(l[i]), float(c[i]), float(v[i]))
                for i in range(int(first), int(n_total))]

    def fetch_bars(self, symbols, timeframe, start, end):
        step = 3600 if timeframe == "1h" else 86400
        return {s: [b for b in self._series(s, step, start, end) if b.ts + step <= end] for s in symbols}

    def fetch_quotes(self, symbols):
        out = {}
        now = clock.now()
        for s in symbols:
            step = 3600 if self.venue == "CRYPTO" else 86400
            bars = self._series(s, step, int(now) - 3 * step, int(now))
            last = bars[-1].close if bars else 100.0
            # small deterministic wiggle within the bar
            wig = math.sin(now / 60.0 + _seed(s) % 7) * 0.001
            mid = last * (1 + wig)
            half = mid * 0.0002
            out[s] = Quote(s, now, mid - half, mid + half, 100, 100, mid, now, "synthetic")
        return out


@lru_cache(maxsize=64)
def _path(sym: str, step: int, n: int):
    rng = np.random.default_rng(_seed(sym) + step)
    vol = 0.6 / math.sqrt(24 * 365) if step == 3600 else 0.30 / math.sqrt(252)
    lengths = rng.integers(50, 400, size=n // 50 + 2)
    drifts = rng.normal(0.0, vol * 0.03, size=lengths.size)
    drift = np.repeat(drifts, lengths)[:n]
    shocks = rng.standard_t(5, size=n) / math.sqrt(5 / 3)
    r = drift + vol * shocks
    c = (100.0 + (_seed(sym) % 400)) * np.exp(np.cumsum(r))
    o = np.concatenate([[c[0] / math.exp(r[0])], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol * 0.4, size=n)))
    l = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol * 0.4, size=n)))
    dollar = (5e9 if step == 86400 else 2e7) * rng.lognormal(0.0, 0.4, size=n)
    v = dollar / c
    return o, h, l, c, v
