import numpy as np
import pytest
import sys

from conftest import put_bars


def _panel(T=400, N=3, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, (T, N)), axis=0))
    o = np.vstack([c[:1], c[:-1]])
    return {"ts": np.arange(T) * 86400 + 1_600_000_000, "symbols": ["A", "B", "Q"], "benchmark": "Q",
            "open": o, "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99, "close": c,
            "volume": np.full((T, N), 1e6)}


EXPRS = ["sma(close, 20)", "ema(close, 10)", "std(close, 15)", "ret(close, 5)", "lag(close, 3)", "highest(high, 20)",
         "lowest(low, 20)", "rsi(close, 14)", "zscore(close, 20)", "rvol(20)", "atr(14)", "crossover(sma(close,5), sma(close,20))",
         "rank(ret(close, 10))", "market(close) > market(sma(close, 50))", "log(close) - log(lag(close, 1))",
         "max(close, sma(close, 10)) / min(open, close)"]


@pytest.mark.parametrize("expr", EXPRS)
def test_dsl_functions_are_causal(expr):
    from quantdesk.research import dsl
    p = _panel()
    full = dsl.evaluate(expr, p, {}, 252, market_col=2)
    for cut in (60, 150, 333):
        sub = {k: (v[:cut] if isinstance(v, np.ndarray) else v) for k, v in p.items()}
        part = dsl.evaluate(expr, sub, {}, 252, market_col=2)
        np.testing.assert_allclose(part.astype(float), full[:cut].astype(float), equal_nan=True, rtol=1e-9, atol=1e-9,
                                   err_msg=f"{expr} changed when future bars were removed")


@pytest.mark.parametrize("bad", ["__import__('os')", "close.__class__", "open('x')", "lambda: 1", "close[0]",
                                 "sma(close, 0.5)", "foo(close)", "close == 1", "'a' > 1", "sma(close, n=3)"])
def test_dsl_rejects_unsafe_or_invalid(bad):
    from quantdesk.research import dsl
    with pytest.raises(dsl.DSLError):
        dsl.evaluate(bad, _panel(), {}, 252)


def test_backtest_executes_next_bar_open():
    from quantdesk.config import DEFAULT_SETTINGS
    from quantdesk.research import backtest as B
    from quantdesk.research.strategy import compute_signals
    p = _panel()
    # a jump at bar 200: signal fires at its close; the fill must be at bar 201's open (after the jump)
    p["close"][200:, 0] *= 1.5
    p["open"][201:, 0] *= 1.5
    spec = {"venue": "NASDAQ", "universe": ["A"], "kind": "dsl", "entry": "ret(close, 1) > 0.3", "exit": "close < 0",
            "sizing": {"method": "fixed", "weight": 0.5}}
    sig = compute_signals(spec, {}, p)
    r = B.run(spec, p, sig, DEFAULT_SETTINGS["costs"]["NASDAQ"], 50, 400)
    eq = r.equity  # eq[i] is the close of bar 50+i
    assert eq[150] == pytest.approx(100000, rel=1e-9)        # flat through the signal bar
    assert r.metrics["n_trades"] == 0 and r.open_positions == 1
    intraday = max(0.0, p["close"][201, 0] / p["open"][201, 0] - 1)
    # bought at bar 201's open (after the jump): at most the intraday move, never the +50% jump (= +$25k)
    assert eq[151] <= 100000 * (1 + 0.5 * intraday) + 1


