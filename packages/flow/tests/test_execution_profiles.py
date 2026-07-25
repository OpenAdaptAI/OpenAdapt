from pathlib import Path

from openadapt_flow.deployment import DeploymentConfig, PolicySection, RuntimeSection
from openadapt_flow.execution_profiles import (
    ExecutionOutcome,
    ExecutionProfile,
    classify_execution_outcome,
    execution_profile_contract,
    stamp_execution_outcome,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.run_gate import (
    GATE_APPROVAL,
    GATE_ENCRYPTION,
    GATE_PROFILE,
    build_runtime_authorization,
    evaluate_run_gate,
)
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision

_KEY = "profile-test-key"


def _effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"record_id": "synthetic-1"},
        idempotency_key="profile-test-run",
        risk="irreversible",
    )


def _workflow(*, effect: bool = True, armed: bool = True) -> Workflow:
    return Workflow(
        name="profile-contract",
        steps=[
            Step(
                id="save",
                intent="save synthetic record",
                action=ActionKind.CLICK,
                risk="irreversible",
                anchor=Anchor(
                    template="save.png",
                    region=(0, 0, 10, 10),
                    click_point=(5, 5),
                    ocr_text="Save",
                    context_text="Synthetic record" if armed else None,
                ),
                identity_armed=armed,
                effects=[_effect()] if effect else [],
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                    )
                ],
            )
        ],
    )


def _sealed(
    tmp_path: Path, workflow: Workflow, *, encrypted: bool
) -> tuple[Workflow, Path]:
    bundle = tmp_path / ("encrypted" if encrypted else "plaintext")
    workflow.save(bundle, encrypt=encrypted, key=_KEY if encrypted else None)
    return Workflow.load(bundle, key=_KEY if encrypted else None), bundle


def _gate(
    workflow: Workflow,
    bundle: Path,
    profile: ExecutionProfile,
    *,
    verifier: object | None,
    durable: bool,
    approval: bool = False,
):
    return evaluate_run_gate(
        workflow,
        bundle_dir=bundle,
        deployment=DeploymentConfig(policy=PolicySection(policy="permissive")),
        effect_verifier=verifier,
        approval_available=approval,
        profile_contract=execution_profile_contract(profile),
        effective_durable=durable,
    )


def test_demo_admits_uncertified_screen_only_bundle_but_is_non_production(tmp_path):
    workflow, bundle = _sealed(
        tmp_path,
        _workflow(effect=False, armed=False),
        encrypted=False,
    )

    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.DEMO,
        verifier=None,
        durable=False,
    )
    assert gate.passed, gate.render()

    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        results=[StepResult(step_id="save", intent="save", ok=True)],
    )
    outcome = stamp_execution_outcome(report, workflow, ExecutionProfile.DEMO)
    assert outcome is ExecutionOutcome.COMPLETED_UNVERIFIED
    assert report.production_eligible is False


def test_standard_requires_durability_and_independent_effects(tmp_path):
    workflow, bundle = _sealed(tmp_path, _workflow(), encrypted=False)

    not_durable = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=object(),
        durable=False,
    )
    assert not not_durable.passed
    assert not_durable.gate(GATE_PROFILE).passed is False

    no_verifier = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=None,
        durable=True,
        approval=True,
    )
    assert not no_verifier.passed
    assert no_verifier.gate(GATE_APPROVAL).passed is False

    admitted = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=object(),
        durable=True,
    )
    assert admitted.passed, admitted.render()
    assert admitted.gate(GATE_ENCRYPTION).passed
    authorization = build_runtime_authorization(workflow, admitted)
    assert authorization.execution_profile == "standard"


def test_regulated_requires_encryption_and_strictly_sealed_assets(tmp_path):
    plaintext, plaintext_bundle = _sealed(
        tmp_path,
        _workflow(),
        encrypted=False,
    )
    refused = _gate(
        plaintext,
        plaintext_bundle,
        ExecutionProfile.REGULATED,
        verifier=object(),
        durable=True,
    )
    assert not refused.passed
    assert refused.gate(GATE_ENCRYPTION).passed is False

    encrypted, encrypted_bundle = _sealed(
        tmp_path,
        _workflow(),
        encrypted=True,
    )
    admitted = _gate(
        encrypted,
        encrypted_bundle,
        ExecutionProfile.REGULATED,
        verifier=object(),
        durable=True,
    )
    assert admitted.passed, admitted.render()


def test_production_profiles_never_verify_screen_only_consequential_result():
    workflow = _workflow()
    unverified = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        results=[StepResult(step_id="save", intent="save", ok=True)],
    )
    verified = unverified.model_copy(deep=True)
    verified.results[0].effect_verified = True

    for profile in (ExecutionProfile.STANDARD, ExecutionProfile.REGULATED):
        assert (
            classify_execution_outcome(unverified, workflow, profile)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )
        assert (
            classify_execution_outcome(verified, workflow, profile)
            is ExecutionOutcome.VERIFIED
        )


def test_halt_and_infrastructure_failure_remain_distinct():
    workflow = Workflow(name="read-only", steps=[])
    halted = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        results=[
            StepResult(
                step_id="<authorization>",
                intent="admission",
                ok=False,
                error="authorization refused",
            )
        ],
    )
    failed = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        results=[
            StepResult(
                step_id="<runtime>",
                intent="launch backend",
                ok=False,
                error="backend connection refused",
            )
        ],
    )

    assert (
        classify_execution_outcome(halted, workflow, ExecutionProfile.REGULATED)
        is ExecutionOutcome.HALTED
    )
    assert (
        classify_execution_outcome(failed, workflow, ExecutionProfile.REGULATED)
        is ExecutionOutcome.FAILED
    )


def test_deployment_runtime_accepts_named_profile():
    runtime = RuntimeSection(profile="standard")
    assert runtime.profile == "standard"


def _authorization_for(
    workflow: Workflow, profile: ExecutionProfile
) -> GovernedRunAuthorization:
    assert workflow.manifest is not None
    return GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
        admitted_policy_name="permissive",
        execution_profile=profile.value,
    )


def test_replayer_rechecks_standard_durability_before_backend_access(tmp_path):
    workflow, bundle = _sealed(tmp_path, _workflow(), encrypted=False)
    backend = FakeBackend()
    report = Replayer(
        backend,
        vision=FakeVision(),
        governed_authorization=_authorization_for(
            workflow,
            ExecutionProfile.STANDARD,
        ),
        durable=False,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run-standard")

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert report.results[0].step_id == "<profile>"
    assert "durable runtime" in (report.results[0].error or "")
    assert backend.actions == []


def test_replayer_rechecks_regulated_encryption_before_backend_access(tmp_path):
    workflow, bundle = _sealed(tmp_path, _workflow(), encrypted=False)
    backend = FakeBackend()
    report = Replayer(
        backend,
        vision=FakeVision(),
        governed_authorization=_authorization_for(
            workflow,
            ExecutionProfile.REGULATED,
        ),
        durable=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run-regulated")

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert report.results[0].step_id == "<profile>"
    assert "encrypted bundle" in (report.results[0].error or "")
    assert backend.actions == []
