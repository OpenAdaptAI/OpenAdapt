import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    ApiBinding,
    EffectVerificationEvidence,
    ExecutionOutcomeEnvelope,
    LoopSpec,
    OutcomeContractCounts,
    Postcondition,
    PostconditionKind,
    ProgramGraph,
    Relation,
    Resolution,
    RunReport,
    State,
    StateKind,
    Step,
    StepResult,
    Transition,
    Workflow,
)
from openadapt_flow.qualification import EnvironmentBoundary, QualificationProject
from openadapt_flow.report import render_run_report
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
from openadapt_flow.runtime.durable import CheckpointStore, resume
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.effect import ReadbackNav, ReadbackSpec
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier
from openadapt_flow.vision.ocr import OcrLine
from tests.test_durable_runtime import FakeSoRVerifier, _approval
from tests.test_replayer import FakeBackend, FakeVision, make_png

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
    def find_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.82,
    ):
        if template_png:
            return SimpleNamespace(
                point=(5, 5),
                region=(0, 0, 10, 10),
                confidence=0.99,
            )
        return super().find_template(
            screen_png,
            template_png,
            search_region=search_region,
            prefer_near=prefer_near,
            scales=scales,
            threshold=threshold,
        )

    def find_text(
        self,
        screen_png,
        text,
        *,
        region=None,
        min_ratio=0.8,
        raise_on_ambiguity=False,
    ):
        if text == "Submit target":
            return SimpleNamespace(
                point=(5, 5),
                region=(0, 0, 10, 10),
                confidence=0.99,
            )
        return super().find_text(
            screen_png,
            text,
            region=region,
            min_ratio=min_ratio,
            raise_on_ambiguity=raise_on_ambiguity,
        )

    def ocr(self, screen_png, *, region=None):
        del screen_png, region
        return [
            OcrLine(
                text="Synthetic record",
                region=(20, 0, 100, 10),
                confidence=0.99,
            )
        ]

    def wait_settled_result(self, backend, **kwargs):
        return SimpleNamespace(png=backend.screenshot(), settled=True)