def test_split_is_chronological_and_freeze_precedes_test(env, monkeypatch):
    from quantdesk.config import DEFAULT_SETTINGS
    from quantdesk.research import backtest as B
    from quantdesk.research import experiment as E
    rng = np.random.default_rng(0)
    for s in ("AAPL", "MSFT", "QQQ"):
        put_bars(s, list(100 * np.exp(np.cumsum(rng.normal(0, 0.015, 900)))))
    calls = []
    orig = B.run

    def spy(spec, panel, sig, costs, start, end, **kw):
        calls.append(("run", start, end))
        return orig(spec, panel, sig, costs, start, end, **kw)
    monkeypatch.setattr(E.B, "run", spy)
    spec = {"name": "x", "venue": "NASDAQ", "universe": ["AAPL", "MSFT"], "kind": "dsl", "entry": "close > sma(close, n)",
            "exit": "close < sma(close, n)", "sizing": {"method": "fixed", "weight": 0.3}, "params": {"n": [20, 50]}}
    frozen = {}

    def on_freeze(f):
        calls.append(("freeze",))
        frozen.update(f)
    res = E.run_experiment(spec, DEFAULT_SETTINGS["costs"]["NASDAQ"], DEFAULT_SETTINGS["research"], "QQQ", [],
                           "2026-06-01", on_freeze=on_freeze)
    w = res["windows"]
    assert w["train"]["end"] < w["validation"]["start"] <= w["validation"]["end"] < w["test"]["start"]
    test_start_idx = next(c[1] for c in calls if c[0] == "run" and c[2] == 900 and c[1] > 600)
    fi = calls.index(("freeze",))
    # every backtest touching the test window happens after the freeze
    for i, c in enumerate(calls):
        if c[0] == "run" and c[2] > test_start_idx:
            assert i > fi, "test window evaluated before the spec was frozen"
    assert frozen["chosen_params"] == res["chosen_params"]
    assert res["leakage"]["holdout_reuse_count"] == 0
    res2 = E.run_experiment(spec, DEFAULT_SETTINGS["costs"]["NASDAQ"], DEFAULT_SETTINGS["research"], "QQQ",
                            [(w["test"]["start"], w["test"]["end"])], "2026-06-01")
    assert res2["leakage"]["holdout_reuse_count"] == 1
    assert res2["test"]["deflated_psr"] <= res["test"]["deflated_psr"] + 1e-9


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux sandbox resource/namespace isolation")
def test_sandbox_blocks_and_detects_lookahead():
    from quantdesk.research import sandbox
    from quantdesk.research.sandbox_policy import PolicyError, check_source
    p = _panel(T=200)
    good = "def signals(o, h, l, c, v, p):\n    m = np.full_like(c, np.nan)\n    for i in range(9, len(c)):\n        m[i] = np.mean(c[i-9:i+1])\n    return c > m, c < m\n"
    e, x = sandbox.run_code_signals(good, p, {})
    assert e.shape == (200, 3)
    peek = good.replace("c > m", "np.roll(c, -1) > c")
    with pytest.raises(sandbox.SandboxError, match="look-ahead"):
        sandbox.run_code_signals(peek, p, {})
    for evil in ["import os", "def signals(o,h,l,c,v,p):\n    return open('/etc/passwd'), c",
                 "def signals(o,h,l,c,v,p):\n    return c.__class__, c", "def signals(o,h,l,c,v,p):\n    return np.load('x'), c",
                 "def signals(o,h,l,c,v,p):\n    return c.tofile('x'), c"]:
        with pytest.raises(PolicyError):
            check_source(evil)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux sandbox resource/namespace isolation")
def test_sandbox_has_no_secrets_or_network(monkeypatch):
    from quantdesk.research import sandbox
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-should-not-leak")
    # bypass the static policy to probe the process isolation itself
    import json
    import subprocess
    import sys
    probe = ("import os, socket\nr={'env': dict(os.environ)}\ntry:\n s=socket.create_connection(('1.1.1.1',443),2); r['net']=True\n"
             "except Exception as e:\n r['net']=False\nprint(json.dumps(r))")
    cmd = list(sandbox._netns_prefix()) + [sys.executable, "-I", "-c", "import json\n" + probe]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"},
                       preexec_fn=sandbox._limits)
    out = json.loads(p.stdout)
    assert "ANTHROPIC_API_KEY" not in out["env"]
    if sandbox._netns_prefix():
        assert out["net"] is False


