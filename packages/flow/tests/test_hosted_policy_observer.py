"""Software controls for the hosted test's passive policy observer."""

from __future__ import annotations

import json

import pytest

from openadapt_flow import policy as policy_module
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.policy import Policy, Violation
from tests.e2e import hosted_policy_observer as observer


def _inputs():
    workflow = Workflow(
        name="synthetic private workflow text",
        steps=[
            Step(
                id="step_002",
                intent="Submit synthetic query",
                action=ActionKind.KEY,
                key="Enter",
            )
        ],
    )
    policy = Policy(
        name="clinical-write",
        prohibit_vacuous_postconditions=True,
        max_unverified_steps=0,
    )
    return workflow, policy


@pytest.mark.parametrize("risk_flag", [False, True])
def test_original_args_once_same_real_refusal_report(monkeypatch, risk_flag):
    workflow, policy = _inputs()
    original = policy_module.evaluate_policy
    calls = []
    returned = []

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        report = original(*args, **kwargs)
        # This sentinel represents text that must never enter safe evidence.
        report.violations[0].reason = "DO_NOT_EXPORT_reason_or_record_text"
        returned.append((report, report.model_dump()))
        return report

    monkeypatch.setattr(policy_module, "evaluate_policy", tracked)
    evidence = {}
    before_workflow, before_policy = workflow.model_dump(), policy.model_dump()
    with observer.observe_policy(evidence):
        result = policy_module.evaluate_policy(
            workflow, policy, require_current_risk_certification=risk_flag
        )
    assert policy_module.evaluate_policy is tracked
    assert len(calls) == 1
    assert calls[0][0][0] is workflow and calls[0][0][1] is policy
    assert calls[0][1] == {"require_current_risk_certification": risk_flag}
    assert result is returned[0][0]
    assert result.model_dump() == returned[0][1]
    assert workflow.model_dump() == before_workflow
    assert policy.model_dump() == before_policy
    assert not result.passed
    assert evidence["policy_checks"] == [
        {
            "policy_name": policy.name,
            "policy_contract_sha256": observer.policy_contract_sha256(policy),
            "require_current_risk_certification": risk_flag,
            "n_steps": 1,
            "passed": False,
            "violations": [
                {"rule": "prohibit_vacuous_postconditions", "step_id": "step_002"},
                {"rule": "max_unverified_steps", "step_id": None},
            ],
        }
    ]
    serialized = json.dumps(evidence)
    assert "DO_NOT_EXPORT" not in serialized
    assert workflow.name not in serialized
    assert "reason" not in serialized


def test_keyword_call_default_risk_flag_and_real_pass(monkeypatch):
    workflow = Workflow(name="synthetic", steps=[])
    policy = Policy(name="permissive")
    original = policy_module.evaluate_policy
    calls = []

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(policy_module, "evaluate_policy", tracked)
    evidence = {}
    with observer.observe_policy(evidence):
        report = policy_module.evaluate_policy(workflow=workflow, policy=policy)
    assert len(calls) == 1 and calls[0][0] == ()
    assert calls[0][1] == {"workflow": workflow, "policy": policy}
    assert report.passed and evidence["policy_checks"][0]["passed"] is True
    assert evidence["policy_checks"][0]["require_current_risk_certification"] is False


def test_original_exception_identity_and_context_restoration(monkeypatch):
    error = RuntimeError("private evaluator failure")
    calls = []

    def failing(*args, **kwargs):
        calls.append((args, kwargs))
        raise error

    monkeypatch.setattr(policy_module, "evaluate_policy", failing)
    evidence = {}
    with pytest.raises(RuntimeError) as caught:
        with observer.observe_policy(evidence):
            policy_module.evaluate_policy(*_inputs())
    assert caught.value is error and len(calls) == 1
    assert policy_module.evaluate_policy is failing
    assert evidence == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("rule", "private reason\nnot_a_rule"),
        ("step_id", "private/path/record"),
    ],
)
def test_unsafe_violation_projection_preserves_actual_report(monkeypatch, field, value):
    workflow, policy = _inputs()
    report = policy_module.evaluate_policy(workflow, policy)
    setattr(report.violations[0], field, value)
    before = report.model_dump()
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return report

    monkeypatch.setattr(policy_module, "evaluate_policy", original)
    evidence = {}
    with observer.observe_policy(evidence):
        result = policy_module.evaluate_policy(workflow, policy)
    assert result is report and result.model_dump() == before
    assert len(calls) == 1 and result.passed is False
    assert evidence == {
        "policy_checks": [],
        "policy_diagnostic_failed": True,
        "policy_diagnostic_failure_code": "projection_failed",
    }
    assert value not in json.dumps(evidence)


def test_projection_exception_never_promotes_or_replaces_refusal(monkeypatch):
    workflow, policy = _inputs()
    report = policy_module.evaluate_policy(workflow, policy)
    monkeypatch.setattr(policy_module, "evaluate_policy", lambda *a, **k: report)

    def unavailable(_policy):
        raise RuntimeError("DO_NOT_EXPORT_projection_failure")

    monkeypatch.setattr(observer, "policy_contract_sha256", unavailable)
    evidence = {}
    with observer.observe_policy(evidence):
        result = policy_module.evaluate_policy(workflow, policy)
    assert result is report and not result.passed
    assert evidence["policy_diagnostic_failure_code"] == "projection_failed"
    assert "DO_NOT_EXPORT" not in json.dumps(evidence)


def test_observation_limit_preserves_every_original_call(monkeypatch):
    workflow, policy = _inputs()
    report = policy_module.evaluate_policy(workflow, policy)
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return report

    monkeypatch.setattr(policy_module, "evaluate_policy", original)
    evidence = {}
    with observer.observe_policy(evidence):
        for _ in range(observer._MAX_CHECKS + 1):
            assert policy_module.evaluate_policy(workflow, policy) is report
    assert len(calls) == observer._MAX_CHECKS + 1
    assert len(evidence["policy_checks"]) == observer._MAX_CHECKS
    assert evidence["policy_diagnostic_failure_code"] == "observation_limit"


def test_violation_limit_does_not_emit_partial_report(monkeypatch):
    workflow, policy = _inputs()
    report = policy_module.evaluate_policy(workflow, policy)
    report.violations = [Violation(rule="synthetic", reason="private text")] * (
        observer._MAX_VIOLATIONS + 1
    )
    monkeypatch.setattr(policy_module, "evaluate_policy", lambda *a, **k: report)
    evidence = {}
    with observer.observe_policy(evidence):
        assert policy_module.evaluate_policy(workflow, policy) is report
    assert evidence["policy_checks"] == []
    assert evidence["policy_diagnostic_failure_code"] == "projection_failed"


def test_actual_certification_gate_preserves_refusal_and_safe_rules():
    from openadapt_flow.run_gate import _gate_certification

    workflow, policy = _inputs()
    original = policy_module.evaluate_policy
    evidence = {}
    with observer.observe_policy(evidence):
        result = _gate_certification(
            workflow,
            policy.name,
            require_current_risk_certification=False,
            certifying_policy=policy,
        )
    assert policy_module.evaluate_policy is original
    assert result.passed is False
    assert result.offenders == ["step_002"]
    assert len(evidence["policy_checks"]) == 1
    check = evidence["policy_checks"][0]
    assert check["passed"] is False
    assert check["require_current_risk_certification"] is False
    assert check["violations"] == [
        {"rule": "prohibit_vacuous_postconditions", "step_id": "step_002"},
        {"rule": "max_unverified_steps", "step_id": None},
    ]
    assert "reason" not in json.dumps(evidence)
    assert workflow.name not in json.dumps(evidence)
