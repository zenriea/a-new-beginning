"""The fast kernels must give exactly the same answers as the straightforward versions."""
import time

import numpy as np
import pytest


def _ema_reference(x, n):
    a = 2.0 / (n + 1.0)
    out = np.full_like(x, np.nan)
    prev = np.full(x.shape[1:], np.nan)
    count = np.zeros(x.shape[1:])
    for t in range(x.shape[0]):
        v = x[t]
        ok = ~np.isnan(v)
        start = ok & np.isnan(prev)
        prev = np.where(start, v, prev)
        upd = ok & ~start
        prev = np.where(upd, a * v + (1 - a) * prev, prev)
        count = count + ok
        out[t] = np.where(count >= n, prev, np.nan)
    return out


@pytest.mark.parametrize("n", [1, 2, 5, 37])
def test_ema_matches_reference_including_gaps(n):
    from quantdesk.research import dsl
    rng = np.random.default_rng(n)
    x = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (400, 4)), axis=0))
    x[:30, 1] = np.nan                 # late listing
    x[rng.random((400, 4)) < 0.05] = np.nan  # random gaps
    x[:, 3] = np.nan                   # no data at all
    np.testing.assert_array_equal(dsl.ema(x, n), _ema_reference(x, n))


def test_backtest_runtime_is_linear_in_history():
    """Per-bar work must not copy whole signal arrays (that made long histories quadratic)."""
    from quantdesk.research import backtest as B
    from quantdesk.research.strategy import compute_signals
    rng = np.random.default_rng(0)

    def panel(T):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (T, 3)), axis=0))
        return {"ts": np.arange(T) * 3600, "symbols": ["A-USD", "B-USD", "C-USD"], "open": c, "high": c * 1.001,
                "low": c * 0.999, "close": c, "volume": np.full((T, 3), 1e6), "benchmark": None}
    spec = {"venue": "CRYPTO", "universe": ["A-USD", "B-USD", "C-USD"], "kind": "dsl", "entry": "close > sma(close, 20)",
            "exit": "close < sma(close, 20)", "sizing": {"method": "fixed", "weight": 0.3}}
    costs = {"taker_fee_bps": 25.0, "base_slippage_bps": 2.0, "impact_coef": 0.8, "backtest_half_spread_bps": 1.5}

    def timed(T):
        p = panel(T)
        sig = compute_signals(spec, {}, p)
        t0 = time.perf_counter()
        B.run(spec, p, sig, costs, 30, T)
        return time.perf_counter() - t0
    small, big = timed(8000), timed(32000)
    assert big < small * 6  # 4x the bars: linear ~4x, quadratic ~16x
