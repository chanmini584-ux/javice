import json
import math
import time

import pytest

from jarvis.memory import MemoryStore
from jarvis.tools import ToolError, ToolRegistry, register_builtin_tools, safe_eval


@pytest.fixture
def registry():
    reg = ToolRegistry()
    register_builtin_tools(reg, MemoryStore())
    return reg


def test_safe_eval():
    assert safe_eval("2 ** 10 + 3 * (4 - 1)") == 1033
    assert safe_eval("sqrt(16) + log(e)") == 5.0
    assert math.isclose(safe_eval("sum([0.1, 0.2])"), 0.3)
    for bad in ("__import__('os')", "open('x')", "(1).real", "9 ** 99999"):
        with pytest.raises(ToolError):
            safe_eval(bad)


@pytest.mark.parametrize(
    "expr",
    [
        "factorial(10**6)",
        "(10**5000)**2000",
        "comb(4 * 10**6, 2 * 10**6)",
        "perm(10**6, 5 * 10**5)",
        "round(5, -10**7)",  # rounds by computing 10**(10**7)
        "sum([1] * 10**9)",  # list repetition: a billion-element list
    ],
)
def test_calculate_refuses_runaway_work_without_doing_it(expr):
    # One slow `calculate` call would freeze the agent's only thread (voice loop or web worker).
    started = time.monotonic()
    with pytest.raises(ToolError):
        safe_eval(expr)
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(
    "expr",
    [
        "prod([9**4400, 9**4400, 9**4400])",  # products and lcms grow with their inputs combined
        "lcm(9**4400 + 1, 9**4400 + 3, 9**4400 + 7)",
        "frexp(8) * 3",  # tuples repeat like lists
        "2**20000",  # Python cannot even print an int this long (4,300-digit limit)
    ],
)
def test_calculate_refuses_results_too_big_to_use(expr):
    with pytest.raises(ToolError):
        safe_eval(expr)


def test_calculate_tool_refuses_huge_output(registry):
    out, is_error = registry.execute("calculate", {"expression": "[" + ",".join(["factorial(1000)"] * 120) + "]"})
    assert is_error and len(out) < 200


def test_calculate_keeps_big_but_sane_numbers():
    assert safe_eval("2 ** 64") == 18446744073709551616
    assert safe_eval("comb(10**6, 2)") == 499999500000
    assert safe_eval("factorial(20)") == 2432902008176640000
    assert math.isclose(safe_eval("1.0001 ** 20000"), 7.3870, rel_tol=1e-3)  # float powers stay allowed


def test_definitions_are_sorted_and_strict_shaped(registry):
    defs = registry.definitions()
    names = [d["name"] for d in defs]
    assert names == sorted(names)
    for d in defs:
        assert d["input_schema"]["additionalProperties"] is False


def test_execute_roundtrip(registry):
    out, err = registry.execute("remember", {"kind": "weakness", "content": "통계 해석이 약함", "importance": 4})
    assert not err and "saved memory" in out
    out, err = registry.execute("recall", {"query": "통계"})
    assert not err and json.loads(out)[0]["kind"] == "weakness"


def test_execute_reports_errors_to_model(registry):
    assert registry.execute("nope", {})[1]
    assert registry.execute("calculate", {})[1]  # missing input
    out, err = registry.execute("calculate", {"expression": "1/0"})
    assert err and "ZeroDivisionError" in out


def test_goal_tools(registry):
    registry.execute("add_goal", {"title": "토익 900", "why": "이직", "deadline": "2027-03-01"})
    out, err = registry.execute("save_plan", {"goal_id": 1, "steps": ["모의고사", "약점 분석"]})
    assert not err
    goals = json.loads(registry.execute("list_goals", {})[0])
    assert goals[0]["plan"][1]["description"] == "약점 분석"


def test_run_python_is_confirm_risk(registry):
    assert registry.get("run_python").risk == "confirm"
    out, err = registry.execute("run_python", {"code": "print(6*7)"})
    assert not err and "42" in out
