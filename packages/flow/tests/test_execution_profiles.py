import hashlib
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
    build_outcome_envelope,
    classify_execution_outcome,
    execution_profile_contract,
    qualified_effect_requirements,
    requires_signed_qualification_admission,
    stamp_execution_outcome,
)
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    ActionDeliveryUncertainty,
    ActionKind,
    Anchor,
    ApiBinding,
    ApiIdentityBinding,
    EffectVerificationEvidence,
    ExecutionOutcomeEnvelope,
    FreshActuationEvent,
    Guard,
    IdentityCheck,
    IdentitySignalEvidence,
    LoopSpec,
    OutcomeContractCounts,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramExecutionScopeFrame,
    ProgramGraph,
    ProgramTransitionEvidence,
    Relation,
    Resolution,
    RunReport,
    State,
    StateKind,
    Step,
    StepResult,
    Transition,
    Workflow,
    predicate_contract_sha256,
)
from openadapt_flow.qualification import (
    EffectVerificationPolicy,
    EnvironmentBoundary,
    QualificationProject,
    workflow_contract_sha256,
)
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
from openadapt_flow.runtime.durable import ApprovalRequired, CheckpointStore, resume
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


def _keyboard_receipt() -> ActionDeliveryReceipt:
    return ActionDeliveryReceipt(
        receipt_id="profile-key",
        operation="physical_press",
        native=False,
        delivered_at="2026-07-28T00:00:00+00:00",
    )


def _coordinate_click_receipt() -> ActionDeliveryReceipt:
    return ActionDeliveryReceipt(
        receipt_id="profile-click",
        operation="guarded_coordinate_click",
        native=False,
        delivered_at="2026-07-28T00:00:00+00:00",
    )


def _coordinate_resolution() -> Resolution:
    return Resolution(
        rung="template",
        point=(5, 5),
        confidence=0.99,
        elapsed_ms=1.0,
    )


def _bind_report_to_workflow(report: RunReport, workflow: Workflow) -> RunReport:
    report.workflow_contract_sha256 = workflow_contract_sha256(workflow)
    run_id = report.governed_authorization_id or "profile-test-run"
    report.run_id_sha256 = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return report


def _transition_evidence(
    *,
    decision_index: int,
    state: State,
    verdicts: list[bool],
    target: str,
    inputs_digest: str,
) -> list[ProgramTransitionEvidence]:
    scope = [ProgramExecutionScopeFrame(graph_id="__program__")]
    return [
        ProgramTransitionEvidence(
            decision_index=decision_index,
            graph_id="__program__",
            state_id=state.id,
            program_scope=scope,
            transition_index=index,
            guard_contract_sha256=predicate_contract_sha256(
                state.transitions[index].guard
            ),
            guard_verdict=verdict,
            selected=index == len(verdicts) - 1,
            selected_target=target,
            guard_evidence_kind=(
                "unconditional"
                if state.transitions[index].guard is None
                else "parameters"
            ),
            governed_runtime_inputs_digest=inputs_digest,
        )
        for index, verdict in enumerate(verdicts)
    ]


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
    def program_predicate_contract(self):
        return {
            "present_texts": sorted(
                text for text, result in self.text_results.items() if result
            )
        }

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
        if text in {"Submit target", "Profile write completed"}:
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
            ),
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
    with_postcondition: bool = True,
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
                            text="Profile write completed",
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


@pytest.mark.parametrize(
    "profile", [ExecutionProfile.STANDARD, ExecutionProfile.REGULATED]
)
def test_real_production_actuation_requires_signed_qualification(profile):
    assert requires_signed_qualification_admission(profile, will_actuate=True)
    assert not requires_signed_qualification_admission(profile, will_actuate=False)


def test_demo_is_the_only_named_unsigned_actuation_profile():
    assert not requires_signed_qualification_admission(
        ExecutionProfile.DEMO,
        will_actuate=True,
    )
    assert not requires_signed_qualification_admission(None, will_actuate=True)


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


