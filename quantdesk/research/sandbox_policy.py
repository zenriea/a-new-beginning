"""Static policy for model-generated strategy code (shared by parent and child)."""
from __future__ import annotations

import ast

NP_ALLOWED = {
    "abs", "all", "any", "arange", "argmax", "argmin", "argsort", "array", "clip", "concatenate",
    "convolve", "cumprod", "cumsum", "diff", "exp", "fmax", "fmin", "full", "full_like", "isfinite",
    "isnan", "log", "log1p", "logical_and", "logical_not", "logical_or", "maximum", "mean", "median",
    "minimum", "nan", "nanmax", "nanmean", "nanmin", "nanstd", "nansum", "ones", "ones_like",
    "percentile", "quantile", "roll", "sign", "sqrt", "square", "std", "sum", "tanh", "var", "where",
    "zeros", "zeros_like", "inf", "float64", "bool_", "nan_to_num", "empty", "empty_like", "power",
    "floor", "ceil", "round", "isclose",
}
ATTR_ALLOWED = {
    "mean", "std", "sum", "min", "max", "shape", "copy", "astype", "cumsum", "size", "any", "all",
    "argmax", "argmin", "tolist", "reshape", "clip", "ndim", "item", "round", "prod", "cumprod",
    "var", "nonzero", "argsort", "flatten", "ravel", "fill", "T",
}
BUILTINS_ALLOWED = {"range", "len", "min", "max", "abs", "float", "int", "bool", "enumerate",
                    "zip", "sum", "round", "list", "tuple", "dict", "True", "False", "None",
                    "reversed", "sorted", "any", "all", "map", "filter"}
FORBIDDEN_NODES = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef,
                   ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom, ast.With,
                   ast.AsyncWith, ast.AsyncFor, ast.Try)


class PolicyError(ValueError):
    pass


def check_source(src: str) -> None:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise PolicyError(f"syntax error: {e.msg} (line {e.lineno})") from None
    defined = set()
    has_signals = False
    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN_NODES):
            raise PolicyError(f"{type(node).__name__} is not allowed")
        if isinstance(node, ast.FunctionDef):
            defined.add(node.name)
            for a in node.args.args:
                defined.add(a.arg)
            if node.name == "signals":
                has_signals = True
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise PolicyError(f"name {node.id!r} not allowed")
            if isinstance(node.ctx, ast.Store):
                defined.add(node.id)
        if isinstance(node, ast.arg):
            defined.add(node.arg)
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise PolicyError(f"attribute {node.attr!r} not allowed")
            if isinstance(node.value, ast.Name) and node.value.id == "np":
                if node.attr not in NP_ALLOWED:
                    raise PolicyError(f"np.{node.attr} is not on the allowlist")
            elif node.attr not in ATTR_ALLOWED:
                raise PolicyError(f"attribute .{node.attr} is not on the allowlist")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined and node.id not in BUILTINS_ALLOWED and node.id not in ("np", "math"):
                raise PolicyError(f"name {node.id!r} is not available")
    top = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.Assign, ast.Expr))]
    if top:
        raise PolicyError("only function definitions and constant assignments allowed at top level")
    if not has_signals:
        raise PolicyError("code must define signals(o, h, l, c, v, p)")
