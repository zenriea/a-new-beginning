"""Strategy expression language.

Expressions are parsed with Python's ``ast`` (mode='eval') and evaluated by this module's
own interpreter - never ``eval``. Only whitelisted names and functions exist. Every
function is causal: the value at bar t depends only on bars <= t.

Values are 2-D arrays shaped (T, N): T bars x N symbols.

Names:      open high low close volume, plus strategy parameters
Functions:  sma ema std ret lag highest lowest rsi zscore rvol atr crossover crossunder
            abs log min max rank market
Operators:  + - * /  < <= > >=  and or not
"""
from __future__ import annotations

import ast
import math

import numpy as np

SERIES = ("open", "high", "low", "close", "volume")
FUNCS = {
    "sma": 2, "ema": 2, "std": 2, "ret": 2, "lag": 2, "highest": 2, "lowest": 2, "rsi": 2,
    "zscore": 2, "rvol": 1, "atr": 1, "crossover": 2, "crossunder": 2, "abs": 1, "log": 1,
    "min": 2, "max": 2, "rank": 1, "market": 1,
}
WINDOW_FUNCS = {"sma", "ema", "std", "ret", "lag", "highest", "lowest", "rsi", "zscore"}
MAX_WINDOW = 4000  # ~5.5 months of hourly crypto bars; slow trend filters need more than 1000


class DSLError(ValueError):
    pass


