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
    if not sandbox.local_available():
        pytest.skip("requires a separate UID and an available Linux network namespace")
    p = _panel(T=200)
    good = "def signals(o, h, l, c, v, p):\n    m = np.full_like(c, np.nan)\n    for i in range(9, len(c)):\n        m[i] = np.mean(c[i-9:i+1])\n    return c > m, c < m\n"
    e, x = sandbox.run_code_signals(good, p, {}, runner=sandbox.run_local)
    assert e.shape == (200, 3)
    peek = good.replace("c > m", "np.roll(c, -1) > c")
    with pytest.raises(sandbox.SandboxError, match="look-ahead"):
        sandbox.run_code_signals(peek, p, {}, runner=sandbox.run_local)
    for evil in ["import os", "def signals(o,h,l,c,v,p):\n    return open('/etc/passwd'), c",
                 "def signals(o,h,l,c,v,p):\n    return c.__class__, c", "def signals(o,h,l,c,v,p):\n    return np.load('x'), c",
                 "def signals(o,h,l,c,v,p):\n    return c.tofile('x'), c"]:
        with pytest.raises(PolicyError):
            check_source(evil)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux sandbox resource/namespace isolation")
def test_sandbox_has_no_secrets_or_network(monkeypatch):
    from quantdesk.research import sandbox
    if not sandbox.local_available():
        pytest.skip("requires a separate UID and an available Linux network namespace")
    monkeypatch.setenv("QD_TEST_ISOLATION_SENTINEL", "parent-only")
    import json
    import subprocess
    import sys
    probe = ("import json, os, socket\n"
             "print(json.dumps({'has_sentinel': 'QD_TEST_ISOLATION_SENTINEL' in os.environ, "
             "'interfaces': [name for _, name in socket.if_nameindex()]}))")
    cmd = list(sandbox._netns_prefix()) + [sys.executable, "-I", "-c", "import json\n" + probe]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"},
                       preexec_fn=sandbox._limits)
    out = json.loads(p.stdout)
    assert not out["has_sentinel"]
    assert out["interfaces"] == ["lo"]


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


@pytest.mark.parametrize(("uid", "prefix"), [(1000, ("unshare",)), (0, ())])
def test_local_sandbox_requires_both_isolation_boundaries(monkeypatch, uid, prefix):
    from quantdesk.research import sandbox
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "geteuid", lambda: uid, raising=False)
    monkeypatch.setattr(sandbox, "_netns_prefix", lambda: prefix)

    def unexpected_child(*args):
        pytest.fail("local mode started a process without both isolation boundaries")

    monkeypatch.setattr(sandbox, "_run_child", unexpected_child)
    with pytest.raises(sandbox.SandboxError, match="separate UID and network namespace"):
        sandbox.run_local({})


def test_spool_service_refuses_a_network_interface(monkeypatch):
    from quantdesk.research import sandbox_service
    monkeypatch.setattr(sandbox_service.sys, "platform", "linux")
    monkeypatch.setattr(sandbox_service.os, "geteuid", lambda: 10001, raising=False)
    monkeypatch.setattr(sandbox_service.socket, "if_nameindex", lambda: [(1, "lo"), (2, "eth0")])
    with pytest.raises(SystemExit, match="network_mode: none"):
        sandbox_service._check_isolation()
    monkeypatch.setattr(sandbox_service.socket, "if_nameindex", lambda: [(1, "lo")])
    sandbox_service._check_isolation()


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


def test_sandbox_policy_allows_math_and_safe_container_methods():
    from quantdesk.research.sandbox_policy import PolicyError, check_source
    ok = (
        "def signals(o, h, l, c, v, p):\n"
        "    n = int(p.get('n', 10))\n"
        "    scale = math.sqrt(n) + math.log(math.e)\n"
        "    vals = []\n"
        "    vals.append(scale)\n"
        "    return c > np.mean(c), c < np.mean(c)\n"
    )
    check_source(ok)
    with pytest.raises(PolicyError):
        check_source("def signals(o, h, l, c, v, p):\n    return math.system('x'), c\n")


def test_bh_curve_and_db_dumps_never_emit_nonfinite_json():
    import json
    from quantdesk import db
    from quantdesk.research.experiment import _bh_curve
    panel = {"symbols": ["QQQ"], "ts": np.arange(5) * 900, "close": np.array([[100.0], [np.nan], [102.0], [np.nan], [104.0]])}
    curve = _bh_curve(panel, ["QQQ"], 0, 5)
    assert all(np.isfinite(v) for v in curve["v"]) and len(curve["v"]) == 3
    raw = db.dumps({"a": float("nan"), "b": [float("inf"), 1.5], "c": np.array([1.0, np.nan])})
    assert "NaN" not in raw and "Infinity" not in raw
    assert json.loads(raw) == {"a": None, "b": [None, 1.5], "c": [1.0, None]}
