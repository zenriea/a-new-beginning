"""Strategy specifications and the per-bar position state machine.

The SAME ``step`` function drives the backtester and live paper trading, so the live
system trades exactly the rules that were tested.

Spec (JSON):
{
  "name": str, "venue": "NASDAQ"|"CRYPTO", "universe": [symbols],
  "kind": "dsl" | "code",
  "entry": expr, "exit": expr,                 # dsl kind
  "code": str,                                 # code kind (runs in the sandbox)
  "rank_by": expr | null, "top_k": int | null, # optional cross-sectional selection
  "sizing": {"method": "fixed"|"inverse_vol", "weight": 0.2, "max_weight": 0.25,
             "target_vol": 0.2, "vol_lookback": 20},
  "stop_loss_pct": float|null, "take_profit_pct": float|null, "max_hold_bars": int|null,
  "params": {name: [values...]}                # grid searched on TRAIN data only
}
Weights are fractions of the strategy's own capital (its sleeve), long-only.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field

import numpy as np

from ..venues import VENUES
from . import dsl

MAX_UNIVERSE = 20


class SpecError(ValueError):
    pass


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def validate_spec(spec: dict, allowed_symbols: dict[str, list[str]], max_combos: int = 12) -> dict:
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    venue = spec.get("venue")
    if venue not in VENUES:
        raise SpecError(f"venue must be one of {list(VENUES)}")
    uni = spec.get("universe") or []
    if not isinstance(uni, list) or not uni or len(uni) > MAX_UNIVERSE or any(not isinstance(s, str) for s in uni):
        raise SpecError(f"universe must list 1..{MAX_UNIVERSE} symbols")
    if len(set(uni)) != len(uni):
        raise SpecError("universe must not contain duplicate symbols")
    bad = [s for s in uni if s not in allowed_symbols.get(venue, [])]
    if bad:
        raise SpecError(f"symbols not in the operator-approved {venue} universe: {bad}")
    kind = spec.get("kind", "dsl")
    if kind not in ("dsl", "code"):
        raise SpecError("kind must be 'dsl' or 'code'")
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        raise SpecError("params must be an object of name -> list of values")
    for k, vals in params.items():
        if not k.isidentifier() or k in dsl.SERIES or k in dsl.FUNCS:
            raise SpecError(f"bad parameter name {k!r}")
        if not isinstance(vals, list) or not vals or len(vals) > 6:
            raise SpecError(f"parameter {k} needs 1..6 candidate values")
        for v in vals:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                raise SpecError(f"parameter {k} values must be finite numbers")
    combos = math.prod(len(v) for v in params.values()) if params else 1
    if combos > max_combos:
        raise SpecError(f"parameter grid has {combos} combinations; limit is {max_combos}")
    grid = param_grid({"params": params})  # every combination, so a bad later value is caught here too
    if kind == "dsl":
        for key in ("entry", "exit"):
            if not spec.get(key):
                raise SpecError(f"dsl strategy needs '{key}'")
            _check_expr(spec[key], grid)
    else:
        if not isinstance(spec.get("code"), str) or len(spec["code"]) > 6000:
            raise SpecError("code strategy needs 'code' (<= 6000 chars)")
        from .sandbox_policy import PolicyError, check_source
        try:
            check_source(spec["code"])
        except PolicyError as e:
            raise SpecError(f"code rejected by sandbox policy: {e}") from None
    if spec.get("rank_by"):
        _check_expr(spec["rank_by"], grid)
        tk = spec.get("top_k")
        if isinstance(tk, bool) or not isinstance(tk, int) or not (1 <= tk <= len(uni)):
            raise SpecError("top_k must be an integer between 1 and len(universe)")
    sz = spec.get("sizing") or {}
    if not isinstance(sz, dict):
        raise SpecError("sizing must be an object")
    method = sz.get("method", "fixed")
    if method not in ("fixed", "inverse_vol"):
        raise SpecError("sizing.method must be fixed or inverse_vol")
    for k in ("weight", "max_weight", "target_vol"):
        if k in sz and (isinstance(sz[k], bool) or not isinstance(sz[k], (int, float)) or not (0 < sz[k] <= 1.0)):
            raise SpecError(f"sizing.{k} must be in (0, 1]")
    lb = sz.get("vol_lookback", 20)
    if isinstance(lb, bool) or not isinstance(lb, int) or not 2 <= lb <= dsl.MAX_WINDOW:
        raise SpecError(f"sizing.vol_lookback must be an integer in [2, {dsl.MAX_WINDOW}]")
    for k in ("stop_loss_pct", "take_profit_pct"):
        v = spec.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1):
            raise SpecError(f"{k} must be in (0,1) or null")
    mh = spec.get("max_hold_bars")
    if mh is not None and (isinstance(mh, bool) or not isinstance(mh, int) or mh < 1):
        raise SpecError("max_hold_bars must be a positive integer or null")
    return spec


def _check_expr(expr: str, grid: list[dict]) -> None:
    """DSL problems are spec errors, so the analyst gets a repair round instead of a crashed experiment."""
    try:
        for combo in grid:
            dsl.validate(expr, combo)
    except dsl.DSLError as e:
        raise SpecError(f"{e} (in `{expr}`)") from None


def param_grid(spec: dict) -> list[dict]:
    params = spec.get("params") or {}
    if not params:
        return [{}]
    keys = sorted(params)
    return [dict(zip(keys, vals)) for vals in itertools.product(*[params[k] for k in keys])]


def describe_rules(spec: dict, params: dict) -> dict:
    """Human-readable rules with parameters substituted."""
    def sub(expr):
        if not expr:
            return expr
        out = expr
        for k, v in sorted(params.items(), key=lambda kv: -len(kv[0])):
            out = _replace_name(out, k, v)
        return out
    return {
        "entry": sub(spec.get("entry")) if spec.get("kind", "dsl") == "dsl" else "custom code (sandboxed)",
        "exit": sub(spec.get("exit")) if spec.get("kind", "dsl") == "dsl" else "custom code (sandboxed)",
        "rank_by": sub(spec.get("rank_by")),
        "top_k": spec.get("top_k"),
        "stop_loss_pct": spec.get("stop_loss_pct"),
        "take_profit_pct": spec.get("take_profit_pct"),
        "max_hold_bars": spec.get("max_hold_bars"),
        "sizing": spec.get("sizing"),
    }


def _replace_name(expr: str, name: str, value) -> str:
    import re
    return re.sub(rf"\b{re.escape(name)}\b", str(value), expr)


@dataclass
class Signals:
    entry: np.ndarray        # (T, N) bool
    exit: np.ndarray         # (T, N) bool
    score: np.ndarray | None  # (T, N) float
    vol: np.ndarray          # (T, N) annualised realised vol (for inverse-vol sizing)


def compute_signals(spec: dict, params: dict, panel: dict, sandbox_runner=None) -> Signals:
    venue = VENUES[spec["venue"]]
    syms = panel["symbols"]
    market_col = None
    bench = panel.get("benchmark")
    if bench and bench in syms:
        market_col = syms.index(bench)
    if spec.get("kind", "dsl") == "dsl":
        entry = dsl.evaluate(spec["entry"], panel, params, venue.periods_per_year, market_col, as_bool=True)
        exit_ = dsl.evaluate(spec["exit"], panel, params, venue.periods_per_year, market_col, as_bool=True)
    else:
        from .sandbox import run_code_signals
        entry, exit_ = run_code_signals(spec["code"], panel, params, runner=sandbox_runner)
    score = None
    if spec.get("rank_by"):
        score = dsl.evaluate(spec["rank_by"], panel, params, venue.periods_per_year, market_col)
    lb = int((spec.get("sizing") or {}).get("vol_lookback", 20))
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(panel["close"] / dsl.lag(panel["close"], 1))
    vol = dsl.std(lr, max(lb, 2)) * math.sqrt(venue.periods_per_year)
    return Signals(entry, exit_, score, vol)


@dataclass
class PosState:
    in_position: bool = False
    entry_price: float | None = None
    entry_bar: int | None = None
    bars_held: int = 0


@dataclass
class StepResult:
    enter: list[int] = field(default_factory=list)   # column indices to buy
    exit: list[tuple[int, str]] = field(default_factory=list)  # (col, reason)


def step(spec: dict, states: list[PosState], t: int, sig: Signals, close_row: np.ndarray) -> StepResult:
    """Advance all symbols' state machines using information available at the close of bar t.

    Must be called once per bar in order. Mutates ``states`` and returns intended actions;
    the caller executes them at the next opportunity (next bar open in backtests, the next
    live quote in paper trading).
    """
    res = StepResult()
    sl = spec.get("stop_loss_pct")
    tp = spec.get("take_profit_pct")
    mh = spec.get("max_hold_bars")
    n = len(states)
    # 1) exits for held positions
    for j in range(n):
        st = states[j]
        if not st.in_position:
            continue
        st.bars_held += 1
        px = close_row[j]
        reason = None
        if not np.isfinite(px):
            continue  # no data for this bar: hold, can't evaluate
        if bool(sig.exit[t, j]):
            reason = "exit rule"
        elif sl and st.entry_price and px <= st.entry_price * (1 - sl):
            reason = f"stop-loss {sl:.0%}"
        elif tp and st.entry_price and px >= st.entry_price * (1 + tp):
            reason = f"take-profit {tp:.0%}"
        elif mh and st.bars_held >= mh:
            reason = f"max holding period {mh} bars"
        if reason:
            res.exit.append((j, reason))
    exiting = {j for j, _ in res.exit}
    # 2) entries (and optional top-k rotation)
    candidates = [j for j in range(n) if not states[j].in_position and bool(sig.entry[t, j])
                  and np.isfinite(close_row[j])]
    if spec.get("rank_by") and spec.get("top_k"):
        k = int(spec["top_k"])
        held = [j for j in range(n) if states[j].in_position and j not in exiting]
        pool = held + candidates
        scores = {j: sig.score[t, j] for j in pool}
        ranked = sorted([j for j in pool if np.isfinite(scores[j])], key=lambda j: -scores[j])
        keep = set(ranked[:k])
        for j in held:
            if j not in keep:
                res.exit.append((j, f"rotated out of top {k}"))
                exiting.add(j)
        candidates = [j for j in candidates if j in keep]
    for j, _ in res.exit:
        states[j].in_position = False
        states[j].entry_price = None
        states[j].entry_bar = None
        states[j].bars_held = 0
    for j in candidates:
        states[j].in_position = True
        states[j].entry_price = float(close_row[j])  # provisional; replaced by fill price
        states[j].entry_bar = t
        states[j].bars_held = 0
        res.enter.append(j)
    return res


def entry_weight(spec: dict, vol_now: float) -> float:
    sz = spec.get("sizing") or {}
    max_w = float(sz.get("max_weight", sz.get("weight", 0.2)))
    if sz.get("method", "fixed") == "inverse_vol":
        tv = float(sz.get("target_vol", 0.15))
        if not np.isfinite(vol_now) or vol_now <= 0:
            return 0.0
        return float(min(max_w, tv / vol_now))
    return float(min(max_w, float(sz.get("weight", 0.2))))
