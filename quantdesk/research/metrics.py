from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()


def perf(equity: np.ndarray, ppy: float) -> dict:
    eq = np.asarray(equity, dtype=float)
    if eq.size < 2 or not np.all(np.isfinite(eq)) or eq[0] <= 0:
        return {"n_bars": int(eq.size), "total_return": 0.0, "cagr": 0.0, "ann_vol": 0.0,
                "sharpe": 0.0, "sortino": 0.0, "max_drawdown": 0.0, "skew": 0.0, "kurt": 3.0}
    r = eq[1:] / eq[:-1] - 1.0
    total = eq[-1] / eq[0] - 1.0
    years = r.size / ppy
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 and eq[-1] > 0 else -1.0
    sd = r.std(ddof=1) if r.size > 1 else 0.0
    mu = r.mean()
    sharpe = (mu / sd) * math.sqrt(ppy) if sd > 0 else 0.0
    dn = r[r < 0]
    dsd = math.sqrt((dn ** 2).sum() / max(r.size, 1)) if dn.size else 0.0
    sortino = (mu / dsd) * math.sqrt(ppy) if dsd > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    mdd = float(((eq - peak) / peak).min())
    if sd > 0 and r.size > 3:
        z = (r - mu) / sd
        skew = float((z ** 3).mean())
        kurt = float((z ** 4).mean())
    else:
        skew, kurt = 0.0, 3.0
    return {"n_bars": int(eq.size), "total_return": float(total), "cagr": float(cagr),
            "ann_vol": float(sd * math.sqrt(ppy)), "sharpe": float(sharpe), "sortino": float(sortino),
            "max_drawdown": mdd, "skew": skew, "kurt": kurt, "per_bar_sharpe": float(mu / sd) if sd > 0 else 0.0}


def trade_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n_trades": 0, "win_rate": None, "avg_trade_return": None, "top5_concentration": None}
    pnls = np.array([t["pnl"] for t in trades])
    rets = np.array([t["return"] for t in trades])
    total = pnls.sum()
    top5 = np.sort(pnls)[::-1][:5].clip(min=0).sum()
    conc = float(top5 / total) if total > 0 else None
    return {"n_trades": len(trades), "win_rate": float((pnls > 0).mean()),
            "avg_trade_return": float(rets.mean()), "median_trade_return": float(np.median(rets)),
            "total_trade_pnl": float(total), "top5_concentration": conc}


def psr(per_bar_sharpe: float, n: int, skew: float, kurt: float, sr_benchmark: float = 0.0) -> float:
    """Probabilistic Sharpe Ratio (Bailey & Lopez de Prado), per-bar Sharpe units."""
    if n < 3:
        return 0.0
    denom = 1 - skew * per_bar_sharpe + (kurt - 1) / 4 * per_bar_sharpe ** 2
    if denom <= 0:
        return 0.0
    return float(_N.cdf((per_bar_sharpe - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom)))


def deflated_sr_benchmark(n_trials: int, n_obs: int) -> float:
    """Expected maximum per-bar Sharpe among ``n_trials`` independent zero-skill trials,
    using Var[SR] ~= 1/(n_obs-1)."""
    if n_trials <= 1 or n_obs < 3:
        return 0.0
    g = 0.5772156649
    sd = math.sqrt(1.0 / (n_obs - 1))
    e = math.e
    return sd * ((1 - g) * _N.inv_cdf(1 - 1 / n_trials) + g * _N.inv_cdf(1 - 1 / (n_trials * e)))
