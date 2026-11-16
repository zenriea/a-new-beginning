"""Shared causal ADV and daily volatility estimator (20 trading days).

Output row t uses input rows strictly before t. The extra final row is the estimate
after all supplied bars have closed, used by paper execution. Prefix sums keep the
backtest linear in history length; partial histories require at least 10 valid bars.
"""
from __future__ import annotations

import numpy as np

LOOKBACK_DAYS = 20
MIN_BARS = 10


def lookback_bars(bars_per_day: float) -> int:
    return int(LOOKBACK_DAYS * bars_per_day)


def estimates(close, volume, bars_per_day: float, lookback: int | None = None):
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    window = lookback if lookback is not None else lookback_bars(bars_per_day)
    if window < 2:
        raise ValueError("liquidity lookback must be at least 2 bars")
    minimum = min(MIN_BARS, window)
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.full_like(close, np.nan)
        returns[1:] = np.log(close[1:] / close[:-1])
        dollar = close * volume
    dollar = np.where((close > 0) & (volume >= 0), dollar, np.nan)
    end = np.arange(len(close) + 1)
    start = np.maximum(0, end - window)

    def rolling(x):
        ok = np.isfinite(x)
        clean = np.where(ok, x, 0.0)
        zero = np.zeros((1,) + x.shape[1:])
        sums = np.concatenate([zero, np.cumsum(clean, axis=0)])
        squares = np.concatenate([zero, np.cumsum(clean * clean, axis=0)])
        counts = np.concatenate([zero, np.cumsum(ok, axis=0)])
        return sums[end] - sums[start], squares[end] - squares[start], counts[end] - counts[start]

    total, _, count = rolling(dollar)
    rsum, rsq, n = rolling(returns)
    with np.errstate(divide="ignore", invalid="ignore"):
        adv = np.where(count >= minimum, total / count * bars_per_day, np.nan)
        variance = np.maximum(0.0, (rsq - rsum * rsum / n) / (n - 1))
        sigma = np.where(n >= minimum, np.sqrt(variance * bars_per_day), np.nan)
    return adv, sigma
