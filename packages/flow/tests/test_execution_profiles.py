from pathlib import Path
from types import SimpleNamespace

from openadapt_flow.connector.executor import status_from_report
from openadapt_flow.console.attention import attention_item
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
    EffectVerificationEvidence,
    Postcondition,
    PostconditionKind,
    ProgramGraph,
    RunReport,
    State,
    StateKind,
    Step,
    StepResult,
    Transition,
    Workflow,
)
from openadapt_flow.qualification import EnvironmentBoundary, QualificationProject
from openadapt_flow.run_gate import (
    GATE_APPROVAL,
    GATE_ENCRYPTION,
    GATE_PROFILE,
    build_runtime_authorization,
    evaluate_run_gate,
)
from openadapt_flow.runner.evidence import summary_status
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.effects.effect import ReadbackNav, ReadbackSpec
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier
from tests.test_replayer import FakeBackend, FakeVision

_KEY = "profile-test-key"


class _TieredVerifier:
    substrate = "test"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def capture_pre_state(self):
        return EffectState(substrate=self.substrate, reachable=True)

    def verify(self, effect, before):
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=effect.kind,
            substrate=self.substrate,
        )


class _ReadyVision(FakeVision):
    def wait_settled_result(self, backend, **kwargs):
        return SimpleNamespace(png=backend.screenshot(), settled=True)


def _effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"record_id": "synthetic-1"},
        idempotency_key="profile-test-run",
        risk="irreversible",
    )


def _key_workflow(
    name: str,
    *,
    with_effect: bool,
    with_postcondition: bool = False,
) -> Workflow:
    return Workflow(
        name=name,
        steps=[
            Step(
                id="submit",
                intent="submit",
                action=ActionKind.KEY,
                key="Enter",
                effects=[_effect()] if with_effect else [],
                expect=(
                    [
                        Postcondition(
                            kind=PostconditionKind.TEXT_PRESENT,
                            text="Saved",
                        )
                    ]
                    if with_postcondition
                    else []
                ),
            )
        ],
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
    settled: bool | None = None,
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
        effective_require_settled=durable if settled is None else settled,
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
        verifier=_TieredVerifier(),
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
        verifier=_TieredVerifier(),
        durable=True,
    )
    assert admitted.passed, admitted.render()
    assert admitted.gate(GATE_ENCRYPTION).passed
    authorization = build_runtime_authorization(workflow, admitted)
    assert authorization.execution_profile == "standard"


def test_production_gate_rejects_immediate_screen_but_accepts_persisted_readback(
    tmp_path,
):
    immediate, immediate_bundle = _sealed(tmp_path, _workflow(), encrypted=False)
    onscreen = OnScreenReadbackVerifier(vision=object())
    refused = _gate(
        immediate,
        immediate_bundle,
        ExecutionProfile.STANDARD,
        verifier=onscreen,
        durable=True,
    )
    assert refused.gate(GATE_APPROVAL).passed is False

    persisted_effect = _effect().model_copy(
        update={
            "readback": ReadbackSpec(
                region=(0, 0, 10, 10),
                different_path=True,
                renavigation=[ReadbackNav(action="key", key="Escape")],
            )
        }
    )
    persisted_workflow = _workflow()
    persisted_workflow.steps[0].effects = [persisted_effect]
    persisted, persisted_bundle = _sealed(
        tmp_path / "persisted",
        persisted_workflow,
        encrypted=False,
    )
    one_action_refused = _gate(
        persisted,
        persisted_bundle,
        ExecutionProfile.STANDARD,
        verifier=onscreen,
        durable=True,
    )
    assert not one_action_refused.passed

    persisted.steps[0].effects[0].readback.renavigation.append(
        ReadbackNav(action="click", point=(5, 5))
    )
    persisted, persisted_bundle = _sealed(
        tmp_path / "persisted-bounded",
        persisted,
        encrypted=False,
    )
    admitted = _gate(
        persisted,
        persisted_bundle,
        ExecutionProfile.STANDARD,
        verifier=onscreen,
        durable=True,
    )
    assert admitted.passed, admitted.render()