class _CountingStateVerifier:
    def __init__(self, *, holds: bool) -> None:
        self.result = holds
        self.calls = 0

    def holds(self, screenshot, expected_state) -> bool:
        del screenshot, expected_state
        self.calls += 1
        return self.result


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
    with_identity: bool = True,
) -> Workflow:
    return Workflow(
        name=name,
        steps=[
            Step(
                id="submit",
                intent="submit",
                action=ActionKind.KEY,
                key="Enter",
                anchor=(
                    Anchor(
                        template="templates/submit.png",
                        region=(0, 0, 10, 10),
                        click_point=(5, 5),
                        ocr_text="Submit target",
                        context_text="Synthetic record",
                    )
                    if with_identity
                    else None
                ),
                identity_armed=True if with_identity else None,
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
    if any(
        step.anchor is not None and step.anchor.template == "templates/submit.png"
        for step in workflow.steps
    ):
        (bundle / "templates" / "submit.png").write_bytes(make_png((10, 10)))
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
    verified.governed_authorization_id = "authorization-1"
    verified.governed_runtime_inputs_digest = "a" * 64
    verified.results[0].postconditions_ok = True
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


def test_outcome_envelope_counts_only_effects_meeting_the_required_tier():
    workflow = _workflow()
    workflow.qualification = QualificationProject(
        environment=EnvironmentBoundary(
            target_kind="web",
            application="fixture",
            application_version="1",
            environment_digest="a" * 64,
            runtime_version="1.22.0",
        ),
        minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    effect_hash = _effect().contract_hash()
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        governed_authorization_id="authorization-1",
        governed_runtime_inputs_digest="b" * 64,
        results=[
            StepResult(
                step_id="save",
                intent="save",
                ok=True,
                effect_verified=True,
                effect_contract_hashes=[effect_hash],
                effect_evidence=[
                    EffectVerificationEvidence(
                        effect_contract_hash=effect_hash,
                        substrate="persisted-state",
                        verification_tier=(
                            VerificationTier.PERSISTED_STATE_REACQUISITION
                        ),
                        initial_verdict="confirmed",
                        final_verdict="confirmed",
                    )
                ],
            )
        ],
    )

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.execution_outcome == "COMPLETED_UNVERIFIED"
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.passed_contracts.effect == 0
    assert "effect_tier_3" in report.outcome_envelope.evidence_classes


def test_missing_declared_postcondition_is_completed_unverified_not_an_envelope_crash():
    workflow = _workflow()
    effect_hash = _effect().contract_hash()
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        governed_authorization_id="authorization-1",
        governed_runtime_inputs_digest="c" * 64,
        required_identity_step_ids=["save"],
        results=[
            StepResult(
                step_id="save",
                intent="save",
                ok=True,
                identity={"status": "verified"},
                effect_verified=True,
                effect_contract_hashes=[effect_hash],
                effect_evidence=[
                    EffectVerificationEvidence(
                        effect_contract_hash=effect_hash,
                        substrate="independent-system",
                        verification_tier=VerificationTier.INDEPENDENT_SYSTEM,
                        initial_verdict="confirmed",
                        final_verdict="confirmed",
                    )
                ],
            )
        ],
    )

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.execution_outcome == ExecutionOutcome.COMPLETED_UNVERIFIED.value
    assert report.success is False
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.required_contracts.postcondition == 1
    assert report.outcome_envelope.passed_contracts.postcondition == 0


def test_verified_envelope_requires_production_profile_eligibility_and_authorization():
    complete = OutcomeContractCounts(authorization=1)
    base = {
        "outcome": "VERIFIED",
        "profile": "standard",
        "production_eligible": True,
        "execution_completed": True,
        "required_contracts": complete,
        "passed_contracts": complete,
        "evidence_classes": ["authorization"],
    }
    for invalid in (
        {**base, "profile": "demo"},
        {**base, "production_eligible": False},
        {
            **base,
            "required_contracts": OutcomeContractCounts(),
            "passed_contracts": OutcomeContractCounts(),
            "evidence_classes": [],
        },
    ):
        with pytest.raises(ValueError):
            ExecutionOutcomeEnvelope(**invalid)


def test_native_run_network_state_remains_unknown_without_instrumentation():
    workflow = Workflow(name="native-read", steps=[])
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        execution_target_kind="windows",
        governed_authorization_id="authorization-1",
        governed_runtime_inputs_digest="d" * 64,
    )

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.execution_outcome == "VERIFIED"
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.external_network_calls == "unknown"


def test_report_separates_network_observation_from_screenshot_egress(tmp_path):
    workflow = Workflow(name="browser-read", steps=[])
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        execution_target_kind="web",
        screenshots_may_leave_box=False,
    )
    stamp_execution_outcome(report, workflow, ExecutionProfile.DEMO)
    report.save(tmp_path)

    markdown = render_run_report(tmp_path).read_text(encoding="utf-8")

    assert "- **External network calls:** `observed`" in markdown
    assert "- **Screenshot egress:** none observed" in markdown
    assert "fully local replay" not in markdown


def test_legacy_report_adopts_network_observation_from_its_envelope():
    report = RunReport.model_validate(
        {
            "workflow_name": "legacy",
            "started_at": "2026-07-25T00:00:00Z",
            "execution_profile": "demo",
            "execution_outcome": "COMPLETED_UNVERIFIED",
            "execution_completed": True,
            "success": True,
            "outcome_envelope": {
                "outcome": "COMPLETED_UNVERIFIED",
                "profile": "demo",
                "execution_completed": True,
                "external_network_calls": "observed",
            },
        }
    )

    assert report.external_network_calls == "observed"


