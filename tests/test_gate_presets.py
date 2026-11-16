import copy

from quantdesk.config import DEFAULT_SETTINGS, GATE_PRESETS
from quantdesk.research import gates


def _result(kind="dsl"):
    return {
        "strategy_kind": kind,
        "validation": {"sharpe": 1.0},
        "test": {"sharpe": 1.0, "n_trades": 50, "deflated_psr": 0.99},
        "challenge": {
            "test_cost_x2": {"total_return": 0.05},
            "test_delay_1bar": {"sharpe": 0.5},
            "neighbor_positive_frac": 1.0, "subperiod_positive_frac": 1.0,
            "top5_concentration_pre_test": 0.3,
        },
    }


def test_default_is_explicit_exploratory_and_does_not_alias_presets():
    assert DEFAULT_SETTINGS["gates"] == GATE_PRESETS["exploratory"]
    assert DEFAULT_SETTINGS["gates"] is not GATE_PRESETS["exploratory"]


def test_strict_accepts_good_dsl_but_not_sample_checked_python_or_unknown_kind():
    assert gates.evaluate(_result(), GATE_PRESETS["strict"])["passed"]
    for kind in ("code", None):
        result = gates.evaluate(_result(kind), GATE_PRESETS["strict"])
        assert result["failed"] == ["causal_dsl"]
    assert gates.evaluate(_result("code"), GATE_PRESETS["exploratory"])["passed"]


def test_exploratory_admission_is_not_strict_validation():
    result = copy.deepcopy(_result())
    result["validation"]["sharpe"] = -0.2
    result["test"].update(sharpe=0.1, n_trades=8, deflated_psr=0.3)
    result["challenge"]["test_cost_x2"]["total_return"] = -0.01
    assert gates.evaluate(result, GATE_PRESETS["exploratory"])["passed"]
    strict = gates.evaluate(result, GATE_PRESETS["strict"])
    assert not strict["passed"]
    assert {"validation_sharpe", "test_trades", "deflated_psr", "test_return_cost_x2"} <= set(strict["failed"])