def test_regulated_refuses_persisted_readback_only_evidence(tmp_path):
    """An irreversible write with only tier-3 evidence is refused under Regulated.

    The same persisted-state verifier the Standard profile admits must fail
    the Regulated approval gate, and the runtime pre-actuation recheck must
    refuse (governed_refusal / HALTED) before any backend action.
    """

    class _PersistedStateVerifier(_TieredVerifier):
        verification_tier = VerificationTier.PERSISTED_STATE_REACQUISITION

    workflow, bundle = _sealed(tmp_path, _workflow(), encrypted=True)
    standard = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=_PersistedStateVerifier(),
        durable=True,
    )
    assert standard.gate(GATE_APPROVAL).passed is True
    refused = _gate(
        workflow,
        bundle,
        ExecutionProfile.REGULATED,
        verifier=_PersistedStateVerifier(),
        durable=True,
    )
    assert not refused.passed
    approval = refused.gate(GATE_APPROVAL)
    assert approval.passed is False
    assert "persisted-state-reacquisition" in approval.detail
    assert "independent-session" in approval.detail

    # Runtime fail-safe: admission with an independent-system verifier, then a
    # weaker tier-3 verifier at run time, must refuse before actuation.
    runtime_workflow, runtime_bundle = _sealed(
        tmp_path / "runtime",
        _key_workflow("regulated-tier-floor", with_effect=True),
        encrypted=True,
    )
    gate = _gate(
        runtime_workflow,
        runtime_bundle,
        ExecutionProfile.REGULATED,
        verifier=_TieredVerifier(),
        durable=True,
    )
    assert gate.passed, gate.render()
    authorization = build_runtime_authorization(runtime_workflow, gate)
    backend = FakeBackend()
    report = Replayer(
        backend,
        vision=_ReadyVision(),
        effect_verifier=_PersistedStateVerifier(),
        governed_authorization=authorization,
        durable=True,
        checkpoint_key=_KEY,
        require_settled=True,
    ).run(
        runtime_workflow,
        bundle_dir=runtime_bundle,
        run_dir=tmp_path / "regulated-tier-floor-run",
    )

    assert report.execution_outcome == ExecutionOutcome.HALTED.value
    assert report.results[0].failure_category == "governed_refusal"
    assert backend.actions == []


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
    verified.results[0].starting_state_settled = True
    verified.results[0].delivery_attempted = True
    verified.results[0].actuation = "guarded_coordinate"
    verified.results[0].resolution = _coordinate_resolution()
    verified.results[0].delivery_receipt = _coordinate_click_receipt()
    verified.required_identity_step_ids = ["save"]
    verified.results[0].identity = IdentityCheck(
        status="verified",
        mode="structured",
        coverage=1.0,
        expected="Synthetic record",
        observed="Synthetic record",
    )
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
            observed_effect="present",
        )
    ]
    _bind_report_to_workflow(verified, workflow)
    persisted = verified.model_copy(deep=True)
    persisted.results[0].effect_evidence[
        0
    ].verification_tier = VerificationTier.PERSISTED_STATE_REACQUISITION
    immediate = verified.model_copy(deep=True)
    immediate.results[0].effect_evidence[
        0
    ].verification_tier = VerificationTier.IMMEDIATE_SCREEN
    arbitrary = verified.model_copy(deep=True)
    arbitrary.results[0].effect_contract_hashes = ["f" * 64]
    arbitrary.results[0].effect_evidence[0].effect_contract_hash = "f" * 64
    missing_identity = verified.model_copy(deep=True)
    missing_identity.results[0].identity = None

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
            classify_execution_outcome(immediate, workflow, profile)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )
        assert (
            classify_execution_outcome(arbitrary, workflow, profile)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )
        assert (
            classify_execution_outcome(missing_identity, workflow, profile)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )
    # Persisted-state reacquisition (tier 3) remains the Standard floor, but
    # the Regulated profile requires INDEPENDENT evidence (tier 2 or
    # stronger): an on-screen re-read of an irreversible write can never
    # grade VERIFIED under Regulated.
    assert (
        classify_execution_outcome(persisted, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.VERIFIED
    )
    assert (
        classify_execution_outcome(persisted, workflow, ExecutionProfile.REGULATED)
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )
    missing_postcondition = workflow.model_copy(deep=True)
    missing_postcondition.steps[0].expect = []
    assert (
        classify_execution_outcome(
            verified,
            missing_postcondition,
            ExecutionProfile.STANDARD,
        )
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
        effect_policies=[
            EffectVerificationPolicy(
                step_id="save",
                actuation_path="gui",
                effect_index=0,
                effect_contract_hash=workflow.steps[0].effects[0].contract_hash(),
                tier=VerificationTier.INDEPENDENT_SYSTEM,
            )
        ],
    )
    assert (
        classify_execution_outcome(
            persisted,
            workflow,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


def _verified_production_report(workflow: Workflow) -> RunReport:
    effect_hash = workflow.steps[0].effects[0].contract_hash()
    return _bind_report_to_workflow(
        RunReport(
            workflow_name=workflow.name,
            started_at="2026-07-28T00:00:00Z",
            success=True,
            execution_completed=True,
            governed_authorization_id="authorization-1",
            governed_runtime_inputs_digest="a" * 64,
            required_identity_step_ids=["save"],
            results=[
                StepResult(
                    step_id="save",
                    intent="save",
                    ok=True,
                    starting_state_settled=True,
                    delivery_attempted=True,
                    actuation="guarded_coordinate",
                    resolution=_coordinate_resolution(),
                    delivery_receipt=_coordinate_click_receipt(),
                    identity=IdentityCheck(
                        status="verified",
                        mode="structured",
                        coverage=1.0,
                        expected="Synthetic record",
                        observed="Synthetic record",
                    ),
                    postconditions_ok=True,
                    effect_verified=True,
                    effect_contract_hashes=[effect_hash],
                    effect_evidence=[
                        EffectVerificationEvidence(
                            effect_contract_hash=effect_hash,
                            substrate="test",
                            verification_tier=VerificationTier.INDEPENDENT_SYSTEM,
                            initial_verdict="confirmed",
                            final_verdict="confirmed",
                            observed_effect="present",
                        )
                    ],
                )
            ],
        ),
        workflow,
    )


def test_production_outcome_refuses_contradictory_terminal_and_effect_evidence():
    workflow = _workflow()
    verified = _verified_production_report(workflow)
    assert (
        classify_execution_outcome(verified, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.VERIFIED
    )

    omitted_identity_requirement = verified.model_copy(deep=True)
    omitted_identity_requirement.required_identity_step_ids = []
    extra_identity_requirement = verified.model_copy(deep=True)
    extra_identity_requirement.required_identity_step_ids.append("undeclared")
    duplicate_identity_requirement = verified.model_copy(deep=True)
    duplicate_identity_requirement.required_identity_step_ids.append("save")

    canceled = verified.model_copy(update={"canceled": True})
    failed = verified.model_copy(update={"terminal_outcome": "failed"})
    bad_effect = verified.model_copy(deep=True)
    bad_effect.results[0].effect_evidence[0].observed_effect = "absent"
    uncertain = verified.model_copy(deep=True)
    uncertain.results[0].delivery_uncertainty = ActionDeliveryUncertainty(
        operation="click",
        native=False,
        observed_at="2026-07-28T00:00:01Z",
        cause_type="ActionDeliveryUncertain",
    )
    unknown_action = verified.model_copy(deep=True)
    unknown_action.results.append(
        StepResult(step_id="undeclared-write", intent="write", ok=True)
    )
    disguised_delivery = verified.model_copy(deep=True)
    disguised_delivery.results[0].skipped = True
    disguised_delivery.results[0].delivery_attempted = True
    unknown_actuation = verified.model_copy(deep=True)
    unknown_actuation.results[0].actuation = "invented_driver"
    runtime_failure = verified.model_copy(deep=True)
    runtime_failure.results[0].failure_category = "runtime_failure"
    contradictory_error = verified.model_copy(deep=True)
    contradictory_error.results[0].error = "delivery failed"

    assert (
        classify_execution_outcome(canceled, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.HALTED
    )
    assert (
        classify_execution_outcome(failed, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.FAILED
    )
    for report in (runtime_failure, contradictory_error):
        assert (
            classify_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
            is ExecutionOutcome.FAILED
        )
    for report in (
        bad_effect,
        uncertain,
        unknown_action,
        disguised_delivery,
        unknown_actuation,
        omitted_identity_requirement,
        extra_identity_requirement,
        duplicate_identity_requirement,
    ):
        assert (
            classify_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )


def test_profile_classifier_allows_optional_identity_abstention_but_not_mismatch():
    """Optional identity does not weaken a positive wrong-entity signal."""

    workflow = Workflow(
        name="optional-identity-observation",
        steps=[Step(id="wait", intent="wait for ready state", action=ActionKind.WAIT)],
    )
    base = _verified_production_report(_workflow())
    base.workflow_name = workflow.name
    base.required_identity_step_ids = []
    base.results = [
        StepResult(
            step_id="wait",
            intent="wait for ready state",
            ok=True,
            identity=IdentityCheck(status="abstain"),
        )
    ]
    _bind_report_to_workflow(base, workflow)

    assert (
        classify_execution_outcome(base, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.VERIFIED
    )

    mismatch = base.model_copy(deep=True)
    mismatch.results[0].identity = IdentityCheck(status="mismatch")
    assert (
        classify_execution_outcome(mismatch, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_receipt",
        "wrong_receipt_operation",
        "missing_resolution",
        "status_only_identity",
        "failed_settling",
        "terminal_fresh_frame_mismatch",
    ),
)
def test_verified_gui_outcome_requires_exact_runtime_action_evidence(mutation):
    workflow = _workflow()
    report = _verified_production_report(workflow)
    result = report.results[0]
    if mutation == "missing_receipt":
        result.delivery_receipt = None
    elif mutation == "wrong_receipt_operation":
        result.delivery_receipt = _keyboard_receipt()
    elif mutation == "missing_resolution":
        result.resolution = None
    elif mutation == "status_only_identity":
        result.identity = IdentityCheck(status="verified", mode="structured")
    elif mutation == "failed_settling":
        result.starting_state_settled = False
    else:
        result.fresh_actuation_events = [
            FreshActuationEvent(
                attempt=1,
                operation="click",
                changed_pixel_count=1,
                changed_bbox=(0, 0, 1, 1),
                frame_size=(10, 10),
                retried=False,
            )
        ]

    assert (
        classify_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


@pytest.mark.parametrize("action", [ActionKind.TYPE, ActionKind.SELECT_OPTION])
def test_human_attended_input_does_not_invent_engine_readback(action):
    workflow = _workflow()
    step = workflow.steps[0]
    step.action = action
    step.text = "Synthetic value"
    if action is ActionKind.SELECT_OPTION:
        step.selection_commit_key = "Enter"
    report = _verified_production_report(workflow)
    result = report.results[0]
    result.actuation = "human_attended"
    result.delivery_attempted = False
    result.delivery_receipt = None
    result.resolution = None
    result.starting_state_settled = None
    result.input_verified = None

    assert (
        classify_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.VERIFIED
    )
    forged = report.model_copy(deep=True)
    forged.results[0].input_verified = True
    assert (
        classify_execution_outcome(forged, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


def test_qualified_per_effect_tier_is_required_for_verified_outcome():
    workflow = _workflow()
    workflow.qualification = QualificationProject(
        environment=EnvironmentBoundary(
            target_kind="web",
            application="fixture",
            application_version="1",
            environment_digest="a" * 64,
            runtime_version="1.26.0",
        ),
        effect_policies=[
            EffectVerificationPolicy(
                step_id="save",
                actuation_path="gui",
                effect_index=0,
                effect_contract_hash=workflow.steps[0].effects[0].contract_hash(),
                tier=VerificationTier.INDEPENDENT_SYSTEM,
            )
        ],
    )
    strong = _verified_production_report(workflow)
    strong.governed_qualified_effect_requirements = list(
        qualified_effect_requirements(workflow, ExecutionProfile.STANDARD)
    )
    weak = strong.model_copy(deep=True)
    weak.results[0].effect_evidence[
        0
    ].verification_tier = VerificationTier.PERSISTED_STATE_REACQUISITION
    omitted_binding = strong.model_copy(deep=True)
    omitted_binding.governed_qualified_effect_requirements = []

    assert (
        classify_execution_outcome(strong, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.VERIFIED
    )
    for report in (weak, omitted_binding):
        assert (
            classify_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )


def test_qualified_per_effect_tier_is_enforced_at_gate_and_runtime(tmp_path):
    class _PersistedVerifier(_TieredVerifier):
        verification_tier = VerificationTier.PERSISTED_STATE_REACQUISITION

    workflow = _workflow()
    workflow.qualification = QualificationProject(
        environment=EnvironmentBoundary(
            target_kind="web",
            application="fixture",
            application_version="1",
            environment_digest="a" * 64,
            runtime_version="1.26.0",
        ),
        effect_policies=[
            EffectVerificationPolicy(
                step_id="save",
                actuation_path="gui",
                effect_index=0,
                effect_contract_hash=workflow.steps[0].effects[0].contract_hash(),
                tier=VerificationTier.INDEPENDENT_SYSTEM,
            )
        ],
    )
    workflow, bundle = _sealed(tmp_path, workflow, encrypted=False)
    requirements = qualified_effect_requirements(workflow, ExecutionProfile.STANDARD)

    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=_PersistedVerifier(),
        durable=True,
    )
    assert gate.gate(GATE_APPROVAL).passed is False

    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
        admitted_policy_name="permissive",
        execution_profile="standard",
        minimum_effect_tier=int(VerificationTier.PERSISTED_STATE_REACQUISITION),
        qualified_effect_requirements=requirements,
    )
    replayer = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
    )
    assert (
        replayer._profile_effect_tier_refusal(
            workflow,
            workflow.steps[0],
            "gui",
            workflow.steps[0].effects,
            _PersistedVerifier(),
        )
        is not None
    )
    assert (
        replayer._profile_effect_tier_refusal(
            workflow,
            workflow.steps[0],
            "gui",
            workflow.steps[0].effects,
            _TieredVerifier(),
        )
        is None
    )


@pytest.mark.parametrize("action", [ActionKind.CLICK, ActionKind.TYPE])
def test_api_verified_outcome_requires_runtime_delivery_attempt_shape(action):
    workflow = _workflow()
    workflow.steps[0].action = action
    if action is ActionKind.TYPE:
        workflow.steps[0].text = "{record_id}"
    workflow.params["record_id"] = "synthetic-1"
    api_effect = workflow.steps[0].effects[0].model_copy(deep=True)
    api_effect.match["record_id"] = ValueExpr(param="record_id")
    workflow.steps[0].api_binding = ApiBinding(
        method="POST",
        url_template="/records",
        on_unavailable="halt",
        body_template={"record_id": "{record_id}"},
        effects=[api_effect],
        identity=[
            ApiIdentityBinding(
                key="record_id",
                param="record_id",
                effect_field="record_id",
                request_pointers=["/body/record_id"],
            )
        ],
    )
    workflow.steps[0].expect = []
    workflow.steps[0].anchor = None
    workflow.steps[0].identity_armed = False
    report = _verified_production_report(workflow)
    report.params = {"record_id": "synthetic-1"}
    report.results[0].actuation = "api"
    report.results[0].delivery_attempted = True
    report.results[0].resolution = None
    report.results[0].delivery_receipt = None
    report.results[0].starting_state_settled = None
    report.results[0].postconditions_ok = None
    report.results[0].identity = IdentityCheck(
        status="verified",
        mode="signal_quorum",
        coverage=1.0,
        signal_evidence=[
            IdentitySignalEvidence(
                signal="record_id",
                source="api_parameter",
                verdict="verified",
                evidence_class="api_request_effect_binding",
                match="exact",
            )
        ],
        quorum_required=1,
        quorum_verified=1,
    )
    _bind_report_to_workflow(report, workflow)
    api_effect_hash = api_effect.resolved_contract_hash(report.params)
    report.results[0].effect_contract_hashes = [api_effect_hash]
    report.results[0].effect_evidence[0].effect_contract_hash = api_effect_hash
    not_delivered = report.model_copy(deep=True)
    not_delivered.results[0].delivery_attempted = False
    mixed_gui_api = report.model_copy(deep=True)
    mixed_gui_api.results[0].resolution = Resolution(
        point=(5, 5),
        rung="geometry",
        confidence=0.1,
        elapsed_ms=1.0,
    )
    mixed_gui_api.results[0].starting_state_settled = True
    mixed_gui_api.results[0].postconditions_ok = True
    status_only_identity = report.model_copy(deep=True)
    status_only_identity.results[0].identity = IdentityCheck(
        status="verified", mode="structured"
    )

    assert (
        classify_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.VERIFIED
    )
    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.required_contracts.postcondition == 0
    assert report.outcome_envelope.passed_contracts.postcondition == 0
    for invalid in (not_delivered, mixed_gui_api, status_only_identity):
        assert (
            classify_execution_outcome(invalid, workflow, ExecutionProfile.STANDARD)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )


@pytest.mark.parametrize(
    ("action", "action_fields"),
    [
        (ActionKind.KEY, {"key": "Enter"}),
        (ActionKind.TYPE, {"text": "Synthetic value"}),
        (
            ActionKind.SELECT_OPTION,
            {
                "text": "Synthetic value",
                "selection_commit_key": "Enter",
                "selection_region": (0, 0, 10, 10),
                "anchor": Anchor(
                    template="field.png",
                    region=(0, 0, 10, 10),
                    click_point=(5, 5),
                ),
            },
        ),
    ],
)
def test_effect_free_api_success_shape_is_never_verified(action, action_fields):
    workflow = Workflow(
        name="effect-free-api",
        steps=[
            Step(
                id="lookup",
                intent="look up record",
                action=action,
                api_binding=ApiBinding(method="GET", url_template="/records"),
                **action_fields,
            )
        ],
    )
    report = _bind_report_to_workflow(
        RunReport(
            workflow_name=workflow.name,
            started_at="2026-07-28T00:00:00Z",
            success=True,
            execution_completed=True,
            governed_authorization_id="authorization-1",
            governed_runtime_inputs_digest="a" * 64,
            results=[
                StepResult(
                    step_id="lookup",
                    intent="look up record",
                    ok=True,
                    actuation="api",
                    delivery_attempted=True,
                )
            ],
        ),
        workflow,
    )

    assert (
        classify_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


def test_program_outcome_requires_exact_ordered_action_trace():
    workflow = Workflow(
        name="program-outcome",
        program=ProgramGraph(
            entry="write",
            states={
                "write": State(
                    id="write",
                    kind=StateKind.ACTION,
                    step=Step(id="write", intent="write", action=ActionKind.KEY),
                    transitions=[Transition(target="done")],
                ),
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
    )
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-28T00:00:00Z",
        success=True,
        execution_completed=True,
        terminal_outcome="success",
        visited_states=["write", "done"],
        governed_authorization_id="authorization-1",
        governed_runtime_inputs_digest="b" * 64,
        results=[
            StepResult(
                step_id="write",
                intent="write",
                ok=True,
                starting_state_settled=True,
                delivery_attempted=True,
                actuation="guarded_keyboard",
                delivery_receipt=_keyboard_receipt(),
                program_scope=[ProgramExecutionScopeFrame(graph_id="__program__")],
            )
        ],
    )
    _bind_report_to_workflow(report, workflow)
    report.program_transition_evidence = _transition_evidence(
        decision_index=0,
        state=workflow.program.states["write"],
        verdicts=[True],
        target="done",
        inputs_digest=report.governed_runtime_inputs_digest or "",
    )
    assert (
        classify_execution_outcome(report, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.VERIFIED
    )

    for invalid in (
        report.model_copy(update={"visited_states": []}),
        report.model_copy(update={"results": []}),
        report.model_copy(update={"terminal_outcome": None}),
        report.model_copy(
            update={
                "results": [
                    report.results[0].model_copy(
                        update={
                            "skipped": True,
                            "ok": True,
                            "starting_state_settled": None,
                            "delivery_attempted": False,
                            "actuation": None,
                        }
                    )
                ]
            }
        ),
    ):
        assert (
            classify_execution_outcome(invalid, workflow, ExecutionProfile.STANDARD)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )


def test_program_fault_prefix_requires_exact_trace_and_prior_delivery():
    workflow = Workflow(
        name="program-fault-prefix",
        program=ProgramGraph(
            entry="prepare",
            states={
                "prepare": State(
                    id="prepare",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="prepare",
                        intent="prepare",
                        action=ActionKind.KEY,
                    ),
                    transitions=[Transition(target="submit")],
                ),
                "submit": State(
                    id="submit",
                    kind=StateKind.ACTION,
                    step=Step(id="submit", intent="submit", action=ActionKind.KEY),
                    transitions=[Transition(target="done")],
                ),
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
    )
    scope = [ProgramExecutionScopeFrame(graph_id="__program__")]
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-28T00:00:00Z",
        success=False,
        execution_completed=False,
        terminal_outcome="halt",
        visited_states=["prepare", "submit"],
        governed_authorization_id="authorization-1",
        governed_runtime_inputs_digest="b" * 64,
        results=[
            StepResult(
                step_id="prepare",
                intent="prepare",
                ok=True,
                starting_state_settled=True,
                delivery_attempted=True,
                actuation="guarded_keyboard",
                delivery_receipt=_keyboard_receipt(),
                program_scope=scope,
            ),
            StepResult(
                step_id="submit",
                intent="submit",
                ok=False,
                safety_halt=True,
                failure_category="governed_refusal",
                delivery_attempted=False,
                error="target ambiguity refused before delivery",
                program_scope=scope,
            ),
            StepResult(
                step_id="<terminal>",
                intent="program halt",
                ok=False,
                safety_halt=True,
                error="target ambiguity refused before delivery",
            ),
        ],
    )
    _bind_report_to_workflow(report, workflow)
    report.program_transition_evidence = _transition_evidence(
        decision_index=0,
        state=workflow.program.states["prepare"],
        verdicts=[True],
        target="submit",
        inputs_digest=report.governed_runtime_inputs_digest or "",
    )

    assert (
        classify_execution_outcome(
            report,
            workflow,
            ExecutionProfile.STANDARD,
            _qualification_fault_target_step_id="submit",
        )
        is ExecutionOutcome.VERIFIED
    )
    for invalid in (
        report.model_copy(update={"visited_states": ["submit"]}),
        report.model_copy(
            update={
                "results": [
                    report.results[0].model_copy(update={"delivery_attempted": False}),
                    *report.results[1:],
                ]
            }
        ),
        report.model_copy(update={"terminal_outcome": "success"}),
        report.model_copy(update={"terminal_outcome": "escalate"}),
        report.model_copy(
            update={
                "results": [
                    *report.results[:-1],
                    report.results[-1].model_copy(
                        update={"error": "an unrelated terminal reason"}
                    ),
                ]
            }
        ),
        report.model_copy(
            update={
                "results": [
                    *report.results[:-1],
                    report.results[-1].model_copy(
                        update={
                            "delivery_attempted": True,
                            "actuation": "api",
                            "before_png": "forged-before.png",
                            "effect_verified": True,
                        }
                    ),
                ]
            }
        ),
        report.model_copy(
            update={
                "results": [
                    report.results[0],
                    report.results[1].model_copy(
                        update={
                            "safety_halt": False,
                            "failure_category": "runtime_failure",
                            "delivery_attempted": True,
                            "actuation": "guarded_keyboard",
                        }
                    ),
                    report.results[2],
                ]
            }
        ),
    ):
        assert (
            classify_execution_outcome(
                invalid,
                workflow,
                ExecutionProfile.STANDARD,
                _qualification_fault_target_step_id="submit",
            )
            is not ExecutionOutcome.VERIFIED
        )


def test_program_outcome_recomputes_ordered_parameter_transitions():
    def _workflow(transitions: list[Transition]) -> Workflow:
        return Workflow(
            name="ordered-program-outcome",
            program=ProgramGraph(
                entry="pick",
                states={
                    "pick": State(
                        id="pick",
                        kind=StateKind.BRANCH,
                        transitions=transitions,
                    ),
                    "first": State(
                        id="first",
                        kind=StateKind.ACTION,
                        step=Step(id="first", intent="first", action=ActionKind.KEY),
                        transitions=[Transition(target="done")],
                    ),
                    "second": State(
                        id="second",
                        kind=StateKind.ACTION,
                        step=Step(id="second", intent="second", action=ActionKind.KEY),
                        transitions=[Transition(target="done")],
                    ),
                    "done": State(
                        id="done", kind=StateKind.TERMINAL, outcome="success"
                    ),
                },
            ),
        )

    def _report(workflow: Workflow, *, route: str) -> RunReport:
        report = RunReport(
            workflow_name=workflow.name,
            started_at="2026-07-28T00:00:00Z",
            success=True,
            execution_completed=True,
            terminal_outcome="success",
            visited_states=["pick", "second", "done"],
            params={"route": route},
            governed_authorization_id="authorization-1",
            governed_runtime_inputs_digest="b" * 64,
            results=[
                StepResult(
                    step_id="second",
                    intent="second",
                    ok=True,
                    starting_state_settled=True,
                    delivery_attempted=True,
                    actuation="guarded_keyboard",
                    delivery_receipt=_keyboard_receipt(),
                    program_scope=[ProgramExecutionScopeFrame(graph_id="__program__")],
                )
            ],
        )
        _bind_report_to_workflow(report, workflow)
        pick = workflow.program.states["pick"]
        first_guard_matches = bool(
            pick.transitions[0].guard is not None and route == "first"
        )
        report.program_transition_evidence = [
            *_transition_evidence(
                decision_index=0,
                state=pick,
                verdicts=[True] if first_guard_matches else [False, True],
                target="first" if first_guard_matches else "second",
                inputs_digest=report.governed_runtime_inputs_digest or "",
            ),
            *_transition_evidence(
                decision_index=1,
                state=workflow.program.states["second"],
                verdicts=[True],
                target="done",
                inputs_digest=report.governed_runtime_inputs_digest or "",
            ),
        ]
        return report

    two_unconditional = _workflow(
        [Transition(target="first"), Transition(target="second")]
    )
    assert (
        classify_execution_outcome(
            _report(two_unconditional, route="second"),
            two_unconditional,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )

    ordered_guard = _workflow(
        [
            Transition(
                guard=Predicate(
                    kind=PredicateKind.PARAM_EQUALS,
                    param="route",
                    value="first",
                ),
                target="first",
            ),
            Transition(target="second"),
        ]
    )
    assert (
        classify_execution_outcome(
            _report(ordered_guard, route="first"),
            ordered_guard,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )
    assert (
        classify_execution_outcome(
            _report(ordered_guard, route="second"),
            ordered_guard,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.VERIFIED
    )
    exact = _report(ordered_guard, route="second")
    for update in (
        {"graph_id": "forged-graph"},
        {"state_id": "forged-state"},
        {"program_scope": [ProgramExecutionScopeFrame(graph_id="forged-graph")]},
        {"decision_index": 1},
    ):
        tampered = exact.model_copy(deep=True)
        tampered.program_transition_evidence[1] = tampered.program_transition_evidence[
            1
        ].model_copy(update=update)
        assert (
            classify_execution_outcome(
                tampered,
                ordered_guard,
                ExecutionProfile.STANDARD,
            )
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )

    visual_guard = _workflow(
        [
            Transition(
                guard=Predicate(
                    kind=PredicateKind.TEXT_PRESENT,
                    text="Approve",
                ),
                target="first",
            ),
            Transition(target="second"),
        ]
    )
    assert (
        classify_execution_outcome(
            _report(visual_guard, route="second"),
            visual_guard,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


def test_program_edge_kind_disambiguates_normal_and_exception_paths(tmp_path):
    def workflow(
        *,
        failing_source: bool,
        branch_source: bool = False,
        missing_subflow: bool = False,
    ) -> Workflow:
        source = (
            State(
                id="source",
                kind=(StateKind.BRANCH if missing_subflow else StateKind.SUBFLOW_CALL),
                subflow=None if missing_subflow else "empty",
                transitions=(
                    [
                        Transition(
                            guard=Predicate(
                                kind=PredicateKind.PARAM_EQUALS,
                                param="route",
                                value="normal",
                            ),
                            target="normal",
                        )
                    ]
                    if missing_subflow
                    else [Transition(target="normal")]
                ),
                on_exception="handler",
            )
            if branch_source
            else State(
                id="source",
                kind=StateKind.ACTION,
                step=Step(
                    id="source-step",
                    intent="source",
                    action=(ActionKind.CLICK if failing_source else ActionKind.KEY),
                    key=None if failing_source else "S",
                ),
                transitions=[Transition(target="handler")],
                on_exception="handler",
            )
        )
        return Workflow(
            name="edge-kind",
            program=ProgramGraph(
                entry="source",
                states={
                    "source": source,
                    "normal": State(
                        id="normal",
                        kind=StateKind.ACTION,
                        step=Step(
                            id="normal-step",
                            intent="normal",
                            action=ActionKind.KEY,
                            key="N",
                        ),
                        transitions=[Transition(target="done")],
                    ),
                    "handler": State(
                        id="handler",
                        kind=StateKind.ACTION,
                        step=Step(
                            id="handler-step",
                            intent="handler",
                            action=ActionKind.KEY,
                            key="H",
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
            subflows=(
                {
                    "empty": ProgramGraph(
                        entry="empty-done",
                        states={
                            "empty-done": State(
                                id="empty-done",
                                kind=StateKind.TERMINAL,
                                outcome="success",
                            )
                        },
                    )
                }
                if branch_source and not missing_subflow
                else {}
            ),
        )

    def governed_run(item: Workflow, root: Path) -> tuple[Workflow, RunReport, Path]:
        bundle = root / "bundle"
        run_dir = root / "run"
        item.save(bundle)
        item = Workflow.load(bundle)
        assert item.manifest is not None
        authorization = GovernedRunAuthorization(
            bundle_content_digest=item.manifest.content_digest,
            runtime_inputs_digest=runtime_inputs_digest(item, None, None),
            admitted_policy_name="permissive",
            execution_profile="standard",
            minimum_effect_tier=int(VerificationTier.PERSISTED_STATE_REACQUISITION),
            required_identity_step_ids=(),
        )
        report = Replayer(
            FakeBackend(),
            vision=_ReadyVision(),
            governed_authorization=authorization,
            durable=True,
            require_settled=True,
            poll_interval_s=0.0,
        ).run(item, bundle_dir=bundle, run_dir=run_dir)
        return item, report, run_dir

    normal_workflow, normal, normal_root = governed_run(
        workflow(failing_source=False), tmp_path / "normal"
    )
    assert normal.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert normal.results[0].exception_handled is False
    assert (
        classify_execution_outcome(
            normal,
            normal_workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=normal_root,
            transition_predicate_vision=_ReadyVision(),
        )
        is ExecutionOutcome.VERIFIED
    )

    exception_workflow, exception, exception_root = governed_run(
        workflow(failing_source=True), tmp_path / "exception"
    )
    assert exception.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert exception.results[0].ok is False
    assert exception.results[0].exception_handled is True
    assert exception.results[0].skipped is False
    assert len(exception.program_exception_evidence) == 1
    action_failure = exception.program_exception_evidence[0]
    assert action_failure.failure_kind == "action_failure"
    assert action_failure.action_failure_category == "runtime_failure"
    assert (
        action_failure.error_sha256
        == hashlib.sha256(
            (exception.results[0].error or "").encode("utf-8")
        ).hexdigest()
    )
    assert (
        classify_execution_outcome(
            exception,
            exception_workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=exception_root,
            transition_predicate_vision=_ReadyVision(),
        )
        is ExecutionOutcome.VERIFIED
    )
    invalid_exception_shape = exception.model_copy(deep=True)
    invalid_exception_shape.results[0].ok = True
    assert (
        classify_execution_outcome(
            invalid_exception_shape,
            exception_workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=exception_root,
            transition_predicate_vision=_ReadyVision(),
        )
        is not ExecutionOutcome.VERIFIED
    )
    for update in (
        {"error_sha256": "0" * 64},
        {"action_failure_category": None},
    ):
        tampered_cause = exception.model_copy(deep=True)
        tampered_cause.program_exception_evidence[0] = action_failure.model_copy(
            update=update
        )
        assert (
            classify_execution_outcome(
                tampered_cause,
                exception_workflow,
                ExecutionProfile.STANDARD,
                transition_evidence_root=exception_root,
                transition_predicate_vision=_ReadyVision(),
            )
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )

    # A non-action branch cannot silently route to on_exception when its exact
    # unconditional transition selected a different state.
    branch_workflow, branch, branch_root = governed_run(
        workflow(failing_source=False, branch_source=True), tmp_path / "branch"
    )
    assert branch.execution_outcome == ExecutionOutcome.VERIFIED.value
    forged_handler = branch.model_copy(deep=True)
    forged_handler.visited_states = [
        "source",
        "empty-done",
        "handler",
        "done",
    ]
    forged_handler.results[0] = forged_handler.results[0].model_copy(
        update={"step_id": "handler-step", "intent": "handler"}
    )
    forged_handler.program_transition_evidence[1] = (
        forged_handler.program_transition_evidence[1].model_copy(
            update={"state_id": "handler"}
        )
    )
    assert (
        classify_execution_outcome(
            forged_handler,
            branch_workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=branch_root,
            transition_predicate_vision=_ReadyVision(),
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )

    # A real non-action failure can use the same target as its normal edge only
    # because the runtime emits a typed, recomputable exception decision.
    missing_workflow, missing, missing_root = governed_run(
        workflow(
            failing_source=False,
            branch_source=True,
            missing_subflow=True,
        ),
        tmp_path / "branch-without-transition",
    )
    assert missing.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert [item.failure_kind for item in missing.program_exception_evidence] == [
        "branch_without_transition"
    ]
    assert missing.program_exception_evidence[0].target_state_id == "handler"
    assert (
        classify_execution_outcome(
            missing,
            missing_workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=missing_root,
            transition_predicate_vision=_ReadyVision(),
        )
        is ExecutionOutcome.VERIFIED
    )
    missing_proof = missing.model_copy(update={"program_exception_evidence": []})
    assert (
        classify_execution_outcome(
            missing_proof,
            missing_workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=missing_root,
            transition_predicate_vision=_ReadyVision(),
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


def test_linear_and_skipped_result_shapes_fail_closed():
    linear = Workflow(
        name="linear-shape",
        steps=[Step(id="step", intent="step", action=ActionKind.KEY, key="S")],
    )
    base = _bind_report_to_workflow(
        RunReport(
            workflow_name=linear.name,
            started_at="2026-07-28T00:00:00Z",
            success=True,
            execution_completed=True,
            governed_authorization_id="authorization-1",
            governed_runtime_inputs_digest="b" * 64,
            results=[
                StepResult(
                    step_id="step",
                    intent="step",
                    ok=True,
                    starting_state_settled=True,
                    delivery_attempted=True,
                    actuation="guarded_keyboard",
                    delivery_receipt=_keyboard_receipt(),
                )
            ],
        ),
        linear,
    )
    for invalid_result in (
        base.results[0].model_copy(update={"exception_handled": True}),
        base.results[0].model_copy(
            update={
                "skipped": True,
                "ok": False,
                "delivery_attempted": False,
                "actuation": None,
            }
        ),
    ):
        invalid = base.model_copy(update={"results": [invalid_result]})
        assert (
            classify_execution_outcome(
                invalid,
                linear,
                ExecutionProfile.STANDARD,
            )
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )

    guarded = Workflow(
        name="declared-guard-skip",
        params={"mode": "disabled"},
        steps=[
            Step(
                id="optional",
                intent="optional",
                action=ActionKind.KEY,
                key="O",
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.PARAM_EQUALS,
                        param="mode",
                        value="enabled",
                    ),
                    on_unmet="skip",
                ),
            )
        ],
    )
    guarded_report = _bind_report_to_workflow(
        RunReport(
            workflow_name=guarded.name,
            started_at="2026-07-28T00:00:00Z",
            success=True,
            execution_completed=True,
            governed_authorization_id="authorization-1",
            governed_runtime_inputs_digest="b" * 64,
            params={"mode": "disabled"},
            results=[
                StepResult(
                    step_id="optional",
                    intent="optional",
                    ok=True,
                    skipped=True,
                    starting_state_settled=True,
                    delivery_attempted=False,
                )
            ],
        ),
        guarded,
    )
    assert (
        classify_execution_outcome(
            guarded_report,
            guarded,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.VERIFIED
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
        effect_policies=[
            EffectVerificationPolicy(
                step_id="save",
                actuation_path="gui",
                effect_index=0,
                effect_contract_hash=workflow.steps[0].effects[0].contract_hash(),
                tier=VerificationTier.INDEPENDENT_SYSTEM,
            )
        ],
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
                        observed_effect="present",
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


def test_outcome_envelope_never_counts_an_unrelated_effect_hash():
    workflow = _workflow()
    report = _verified_production_report(workflow)
    exact_hash = report.results[0].effect_contract_hashes[0]
    unrelated = "sha256:" + "f" * 64
    report.results[0].effect_contract_hashes = [unrelated]
    report.results[0].effect_evidence[0].effect_contract_hash = unrelated

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.execution_outcome == ExecutionOutcome.COMPLETED_UNVERIFIED.value
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.required_contracts.effect == 1
    assert report.outcome_envelope.passed_contracts.effect == 0
    assert not any(
        item.startswith("effect_tier_")
        for item in report.outcome_envelope.evidence_classes
    )

    extra_evidence = _verified_production_report(workflow)
    extra_evidence.results[0].effect_evidence.append(
        EffectVerificationEvidence(
            effect_contract_hash=unrelated,
            substrate="test",
            verification_tier=VerificationTier.INDEPENDENT_SYSTEM,
            initial_verdict="confirmed",
            final_verdict="confirmed",
            observed_effect="present",
        )
    )
    assert extra_evidence.results[0].effect_contract_hashes == [exact_hash]

    stamp_execution_outcome(extra_evidence, workflow, ExecutionProfile.STANDARD)

    assert extra_evidence.outcome_envelope is not None
    assert extra_evidence.outcome_envelope.passed_contracts.effect == 0


def test_incomplete_qualification_effect_policy_never_verifies_or_counts():
    workflow = _workflow()
    workflow.qualification = QualificationProject(
        environment=EnvironmentBoundary(
            target_kind="web",
            application="fixture",
            application_version="1",
            environment_digest="a" * 64,
            runtime_version="1.26.0",
        ),
        minimum_effect_tier=VerificationTier.PERSISTED_STATE_REACQUISITION,
        effect_policies=[],
    )
    report = _verified_production_report(workflow)
    report.results[0].effect_evidence[
        0
    ].verification_tier = VerificationTier.PERSISTED_STATE_REACQUISITION

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.execution_outcome == ExecutionOutcome.COMPLETED_UNVERIFIED.value
    assert report.production_eligible is False
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.required_contracts.effect == 1
    assert report.outcome_envelope.passed_contracts.effect == 0


def test_linear_result_cannot_inject_loop_scope_into_effect_binding():
    workflow = _workflow()
    workflow.params["record_id"] = "base"
    workflow.data_sources["rows"] = Relation(
        name="rows", rows=[{"record_id": "forged"}]
    )
    workflow.steps[0].effects[0].match["record_id"] = ValueExpr(param="record_id")
    report = _verified_production_report(workflow)
    report.params = {"record_id": "base"}
    report.results[0].program_scope = [
        ProgramExecutionScopeFrame(
            graph_id="__program__",
            loop_state_id="forged-loop",
            relation="rows",
            row_index=0,
        )
    ]
    forged_hash = (
        workflow.steps[0].effects[0].resolved_contract_hash({"record_id": "forged"})
    )
    report.results[0].effect_contract_hashes = [forged_hash]
    report.results[0].effect_evidence[0].effect_contract_hash = forged_hash
    _bind_report_to_workflow(report, workflow)

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.execution_outcome == ExecutionOutcome.COMPLETED_UNVERIFIED.value
    assert report.production_eligible is False
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.passed_contracts.effect == 0


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
    assert len(report.outcome_envelope.postcondition_evidence) == 1
    assert report.outcome_envelope.postcondition_evidence[0].verdict == "unverifiable"


def test_outcome_envelope_retains_exact_explicit_and_intrinsic_contracts():
    workflow = Workflow(
        name="typed-postcondition-proof",
        steps=[
            Step(
                id="type-note",
                intent="type note",
                action=ActionKind.TYPE,
                text="synthetic",
                expect=[
                    Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved"),
                    Postcondition(kind=PostconditionKind.TEXT_ABSENT, text="Draft"),
                ],
            )
        ],
    )
    report = _bind_report_to_workflow(
        RunReport(
            workflow_name=workflow.name,
            started_at="2026-07-28T00:00:00Z",
            execution_profile="standard",
            execution_outcome="VERIFIED",
            execution_completed=True,
            production_eligible=True,
            success=True,
            governed_authorization_id="authorization-1",
            governed_runtime_inputs_digest="a" * 64,
            results=[
                StepResult(
                    step_id="type-note",
                    intent="type note",
                    ok=True,
                    postconditions_ok=True,
                    input_verified=True,
                    starting_state_settled=True,
                    delivery_attempted=True,
                    actuation="guarded_keyboard",
                )
            ],
        ),
        workflow,
    )

    envelope = build_outcome_envelope(report, workflow)

    assert envelope.workflow_contract_sha256 == workflow_contract_sha256(workflow)
    assert envelope.required_contracts.postcondition == 3
    assert envelope.passed_contracts.postcondition == 3
    assert [item.contract_kind for item in envelope.postcondition_evidence] == [
        "explicit_predicate",
        "explicit_predicate",
        "intrinsic_input_readback",
    ]
    assert [item.contract_index for item in envelope.postcondition_evidence] == [
        0,
        1,
        0,
    ]
    assert {item.action_kind for item in envelope.postcondition_evidence} == {
        ActionKind.TYPE
    }
    assert {item.actuation_path for item in envelope.postcondition_evidence} == {"gui"}
    assert len({item.contract_sha256 for item in envelope.postcondition_evidence}) == 3


def test_outcome_envelope_preserves_api_postcondition_bypass() -> None:
    workflow = _workflow()
    report = _verified_production_report(workflow)
    report.execution_profile = "standard"
    report.execution_outcome = "VERIFIED"
    report.production_eligible = True
    report.results[0].actuation = "api"
    report.results[0].postconditions_ok = None

    envelope = build_outcome_envelope(report, workflow)

    assert envelope.required_contracts.postcondition == 0
    assert envelope.passed_contracts.postcondition == 0
    assert envelope.postcondition_evidence == []


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
    _bind_report_to_workflow(report, workflow)

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.execution_outcome == "VERIFIED"
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.external_network_calls == "unknown"


def test_verified_qualification_run_is_evidence_not_production_authority():
    workflow = Workflow(name="qualification-read", steps=[])
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-25T00:00:00Z",
        success=True,
        execution_completed=True,
        governed_authorization_id="authorization-qualification",
        governed_approval_source="qualification-campaign",
        governed_runtime_inputs_digest="f" * 64,
        governed_qualification_case_id_sha256="a" * 64,
    )
    _bind_report_to_workflow(report, workflow)

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.execution_outcome == "VERIFIED"
    assert report.production_eligible is False
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.qualification_evidence_only is True
    assert report.outcome_envelope.production_eligible is False
    restored = RunReport.model_validate(report.model_dump(mode="json"))
    assert restored.outcome_envelope is not None
    assert restored.outcome_envelope.qualification_evidence_only is True


def test_approval_source_text_cannot_spoof_qualification_only_status() -> None:
    workflow = Workflow(name="ordinary-standard-run", steps=[])
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-28T00:00:00Z",
        success=True,
        execution_completed=True,
        governed_authorization_id="ordinary-authorization",
        governed_approval_source="qualification-campaign",
        governed_runtime_inputs_digest="f" * 64,
    )
    _bind_report_to_workflow(report, workflow)

    stamp_execution_outcome(report, workflow, ExecutionProfile.STANDARD)

    assert report.qualification_evidence_only is False
    assert report.production_eligible is True


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


def test_backend_exception_can_verify_only_from_the_complete_effect_contract(tmp_path):
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

    result = report.results[0]
    assert report.halt is None
    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.resolved_by_contract is True
    assert result.postconditions_ok is True
    assert result.effect_verified is True
    assert result.failure_category is None


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
        "postcondition": 1,
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
        "postcondition",
    ]
    assert report.outcome_envelope.model_calls == 0


def test_standard_direct_resume_cursor_injection_is_refused(tmp_path):
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

    with pytest.raises(ApprovalRequired, match="authenticated resume API"):
        Replayer(
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
        approval=_approval(bundle, run_dir),
    )

    assert resumed.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert resumed.success is True
    assert resumed.model_calls == 1
    assert resumed.external_network_calls == "observed"
    assert resumed.outcome_envelope is not None
    assert resumed.outcome_envelope.model_calls == 1
    assert resumed.outcome_envelope.external_network_calls == "observed"


def test_standard_direct_legacy_checkpoint_resume_is_refused(tmp_path):
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

    with pytest.raises(ApprovalRequired, match="authenticated resume API"):
        Replayer(
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
                    expect=[
                        Postcondition(
                            kind=PostconditionKind.REGION_STABLE,
                            region=(0, 0, 10, 10),
                            phash="aa",
                        )
                    ],
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
    assert [result.program_scope[-1].row_index for result in resumed.results] == [
        0,
        1,
        2,
    ]
    assert len({result.effect_contract_hashes[0] for result in resumed.results}) == 3
    assert all(
        result.identity is not None and result.identity.status == "verified"
        for result in resumed.results
    )
    assert resumed.outcome_envelope is not None
    assert resumed.outcome_envelope.required_contracts.identity == 3
    assert resumed.outcome_envelope.passed_contracts.identity == 3
    assert resumed.outcome_envelope.required_contracts.effect == 3
    assert resumed.outcome_envelope.passed_contracts.effect == 3
    assert resumed.model_calls == 0
    assert resumed.rung_counts["template"] == 3
    assert resumed.screenshots_may_leave_box is False


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
    vision.text_results["Profile write completed"] = object()
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
    # The connector's three-state transport maps a completed-but-unverified
    # business outcome to operator attention (halt), not infrastructure failure.
    # The attached outcome envelope preserves the exact distinction.
    assert status_from_report(0, report.model_dump(mode="json")) == "halt"
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