def test_completed_compensation_produces_non_success_rolled_back_outcome(tmp_path):
    workflow = _workflow()
    effect_hash = _effect().contract_hash()
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        execution_completed=True,
        governed_authorization_id="authorization-1",
        governed_runtime_inputs_digest="c" * 64,
        results=[
            StepResult(
                step_id="save",
                intent="save",
                ok=True,
                effect_verified=True,
                effect_contract_hashes=[effect_hash],
                effect_evidence=[
                    EffectVerificationEvidence(
                        effect_contract_hash=effect_hash,
                        substrate="independent-system",
                        verification_tier=VerificationTier.INDEPENDENT_SYSTEM,
                        initial_verdict="refuted",
                        final_verdict="confirmed",
                        reconciliation_completed=True,
                        reconciliation_actions=1,
                    )
                ],
            )
        ],
    )

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
    run_dir = tmp_path / "rolled-back"
    report.save(run_dir)
    rendered = render_run_report(run_dir).read_text(encoding="utf-8")

    assert report.execution_outcome == "ROLLED_BACK"
    assert report.success is False
    assert report.production_eligible is False
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.compensation_actions == 1
    assert "compensation" in report.outcome_envelope.evidence_classes
    assert "remains a non-success outcome" in rendered
    attention = attention_item(tmp_path, run_dir)
    assert attention is not None
    assert attention.category == "operator_review"
    assert attention.status == "rolled_back"


def test_unconfirmed_compensation_attempt_is_not_reported_as_rolled_back():
    workflow = _workflow()
    effect_hash = _effect().contract_hash()
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        execution_completed=True,
        governed_authorization_id="authorization-1",
        governed_runtime_inputs_digest="e" * 64,
        results=[
            StepResult(
                step_id="save",
                intent="save",
                ok=True,
                postconditions_ok=True,
                effect_verified=False,
                effect_contract_hashes=[effect_hash],
                effect_evidence=[
                    EffectVerificationEvidence(
                        effect_contract_hash=effect_hash,
                        substrate="independent-system",
                        verification_tier=VerificationTier.INDEPENDENT_SYSTEM,
                        initial_verdict="refuted",
                        final_verdict="refuted",
                        reconciliation_completed=True,
                        reconciliation_actions=1,
                    )
                ],
            )
        ],
    )

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.execution_outcome == ExecutionOutcome.COMPLETED_UNVERIFIED.value
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.compensation_actions == 0
    assert "compensation" not in report.outcome_envelope.evidence_classes


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
    # Enter targets the retained "Submit" control, so it is a consequential
    # keyboard submission and needs an effect contract before Standard can
    # reach the actuation-boundary settled-state recheck exercised here.
    workflow = _key_workflow("settled-boundary", with_effect=True)
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
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.required_contracts.model_dump() == {
        "authorization": 1,
        "identity": 1,
        "postcondition": 0,
        "effect": 1,
    }
    assert (
        report.outcome_envelope.passed_contracts
        == report.outcome_envelope.required_contracts
    )
    assert report.outcome_envelope.evidence_classes == [
        "authorization",
        "effect_tier_1",
        "identity",
    ]
    assert report.outcome_envelope.model_calls == 0


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
    assert resumed.results[0].identity is not None
    assert resumed.results[0].identity.status == "verified"


