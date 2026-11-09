"""Test-only projection of actual policy results; never an admission decision."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

from openadapt_flow import policy as policy_module
from openadapt_flow.policy import policy_contract_sha256

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_RULE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_STEP = re.compile(r"step_[0-9]{3,6}")
_MAX_CHECKS = 64
_MAX_VIOLATIONS = 256


def _safe_projection(report: Any, policy: Any, risk_flag: Any) -> dict[str, Any]:
    name = report.policy_name
    if (
        not isinstance(name, str)
        or _NAME.fullmatch(name) is None
        or name != policy.name
        or type(risk_flag) is not bool
        or type(report.passed) is not bool
        or type(report.n_steps) is not int
        or not 0 <= report.n_steps <= 10_000
        or len(report.violations) > _MAX_VIOLATIONS
    ):
        raise ValueError("unsafe policy projection")
    violations = []
    for violation in report.violations:
        rule, step_id = violation.rule, violation.step_id
        if (
            not isinstance(rule, str)
            or _RULE.fullmatch(rule) is None
            or (
                step_id is not None
                and (not isinstance(step_id, str) or _STEP.fullmatch(step_id) is None)
            )
        ):
            raise ValueError("unsafe violation projection")
        violations.append({"rule": rule, "step_id": step_id})
    digest = policy_contract_sha256(policy)
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid policy digest")
    return {
        "policy_name": name,
        "policy_contract_sha256": digest,
        "require_current_risk_certification": risk_flag,
        "n_steps": report.n_steps,
        "passed": report.passed,
        "violations": violations,
    }


def _failure(evidence: dict[str, Any], code: str) -> None:
    # The caller supplies its owned plain evidence dict. If even that sink is
    # unavailable, diagnostics still must not replace the evaluator's result.
    try:
        evidence["policy_diagnostic_failed"] = True
        evidence["policy_diagnostic_failure_code"] = code
    except Exception:
        pass


@contextmanager
def observe_policy(evidence: dict[str, Any]) -> Iterator[None]:
    """Observe existing evaluator calls in this test's synchronous context.

    The original callable receives the exact args once. Its exception, or the
    identical returned report, remains authoritative. Reasons, workflow names,
    source text and locator/frame data are never projected. The context restores
    the original callable on every exit; it is not for concurrent native runs.
    """
    original = policy_module.evaluate_policy

    def observed(*args: Any, **kwargs: Any) -> Any:
        report = original(*args, **kwargs)
        try:
            checks = evidence.setdefault("policy_checks", [])
            if type(checks) is not list:
                raise ValueError("invalid diagnostic sink")
            if len(checks) >= _MAX_CHECKS:
                _failure(evidence, "observation_limit")
                return report
            policy = args[1] if len(args) > 1 else kwargs["policy"]
            risk_flag = kwargs.get("require_current_risk_certification", False)
            checks.append(_safe_projection(report, policy, risk_flag))
        except Exception:
            _failure(evidence, "projection_failed")
        return report

    with patch.object(policy_module, "evaluate_policy", observed):
        yield
