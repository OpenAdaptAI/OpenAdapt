"""Regression tests: EvaluatorClient must not render a failure as a result.

The reported defect: ``_load_evaluators`` swallowed ``ImportError``, so a broken
WAA evaluators install and a healthy one produced the same client, and every
later run was silently scored by the built-in fallback evaluator. PR #275 added
a ``logger.warning`` and returned the same fallback-configured client, which is
the same bug with better documentation -- a caller still could not tell.

Each test here fails against the pre-fix (log-only) implementation.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from openadapt_evals.evaluation.client import (
    EvaluationError,
    EvaluationResult,
    EvaluatorClient,
    EvaluatorsUnavailableError,
)


@pytest.fixture
def broken_evaluators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A WAA evaluators tree that is present on disk but raises on import.

    This is the *broken install* case, which is what must be distinguishable
    from the legitimately-absent optional case.
    """
    desktop_env = tmp_path / "desktop_env"
    evaluators = desktop_env / "evaluators"
    evaluators.mkdir(parents=True)
    (evaluators / "__init__.py").write_text("")
    (evaluators / "getters.py").write_text("")

    real_import = builtins.__import__

    def _raising_import(name, *args, **kwargs):
        if name == "evaluators":
            raise ImportError("cannot import name 'getters' from 'evaluators'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _raising_import)
    return desktop_env


def test_broken_evaluators_install_raises_instead_of_silently_falling_back(
    broken_evaluators: Path,
) -> None:
    """A present-but-unimportable evaluators package must not construct a client.

    Pre-fix this returned a perfectly usable client whose every result was
    fallback-scored, with nothing in the object or in any result recording it.
    """
    with pytest.raises(EvaluatorsUnavailableError) as excinfo:
        EvaluatorClient(vm_ip="10.0.0.1", waa_evaluators_path=broken_evaluators)

    message = str(excinfo.value)
    assert "not importable" in message
    assert "the install is broken, not absent" in message
    assert str(broken_evaluators) in message


def test_missing_evaluators_reports_absent_not_broken(tmp_path: Path) -> None:
    """The absent case must be reported differently from the broken case."""
    with pytest.raises(EvaluatorsUnavailableError) as excinfo:
        EvaluatorClient(vm_ip="10.0.0.1", waa_evaluators_path=tmp_path / "nope")

    message = str(excinfo.value)
    assert "not importable" in message or "not found" in message
    # Distinguishable from the broken-install wording.
    assert "the install is broken, not absent" not in message


def test_deliberate_fallback_is_opt_in_and_stamped_on_every_result(
    broken_evaluators: Path,
) -> None:
    """Opting into fallback scoring is allowed, but never invisible."""
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=broken_evaluators,
        require_waa_evaluators=False,
    )

    assert client.evaluator_source == "fallback"
    assert client.evaluators_error is not None

    result = client.evaluate({})
    assert result.evaluator_source == "fallback"