def test_resume_preserves_failed_leg_model_and_network_evidence(tmp_path):
    workflow, bundle = _sealed(
        tmp_path,
        Workflow(
            name="whole-run-evidence",
            steps=[
                Step(
                    id="wait-for-saved",
                    intent="wait for saved state",
                    action=ActionKind.WAIT,
                    expect=[
                        Postcondition(
                            kind=PostconditionKind.TEXT_PRESENT,
                            text="Saved",
                            timeout_s=0.0,
                        )
                    ],
                )
            ],
        ),
        encrypted=False,
    )
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=None,
        durable=True,
    )
    authorization = build_runtime_authorization(workflow, gate)
    run_dir = tmp_path / "whole-run-evidence"
    verifier = _CountingStateVerifier(holds=False)

    initial = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        state_verifier=verifier,
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert initial.execution_outcome == ExecutionOutcome.HALTED.value
    assert initial.model_calls == 1
    assert verifier.calls == 1
    manifest = CheckpointStore(run_dir).read_manifest()
    assert manifest is not None
    assert manifest.model_calls == 1
    assert manifest.external_network_calls == "observed"

    resumed_vision = _ReadyVision()
    resumed_vision.text_results["Saved"] = SimpleNamespace(
        point=(5, 5),
        region=(0, 0, 10, 10),
        confidence=0.99,
    )
    resumed = resume(
        run_dir,
        Replayer(
            FakeBackend(),
            vision=resumed_vision,
            require_settled=True,
            poll_interval_s=0.0,
        ),
        approval=_approval(bundle),
    )

    assert resumed.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert resumed.success is True
    assert resumed.model_calls == 1
    assert resumed.external_network_calls == "observed"
    assert resumed.outcome_envelope is not None
    assert resumed.outcome_envelope.model_calls == 1
    assert resumed.outcome_envelope.external_network_calls == "observed"