# ---------------------------------------------------------------------------- parsing
def parse(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip():
        raise DSLError("expression must be a non-empty string")
    if len(expr) > 1000:
        raise DSLError("expression too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise DSLError(f"syntax error: {e.msg}") from None
    return tree


def validate(expr: str, params: dict | None = None) -> set[str]:
    """Static check; returns the parameter names referenced. Raises DSLError."""
    params = params or {}
    used: set[str] = set()

    def visit(node, depth=0):
        if depth > 40:
            raise DSLError("expression nested too deeply")
        if isinstance(node, ast.Expression):
            return visit(node.body, depth + 1)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise DSLError(f"only numeric constants allowed, got {node.value!r}")
            if not math.isfinite(node.value):
                raise DSLError("numeric constants must be finite")
            return
        if isinstance(node, ast.Name):
            if node.id in SERIES:
                return
            if node.id in params:
                used.add(node.id)
                return
            raise DSLError(f"unknown name {node.id!r}")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.Not, ast.UAdd)):
            return visit(node.operand, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            visit(node.left, depth + 1)
            return visit(node.right, depth + 1)
        if isinstance(node, ast.BoolOp):
            for v in node.values:
                visit(v, depth + 1)
            return
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if not isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                    raise DSLError("only < <= > >= comparisons are allowed")
            visit(node.left, depth + 1)
            for c in node.comparators:
                visit(c, depth + 1)
            return
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCS:
                raise DSLError(f"unknown function {ast.unparse(node.func)!r}")
            if node.keywords:
                raise DSLError("keyword arguments are not supported")
            name = node.func.id
            want = FUNCS[name]
            if len(node.args) != want:
                raise DSLError(f"{name}() takes {want} argument(s)")
            if name in WINDOW_FUNCS or name in ("rvol", "atr"):  # same window check the evaluator applies
                _int_arg(node.args[1 if name in WINDOW_FUNCS else 0], Context({}, params, 0, None), name)
            for a in node.args:
                visit(a, depth + 1)
            return
        raise DSLError(f"unsupported syntax: {type(node).__name__}")

    visit(parse(expr))
    return used


# ---------------------------------------------------------------------------- kernels
def _roll_sum(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan)
    if n > x.shape[0]:
        return out
    valid = ~np.isnan(x)
    xs = np.where(valid, x, 0.0)
    c = np.cumsum(xs, axis=0)
    cv = np.cumsum(valid, axis=0)
    s = c[n - 1:].copy()
    s[1:] -= c[:-n]
    cnt = cv[n - 1:].copy()
    cnt[1:] -= cv[:-n]
    s[cnt < n] = np.nan
    out[n - 1:] = s
    return out


def sma(x, n):
    return _roll_sum(x, n) / n


def std(x, n):
    m = sma(x, n)
    m2 = sma(x * x, n)
    var = np.maximum(m2 - m * m, 0.0) * n / max(n - 1, 1)
    return np.sqrt(var)


def ema(x, n):
    """Exponential average seeded with the first valid value. Gaps (NaN) are skipped and
    carry the previous value; output starts once n valid values have been seen."""
    a = 2.0 / (n + 1.0)
    b = 1.0 - a
    out = np.full_like(x, np.nan)
    flat = x.reshape(x.shape[0], -1)
    res = out.reshape(x.shape[0], -1)
    for j in range(flat.shape[1]):  # plain floats per column: ~20x faster than per-row numpy
        col = flat[:, j].tolist()
        dst = [math.nan] * len(col)
        prev = math.nan
        count = 0
        for t, v in enumerate(col):
            if v == v:  # not NaN
                prev = v if prev != prev else a * v + b * prev
                count += 1
            if count >= n:
                dst[t] = prev
        res[:, j] = dst
    return out


def lag(x, n):
    out = np.full_like(x, np.nan)
    if n == 0:
        return x.copy()
    if n < x.shape[0]:
        out[n:] = x[:-n]
    return out


def ret(x, n):
    with np.errstate(divide="ignore", invalid="ignore"):
        return x / lag(x, n) - 1.0


def _roll_ext(x, n, fn):
    out = np.full_like(x, np.nan)
    if n > x.shape[0]:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    w = sliding_window_view(x, n, axis=0)
    with np.errstate(invalid="ignore"):
        out[n - 1:] = fn(w, axis=-1)
    return out


def highest(x, n):
    return _roll_ext(x, n, np.max)


def lowest(x, n):
    return _roll_ext(x, n, np.min)


def rsi(x, n):
    d = np.diff(x, axis=0, prepend=np.nan)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    up[np.isnan(d)] = np.nan
    dn[np.isnan(d)] = np.nan
    # Wilder smoothing == EMA with alpha 1/n  ->  span 2n-1
    au = ema(up, 2 * n - 1)
    ad = ema(dn, 2 * n - 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
        out = 100 - 100 / (1 + rs)
    out = np.where((ad == 0) & (au > 0), 100.0, out)
    return out


def zscore(x, n):
    with np.errstate(divide="ignore", invalid="ignore"):
        return (x - sma(x, n)) / std(x, n)


def rank(x):
    """Cross-sectional percentile rank in [0,1] at each bar (NaN stays NaN)."""
    out = np.full_like(x, np.nan)
    for t in range(x.shape[0]):
        row = x[t]
        ok = ~np.isnan(row)
        k = int(ok.sum())
        if k == 0:
            continue
        order = row[ok].argsort().argsort()
        out[t, ok] = order / (k - 1) if k > 1 else 0.5
    return out


# ---------------------------------------------------------------------------- evaluation
class Context:
    def __init__(self, panel: dict, params: dict, periods_per_year: float, market_col: int | None):
        self.panel = panel
        self.params = params
        self.ppy = periods_per_year
        self.market_col = market_col


def _int_arg(node, ctx: Context, fname: str) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        v = node.value
    elif isinstance(node, ast.Name) and node.id in ctx.params:
        v = ctx.params[node.id]
    else:
        raise DSLError(f"window argument of {fname}() must be a number or parameter")
    minimum = 0 if fname == "lag" else 1
    if isinstance(v, bool) or not math.isfinite(v) or not float(v).is_integer() or not minimum <= v <= MAX_WINDOW:
        raise DSLError(f"window of {fname}() must be an integer in [{minimum}, {MAX_WINDOW}], got {v}")
    return int(v)


def _ev(node, ctx: Context):
    p = ctx.panel
    if isinstance(node, ast.Expression):
        return _ev(node.body, ctx)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in SERIES:
            return p[node.id]
        return float(ctx.params[node.id])
    if isinstance(node, ast.UnaryOp):
        v = _ev(node.operand, ctx)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return v
        return ~_bool(v)
    if isinstance(node, ast.BinOp):
        a, b = _ev(node.left, ctx), _ev(node.right, ctx)
        with np.errstate(divide="ignore", invalid="ignore"):
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            r = np.divide(a, b)
            return np.where(np.isinf(r), np.nan, r) if isinstance(r, np.ndarray) else (
                r if math.isfinite(r) else float("nan"))
    if isinstance(node, ast.BoolOp):
        vals = [_bool(_ev(v, ctx)) for v in node.values]
        out = vals[0]
        for v in vals[1:]:
            out = (out & v) if isinstance(node.op, ast.And) else (out | v)
        return out
    if isinstance(node, ast.Compare):
        left = _ev(node.left, ctx)
        out = None
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, ctx)
            with np.errstate(invalid="ignore"):
                if isinstance(op, ast.Lt):
                    r = np.less(left, right)
                elif isinstance(op, ast.LtE):
                    r = np.less_equal(left, right)
                elif isinstance(op, ast.Gt):
                    r = np.greater(left, right)
                else:
                    r = np.greater_equal(left, right)
            out = r if out is None else (out & r)
            left = right
        return out
    if isinstance(node, ast.Call):
        name = node.func.id
        args = node.args
        if name in WINDOW_FUNCS:
            x = _arr(_ev(args[0], ctx), p)
            n = _int_arg(args[1], ctx, name)
            return globals()[name](x, n)
        if name == "rvol":
            n = _int_arg(args[0], ctx, name)
            with np.errstate(divide="ignore", invalid="ignore"):
                lr = np.log(p["close"] / lag(p["close"], 1))
            return std(lr, n) * math.sqrt(ctx.ppy)
        if name == "atr":
            n = _int_arg(args[0], ctx, name)
            pc = lag(p["close"], 1)
            tr = np.fmax(p["high"] - p["low"], np.fmax(np.abs(p["high"] - pc), np.abs(p["low"] - pc)))
            return sma(tr, n)
        if name in ("crossover", "crossunder"):
            a = _arr(_ev(args[0], ctx), p)
            b = _arr(_ev(args[1], ctx), p)
            with np.errstate(invalid="ignore"):
                if name == "crossover":
                    return (a > b) & (lag(a, 1) <= lag(b, 1))
                return (a < b) & (lag(a, 1) >= lag(b, 1))
        if name == "abs":
            return np.abs(_ev(args[0], ctx))
        if name == "log":
            with np.errstate(divide="ignore", invalid="ignore"):
                v = np.log(_ev(args[0], ctx))
            return np.where(np.isfinite(v), v, np.nan)
        if name in ("min", "max"):
            a, b = _ev(args[0], ctx), _ev(args[1], ctx)
            return np.fmin(a, b) if name == "min" else np.fmax(a, b)
        if name == "rank":
            return rank(_arr(_ev(args[0], ctx), p))
        if name == "market":
            if ctx.market_col is None:
                raise DSLError("market() needs the venue benchmark in the data panel")
            v = _arr(_ev(args[0], ctx), p)
            col = v[:, ctx.market_col:ctx.market_col + 1]
            return np.repeat(col, v.shape[1], axis=1)
    raise DSLError(f"cannot evaluate {ast.dump(node)[:80]}")


def _arr(v, p):
    if isinstance(v, np.ndarray):
        return v.astype(float) if v.dtype == bool else v
    return np.full(p["close"].shape, float(v))


def _bool(v):
    if isinstance(v, np.ndarray):
        if v.dtype == bool:
            return v
        with np.errstate(invalid="ignore"):
            return np.nan_to_num(v, nan=0.0) != 0
    return bool(v) and math.isfinite(v)


def evaluate(expr: str, panel: dict, params: dict, periods_per_year: float,
             market_col: int | None = None, as_bool: bool = False) -> np.ndarray:
    validate(expr, params)
    ctx = Context(panel, params, periods_per_year, market_col)
    v = _ev(parse(expr), ctx)
    v = _arr(v, panel) if not (isinstance(v, np.ndarray) and v.dtype == bool) else v
    if as_bool:
        return _bool(v)
    return v
