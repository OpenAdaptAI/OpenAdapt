"""Regression tests for command-backed evaluation receipts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from openadapt_evals.adapters import EvaluationUnavailableError
from openadapt_evals.task_config import TaskCheck, TaskConfig


def _response(
    *,
    returncode: int = 0,
    stdout: str = "saved",
    stderr: str = "",
    **outer: object,
) -> MagicMock:
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "output": json.dumps(
            {
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        ),
        **outer,
    }
    return response


def _task(check: TaskCheck) -> TaskConfig:
    return TaskConfig(
        name="Receipt check",
        id="receipt-check",
        domain="desktop",
        setup=[],
        checks=[check],
        combine="and",
        max_steps=1,
        milestones=[],
    )


def test_successful_empty_receipt_can_prove_exact_empty() -> None:
    with patch("requests.post", return_value=_response(stdout="")):
        actual = TaskConfig._run_vm_command("true", "http://vm")

    assert actual == ""
    assert TaskConfig._check_match(actual, "", "exact") is True


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (_response(returncode=1, stdout=""), "evaluation"),
        (_response(stderr="warning"), "evaluation"),
        (_response(success=False), "infrastructure"),
        (_response(error="server failed"), "infrastructure"),
        (_response(returncode=0, stdout="ok", stderr="", returncode_outer=1), None),
    ],
)
def test_failed_command_or_server_receipt_is_unscored(
    response: MagicMock,
    error_type: str | None,
) -> None:
    if error_type is None:
        payload = response.json.return_value
        payload["returncode"] = payload.pop("returncode_outer")
        error_type = "infrastructure"

    with (
        patch("requests.post", return_value=response),
        pytest.raises(EvaluationUnavailableError) as excinfo,
    ):
        TaskConfig._run_vm_command("command", "http://vm")

    assert excinfo.value.error_type == error_type


def test_plain_stdout_without_receipt_is_unscored() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"output": ""}

    with (
        patch("requests.post", return_value=response),
        pytest.raises(EvaluationUnavailableError) as excinfo,
    ):
        TaskConfig._run_vm_command("false", "http://vm")

    assert excinfo.value.error_type == "evaluation"


def test_duplicate_command_receipt_fields_are_unscored() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "output": (
            '{"returncode":1,"returncode":0,"stdout":"",'
            '"stderr":"failed","stderr":""}'
        )
    }

    with (
        patch("requests.post", return_value=response),
        pytest.raises(EvaluationUnavailableError) as excinfo,
    ):
        TaskConfig._run_vm_command("false", "http://vm")

    assert excinfo.value.error_type == "evaluation"


def test_duplicate_outer_receipt_fields_are_unscored() -> None:
    response = requests.Response()
    response.status_code = 200
    response._content = (
        b'{"returncode":1,"returncode":0,'
        b'"output":"{\\"returncode\\":0,\\"stdout\\":\\"\\",'
        b'\\"stderr\\":\\"\\"}"}'
    )

    with (
        patch("requests.post", return_value=response),
        pytest.raises(EvaluationUnavailableError) as excinfo,
    ):
        TaskConfig._run_vm_command("command", "http://vm")

    assert excinfo.value.error_type == "infrastructure"


@pytest.mark.parametrize("success", [False, 0, "false", None])
def test_invalid_explicit_outer_success_is_unscored(success: object) -> None:
    with (
        patch("requests.post", return_value=_response(success=success)),
        pytest.raises(EvaluationUnavailableError) as excinfo,
    ):
        TaskConfig._run_vm_command("command", "http://vm")

    assert excinfo.value.error_type == "infrastructure"


def test_request_error_preserves_infrastructure_category() -> None:
    with (
        patch("requests.post", side_effect=requests.ConnectionError("offline")),
        pytest.raises(EvaluationUnavailableError) as excinfo,
    ):
        TaskConfig._run_vm_command("command", "http://vm")

    assert excinfo.value.error_type == "infrastructure"


def test_failed_command_plus_empty_exact_cannot_score() -> None:
    task = _task(TaskCheck(check="command", run="false", expect="", match="exact"))

    with (
        patch("requests.post", return_value=_response(returncode=1, stdout="")),
        pytest.raises(EvaluationUnavailableError),
    ):
        task.evaluate_checks_local(b"", "http://vm")


def test_whitespace_only_command_cannot_score_exact_empty() -> None:
    task = _task(TaskCheck(check="command", run="   ", expect="", match="exact"))

    with pytest.raises(EvaluationUnavailableError) as excinfo:
        task.evaluate_checks_local(b"", "http://vm")

    assert excinfo.value.error_type == "evaluation"


@pytest.mark.parametrize("match_type", ["contains", "regex", "fuzzy"])
def test_match_contract_that_empty_expected_would_trivially_pass_is_refused(
    match_type: str,
) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        TaskConfig._check_match("anything", "", match_type)


def test_empty_contains_is_unscored_in_local_evaluation() -> None:
    task = _task(
        TaskCheck(check="command", run="echo saved", expect="", match="contains")
    )

    with (
        patch("requests.post", return_value=_response(stdout="saved")),
        pytest.raises(EvaluationUnavailableError) as excinfo,
    ):
        task.evaluate_checks_local(b"", "http://vm")

    assert excinfo.value.error_type == "evaluation"
