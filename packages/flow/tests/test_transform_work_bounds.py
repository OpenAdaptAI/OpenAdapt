"""Synthetic cost regressions; every adversarial evaluation has a hard timeout."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openadapt_flow import transforms as tr

_CHILD = """
import atexit, os, time

def _milestone(phase):
    os.write(2, ("transform-phase " + phase + " " + str(time.monotonic_ns())
                 + " " + str(time.process_time_ns()) + "\\n").encode("ascii"))

atexit.register(_milestone, "atexit")
_milestone("before_imports")
try:
    import json, sys
    from openadapt_flow import transforms as tr
    _milestone("after_imports")
    request = json.loads(sys.argv[1])
    source, params, mode = request['source'], request['params'], request['mode']
    _milestone("before_evaluation")
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
    _milestone("verdict_ready")
    reply['origin'] = tr.__file__
    print(json.dumps(reply))
    _milestone("after_print")
finally:
    _milestone("body_finally")
"""


_MILESTONE = re.compile(
    r"transform-phase (?:before_imports|after_imports|before_evaluation|"
    r"verdict_ready|after_print|body_finally|atexit) [0-9]{1,20} [0-9]{1,20}"
)


def _timeout_note(stderr: bytes | str | None, started: int, observed: int) -> str:
    milestones = None
    if isinstance(stderr, (bytes, str)) and len(stderr) <= 1024:
        text = (
            stderr.decode("ascii", errors="replace")
            if isinstance(stderr, bytes)
            else stderr
        )
        lines = text.splitlines()
        if 0 < len(lines) <= 7 and all(_MILESTONE.fullmatch(line) for line in lines):
            milestones = lines
    # This brackets run(), not its later communicate() deadline. A milestone
    # during atexit also does not prove that interpreter/pipe teardown finished.
    return "transform timeout diagnostics: " + json.dumps(
        {
            "parent_call_start_ns": started,
            "parent_observed_timeout_ns": observed,
            "parent_scope": "run_call_not_exact_communicate_deadline",
            "timeout_seconds": 3,
            "milestones": milestones,
            "process_exit": "unknown",
        }
    )


def run_bounded(mode: str, source: str, params: dict[str, str]) -> dict:
    started = time.monotonic_ns()
    try:
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
    except subprocess.TimeoutExpired as exc:
        print(_timeout_note(exc.stderr, started, time.monotonic_ns()), file=sys.stderr)
        raise
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


@pytest.mark.parametrize("mode", ["evaluate", "predicate", "transform"])
def test_complete_expected_stdout_does_not_waive_child_timeout(
    monkeypatch, capsys, mode
):
    reply = {"code": "bounds", "origin": str(Path(tr.__file__).resolve())}
    if mode == "predicate":
        reply["value"] = "unknown"
    stdout = json.dumps(reply).encode()
    stderr = "".join(
        f"transform-phase {phase} {i} {i}\n"
        for i, phase in enumerate(
            (
                "before_imports",
                "after_imports",
                "before_evaluation",
                "verdict_ready",
                "after_print",
                "body_finally",
                "atexit",
            ),
            start=1,
        )
    ).encode()
    failure = subprocess.TimeoutExpired(
        "synthetic child", 3, output=stdout, stderr=stderr
    )

    def timed_out(*args, **kwargs):
        assert kwargs == dict(capture_output=True, text=True, timeout=3, check=True)
        assert args[0][:3] == [sys.executable, "-c", _CHILD]
        raise failure

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_bounded(mode, "x == 'a'", {"x": "a"})
    assert caught.value is failure
    diagnostic = capsys.readouterr()
    assert diagnostic.out == ""
    note = json.loads(diagnostic.err.removeprefix("transform timeout diagnostics: "))
    assert note["milestones"] == stderr.decode().splitlines()
    assert note["parent_observed_timeout_ns"] >= note["parent_call_start_ns"]
    assert note["parent_scope"] == "run_call_not_exact_communicate_deadline"
    assert note["process_exit"] == "unknown"
    assert "origin" not in diagnostic.err
    assert "bounds" not in diagnostic.err


@pytest.mark.parametrize(
    "stderr, expected",
    [
        (None, None),
        (b"unrelated diagnostic content", None),
        (b"transform-phase value 1 2", None),
        (b"transform-phase atexit 123456789012345678901 2", None),
        (b"transform-phase atexit 1 2\n" * 8, None),
        (b"x" * 1025, None),
        (b"transform-phase atexit 1 2\n\xff", None),
        ("transform-phase atexit 1 2", ["transform-phase atexit 1 2"]),
    ],
)
def test_timeout_milestones_are_bounded_and_do_not_export_other_stderr(
    stderr, expected
):
    note = json.loads(
        _timeout_note(stderr, 1, 2).removeprefix("transform timeout diagnostics: ")
    )
    assert note == {
        "parent_call_start_ns": 1,
        "parent_observed_timeout_ns": 2,
        "parent_scope": "run_call_not_exact_communicate_deadline",
        "timeout_seconds": 3,
        "milestones": expected,
        "process_exit": "unknown",
    }
