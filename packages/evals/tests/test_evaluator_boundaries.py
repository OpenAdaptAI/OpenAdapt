"""Fail-closed evaluator boundary regression tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from openadapt_evals.server.evaluate_endpoint import (
    EvaluationNotRunError,
    StandaloneMetrics,
    create_standalone_evaluator,
    evaluate_task_state,
    run_metric,
)


def _task_with_postconfig(postconfig: list) -> dict:
    return {
        "evaluator": {
            "result": {"type": "thing"},
            "expected": {"value": "saved"},
            "func": "exact_match",
            "postconfig": postconfig,
        }
    }


@pytest.mark.parametrize(
    ("postconfig", "status", "error_type"),
    [
        ([{"type": "unknown"}], 200, "evaluation"),
        ([{"type": "activate_window", "name": "App"}], 500, "infrastructure"),
        ([{"type": "execute", "command": "save"}], 503, "infrastructure"),
    ],
)
def test_required_postconfig_failure_is_unscored(
    postconfig: list, status: int, error_type: str
) -> None:
    getters = SimpleNamespace(get_thing=lambda _env, _spec: "saved")
    metric = MagicMock(return_value=1.0)
    metrics = SimpleNamespace(exact_match=metric)
    response = MagicMock(status_code=status)

    with (
        patch(
            "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
            return_value=(getters, metrics),
        ),
        patch("requests.post", return_value=response),
    ):
        result = evaluate_task_state(_task_with_postconfig(postconfig))

    assert result["scored"] is False
    assert result["error_type"] == error_type
    metric.assert_not_called()


def test_postconfig_request_exception_is_unscored_infrastructure() -> None:
    getters = SimpleNamespace(get_thing=lambda _env, _spec: "saved")
    metric = MagicMock(return_value=1.0)
    metrics = SimpleNamespace(exact_match=metric)

    with (
        patch(
            "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
            return_value=(getters, metrics),
        ),
        patch("requests.post", side_effect=requests.ConnectionError("offline")),
    ):
        result = evaluate_task_state(
            _task_with_postconfig([{"type": "execute", "command": "save"}])
        )

    assert result["scored"] is False
    assert result["error_type"] == "infrastructure"
    metric.assert_not_called()


@pytest.mark.parametrize(
    "receipt",
    [
        {"success": False},
        {"failed": False},
        {"success": True, "stderr": "warning"},
        {"delivery_state": "uncertain"},
        {"delivery_state": "invalid"},
        {},
    ],
)
def test_postconfig_http_200_requires_success_receipt(receipt: object) -> None:
    getters = SimpleNamespace(get_thing=lambda _env, _spec: "saved")
    metric = MagicMock(return_value=1.0)
    metrics = SimpleNamespace(exact_match=metric)
    response = MagicMock(status_code=200)
    response.json.return_value = receipt

    with (
        patch(
            "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
            return_value=(getters, metrics),
        ),
        patch("requests.post", return_value=response),
    ):
        result = evaluate_task_state(
            _task_with_postconfig([{"type": "execute", "command": "save"}])
        )

    assert result["scored"] is False
    assert result["error_type"] == "infrastructure"
    metric.assert_not_called()


def test_postconfig_success_receipt_allows_evaluation() -> None:
    getters = SimpleNamespace(get_thing=lambda _env, _spec: "saved")
    metric = MagicMock(return_value=1.0)
    metrics = SimpleNamespace(exact_match=metric)
    response = MagicMock(status_code=200)
    response.json.return_value = {"success": True, "delivery_state": "delivered"}

    with (
        patch(
            "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
            return_value=(getters, metrics),
        ),
        patch("requests.post", return_value=response),
    ):
        result = evaluate_task_state(
            _task_with_postconfig([{"type": "execute", "command": "save"}])
        )

    assert result["scored"] is True
    assert result["success"] is True


@pytest.mark.parametrize("score", [True, float("nan"), float("inf"), -0.1, 1.1])
def test_metric_rejects_invalid_scores(score: object) -> None:
    metrics = SimpleNamespace(exact_match=lambda _actual, _expected: score)

    with pytest.raises(EvaluationNotRunError, match="invalid score"):
        run_metric("exact_match", "saved", "saved", metrics=metrics)


def _standalone_config(**overrides: object) -> dict:
    evaluator = {
        "result": {"type": "vm_command_line", "command": "read"},
        "expected": {"value": "saved"},
        "func": "exact_match",
    }
    evaluator.update(overrides)
    return {"evaluator": evaluator}


@pytest.mark.parametrize(
    "result_spec",
    [None, {}, {"type": "vm_command_line"}, {"type": "vm_file"}],
)
def test_missing_or_incomplete_result_contract_is_unscored(
    result_spec: object,
) -> None:
    task = {
        "evaluator": {
            "result": result_spec,
            "expected": {"value": ""},
            "func": "exact_match",
        }
    }
    getters = SimpleNamespace(
        get_vm_command_line=lambda _env, _spec: "",
        get_vm_file=lambda _env, _spec: "",
    )
    metrics = SimpleNamespace(exact_match=lambda _actual, _expected: 1.0)
    with patch(
        "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
        return_value=(getters, metrics),
    ):
        result = evaluate_task_state(task)

    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


@pytest.mark.parametrize(
    "result_spec",
    [None, {}, {"type": "vm_command_line"}, {"type": "vm_file"}],
)
def test_standalone_missing_or_incomplete_result_is_unscored(
    result_spec: object,
) -> None:
    evaluate = create_standalone_evaluator()
    result = evaluate(
        {
            "evaluator": {
                "result": result_spec,
                "expected": {"value": ""},
                "func": "exact_match",
            }
        }
    )

    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


def test_whitespace_only_command_contract_is_unscored_in_both_evaluators() -> None:
    task = {
        "evaluator": {
            "result": {"type": "vm_command_line", "command": "   "},
            "expected": {"value": ""},
            "func": "exact_match",
        }
    }
    getters = SimpleNamespace(get_vm_command_line=lambda _env, _spec: "")
    metrics = SimpleNamespace(exact_match=lambda _actual, _expected: 1.0)
    with patch(
        "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
        return_value=(getters, metrics),
    ):
        main_result = evaluate_task_state(task)
    standalone_result = create_standalone_evaluator()(task)

    assert main_result["scored"] is False
    assert standalone_result["scored"] is False


def test_whitespace_only_postconfig_command_is_unscored() -> None:
    task = _task_with_postconfig([{"type": "execute", "command": "   "}])
    getters = SimpleNamespace(get_thing=lambda _env, _spec: "saved")
    metrics = SimpleNamespace(exact_match=lambda _actual, _expected: 1.0)
    with patch(
        "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
        return_value=(getters, metrics),
    ):
        result = evaluate_task_state(task)

    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


def test_standalone_http_200_failure_cannot_verify_empty_output() -> None:
    evaluate = create_standalone_evaluator()
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "success": False,
        "error": "failed",
        "output": "",
    }
    config = _standalone_config(expected={"value": ""})

    with patch("requests.post", return_value=response):
        result = evaluate(config)

    assert result["scored"] is False
    assert result["error_type"] == "infrastructure"


def test_standalone_passes_declared_metric_options() -> None:
    evaluate = create_standalone_evaluator()
    response = MagicMock(status_code=200)
    response.json.return_value = {"returncode": 0, "output": "saved"}
    metric = MagicMock(return_value=1.0)

    with (
        patch("requests.post", return_value=response),
        patch.object(StandaloneMetrics, "exact_match", metric),
    ):
        result = evaluate(_standalone_config(options={"strict": True}))

    assert result["scored"] is True
    metric.assert_called_once_with("saved", "saved", strict=True)


def test_standalone_refuses_declared_postconfig_it_cannot_execute() -> None:
    evaluate = create_standalone_evaluator()
    result = evaluate(
        _standalone_config(postconfig=[{"type": "execute", "command": "prepare"}])
    )

    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


@pytest.mark.parametrize("postconfig", [None, {}, ""])
def test_main_evaluator_rejects_malformed_falsy_postconfig(
    postconfig: object,
) -> None:
    task = _task_with_postconfig([])
    task["evaluator"]["postconfig"] = postconfig
    getters = SimpleNamespace(get_thing=lambda _env, _spec: "saved")
    metrics = SimpleNamespace(exact_match=lambda _actual, _expected: 1.0)
    with patch(
        "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
        return_value=(getters, metrics),
    ):
        result = evaluate_task_state(task)

    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


@pytest.mark.parametrize("infeasible", ["false", 1, {}])
def test_malformed_infeasible_marker_cannot_turn_fail_into_success(
    infeasible: object,
) -> None:
    task = {
        "evaluator": {"infeasible": infeasible},
        "agent_last_action": "FAIL",
    }
    with patch(
        "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
        return_value=(MagicMock(), MagicMock()),
    ):
        result = evaluate_task_state(task)

    assert result["success"] is False
    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


def test_infeasible_marker_requires_string_last_action() -> None:
    task = {
        "evaluator": {"infeasible": True},
        "agent_last_action": 1,
    }
    with patch(
        "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
        return_value=(MagicMock(), MagicMock()),
    ):
        result = evaluate_task_state(task)

    assert result["success"] is False
    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


def test_standalone_unknown_metric_is_not_substituted() -> None:
    evaluate = create_standalone_evaluator()
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "success": True,
        "delivery_state": "delivered",
        "output": "saved",
    }
    with patch("requests.post", return_value=response):
        result = evaluate(_standalone_config(func="not_a_metric"))

    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


def test_standalone_getter_exception_is_not_a_measured_zero() -> None:
    evaluate = create_standalone_evaluator()
    with patch(
        "openadapt_evals.server.evaluate_endpoint.StandaloneGetters.get_vm_command_line",
        side_effect=RuntimeError("offline"),
    ):
        result = evaluate(_standalone_config())

    assert result["scored"] is False
    assert result["error_type"] == "infrastructure"


def test_standalone_non_2xx_getter_is_unscored_infrastructure() -> None:
    evaluate = create_standalone_evaluator()
    response = MagicMock(status_code=503)
    with patch("requests.post", return_value=response):
        result = evaluate(_standalone_config())

    assert result["scored"] is False
    assert result["error_type"] == "infrastructure"


@pytest.mark.parametrize(
    "overrides",
    [
        {"func": 1},
        {"func": ["exact_match", ""]},
        {"options": []},
        {"conj": "maybe"},
    ],
)
def test_evaluator_rejects_malformed_metric_contract(overrides: dict) -> None:
    task = _task_with_postconfig([])
    task["evaluator"].update(overrides)
    getters = SimpleNamespace(get_thing=lambda _env, _spec: "saved")
    metrics = SimpleNamespace(exact_match=lambda _actual, _expected: 1.0)

    with patch(
        "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
        return_value=(getters, metrics),
    ):
        result = evaluate_task_state(task)

    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


def test_standalone_metric_exception_is_not_a_measured_zero() -> None:
    evaluate = create_standalone_evaluator()
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "success": True,
        "delivery_state": "delivered",
        "output": "saved",
    }
    with (
        patch("requests.post", return_value=response),
        patch.object(StandaloneMetrics, "exact_match", side_effect=RuntimeError("bad")),
    ):
        result = evaluate(_standalone_config())

    assert result["scored"] is False
    assert result["error_type"] == "evaluation"


def test_standalone_success_emits_canonical_scored_contract() -> None:
    evaluate = create_standalone_evaluator()
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "success": True,
        "delivery_state": "delivered",
        "output": "saved",
    }
    with patch("requests.post", return_value=response):
        result = evaluate(_standalone_config())

    assert result["success"] is True
    assert result["score"] == 1.0
    assert result["scored"] is True
    assert result["error_type"] is None