def test_standard_requires_settled_state_detection(tmp_path):
    workflow, bundle = _sealed(tmp_path, _workflow(), encrypted=False)
    report = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=_TieredVerifier(),
        durable=True,
        settled=False,
    )
    assert report.gate(GATE_PROFILE).passed is False
    assert "settled-state" in report.gate(GATE_PROFILE).detail


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
        verifier=_TieredVerifier(),
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
        verifier=_TieredVerifier(),
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
    effect_hash = _effect().contract_hash()
    verified.results[0].effect_contract_hashes = [effect_hash]
    verified.results[0].effect_evidence = [
        EffectVerificationEvidence(
            effect_contract_hash=effect_hash,
            substrate="test",
            verification_tier=VerificationTier.INDEPENDENT_SYSTEM,
            initial_verdict="confirmed",
            final_verdict="confirmed",
        )
    ]
    persisted = verified.model_copy(deep=True)
    persisted.results[0].effect_evidence[
        0
    ].verification_tier = VerificationTier.PERSISTED_STATE_REACQUISITION
    immediate = verified.model_copy(deep=True)
    immediate.results[0].effect_evidence[
        0
    ].verification_tier = VerificationTier.IMMEDIATE_SCREEN

    for profile in (ExecutionProfile.STANDARD, ExecutionProfile.REGULATED):
        assert (
            classify_execution_outcome(unverified, workflow, profile)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )
        assert (
            classify_execution_outcome(verified, workflow, profile)
            is ExecutionOutcome.VERIFIED
        )
        assert (
            classify_execution_outcome(persisted, workflow, profile)
            is ExecutionOutcome.VERIFIED
        )
        assert (
            classify_execution_outcome(immediate, workflow, profile)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )
    duplicated = verified.model_copy(deep=True)
    duplicated.results[0].effect_contract_hashes.append(effect_hash)
    assert (
        classify_execution_outcome(
            duplicated,
            workflow,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )
    workflow.qualification = QualificationProject(
        environment=EnvironmentBoundary(
            target_kind="web",
            application="fixture",
            application_version="1",
            environment_digest="a" * 64,
            runtime_version="1.21.0",
        ),
        minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    assert (
        classify_execution_outcome(
            persisted,
            workflow,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


def test_missing_linear_step_result_never_verifies():
    workflow = _workflow()
    incomplete = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        execution_completed=True,
        results=[],
    )

    assert (
        classify_execution_outcome(
            incomplete,
            workflow,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
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


def test_backend_exception_is_failed_even_when_halt_observation_is_emitted(tmp_path):
    workflow = _key_workflow("backend-failure", with_effect=True)
    workflow, bundle = _sealed(tmp_path, workflow, encrypted=False)
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=_TieredVerifier(),
        durable=True,
    )
    authorization = build_runtime_authorization(workflow, gate)

    class _BrokenBackend(FakeBackend):
        def press(self, key):
            raise ConnectionError("backend disconnected")

    report = Replayer(
        _BrokenBackend(),
        vision=_ReadyVision(),
        effect_verifier=_TieredVerifier(),
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "backend-run")

    assert report.halt is not None
    assert report.execution_outcome == ExecutionOutcome.FAILED.value
    assert report.results[0].failure_category == "runtime_failure"


def test_standard_rechecks_settled_requirement_at_actuation_boundary(tmp_path):
    workflow = _key_workflow("settled-boundary", with_effect=False)
    workflow, bundle = _sealed(tmp_path, workflow, encrypted=False)
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=_TieredVerifier(),
        durable=True,
    )
    authorization = build_runtime_authorization(workflow, gate)

    class _MutatingBackend(FakeBackend):
        replayer = None

        def screenshot(self):
            assert self.replayer is not None
            self.replayer.require_settled = False
            return super().screenshot()

    backend = _MutatingBackend()
    replayer = Replayer(
        backend,
        vision=FakeVision(),
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    )
    backend.replayer = replayer
    report = replayer.run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / "settled-boundary-run",
    )

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert report.results[0].failure_category == "governed_refusal"
    assert backend.actions == []


def test_standard_rechecks_effect_tier_before_actuation(tmp_path):
    workflow = _key_workflow("tier-boundary", with_effect=True)
    workflow, bundle = _sealed(tmp_path, workflow, encrypted=False)
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=_TieredVerifier(),
        durable=True,
    )
    authorization = build_runtime_authorization(workflow, gate)

    backend = FakeBackend()
    report = Replayer(
        backend,
        vision=_ReadyVision(),
        effect_verifier=OnScreenReadbackVerifier(
            backend=backend,
            vision=object(),
        ),
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "tier-boundary-run")

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert report.results[0].failure_category == "governed_refusal"
    assert backend.actions == []


def test_standard_verified_run_records_tiered_effect_evidence(tmp_path):
    workflow, bundle = _sealed(
        tmp_path,
        _key_workflow("verified-run", with_effect=True),
        encrypted=False,
    )
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=_TieredVerifier(),
        durable=True,
    )
    authorization = build_runtime_authorization(workflow, gate)
    report = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        effect_verifier=_TieredVerifier(),
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "verified-run")

    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert report.success is True
    assert report.results[0].effect_evidence[0].verification_tier == 1


