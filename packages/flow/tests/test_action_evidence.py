"""Focused action-evidence identity contract tests."""

from openadapt_flow.action_evidence import action_evidence_error
from openadapt_flow.ir import ActionKind, IdentityCheck, Step, StepResult


def _wait_step() -> Step:
    return Step(id="wait", intent="wait", action=ActionKind.WAIT)


def _result(identity: IdentityCheck | None) -> StepResult:
    return StepResult(step_id="wait", intent="wait", ok=True, identity=identity)


def test_optional_identity_abstention_does_not_reclassify_a_safe_wait() -> None:
    assert (
        action_evidence_error(
            _wait_step(),
            _result(IdentityCheck(status="abstain")),
            identity_required=False,
            strict_production=False,
        )
        is None
    )


def test_optional_identity_mismatch_still_rejects_the_action() -> None:
    assert (
        action_evidence_error(
            _wait_step(),
            _result(IdentityCheck(status="mismatch")),
            identity_required=False,
            strict_production=False,
        )
        == "successful action retains an identity mismatch verdict"
    )


def test_required_identity_still_needs_nonvacuous_verified_evidence() -> None:
    assert (
        action_evidence_error(
            _wait_step(),
            _result(IdentityCheck(status="verified", mode="structured")),
            identity_required=True,
            strict_production=False,
        )
        == "verified identity evidence is only a status label"
    )