def _spec(**kw):
    s = {"name": "x", "venue": "CRYPTO", "universe": ["BTC-USD"], "kind": "dsl", "entry": "close > sma(close, n)",
         "exit": "close < sma(close, n)", "sizing": {"method": "fixed", "weight": 0.2}, "params": {"n": [24, 1440]}}
    s.update(kw)
    return s


def test_long_hourly_windows_are_allowed():
    from quantdesk.research.strategy import validate_spec
    validate_spec(_spec(), {"CRYPTO": ["BTC-USD"]})          # 60 days of hourly bars


def test_local_sandbox_fails_closed_on_unsupported_platform(env, monkeypatch):
    from quantdesk.research import sandbox
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    with pytest.raises(sandbox.SandboxError, match="requires Linux"):
        sandbox.run_local({})
    assert sandbox.isolation_report()["local_supported"] is False


def test_every_grid_value_is_checked_and_dsl_errors_are_spec_errors():
    import pytest
    from quantdesk.research.strategy import SpecError, validate_spec
    allowed = {"CRYPTO": ["BTC-USD"]}
    with pytest.raises(SpecError, match="window"):
        validate_spec(_spec(params={"n": [24, 9000]}), allowed)   # bad value is not the first one
    with pytest.raises(SpecError, match="window"):
        validate_spec(_spec(entry="close > sma(close, 9000)"), allowed)
    with pytest.raises(SpecError):
        validate_spec(_spec(entry="close >"), allowed)            # syntax error -> repairable, not a crash


@pytest.mark.parametrize("expr", ["lag(close, 0.5)", "close > 1e999", "sma(close, 1e999)"])
def test_dsl_rejects_nonfinite_and_fractional_windows(expr):
    from quantdesk.research import dsl
    with pytest.raises(dsl.DSLError):
        dsl.validate(expr)


@pytest.mark.parametrize("patch", [{"universe": ["BTC-USD", "BTC-USD"]},
                                  {"params": {"n": [float("inf")]}},
                                  {"sizing": {"vol_lookback": -1}},
                                  {"sizing": {"weight": True}},
                                  {"max_hold_bars": True}])
def test_invalid_specs_are_repairable_errors(patch):
    from quantdesk.research.strategy import SpecError, validate_spec
    with pytest.raises(SpecError):
        validate_spec(_spec(**patch), {"CRYPTO": ["BTC-USD"]})


@pytest.mark.parametrize("train,val", [(0.6, 0.15), (0.4, 0.1)])
def test_blinding_excludes_shorter_subset_holdouts_and_respects_custom_splits(env, train, val):
    from quantdesk.research.experiment import build_panel, split_windows
    from quantdesk.research.observe import observe
    start = 1700000100
    put_bars("QQQ", [100.0] * 900, start=start)
    put_bars("AAPL", [100.0] * 1500, start=start)  # extra late bars must not move blinding into QQQ's test
    put_bars("MSFT", [100.0] * 500, start=start + 300 * 900)
    obs = observe("NASDAQ", ["QQQ", "AAPL", "MSFT"], "QQQ", blind_frac=train + val)
    for universe in (["QQQ"], ["AAPL"], ["MSFT"], ["AAPL", "MSFT"]):
        panel = build_panel("NASDAQ", universe, "QQQ")
        test_start = split_windows(len(panel["ts"]), 55, train, val)["test"][0]
        assert obs["as_of"] < panel["ts"][test_start]


def test_buy_and_hold_pays_entry_costs():
    import numpy as np
    from quantdesk.research.backtest import buy_and_hold
    p = np.full((5, 1), 100.0)
    costs = dict(backtest_half_spread_bps=0, base_slippage_bps=0, impact_coef=0, taker_fee_bps=25)
    m = buy_and_hold(dict(symbols=["BTC-USD"], open=p, close=p, ts=np.arange(5)), ["BTC-USD"], 0, 5, costs, "CRYPTO")
    assert m["total_return"] < -0.002