def test_standard_resume_from_legacy_checkpoint_is_unverified(tmp_path):
    workflow, bundle = _sealed(
        tmp_path,
        _key_workflow("legacy-resume", with_effect=True),
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
    run_dir = tmp_path / "legacy-resume"
    initial = Replayer(
        backend,
        vision=_ReadyVision(),
        effect_verifier=_TieredVerifier(),
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert initial.execution_outcome == ExecutionOutcome.VERIFIED.value

    checkpoint_path = next((run_dir / "checkpoints").glob("step_*.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint.pop("identity")
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
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
    assert resumed.execution_outcome == ExecutionOutcome.COMPLETED_UNVERIFIED.value
    assert resumed.success is False


def _governed_loop_workflow() -> Workflow:
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": ValueExpr(param="patient")},
        expected_count=1,
        risk="irreversible",
    )
    body = ProgramGraph(
        entry="write",
        states={
            "write": State(
                id="write",
                kind=StateKind.ACTION,
                step=Step(
                    id="write",
                    intent="write current patient",
                    action=ActionKind.KEY,
                    key="Enter",
                    anchor=Anchor(
                        template="templates/write.png",
                        region=(0, 0, 10, 10),
                        click_point=(5, 5),
                        ocr_text="Submit target",
                        context_text="Synthetic record",
                    ),
                    identity_armed=True,
                    effects=[effect],
                ),
                transitions=[Transition(target="row-done")],
            ),
            "row-done": State(
                id="row-done",
                kind=StateKind.TERMINAL,
                outcome="success",
            ),
        },
    )
    return Workflow(
        name="governed-loop-resume",
        program=ProgramGraph(
            entry="loop",
            states={
                "loop": State(
                    id="loop",
                    kind=StateKind.LOOP,
                    loop=LoopSpec(relation="patients", body="body", var="patient"),
                    transitions=[Transition(target="done")],
                ),
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
        subflows={"body": body},
        data_sources={
            "patients": Relation(
                name="patients",
                rows=[
                    {"patient": "Alice"},
                    {"patient": "Bob"},
                    {"patient": "Cara"},
                ],
            )
        },
    )


def test_standard_program_resume_preserves_exact_loop_contracts_without_reactuation(
    tmp_path,
):
    workflow = _governed_loop_workflow()
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    (bundle / "templates" / "write.png").write_bytes(make_png((10, 10)))
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    assert workflow.manifest is not None
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
        admitted_policy_name="permissive",
        execution_profile="standard",
        minimum_effect_tier=int(VerificationTier.PERSISTED_STATE_REACQUISITION),
        required_identity_step_ids=("write",),
    )
    verifier = FakeSoRVerifier()
    verifier.verification_tier = VerificationTier.INDEPENDENT_SYSTEM
    verifier.refute.add((("patient", "Bob"),))
    run_dir = tmp_path / "run"
    initial = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        effect_verifier=verifier,
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert initial.execution_outcome == ExecutionOutcome.HALTED.value
    assert len(CheckpointStore(run_dir).program_checkpoints()) == 1, (
        initial.model_dump_json(indent=2)
    )
    store = CheckpointStore(run_dir)
    checkpoint = store.program_checkpoints()[0]
    checkpoint.resolution = Resolution(
        rung="grounder", point=(5, 5), confidence=0.9, elapsed_ms=1.0
    )
    checkpoint.drift_oracle_calls = 1
    store.write_program_checkpoint(checkpoint)
    manifest = store.read_manifest()
    assert manifest is not None
    manifest.screenshots_may_leave_box = True
    store.write_manifest(manifest)
    verifier.refute.clear()
    resumed_backend = FakeBackend()
    resumed = resume(
        run_dir,
        Replayer(
            resumed_backend,
            vision=_ReadyVision(),
            effect_verifier=verifier,
            require_settled=True,
        ),
        approval=_approval(bundle),
    )

    assert resumed.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert resumed.success is True
    assert resumed_backend.actions == [("press", "Enter"), ("press", "Enter")]
    assert len(resumed.results) == 3
    assert all(
        result.identity is not None and result.identity.status == "verified"
        for result in resumed.results
    )
    assert resumed.outcome_envelope is not None
    assert resumed.outcome_envelope.required_contracts.identity == 3
    assert resumed.outcome_envelope.passed_contracts.identity == 3
    assert resumed.outcome_envelope.required_contracts.effect == 3
    assert resumed.outcome_envelope.passed_contracts.effect == 3
    assert resumed.model_calls == 2
    assert resumed.rung_counts["grounder"] == 1
    assert resumed.screenshots_may_leave_box is True


def test_program_resume_idempotency_includes_self_contained_api_effects():
    effect = _effect()
    effect_hash = effect.contract_hash()
    state = State(
        id="api-write",
        kind=StateKind.ACTION,
        step=Step(
            id="api-write",
            intent="write through API",
            action=ActionKind.KEY,
            key="Enter",
            api_binding=ApiBinding(
                url_template="/records",
                effects=[effect],
            ),
        ),
    )
    replayer = Replayer(FakeBackend(), vision=FakeVision())
    replayer._completed_effect_keys = [effect_hash]
    replayer._completed_unverified_effect_keys = []
    replayer._completed_effect_evidence = [
        EffectVerificationEvidence(
            effect_contract_hash=effect_hash,
            substrate="independent-system",
            verification_tier=VerificationTier.INDEPENDENT_SYSTEM,
            initial_verdict="confirmed",
            final_verdict="confirmed",
        )
    ]
    report = RunReport(
        workflow_name="api-program-resume",
        started_at="2026-07-25T00:00:00Z",
    )

    skipped = replayer._skip_completed_effect_state(state, {}, report)

    assert skipped is True
    assert len(report.results) == 1
    assert report.results[0].effect_verified is True
    assert report.results[0].effect_contract_hashes == [effect_hash]


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


@pytest.mark.parametrize(
    ("profile", "encrypted", "expected_key"),
    [
        (ExecutionProfile.DEMO, False, None),
        (ExecutionProfile.DEMO, True, None),
        (ExecutionProfile.STANDARD, False, None),
        (ExecutionProfile.STANDARD, True, _KEY),
        (ExecutionProfile.REGULATED, True, _KEY),
    ],
)
def test_cli_replayer_wires_checkpoint_encryption_by_profile(
    monkeypatch, profile, encrypted, expected_key
):
    import openadapt_flow.__main__ as main
    import openadapt_flow.deployment as deployment

    captured = {}

    def capture(_backend, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(deployment, "build_replayer", capture)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    main._configured_replayer(
        object(),
        workflow=SimpleNamespace(encrypted=encrypted),
        allow_egress=False,
        effect_verifier=None,
        api_actuator=None,
        durable=profile is not ExecutionProfile.DEMO,
        use_structural=True,
        governed_authorization=SimpleNamespace(execution_profile=profile.value),
    )

    assert captured["checkpoint_key"] == expected_key


@pytest.mark.parametrize(
    ("profile", "encrypted"),
    [
        (ExecutionProfile.STANDARD, True),
        (ExecutionProfile.REGULATED, False),
    ],
)
def test_cli_replayer_refuses_missing_required_checkpoint_key_before_build(
    monkeypatch, profile, encrypted
):
    import openadapt_flow.__main__ as main
    import openadapt_flow.deployment as deployment
    from openadapt_flow.crypto import MissingKeyError

    build_calls = 0

    def capture(_backend, **_kwargs):
        nonlocal build_calls
        build_calls += 1
        return object()

    monkeypatch.setattr(deployment, "build_replayer", capture)
    monkeypatch.delenv("OPENADAPT_BUNDLE_KEY", raising=False)

    with pytest.raises(MissingKeyError, match="no encryption passphrase configured"):
        main._configured_replayer(
            object(),
            workflow=SimpleNamespace(encrypted=encrypted),
            allow_egress=False,
            effect_verifier=None,
            api_actuator=None,
            durable=True,
            use_structural=True,
            governed_authorization=SimpleNamespace(execution_profile=profile.value),
        )

    assert build_calls == 0


def test_encrypted_demo_does_not_require_durable_checkpoint_key(tmp_path):
    workflow, _bundle = _sealed(tmp_path, _workflow(), encrypted=True)
    replayer = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        governed_authorization=_authorization_for(workflow, ExecutionProfile.DEMO),
    )

    assert replayer._profile_runtime_refusal(workflow) is None


def test_demo_declared_write_requires_approval_and_stays_non_production(tmp_path):
    workflow = _key_workflow(
        "demo-write",
        with_effect=True,
        with_postcondition=True,
        with_identity=False,
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
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.outcome == "COMPLETED_UNVERIFIED"
    assert report.outcome_envelope.passed_contracts.effect == 0
    assert report.outcome_envelope.required_contracts.effect == 1
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
        checkpoint_key=_KEY,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run-regulated")

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert report.results[0].step_id == "<profile>"
    assert "encrypted bundle" in (report.results[0].error or "")
    assert backend.actions == []


@pytest.mark.parametrize(
    "profile", [ExecutionProfile.STANDARD, ExecutionProfile.REGULATED]
)
def test_application_sealed_production_run_seals_durable_state(tmp_path, profile):
    workflow, bundle = _sealed(
        tmp_path,
        _key_workflow(f"encrypted-{profile.value}", with_effect=True),
        encrypted=True,
    )
    gate = _gate(
        workflow,
        bundle,
        profile,
        verifier=_TieredVerifier(),
        durable=True,
    )
    assert gate.passed, gate.render()
    authorization = build_runtime_authorization(workflow, gate)
    run_dir = tmp_path / f"encrypted-{profile.value}-run"

    report = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        effect_verifier=_TieredVerifier(),
        governed_authorization=authorization,
        durable=True,
        checkpoint_key=_KEY,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert list((run_dir / "checkpoints").glob("*.json.enc"))
    assert not list((run_dir / "checkpoints").glob("*.json"))


@pytest.mark.parametrize(
    "profile", [ExecutionProfile.STANDARD, ExecutionProfile.REGULATED]
)
def test_replayer_rechecks_checkpoint_encryption_before_backend_access(
    tmp_path, profile
):
    workflow, bundle = _sealed(tmp_path, _workflow(), encrypted=True)
    backend = FakeBackend()
    report = Replayer(
        backend,
        vision=FakeVision(),
        governed_authorization=_authorization_for(workflow, profile),
        durable=True,
        require_settled=True,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / f"run-no-checkpoint-key-{profile.value}",
    )

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert report.results[0].step_id == "<profile>"
    assert "encrypted durable checkpoints" in (report.results[0].error or "")
    assert backend.actions == []
