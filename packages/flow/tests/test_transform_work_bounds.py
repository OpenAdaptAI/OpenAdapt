"""Synthetic cost regressions; every adversarial evaluation has a hard timeout."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from openadapt_flow import transforms as tr

_CHILD = """
import json, sys
from openadapt_flow import transforms as tr
request = json.loads(sys.argv[1])
source, params, mode = request['source'], request['params'], request['mode']
try:
    if mode == 'predicate':
        result = tr.evaluate_predicate(source, params)
        reply = {'code': result.refusal.value if result.refusal else None,
                 'value': result.outcome.value}
    elif mode == 'transform':
        out, _ = tr.apply_transforms(params, [tr.ParamTransform(
            output_param='result', expression=source)])
        reply = {'code': None, 'value': out['result']}
    elif mode == 'batch':
        original = dict(params)
        try:
            tr.apply_transforms(params, [tr.ParamTransform(
                output_param='result_' + str(i), expression=source)
                for i in range(tr.MAX_TRANSFORMS)])
        finally:
            assert params == original
        reply = {'code': None}
    else:
        reply = {'code': None, 'value': tr.evaluate(source, params).value}
except tr.ExpressionRefused as exc:
    reply = {'code': exc.code.value}
except ValueError as exc:
    # ParamTransform validates patterns before evaluation. Preserve the
    # declared refusal, but never accept an arbitrary validation exception.
    from pydantic import ValidationError
    if mode != 'transform' or not isinstance(exc, ValidationError):
        raise
    assert 'unsafe_pattern:' in str(exc)
    reply = {'code': 'unsafe_pattern'}
reply['origin'] = tr.__file__
print(json.dumps(reply))
"""


def run_bounded(mode: str, source: str, params: dict[str, str]) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            json.dumps(
                {
                    "mode": mode,
                    "source": source,
                    "params": params,
                }
            ),
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    reply = json.loads(result.stdout)
    assert Path(reply.pop("origin")).resolve() == Path(tr.__file__).resolve()
    return reply


@pytest.mark.parametrize("mode", ["evaluate", "predicate", "transform"])
@pytest.mark.parametrize(
    "pattern, size, code",
    [
        ("(a?){1000}b", 16, "unsafe_pattern"),
        ("(a?){0100}b", 16, "unsafe_pattern"),
        ("a{0,100}" * 4 + "b", 200, "bounds"),
        ("(a|aa)" * 28 + "b", 56, "bounds"),
        ("(((a{100}){100}){100}){100}", 1, "unsafe_pattern"),
    ],
)
def test_backtracking_work_refuses_before_matching(mode, pattern, size, code):
    source = "matches(x, " + repr(pattern) + ")"
    assert run_bounded(mode, source, {"x": "a" * size})["code"] == code


@pytest.mark.parametrize("mode", ["evaluate", "predicate", "transform"])
@pytest.mark.parametrize(
    "source, params, expected",
    [
        (r"matches(x, '\\w*a\\w*b')", {"x": "a" * 512}, False),
        ("matches(x, '(ab){2}')", {"x": "abab"}, True),
        ("matches(x, '(cat|dog)?')", {"x": "dog"}, True),
        ("lower(x) == 'abc'", {"x": "ABC"}, True),
    ],
)
def test_ordinary_results_survive_shared_cost_accounting(
    mode, source, params, expected
):
    reply = run_bounded(mode, source, params)
    assert reply["code"] is None
    assert reply["value"] == (str(expected).lower() if mode != "evaluate" else expected)


@pytest.mark.parametrize("mode", ["evaluate", "predicate", "transform"])
def test_unicode_expansion_refuses_across_entrypoints(mode):
    source = "fuzzy_match(x, ['a'], 0.9) == 'a'"
    assert run_bounded(mode, source, {"x": "\ufdfa" * 256})["code"] == "bounds"


@pytest.mark.parametrize("mode", ["evaluate", "predicate", "transform"])
def test_text_size_precedes_unicode_copy(mode):
    # A length refusal must happen before UTF-8 allocation/validation.
    assert run_bounded(mode, "x == 'a'", {"x": "\udc00" * 10_001})["code"] == "bounds"


def test_transform_batch_cannot_reset_aggregate_budget():
    assert (
        run_bounded("batch", "lower(upper(x))", {"x": "a" * 10_000})["code"] == "bounds"
    )


def test_parameter_count_has_one_shared_bound():
    params = {"p" + str(i): "a" for i in range(tr.MAX_PARAMS + 1)}
    assert run_bounded("evaluate", "true", params)["code"] == "bounds"


def test_completion_of_last_operator_checks_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(tr.time, "monotonic", lambda: clock[0])
    original = tr._STDLIB["trim"]

    def late_trim(value):
        clock[0] = tr.MAX_EVAL_SECONDS + 1
        return value.strip()

    monkeypatch.setitem(
        tr._STDLIB, "trim", tr._Fn(original.args, original.result, late_trim)
    )
    with pytest.raises(tr.ExpressionRefused) as exc:
        tr.evaluate("trim(x)", {"x": " a "})
    assert exc.value.code is tr.RefusalCode.BOUNDS
