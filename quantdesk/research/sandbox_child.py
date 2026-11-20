"""Sandbox child process. Reads one job (JSON) on stdin, writes the result on stdout.

Started with a cleared environment and resource limits by sandbox.py. Untrusted code is
re-checked against the policy, then executed with a restricted globals dict.
"""
import json
import math
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np

from quantdesk.research.sandbox_policy import BUILTINS_ALLOWED, MATH_ALLOWED, NP_ALLOWED, check_source


def _safe_np():
    ns = types.SimpleNamespace()
    for name in NP_ALLOWED:
        setattr(ns, name, getattr(np, name))
    return ns


def _builtins():
    import builtins
    return {k: getattr(builtins, k) for k in BUILTINS_ALLOWED if hasattr(builtins, k)}


def _run(fn, cols, params, upto=None):
    out_e, out_x = [], []
    for col in cols:
        arrs = [np.array(col[k], dtype=float) for k in ("o", "h", "l", "c", "v")]
        if upto is not None:
            arrs = [a[:upto] for a in arrs]
        res = fn(*arrs, dict(params))
        if not (isinstance(res, tuple) and len(res) == 2):
            raise ValueError("signals() must return (entry, exit)")
        e = np.asarray(res[0]).astype(bool)
        x = np.asarray(res[1]).astype(bool)
        if e.shape != arrs[3].shape or x.shape != arrs[3].shape:
            raise ValueError("entry/exit must have the same length as the input series")
        out_e.append(e)
        out_x.append(x)
    return out_e, out_x


def main():
    job = json.loads(sys.stdin.read())
    try:
        check_source(job["code"])
        g = {"__builtins__": _builtins(), "np": _safe_np(), "math": types.SimpleNamespace(
            **{k: getattr(math, k) for k in MATH_ALLOWED})}
        exec(compile(job["code"], "<strategy>", "exec"), g)
        fn = g["signals"]
        cols = job["series"]
        e_full, x_full = _run(fn, cols, job["params"])
        T = len(cols[0]["c"]) if cols else 0
        causal = True
        detail = []
        for frac in (0.5, 0.75, 0.9):
            cut = int(T * frac)
            if cut < 10:
                continue
            e_p, x_p = _run(fn, cols, job["params"], upto=cut)
            for j in range(len(cols)):
                if not (np.array_equal(e_p[j], e_full[j][:cut]) and np.array_equal(x_p[j], x_full[j][:cut])):
                    causal = False
                    detail.append(f"series {j}: signals before bar {cut} change when later bars are removed")
        print(json.dumps({"ok": True, "entry": [e.tolist() for e in e_full],
                          "exit": [x.tolist() for x in x_full], "causal": causal, "detail": detail}))
    except Exception as ex:  # report, never crash silently
        print(json.dumps({"ok": False, "error": f"{type(ex).__name__}: {ex}"[:500]}))


if __name__ == "__main__":
    main()