def test_unreachable_vm_is_not_a_measured_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable backend must not be published as a legitimate 0.0."""
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )

    def _refuse(*_args, **_kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "post", _refuse)

    result = client.evaluate(
        {
            "evaluator": {
                "result": {"type": "file_content", "path": "C:/out.txt"},
                "expected": {"value": "hello"},
                "func": "exact_match",
            }
        }
    )

    assert result.score == 0.0
    assert result.success is False
    # The point of the fix: score 0.0 here is NOT a measurement.
    assert result.scored is False
    assert result.error_type == "infrastructure"
    assert result.to_dict()["scored"] is False


def test_vm_reported_command_error_is_not_an_empty_measurement(
    tmp_path: Path,
) -> None:
    """A getter whose command failed must not be scored as empty output."""
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )

    with pytest.raises(EvaluationError):
        client._require_output({"error": "WinError 5", "output": ""}, "file_content")


@pytest.mark.parametrize("receipt", [{}, None, {"output": None}, {"output": 1}])
def test_missing_or_non_string_getter_output_is_not_measured(
    tmp_path: Path, receipt: object
) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )

    with pytest.raises(EvaluationError):
        client._require_output(receipt, "file_content")


def test_explicit_empty_getter_output_remains_valid(tmp_path: Path) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )

    assert client._require_output(
        {"returncode": 0, "output": ""}, "file_content"
    ) == ""


def test_delivery_alone_does_not_prove_command_completion(tmp_path: Path) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )

    with pytest.raises(EvaluationError):
        client._require_output(
            {"delivery_state": "delivered", "output": ""},
            "file_content",
        )


@pytest.mark.parametrize(
    "receipt",
    [
        {"success": False, "output": "saved"},
        {"returncode": 0, "stderr": "warning", "output": "saved"},
        {"delivery_state": "uncertain", "output": "saved"},
        {"delivery_state": "invalid", "output": "saved"},
        {"output": "saved"},
    ],
)
def test_failed_or_unproved_getter_receipt_is_not_measured(
    tmp_path: Path, receipt: dict
) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )

    with pytest.raises(EvaluationError):
        client._require_output(receipt, "file_content")


def test_empty_process_name_cannot_match_every_output(tmp_path: Path) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )
    env = MagicMock()

    with pytest.raises(EvaluationError):
        client._fallback_getter(env, "process_running", {"process": ""})
    env.execute.assert_not_called()


def test_loaded_getter_requires_known_result_parameters(tmp_path: Path) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )
    client._getters = SimpleNamespace(get_file_content=lambda _env, _spec: "")
    client._metrics = SimpleNamespace(exact_match=lambda _actual, _expected: 1.0)

    result = client.evaluate({
        "evaluator": {
            "result": {"type": "file_content"},
            "expected": {"value": ""},
            "func": "exact_match",
        }
    })

    assert result.success is False
    assert result.scored is False
    assert result.error_type == "evaluation"


def test_metric_options_are_applied_and_invalid_conjunction_is_refused(
    tmp_path: Path,
) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )
    metric = MagicMock(side_effect=lambda _a, _e, strict=False: 0.0 if strict else 1.0)
    client._metrics = SimpleNamespace(exact_match=metric)

    assert client._run_metric(
        {"func": "exact_match", "options": {"strict": True}}, "a", "a"
    ) == 0.0
    metric.assert_called_once_with("a", "a", strict=True)

    with pytest.raises(EvaluationError, match="conj='and'"):
        client._run_metric({"func": "exact_match", "conj": "invalid"}, "a", "a")


def test_failed_http_receipt_is_classified_as_infrastructure(tmp_path: Path) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )
    response = MagicMock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {"success": False, "output": ""}

    with patch("requests.post", return_value=response):
        result = client.evaluate({
            "evaluator": {
                "result": {"type": "file_content", "path": "C:/out.txt"},
                "expected": {"value": ""},
                "func": "exact_match",
            }
        })

    assert result.success is False
    assert result.scored is False
    assert result.error_type == "infrastructure"


def test_duplicate_outer_receipt_keys_cannot_score_exact_empty(tmp_path: Path) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"returncode":1,"returncode":0,"output":""}'

    with patch("requests.post", return_value=response):
        result = client.evaluate({
            "evaluator": {
                "result": {"type": "file_content", "path": "C:/out.txt"},
                "expected": {"value": ""},
                "func": "exact_match",
            }
        })

    assert result.success is False
    assert result.scored is False
    assert result.error_type == "infrastructure"


@pytest.mark.parametrize(("value", "rendered"), [("", ""), (0, "0"), (False, "False")])
def test_result_serialization_preserves_falsy_evidence(
    value: object,
    rendered: str,
) -> None:
    payload = EvaluationResult(True, 1.0, actual=value, expected=value).to_dict()

    assert payload["actual"] == rendered
    assert payload["expected"] == rendered


@pytest.mark.parametrize("expected", [None, {}, {"type": "literal"}])
def test_missing_or_malformed_expected_contract_is_unscored(
    tmp_path: Path, expected: object
) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )
    config = {"result": {"type": "file_content", "path": "C:/out.txt"}}
    if expected is not None:
        config["expected"] = expected

    with pytest.raises(EvaluationError):
        client._get_expected_value(config)


def test_unknown_metric_name_is_not_silently_scored_with_exact_match(
    tmp_path: Path,
) -> None:
    """Substituting a different metric publishes a number nobody requested."""
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )

    with pytest.raises(EvaluationError):
        client._fallback_metric("check_csv_content", "a", "a")


def test_known_metric_still_scores_normally(tmp_path: Path) -> None:
    """The fix must not break the healthy path."""
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )

    assert client._fallback_metric("exact_match", "a", "a") == 1.0
    assert client._fallback_metric("exact_match", "a", "b") == 0.0


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1, True])
def test_invalid_metric_score_is_not_a_measurement(
    tmp_path: Path, score: object
) -> None:
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )
    client._metrics = MagicMock()
    client._metrics.exact_match.return_value = score

    with pytest.raises(EvaluationError):
        client._run_metric({"func": "exact_match"}, "a", "a")


def test_missing_evaluator_config_is_flagged_not_scored(tmp_path: Path) -> None:
    """A task with no evaluator spec was never measured."""
    client = EvaluatorClient(
        vm_ip="10.0.0.1",
        waa_evaluators_path=tmp_path,
        require_waa_evaluators=False,
    )

    result = client.evaluate({})
    assert isinstance(result, EvaluationResult)
    assert result.scored is False
    assert result.error_type == "evaluation"
