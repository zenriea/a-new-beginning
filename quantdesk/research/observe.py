"""Deterministic market observation: what changed, computed from stored bars only."""
from __future__ import annotations

import math

import numpy as np

from ..marketdata import store
from ..venues import VENUES
from . import dsl


def _f(x, nd=4):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def observe(venue: str, symbols: list[str], benchmark: str, end_ts: int | None = None,
            blind_frac: float | None = None) -> dict:
    """Summary statistics per symbol and notable changes.

    ``blind_frac``: if set (e.g. 0.75), only the first fraction of stored history is used
    - this is the blinded research view that cannot overlap any future test window.
    """
    v = VENUES[venue]
    syms = list(dict.fromkeys(symbols + [benchmark]))
    panel = store.load_panel(syms, v.timeframe, end=end_ts)
    T = len(panel["ts"])
    if T < 30:
        return {"venue": venue, "error": f"only {T} bars stored", "symbols": {}, "notable": []}
    if blind_frac:
        cut = int(T * blind_frac)
        for k in ("ts", "open", "high", "low", "close", "volume"):
            panel[k] = panel[k][:cut]
        T = cut
    c = panel["close"]
    ppy = v.periods_per_year
    # one day, one week, one month of bars
    short, mid, long_ = (26, 130, 520) if venue == "NASDAQ" else (96, 672, 2880)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(c / dsl.lag(c, 1))
    vol_s = dsl.std(lr, mid) * math.sqrt(ppy)
    vol_l = dsl.std(lr, long_ * 2 if T > long_ * 2 + 5 else long_) * math.sqrt(ppy)
    sma_l = dsl.sma(c, min(long_, T - 1))
    hi = dsl.highest(c, min(long_ * 3, T))
    out = {}
    notable = []
    bcol = syms.index(benchmark)
    for j, s in enumerate(syms):
        col = c[:, j]
        last_i = int(np.where(np.isfinite(col))[0][-1]) if np.isfinite(col).any() else None
        if last_i is None:
            continue
        def r(n):
            return col[last_i] / col[last_i - n] - 1 if last_i - n >= 0 and np.isfinite(col[last_i - n]) else None
        row = {
            "last": _f(col[last_i], 2), "as_of": int(panel["ts"][last_i]),
            f"ret_{short}": _f(r(short)), f"ret_{mid}": _f(r(mid)), f"ret_{long_}": _f(r(long_)),
            "vol_recent": _f(vol_s[last_i, j]), "vol_longer": _f(vol_l[last_i, j]),
            "vs_long_sma": _f(col[last_i] / sma_l[last_i, j] - 1 if np.isfinite(sma_l[last_i, j]) else None),
            "from_high": _f(col[last_i] / hi[last_i, j] - 1 if np.isfinite(hi[last_i, j]) else None),
        }
        if j != bcol:
            a, b = lr[-mid * 3:, j], lr[-mid * 3:, bcol]
            ok = np.isfinite(a) & np.isfinite(b)
            row["corr_benchmark"] = _f(np.corrcoef(a[ok], b[ok])[0, 1]) if ok.sum() > 10 else None
        out[s] = row
        vr = (row["vol_recent"] or 0) / row["vol_longer"] if row["vol_longer"] else None
        if vr and vr > 1.5:
            notable.append({"symbol": s, "kind": "volatility_up",
                            "text": f"{s} realised vol {row['vol_recent']:.0%} vs {row['vol_longer']:.0%} longer-run"})
        if vr and vr < 0.6:
            notable.append({"symbol": s, "kind": "volatility_down",
                            "text": f"{s} realised vol compressed to {row['vol_recent']:.0%} from {row['vol_longer']:.0%}"})
        if row["from_high"] is not None and row["from_high"] >= -0.001:
            notable.append({"symbol": s, "kind": "new_high", "text": f"{s} closed at a 3-month high ({row['last']})"})
        sd = vol_s[last_i, j] / math.sqrt(ppy) if np.isfinite(vol_s[last_i, j]) else None
        last_ret = lr[last_i, j]
        if sd and np.isfinite(last_ret) and abs(last_ret) > 3 * sd:
            notable.append({"symbol": s, "kind": "large_move",
                            "text": f"{s} moved {math.expm1(last_ret):+.1%} in the last bar (~{abs(last_ret) / sd:.1f} sd)"})
    # regime flip of the benchmark vs its long SMA over the last few bars
    bc = c[:, bcol]
    above = bc > sma_l[:, bcol]
    if T > short + 1 and np.isfinite(sma_l[-1, bcol]) and np.isfinite(sma_l[-short - 1, bcol]):
        if bool(above[-1]) != bool(above[-short - 1]):
            notable.append({"symbol": benchmark, "kind": "regime_flip",
                            "text": f"{benchmark} crossed {'above' if above[-1] else 'below'} its long moving average"})
    rets = [out[s].get(f"ret_{mid}") for s in symbols if s in out and out[s].get(f"ret_{mid}") is not None]
    disp = float(np.std(rets)) if len(rets) > 2 else None
    return {"venue": venue, "bars": T, "as_of": int(panel["ts"][-1]), "horizons": [short, mid, long_],
            "timeframe": v.timeframe, "benchmark": benchmark, "symbols": out, "notable": notable,
            "cross_sectional_dispersion": _f(disp), "blinded": bool(blind_frac)}