def test_standard_resume_retains_structured_effect_evidence(tmp_path):
    workflow, bundle = _sealed(
        tmp_path,
        _key_workflow("verified-resume", with_effect=True),
        encrypted=False,
    )
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=_TieredVerifier(),
        durable=True,
    )
    authorization = build_runtime_authorization(workflow, gate)
    backend = FakeBackend()
    run_dir = tmp_path / "verified-resume"

    initial = Replayer(
        backend,
        vision=_ReadyVision(),
        effect_verifier=_TieredVerifier(),
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert initial.execution_outcome == ExecutionOutcome.VERIFIED.value
    actions_before_resume = list(backend.actions)

    resumed = Replayer(
        backend,
        vision=_ReadyVision(),
        effect_verifier=_TieredVerifier(),
        governed_authorization=authorization,
        governed_continuation=True,
        durable=True,
        require_settled=True,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        resume_from=1,
    )

    assert backend.actions == actions_before_resume
    assert resumed.execution_outcome == ExecutionOutcome.VERIFIED.value, (
        resumed.model_dump_json(indent=2)
    )
    assert resumed.results[0].effect_evidence[0].verification_tier == 1


def test_standard_program_routes_ordinary_failure_to_authored_handler(tmp_path):
    workflow = Workflow(
        name="governed-handler",
        program=ProgramGraph(
            entry="try-action",
            states={
                "try-action": State(
                    id="try-action",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="try-action",
                        intent="optional control",
                        action=ActionKind.CLICK,
                    ),
                    transitions=[Transition(target="done")],
                    on_exception="recover",
                ),
                "recover": State(
                    id="recover",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="recover",
                        intent="dismiss optional surface",
                        action=ActionKind.KEY,
                        key="Escape",
                    ),
                    transitions=[Transition(target="done")],
                ),
                "done": State(
                    id="done",
                    kind=StateKind.TERMINAL,
                    outcome="success",
                ),
            },
        ),
    )
    workflow, bundle = _sealed(tmp_path, workflow, encrypted=False)
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=_TieredVerifier(),
        durable=True,
    )
    assert gate.passed, gate.render()
    authorization = build_runtime_authorization(workflow, gate)
    backend = FakeBackend()

    report = Replayer(
        backend,
        vision=_ReadyVision(),
        effect_verifier=_TieredVerifier(),
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / "governed-handler-run",
    )

    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert report.results[0].exception_handled is True
    assert report.results[0].failure_category == "runtime_failure"
    assert backend.actions == [("press", "Escape")]


def test_deployment_runtime_accepts_named_profile():
    runtime = RuntimeSection(profile="standard")
    assert runtime.profile == "standard"


def test_demo_declared_write_requires_approval_and_stays_non_production(tmp_path):
    workflow = _key_workflow(
        "demo-write",
        with_effect=True,
        with_postcondition=True,
    )
    workflow, bundle = _sealed(tmp_path, workflow, encrypted=False)
    refused = _gate(
        workflow,
        bundle,
        ExecutionProfile.DEMO,
        verifier=None,
        durable=False,
    )
    assert refused.gate(GATE_APPROVAL).passed is False
    admitted = _gate(
        workflow,
        bundle,
        ExecutionProfile.DEMO,
        verifier=None,
        durable=False,
        approval=True,
    )
    authorization = build_runtime_authorization(workflow, admitted)
    vision = FakeVision()
    vision.text_results["Saved"] = object()
    report = Replayer(
        FakeBackend(),
        vision=vision,
        governed_authorization=authorization,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "demo-run")

    assert report.execution_outcome == ExecutionOutcome.COMPLETED_UNVERIFIED.value
    assert report.execution_completed is True
    assert report.success is True
    assert report.results[0].effect_approved_unverified is True


def test_production_unverified_completion_is_not_legacy_success_or_suppressed(
    tmp_path,
):
    workflow = _workflow()
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        results=[StepResult(step_id="save", intent="save", ok=True)],
    )
    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
    run_dir = tmp_path / "run"
    report.save(run_dir)

    assert report.execution_completed is True
    assert report.success is False
    assert status_from_report(0, report.model_dump(mode="json")) == "failed"
    assert summary_status(report) == "failed"
    assert attention_item(tmp_path, run_dir) is not None


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
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run-regulated")

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert report.results[0].step_id == "<profile>"
    assert "encrypted bundle" in (report.results[0].error or "")
    assert backend.actions == []
