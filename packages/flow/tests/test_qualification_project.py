"""Durable contracts for the versioned qualification project.

These tests exercise schema round-trip, coverage refusal, case evidence, and
the existing policy/certification seam.  They do not pin CLI prose or UI copy.
"""

from __future__ import annotations

import hashlib
import io
import json
from base64 import b64encode
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image, ImageDraw

from openadapt_flow import vision as vision_module
from openadapt_flow.__main__ import main
from openadapt_flow.bundle_validation import (
    BundleIntegrityError,
    build_runtime_parameter_schema,
    compute_parameter_schema_digest,
)
from openadapt_flow.execution_profiles import (
    ExecutionProfile,
    build_outcome_envelope,
    qualified_effect_requirements,
)
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    ActionKind,
    Anchor,
    ApiBinding,
    ApiIdentityBinding,
    BundleManifest,
    BusinessDecisionOption,
    BusinessDecisionSpec,
    EffectVerificationEvidence,
    GovernedAuthorizationParameter,
    GovernedAuthorizationTemplate,
    IdentityCheck,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    RunReport,
    SafetyRefusalEvidence,
    State,
    StateKind,
    Step,
    StepResult,
    VisualResolutionEvidence,
    Workflow,
)
from openadapt_flow.judgment_cases import (
    JudgmentCaseProvenanceV1,
    JudgmentCaseV1,
    JudgmentDecisionBindingV1,
    JudgmentDisposition,
    JudgmentFactFieldV1,
    JudgmentFactSchemaBindingV1,
    JudgmentFactSchemaV1,
    JudgmentFactType,
    LocalEvidenceRefV1,
)
from openadapt_flow.policy import (
    Policy,
    effective_step_risk,
    load_policy,
    policy_contract_sha256,
)
from openadapt_flow.qualification import (
    REMOTE_SAFE_ENTITY_LABELS,
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    EvidenceRef,
    IdentityEnforcement,
    IdentityPolicy,
    IdentitySignalPolicy,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationCertification,
    QualificationError,
    QualificationOutcome,
    QualificationRefusalCode,
    QualifiedEntityLabel,
    RequalificationCondition,
    VerificationTier,
    add_case,
    add_requalification_condition,
    certify_project,
    current_certification_matches,
    entity_label_options,
    evaluate_qualification,
    init_project,
    list_entity_labels,
    project_schema,
    qualification_action_requirements,
    record_case_results,
    remove_entity_label,
    save_qualified_workflow,
    set_action_classification,
    set_business_decision,
    set_case_scope,
    set_effect_policy,
    set_entity_label,
    set_identity_policy,
    set_judgment_cases,
    set_minimum_effect_tier,
    set_trusted_fault_driver_key,
    set_trusted_runner_key,
    sign_case_result,
    workflow_contract_sha256,
)
from openadapt_flow.qualification_environment import (
    qualification_environment_binding_sha256,
)
from openadapt_flow.qualification_faults import (
    FaultMutationReceipt,
    expected_fault_detector,
    sha256_bytes,
    sign_fault_mutation_receipt,
)
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    effective_runtime_params,
    runtime_inputs_bytes,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from openadapt_flow.runtime.resolver import (
    resolve as resolve_target,
)
from openadapt_flow.runtime.resolver import (
    visual_resolution_anchor_contract_sha256,
    visual_resolution_evaluator_contract_sha256,
)

_RUNNER_PRIVATE_KEY = Ed25519PrivateKey.generate()
_RUNNER_PRIVATE_BYTES = _RUNNER_PRIVATE_KEY.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
)
_RUNNER_PUBLIC_BASE64 = b64encode(
    _RUNNER_PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
).decode("ascii")


def _workflow() -> Workflow:
    return Workflow(
        name="qualified-write",
        params={"record_id": "example", "note": "example"},
        steps=[
            Step(
                id="save",
                intent="Save the record",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/save.png",
                    region=(10, 10, 40, 20),
                    click_point=(30, 20),
                    ocr_text="Save",
                    structured_identity="record identity",
                ),
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                    )
                ],
                effects=[
                    Effect(
                        kind=EffectKind.FIELD_EQUALS,
                        match={"id": ValueExpr(param="record_id")},
                        field="note",
                        value=ValueExpr(param="note"),
                        idempotency_key=ValueExpr(param="record_id"),
                        risk="irreversible",
                    )
                ],
                risk="irreversible",
                identity_armed=True,
            )
        ],
    )


def _environment() -> EnvironmentBoundary:
    return EnvironmentBoundary(
        target_kind="citrix",
        application="Qualified application",
        application_identity="qualified-app",
        application_version="1",
        environment_observer_id="fixture-observer",
        environment_observer_contract_sha256="c" * 64,
        environment_digest="b" * 64,
        runtime_version="1.20.2",
        required_capabilities=["pixel_observation", "effect_verification"],
    )


def test_entity_labels_are_qualification_contract_and_invalidate_certification():
    workflow = _workflow()
    project = init_project(workflow, environment=_environment())
    before = project.contract_sha256()
    certification = QualificationCertification(
        project_revision=project.revision,
        project_contract_sha256=before,
        workflow_contract_sha256="a" * 64,
        environment_contract_sha256="c" * 64,
        policy_name="clinical-write",
        policy_contract_sha256="b" * 64,
        passed=True,
        report_sha256="d" * 64,
        certified_at="2026-07-01T00:00:00+00:00",
    )
    project.last_certification = certification

    set_entity_label(
        workflow,
        QualifiedEntityLabel(step_id="save", label="patient record", fallback="record"),
    )
    assert project.entity_labels["save"].label == "patient record"
    assert project.contract_sha256() != before
    assert project.last_certification is None
    assert list_entity_labels(workflow) == [project.entity_labels["save"]]

    remove_entity_label(workflow, "save")
    assert project.entity_labels == {}


def test_entity_label_is_optional_for_certification(tmp_path: Path) -> None:
    """Presentation metadata must never become a safety admission gate."""

    workflow = _workflow()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    assert workflow.qualification is not None
    assert workflow.qualification.entity_labels == {}
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)

    report = certify_project(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )

    assert report.passed
    assert workflow.qualification.entity_labels == {}


def test_entity_label_options_are_ordered_and_derive_every_public_label() -> None:
    options = entity_label_options()
    assert options == [
        {"label": "patient record", "fallback": "record"},
        {"label": "member record", "fallback": "record"},
        {"label": "insurance claim", "fallback": "item"},
        {"label": "loan application", "fallback": "item"},
        {"label": "customer account", "fallback": "record"},
        {"label": "service request", "fallback": "item"},
        {"label": "case", "fallback": "item"},
        {"label": "order", "fallback": "item"},
        {"label": "invoice", "fallback": "item"},
        {"label": "document", "fallback": "item"},
        {"label": "record", "fallback": "record"},
        {"label": "item", "fallback": "item"},
    ]
    assert REMOTE_SAFE_ENTITY_LABELS == tuple(option["label"] for option in options)
    label_schema = project_schema()["$defs"]["QualifiedEntityLabel"]["properties"][
        "label"
    ]
    assert "enum" not in label_schema
    assert label_schema["x-openadapt-reviewed-remote-options"] == list(
        REMOTE_SAFE_ENTITY_LABELS
    )


@pytest.mark.parametrize("option", entity_label_options())
def test_entity_label_accepts_each_reviewed_remote_safe_class(
    option: dict[str, str],
) -> None:
    entity = QualifiedEntityLabel(step_id="save", **option)
    assert entity.model_dump() == {"step_id": "save", **option}


def test_entity_label_refuses_a_noncanonical_fallback() -> None:
    with pytest.raises(ValueError, match="fallback"):
        QualifiedEntityLabel(step_id="save", label="insurance claim", fallback="record")


def test_entity_label_accepts_a_custom_local_class() -> None:
    entity = QualifiedEntityLabel(
        step_id="save", label="appointment request", fallback="item"
    )
    assert entity.label == "appointment request"
    assert entity.fallback == "item"


@pytest.mark.parametrize(
    "label",
    ("Patient Record", "record/class", "one two three four five"),
)
def test_entity_label_refuses_invalid_local_class_syntax(
    label: str,
) -> None:
    with pytest.raises(ValueError, match="lowercase class name"):
        QualifiedEntityLabel(step_id="save", label=label, fallback="record")


def test_api_only_qualification_uses_only_executable_targets() -> None:
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"id": ValueExpr(param="record_id")},
        field="note",
        value=ValueExpr(param="note"),
        idempotency_key=ValueExpr(param="record_id"),
        risk="irreversible",
    )
    step = Step(
        id="save",
        intent="Save the record",
        action=ActionKind.KEY,
        key="Enter",
        risk="irreversible",
        api_binding=ApiBinding(
            url_template="/records",
            body_template={
                "id": "{record_id}",
                "note": "{note}",
            },
            on_unavailable="halt",
            effects=[effect],
            identity=[
                ApiIdentityBinding(
                    key="record_id",
                    param="record_id",
                    effect_field="id",
                    request_pointers=["/body/id"],
                )
            ],
        ),
    )
    workflow = Workflow(
        name="api-only-qualified",
        params={"record_id": "example", "note": "example"},
        steps=[step],
    )
    init_project(workflow, environment=_environment())
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="save",
            classification=ActionRiskClass.IRREVERSIBLE,
            explanation="Saving changes the source-of-record state",
            operator_confirmed=True,
        ),
    )
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id="save",
            enforcement=IdentityEnforcement.SIGNAL_QUORUM,
            signals=[
                IdentitySignalPolicy(
                    key="record_id",
                    source="structured",
                    extract_pattern=r"^(?P<value>.+)$",
                )
            ],
            quorum=1,
        ),
    )
    set_effect_policy(
        workflow,
        step_id="save",
        effect_index=0,
        tier=VerificationTier.INDEPENDENT_SYSTEM,
        actuation_path="api",
    )
    runtime_digest = runtime_inputs_digest(workflow, None, None)
    for case_id in ("fault-weak-effect", "fault-missing-effect"):
        set_case_scope(
            workflow,
            case_id=case_id,
            runtime_input_sha256=runtime_digest,
            action_targets=[
                QualificationActionTarget(step_id="save", actuation_path="api")
            ],
        )
    add_case(
        workflow,
        QualificationCase(
            id="representative-api",
            kind=QualificationCaseKind.REPRESENTATIVE,
            runtime_input_sha256=runtime_digest,
            action_targets=[
                QualificationActionTarget(step_id="save", actuation_path="api")
            ],
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )

    with pytest.raises(QualificationError, match="non-executable"):
        set_case_scope(
            workflow,
            case_id="fault-weak-effect",
            runtime_input_sha256=runtime_digest,
            action_targets=[
                QualificationActionTarget(step_id="save", actuation_path="gui")
            ],
        )

    report = evaluate_qualification(workflow)
    assert report.identity_covered_action_count == 1
    assert report.effect_covered_action_count == 1
    refusal_codes = {item.code for item in report.refusals}
    assert QualificationRefusalCode.STEP_IDENTITY_UNARMED not in refusal_codes
    assert QualificationRefusalCode.EFFECT_CONTRACT_MISSING not in refusal_codes
    assert QualificationRefusalCode.CASE_TARGET_INVALID not in refusal_codes


def _configure(workflow: Workflow, *, tier: VerificationTier) -> None:
    init_project(workflow, environment=_environment())
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="save",
            classification=ActionRiskClass.IRREVERSIBLE,
            explanation="Saving changes the source-of-record state",
            operator_confirmed=True,
        ),
    )
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id="save",
            enforcement=IdentityEnforcement.CANONICAL_LADDER,
        ),
    )
    set_effect_policy(workflow, step_id="save", effect_index=0, tier=tier)
    set_trusted_runner_key(
        workflow,
        key_id="test-runner",
        public_key_base64=_RUNNER_PUBLIC_BASE64,
    )


def _qualification_visual_fixture() -> tuple[bytes, bytes]:
    """Return one exact frame and its compiled target crop."""

    frame_image = Image.new("RGB", (100, 60), "white")
    draw = ImageDraw.Draw(frame_image)
    draw.rectangle((10, 10, 49, 29), outline="black", width=2)
    draw.line((14, 14, 44, 25), fill="navy", width=2)
    draw.rectangle((36, 13, 44, 21), fill="orange")
    frame_buffer = io.BytesIO()
    frame_image.save(frame_buffer, format="PNG")
    template_buffer = io.BytesIO()
    frame_image.crop((10, 10, 50, 30)).save(template_buffer, format="PNG")
    return frame_buffer.getvalue(), template_buffer.getvalue()


def _record_passing_campaign(workflow: Workflow, evidence_root: Path) -> None:
    project = workflow.qualification
    assert project is not None
    action = workflow.steps[0]
    action_id = action.id
    case_input = runtime_inputs_bytes(workflow, None, None)
    case_input_sha256 = hashlib.sha256(case_input).hexdigest()
    project.cases = [
        case.model_copy(
            update={
                "runtime_input_sha256": case_input_sha256,
                "action_targets": [
                    QualificationActionTarget(
                        step_id=action_id,
                        actuation_path="gui",
                    )
                ],
            }
        )
        for case in project.cases
    ]
    required_actions_for_cases, _required_identity_for_cases = (
        qualification_action_requirements(workflow)
    )
    if action_id not in required_actions_for_cases:
        project.cases = []
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind=QualificationCaseKind.REPRESENTATIVE,
            input_ref="fixtures/representative-1",
            runtime_input_sha256=case_input_sha256,
            action_targets=[
                QualificationActionTarget(step_id=action_id, actuation_path="gui")
            ],
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    set_trusted_fault_driver_key(
        workflow,
        key_id="test-fault-driver",
        public_key_base64=_RUNNER_PUBLIC_BASE64,
    )
    project = workflow.qualification
    assert project is not None
    evidence_root.mkdir(parents=True, exist_ok=True)
    campaign_sha256 = sha256_bytes(b"qualification-campaign")
    observed_application_sha256 = sha256_bytes(b"qualified-app")
    observed_version_sha256 = sha256_bytes(b"1")
    observed_session_sha256 = "3" * 64
    observed_binding = qualification_environment_binding_sha256(
        target_kind="citrix",
        observer_id="fixture-observer",
        observer_contract_sha256="c" * 64,
        application_identity_sha256=observed_application_sha256,
        application_version_sha256=observed_version_sha256,
        environment_digest=project.environment.environment_digest,
        session_identity_sha256=observed_session_sha256,
    )
    qualification_policy = load_policy("clinical-write")
    required_actions, required_identity = qualification_action_requirements(workflow)
    case_params = effective_runtime_params(workflow, None)
    resolved_action_effects = [effect.resolve(case_params) for effect in action.effects]
    effect_tiers = {
        binding.effect_index: binding.tier
        for binding in project.effect_policies
        if binding.step_id == action_id and binding.actuation_path == "gui"
    }
    governed_effect_requirements = list(
        qualified_effect_requirements(workflow, ExecutionProfile.STANDARD)
    )
    results: list[QualificationCaseResult] = []
    fault_frame_bytes, fault_template_bytes = _qualification_visual_fixture()
    fault_frame_sha256 = hashlib.sha256(fault_frame_bytes).hexdigest()
    fault_template_sha256 = hashlib.sha256(fault_template_bytes).hexdigest()
    fault_frame_inventory_ref = f"private/resolution-inputs/{fault_frame_sha256}.png"
    fault_template_inventory_ref = (
        f"private/resolution-inputs/{fault_template_sha256}.png"
    )
    if workflow.manifest is None:
        workflow.manifest = BundleManifest(
            file_hashes={action.anchor.template: fault_template_sha256}
        )
    else:
        assert (
            workflow.manifest.file_hashes[action.anchor.template]
            == fault_template_sha256
        )
    reproduced = resolve_target(
        action.anchor,
        fault_frame_bytes,
        vision_module,
        None,
        action.intent,
        template_png=fault_template_bytes,
        viewport=(100, 60),
        structural=None,
    )
    assert reproduced is not None
    fault_resolution, fault_matched_region = reproduced
    fault_resolution = fault_resolution.model_copy(
        update={
            "visual_evidence": VisualResolutionEvidence(
                frame_sha256=fault_frame_sha256,
                frame_inventory_ref=fault_frame_inventory_ref,
                template_sha256=fault_template_sha256,
                template_inventory_ref=fault_template_inventory_ref,
                evaluator_contract_sha256=(
                    visual_resolution_evaluator_contract_sha256()
                ),
                anchor_contract_sha256=(
                    visual_resolution_anchor_contract_sha256(
                        action.anchor,
                        template_sha256=fault_template_sha256,
                        allow_target_ocr=True,
                    )
                ),
                matched_region=fault_matched_region,
            )
        }
    )
    (evidence_root / fault_frame_inventory_ref).parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    (evidence_root / fault_frame_inventory_ref).write_bytes(fault_frame_bytes)
    (evidence_root / fault_template_inventory_ref).write_bytes(fault_template_bytes)
    for case in project.cases:
        run_sha256 = sha256_bytes(f"run:{case.id}".encode())
        if case.kind is QualificationCaseKind.REPRESENTATIVE:
            report = RunReport(
                workflow_name=workflow.name,
                run_id_sha256=run_sha256,
                workflow_contract_sha256=workflow_contract_sha256(workflow),
                started_at="2026-07-28T00:00:00Z",
                execution_profile="standard",
                execution_outcome="VERIFIED",
                production_eligible=False,
                execution_completed=True,
                execution_target_kind="citrix",
                governed_policy_name=qualification_policy.name,
                governed_policy_contract_sha256=policy_contract_sha256(
                    qualification_policy
                ),
                governed_minimum_effect_tier=int(project.minimum_effect_tier),
                governed_qualified_effect_requirements=governed_effect_requirements,
                governed_authorization_id="qualification-representative",
                governed_runtime_inputs_digest=case_input_sha256,
                required_identity_step_ids=sorted(required_identity),
                governed_qualification_project_id=project.project_id,
                governed_qualification_project_revision=project.revision,
                governed_qualification_project_contract_sha256=(
                    project.contract_sha256()
                ),
                governed_qualification_campaign_id_sha256=campaign_sha256,
                governed_qualification_case_id_sha256=sha256_bytes(case.id.encode()),
                governed_qualification_case_input_sha256=case_input_sha256,
                governed_qualification_run_id_sha256=run_sha256,
                governed_qualification_case_kind=case.kind.value,
                governed_qualification_case_action_paths={action_id: "gui"},
                params=case_params,
                qualification_evidence_only=True,
                observed_application_sha256=observed_application_sha256,
                observed_application_version_sha256=observed_version_sha256,
                observed_session_sha256=observed_session_sha256,
                observed_environment_digest=project.environment.environment_digest,
                observed_environment_binding_sha256=observed_binding,
                qualification_environment_observer_id="fixture-observer",
                qualification_environment_observer_contract_sha256="c" * 64,
                results=[
                    StepResult(
                        step_id=action_id,
                        intent=action.intent,
                        ok=True,
                        resolution=fault_resolution,
                        delivery_attempted=True,
                        delivery_receipt=ActionDeliveryReceipt(
                            receipt_id="qualification-click",
                            operation="guarded_coordinate_click",
                            native=False,
                            delivered_at="2026-07-28T00:00:00+00:00",
                        ),
                        before_png=fault_frame_inventory_ref,
                        identity=(
                            IdentityCheck(
                                status="verified",
                                mode="structured",
                                coverage=1.0,
                                expected="record identity",
                                observed="record identity",
                            )
                            if action_id in required_identity
                            else None
                        ),
                        postconditions_ok=True,
                        actuation="guarded_coordinate",
                        starting_state_settled=True,
                        effect_verified=(
                            True if action_id in required_actions else None
                        ),
                        effect_contract_hashes=(
                            [
                                effect.contract_hash()
                                for effect in resolved_action_effects
                            ]
                            if action_id in required_actions
                            else []
                        ),
                        effect_evidence=(
                            [
                                EffectVerificationEvidence(
                                    effect_contract_hash=effect.contract_hash(),
                                    substrate="fixture-system-of-record",
                                    verification_tier=int(effect_tiers[index]),
                                    initial_verdict="confirmed",
                                    final_verdict="confirmed",
                                    observed_effect="present",
                                )
                                for index, effect in enumerate(resolved_action_effects)
                            ]
                            if action_id in required_actions
                            else []
                        ),
                    )
                ],
                success=True,
            )
            report.outcome_envelope = build_outcome_envelope(report, workflow)
            representative_bytes = report.model_dump_json().encode()
            representative_input_path = "representative-input.json"
            (evidence_root / "representative-report.json").write_bytes(
                representative_bytes
            )
            (evidence_root / representative_input_path).write_bytes(case_input)
            representative_evidence = [
                EvidenceRef(
                    kind="run_report",
                    sha256=hashlib.sha256(representative_bytes).hexdigest(),
                    relative_path="representative-report.json",
                ),
                EvidenceRef(
                    kind="case_input",
                    sha256=case_input_sha256,
                    relative_path=representative_input_path,
                ),
            ]
            results.append(
                sign_case_result(
                    QualificationCaseResult(
                        case_id=case.id,
                        project_id=project.project_id,
                        project_revision=project.revision,
                        project_contract_sha256=project.contract_sha256(),
                        workflow_contract_sha256=workflow_contract_sha256(workflow),
                        environment_contract_sha256=(
                            project.environment.contract_sha256()
                        ),
                        environment_digest=project.environment.environment_digest,
                        runtime_version=project.environment.runtime_version,
                        runner_id="test-runner",
                        runner_capabilities=[
                            "pixel_observation",
                            "effect_verification",
                        ],
                        status="passed",
                        observed_outcome=case.expected_outcome,
                        campaign_id_sha256=campaign_sha256,
                        case_input_sha256=case_input_sha256,
                        run_id_sha256=run_sha256,
                        evidence=representative_evidence,
                        attestation_key_id="test-runner",
                    ),
                    private_key=_RUNNER_PRIVATE_BYTES,
                )
            )
            continue

        gate, code = expected_fault_detector(case.kind.value)
        mutation_bytes = f"mutation:{case.kind.value}".encode()
        receipt = sign_fault_mutation_receipt(
            FaultMutationReceipt(
                project_id=project.project_id,
                project_revision=project.revision,
                project_contract_sha256=project.contract_sha256(),
                campaign_id_sha256=campaign_sha256,
                case_id_sha256=sha256_bytes(case.id.encode()),
                case_input_sha256=case_input_sha256,
                run_id_sha256=run_sha256,
                step_id_sha256=sha256_bytes(action_id.encode()),
                actuation_path="gui",
                fault_kind=case.kind.value,
                gate=gate,
                driver_id="fixture-driver",
                driver_contract_sha256="d" * 64,
                before_input_sha256="1" * 64,
                after_input_sha256="2" * 64,
                mutation_artifact_sha256=hashlib.sha256(mutation_bytes).hexdigest(),
                attestation_key_id="test-fault-driver",
            ),
            private_key=_RUNNER_PRIVATE_BYTES,
        )
        reached_target = case.kind is not QualificationCaseKind.AMBIGUITY
        reached_effect_gate = case.kind in {
            QualificationCaseKind.WEAK_EFFECT,
            QualificationCaseKind.MISSING_EFFECT,
        }
        fault_frame_path = (
            f"{case.id}.before.png"
            if reached_target and case.kind is not QualificationCaseKind.STALE_IDENTITY
            else None
        )
        effect_contract_hashes = (
            [effect.contract_hash() for effect in resolved_action_effects]
            if case.kind
            in {
                QualificationCaseKind.STALE_IDENTITY,
                QualificationCaseKind.WEAK_EFFECT,
            }
            else []
        )
        report = RunReport(
            workflow_name=workflow.name,
            run_id_sha256=run_sha256,
            workflow_contract_sha256=workflow_contract_sha256(workflow),
            started_at="2026-07-28T00:00:00Z",
            execution_profile="standard",
            execution_outcome="HALTED",
            production_eligible=False,
            execution_completed=False,
            execution_target_kind="citrix",
            governed_policy_name=qualification_policy.name,
            governed_policy_contract_sha256=policy_contract_sha256(
                qualification_policy
            ),
            governed_minimum_effect_tier=int(project.minimum_effect_tier),
            governed_qualified_effect_requirements=governed_effect_requirements,
            governed_authorization_id="qualification-fault",
            governed_runtime_inputs_digest=case_input_sha256,
            required_identity_step_ids=sorted(required_identity),
            governed_qualification_project_id=project.project_id,
            governed_qualification_project_revision=project.revision,
            governed_qualification_project_contract_sha256=(project.contract_sha256()),
            governed_qualification_campaign_id_sha256=campaign_sha256,
            governed_qualification_case_id_sha256=sha256_bytes(case.id.encode()),
            governed_qualification_case_input_sha256=case_input_sha256,
            governed_qualification_run_id_sha256=run_sha256,
            governed_qualification_case_kind=case.kind.value,
            governed_qualification_case_action_paths={action_id: "gui"},
            governed_qualification_fault_driver_id="fixture-driver",
            governed_qualification_fault_driver_contract_sha256="d" * 64,
            governed_qualification_fault_driver_key_id="test-fault-driver",
            governed_qualification_fault_step_id_sha256=sha256_bytes(
                action_id.encode()
            ),
            qualification_evidence_only=True,
            qualification_fault_mutations=[receipt],
            observed_application_sha256=observed_application_sha256,
            observed_application_version_sha256=observed_version_sha256,
            observed_session_sha256=observed_session_sha256,
            observed_environment_digest=project.environment.environment_digest,
            observed_environment_binding_sha256=observed_binding,
            qualification_environment_observer_id="fixture-observer",
            qualification_environment_observer_contract_sha256="c" * 64,
            params=case_params,
            results=[
                StepResult(
                    step_id=action_id,
                    intent=action.intent,
                    ok=False,
                    risk=action.risk,
                    risk_explanation=action.risk_explanation,
                    risk_review_required=action.risk_review_required,
                    safety_halt=True,
                    failure_category=(
                        "safety_halt"
                        if case.kind is QualificationCaseKind.AMBIGUITY
                        else "governed_refusal"
                    ),
                    delivery_attempted=False,
                    before_png=fault_frame_path,
                    resolution=(
                        fault_resolution
                        if reached_target
                        and case.kind is not QualificationCaseKind.STALE_IDENTITY
                        else None
                    ),
                    identity=(
                        IdentityCheck(
                            status=(
                                "mismatch"
                                if case.kind is QualificationCaseKind.WRONG_IDENTITY
                                else "verified"
                            ),
                            mode="structured",
                            coverage=(
                                0.0
                                if case.kind is QualificationCaseKind.WRONG_IDENTITY
                                else 1.0
                            ),
                            expected="record identity",
                            observed=(
                                "other record"
                                if case.kind is QualificationCaseKind.WRONG_IDENTITY
                                else "record identity"
                            ),
                        )
                        if reached_target
                        else None
                    ),
                    effect_verified=False if reached_effect_gate else None,
                    effect_results=(
                        [
                            (
                                "no EffectVerifier configured for a step that "
                                "declares effects (fail-safe HALT)"
                                if case.kind is QualificationCaseKind.MISSING_EFFECT
                                else "the qualification fault detector refused "
                                "before actuation"
                            )
                        ]
                        if reached_effect_gate
                        else []
                    ),
                    effect_contract_hashes=effect_contract_hashes,
                    starting_state_settled=True,
                    error="the qualification fault detector refused before actuation",
                    safety_refusal_evidence=SafetyRefusalEvidence(
                        stage=gate,
                        code=code,
                        detector_input_sha256=receipt.after_input_sha256,
                    ),
                )
            ],
        )
        report.outcome_envelope = build_outcome_envelope(report, workflow)
        report_bytes = report.model_dump_json().encode()
        receipt_bytes = receipt.artifact_bytes()
        prefix = case.id
        artifacts = {
            f"{prefix}.report.json": report_bytes,
            f"{prefix}.input.json": case_input,
            f"{prefix}.receipt.json": receipt_bytes,
            f"{prefix}.mutation.bin": mutation_bytes,
            **(
                {fault_frame_path: fault_frame_bytes}
                if fault_frame_path is not None
                else {}
            ),
        }
        for relative_path, payload in artifacts.items():
            (evidence_root / relative_path).write_bytes(payload)
        evidence = [
            EvidenceRef(
                kind="run_report",
                sha256=hashlib.sha256(report_bytes).hexdigest(),
                relative_path=f"{prefix}.report.json",
            ),
            EvidenceRef(
                kind="case_input",
                sha256=case_input_sha256,
                relative_path=f"{prefix}.input.json",
            ),
            EvidenceRef(
                kind="fault_receipt",
                sha256=receipt.receipt_sha256(),
                relative_path=f"{prefix}.receipt.json",
            ),
            EvidenceRef(
                kind="fault_mutation",
                sha256=receipt.mutation_artifact_sha256,
                relative_path=f"{prefix}.mutation.bin",
            ),
            *(
                [
                    EvidenceRef(
                        kind="other",
                        sha256=hashlib.sha256(fault_frame_bytes).hexdigest(),
                        relative_path=fault_frame_path,
                    )
                ]
                if fault_frame_path is not None
                else []
            ),
            *(
                [
                    EvidenceRef(
                        kind="other",
                        sha256=fault_frame_sha256,
                        relative_path=fault_frame_inventory_ref,
                    ),
                    EvidenceRef(
                        kind="other",
                        sha256=fault_template_sha256,
                        relative_path=fault_template_inventory_ref,
                    ),
                ]
                if reached_target
                and case.kind is not QualificationCaseKind.STALE_IDENTITY
                else []
            ),
        ]
        results.append(
            sign_case_result(
                QualificationCaseResult(
                    case_id=case.id,
                    project_id=project.project_id,
                    project_revision=project.revision,
                    project_contract_sha256=project.contract_sha256(),
                    workflow_contract_sha256=workflow_contract_sha256(workflow),
                    environment_contract_sha256=project.environment.contract_sha256(),
                    environment_digest=project.environment.environment_digest,
                    runtime_version=project.environment.runtime_version,
                    runner_id="test-runner",
                    runner_capabilities=[
                        "pixel_observation",
                        "effect_verification",
                    ],
                    status="passed",
                    observed_outcome=case.expected_outcome,
                    campaign_id_sha256=campaign_sha256,
                    case_input_sha256=case_input_sha256,
                    run_id_sha256=run_sha256,
                    evidence=evidence,
                    attestation_key_id="test-runner",
                ),
                private_key=_RUNNER_PRIVATE_BYTES,
            )
        )
    record_case_results(workflow, results, evidence_root=evidence_root)


def _replace_representative_report(
    workflow: Workflow,
    evidence_root: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """Replace and re-sign the retained representative report after mutation."""

    project = workflow.qualification
    assert project is not None
    case = next(item for item in project.cases if item.id == "representative-1")
    result = case.results[-1]
    path = evidence_root / "representative-report.json"
    payload = json.loads(path.read_text())
    mutate(payload)
    changed_bytes = json.dumps(payload, separators=(",", ":")).encode()
    path.write_bytes(changed_bytes)
    changed_digest = hashlib.sha256(changed_bytes).hexdigest()
    changed_refs = [
        ref.model_copy(update={"sha256": changed_digest})
        if ref.kind == "run_report"
        else ref
        for ref in result.evidence
    ]
    case.results[-1] = sign_case_result(
        result.model_copy(
            update={"evidence": changed_refs, "attestation_signature": ""}
        ),
        private_key=_RUNNER_PRIVATE_BYTES,
    )


def _replace_fault_report(
    workflow: Workflow,
    evidence_root: Path,
    case_id: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """Replace and re-sign one retained fault report after mutation."""

    project = workflow.qualification
    assert project is not None
    case = next(item for item in project.cases if item.id == case_id)
    result = case.results[-1]
    path = evidence_root / f"{case_id}.report.json"
    payload = json.loads(path.read_text())
    mutate(payload)
    changed_bytes = json.dumps(payload, separators=(",", ":")).encode()
    path.write_bytes(changed_bytes)
    changed_digest = hashlib.sha256(changed_bytes).hexdigest()
    changed_refs = [
        ref.model_copy(update={"sha256": changed_digest})
        if ref.kind == "run_report"
        else ref
        for ref in result.evidence
    ]
    case.results[-1] = sign_case_result(
        result.model_copy(
            update={"evidence": changed_refs, "attestation_signature": ""}
        ),
        private_key=_RUNNER_PRIVATE_BYTES,
    )


def test_fault_report_cannot_claim_a_success_terminal(tmp_path: Path) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    _replace_fault_report(
        workflow,
        evidence_root,
        "fault-ambiguity",
        lambda payload: payload.update({"terminal_outcome": "success"}),
    )

    report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )

    assert any(
        refusal.case_id == "fault-ambiguity"
        and refusal.code is QualificationRefusalCode.CASE_ATTESTATION_INVALID
        for refusal in report.refusals
    )


def test_identity_normalization_is_explicit_and_quorum_is_bounded() -> None:
    with pytest.raises(ValueError, match="requires at least one"):
        IdentitySignalPolicy(
            key="subject_name",
            source="structured",
            extract_pattern=r"^(?P<value>.+?) account ",
            match="normalized",
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        IdentityPolicy(
            step_id="save",
            signals=[
                IdentitySignalPolicy(
                    key="record_id",
                    source="structured",
                    extract_pattern=r"record (?P<value>identity)",
                    match="exact",
                )
            ],
            quorum=2,
        )


def test_representative_case_must_expect_verified() -> None:
    with pytest.raises(ValueError, match="must expect VERIFIED"):
        QualificationCase(
            id="bad-representative",
            kind="representative",
            expected_outcome="completed_unverified",
        )


def test_optional_representative_does_not_satisfy_production_campaign() -> None:
    workflow = Workflow(
        name="read-only",
        steps=[Step(id="wait", intent="Wait", action=ActionKind.WAIT)],
    )
    init_project(workflow, environment=_environment())
    add_case(
        workflow,
        QualificationCase(
            id="optional-representative",
            kind="representative",
            expected_outcome="verified",
            required=False,
        ),
    )
    report = evaluate_qualification(workflow)
    assert QualificationRefusalCode.REPRESENTATIVE_CASE_MISSING in {
        refusal.code for refusal in report.refusals
    }


def test_legacy_workflow_serialization_omits_empty_qualification() -> None:
    payload = Workflow(name="legacy").model_dump(mode="json")
    assert "qualification" not in payload


def test_qualification_uses_current_risk_inference_for_old_bundle_fields() -> None:
    workflow = Workflow(
        name="current-risk-inference",
        steps=[
            Step(
                id="external",
                intent="invoke configured operation",
                action=ActionKind.WAIT,
                api_binding=ApiBinding(
                    kind="mcp",
                    method="invoke",
                    url_template="configured.operation",
                ),
            ),
            Step(
                id="shortcut",
                intent="press F2",
                action=ActionKind.KEY,
                key="F2",
            ),
            Step(
                id="retained-review",
                intent="open the next view",
                action=ActionKind.CLICK,
                risk_review_required=True,
                risk_explanation="retained application-specific review reason",
            ),
        ],
    )

    project = init_project(workflow, environment=_environment())

    assert project.action_classifications["external"].classification is (
        ActionRiskClass.IRREVERSIBLE
    )
    assert project.action_classifications["shortcut"].classification is (
        ActionRiskClass.UNKNOWN
    )
    retained = project.action_classifications["retained-review"]
    assert retained.classification is ActionRiskClass.UNKNOWN
    assert retained.explanation == "retained application-specific review reason"


def test_consequential_coverage_and_tier_four_fail_closed() -> None:
    workflow = _workflow()
    init_project(workflow, environment=_environment(), minimum_effect_tier=4)
    report = evaluate_qualification(workflow)
    codes = {refusal.code for refusal in report.refusals}
    assert QualificationRefusalCode.ACTION_CLASSIFICATION_UNCONFIRMED in codes

    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="save",
            classification="irreversible",
            explanation="Saving changes source-of-record state",
            operator_confirmed=True,
        ),
    )
    report = evaluate_qualification(workflow)
    codes = {refusal.code for refusal in report.refusals}
    assert QualificationRefusalCode.IDENTITY_POLICY_MISSING in codes
    assert QualificationRefusalCode.EFFECT_POLICY_MISSING in codes
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id="save",
            enforcement="canonical_ladder",
        ),
    )
    set_effect_policy(
        workflow,
        step_id="save",
        effect_index=0,
        tier=VerificationTier.IMMEDIATE_SCREEN,
    )
    report = evaluate_qualification(workflow)
    assert QualificationRefusalCode.HIGH_RISK_SCREEN_ONLY in {
        refusal.code for refusal in report.refusals
    }


def test_irreversible_action_cannot_be_down_classified() -> None:
    workflow = _workflow()
    init_project(workflow, environment=_environment())
    with pytest.raises(ValueError, match="cannot be down-classified"):
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id="save",
                classification="read_only",
                explanation="Unsafe attempted override",
                operator_confirmed=True,
            ),
        )


def test_operator_can_override_inferred_risk_without_declared_effect() -> None:
    workflow = Workflow(
        name="reviewed-navigation",
        steps=[
            Step(
                id="continue",
                intent="Continue to the review screen",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/continue.png",
                    region=(10, 10, 40, 20),
                    click_point=(30, 20),
                    ocr_text="Continue",
                ),
                risk="irreversible",
                risk_explanation="control label contains a consequential-write verb",
                risk_review_required=True,
            )
        ],
    )
    init_project(workflow, environment=_environment())
    before_contract = workflow_contract_sha256(workflow)

    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="continue",
            classification="read_only",
            explanation="This control only opens the review screen",
            operator_confirmed=True,
        ),
    )

    step = workflow.steps[0]
    assert step.risk == "reversible"
    assert step.risk_review_required is False
    assert step.risk_explanation == "operator-qualified override: reversible"
    assert workflow_contract_sha256(workflow) != before_contract


def test_api_effect_cannot_hide_behind_weaker_step_effect() -> None:
    workflow = _workflow()
    workflow.steps[0].effects[0].risk = "reversible"
    workflow.steps[0].api_binding = ApiBinding(
        url_template="/api/records",
        effects=[
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"id": ValueExpr(param="record_id")},
                risk="irreversible",
            )
        ],
    )
    init_project(workflow, environment=_environment())

    with pytest.raises(ValueError, match="cannot be down-classified"):
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id="save",
                classification="state_changing",
                explanation="Attempt to ignore the API write contract",
                operator_confirmed=True,
            ),
        )


def test_binding_only_effect_cannot_cover_gui_fallback() -> None:
    workflow = _workflow()
    effect = workflow.steps[0].effects.pop()
    workflow.steps[0].api_binding = ApiBinding(
        url_template="/api/records",
        effects=[effect],
    )
    init_project(workflow, environment=_environment())

    report = evaluate_qualification(workflow)

    assert QualificationRefusalCode.EFFECT_CONTRACT_MISSING in {
        refusal.code for refusal in report.refusals
    }


def test_deserialized_risk_down_classification_cannot_bypass_coverage() -> None:
    workflow = _workflow()
    init_project(workflow, environment=_environment(), minimum_effect_tier=4)
    project = workflow.qualification
    assert project is not None
    project.action_classifications["save"] = ActionRiskClassification(
        step_id="save",
        classification="state_changing",
        explanation="Tampered weaker classification",
        operator_confirmed=True,
    )
    report = evaluate_qualification(workflow)
    codes = {refusal.code for refusal in report.refusals}
    assert QualificationRefusalCode.ACTION_CLASSIFICATION_CONFLICT in codes
    assert QualificationRefusalCode.STEP_IDENTITY_UNARMED not in codes
    assert QualificationRefusalCode.IDENTITY_POLICY_MISSING in codes
    assert report.consequential_action_count == 1


def test_effect_bearing_action_cannot_be_deserialized_as_read_only() -> None:
    workflow = _workflow()
    workflow.steps[0].risk = "reversible"
    workflow.steps[0].effects[0].risk = "reversible"
    init_project(workflow, environment=_environment(), minimum_effect_tier=4)
    project = workflow.qualification
    assert project is not None
    project.action_classifications["save"] = ActionRiskClassification(
        step_id="save",
        classification="read_only",
        explanation="Tampered weaker classification",
        operator_confirmed=True,
    )
    report = evaluate_qualification(workflow)
    assert QualificationRefusalCode.ACTION_CLASSIFICATION_CONFLICT in {
        refusal.code for refusal in report.refusals
    }
    assert report.state_changing_action_count == 1
    assert report.effect_required_action_count == 1


def test_effect_policy_is_bound_to_exact_contract_hash() -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    workflow.steps[0].effects[0].field = "different_field"
    report = evaluate_qualification(workflow)
    assert QualificationRefusalCode.EFFECT_CONTRACT_CHANGED in {
        refusal.code for refusal in report.refusals
    }


def test_signal_quorum_is_executable_qualification_identity_coverage() -> None:
    workflow = _workflow()
    init_project(workflow, environment=_environment())
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="save",
            classification="irreversible",
            explanation="Saving changes source-of-record state",
            operator_confirmed=True,
        ),
    )
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id="save",
            enforcement="signal_quorum",
            signals=[
                IdentitySignalPolicy(
                    key="record_id",
                    source="structured",
                    extract_pattern=r"record (?P<value>identity)",
                    match="exact",
                )
            ],
            quorum=1,
        ),
    )
    report = evaluate_qualification(workflow)
    assert QualificationRefusalCode.IDENTITY_POLICY_UNENFORCED not in {
        refusal.code for refusal in report.refusals
    }
    assert report.identity_covered_action_count == 1


def test_state_changing_requires_identity_without_becoming_consequential() -> None:
    workflow = Workflow(
        name="reversible-write",
        params={"record_id": "1"},
        steps=[
            Step(
                id="draft",
                intent="Update a reversible draft",
                action=ActionKind.TYPE,
                text="draft",
                anchor=Anchor(
                    template="templates/draft.png",
                    region=(0, 0, 10, 10),
                    click_point=(5, 5),
                    context_text="Draft record",
                ),
                effects=[
                    Effect(
                        kind=EffectKind.FIELD_EQUALS,
                        match={"id": ValueExpr(param="record_id")},
                        field="draft",
                        value=ValueExpr(literal="draft"),
                        risk="reversible",
                    )
                ],
                risk="reversible",
            )
        ],
    )
    init_project(
        workflow,
        environment=_environment(),
        minimum_effect_tier=VerificationTier.IMMEDIATE_SCREEN,
    )
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="draft",
            classification="state_changing",
            explanation="Updates a reversible draft field",
            operator_confirmed=True,
        ),
    )
    set_effect_policy(
        workflow,
        step_id="draft",
        effect_index=0,
        tier=VerificationTier.IMMEDIATE_SCREEN,
    )
    refused = evaluate_qualification(workflow)
    refused_codes = {refusal.code for refusal in refused.refusals}
    assert QualificationRefusalCode.IDENTITY_POLICY_MISSING in refused_codes

    workflow.steps[0].identity_armed = True
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id="draft",
            enforcement=IdentityEnforcement.CANONICAL_LADDER,
        ),
    )
    report = evaluate_qualification(workflow)
    codes = {refusal.code for refusal in report.refusals}
    assert QualificationRefusalCode.IDENTITY_POLICY_MISSING not in codes
    assert QualificationRefusalCode.STEP_IDENTITY_UNARMED not in codes
    assert QualificationRefusalCode.HIGH_RISK_SCREEN_ONLY not in codes
    assert report.state_changing_action_count == 1
    assert report.consequential_action_count == 0
    assert report.effect_required_action_count == 1
    assert report.effect_covered_action_count == 1


def test_workflow_change_invalidates_signed_case_results(tmp_path: Path) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    workflow.steps[0].intent = "Changed after the campaign"
    report = evaluate_qualification(workflow, evidence_root=evidence_root)
    assert QualificationRefusalCode.CASE_WORKFLOW_CHANGED in {
        refusal.code for refusal in report.refusals
    }


def test_environment_change_invalidates_signed_case_results(tmp_path: Path) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    project.environment.environment_digest = "d" * 64
    report = evaluate_qualification(workflow, evidence_root=evidence_root)
    assert QualificationRefusalCode.CASE_ENVIRONMENT_CHANGED in {
        refusal.code for refusal in report.refusals
    }


def test_project_contract_change_invalidates_signed_case_results(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    project.minimum_effect_tier = VerificationTier.IMMEDIATE_SCREEN
    report = evaluate_qualification(workflow, evidence_root=evidence_root)
    assert QualificationRefusalCode.CASE_ATTESTATION_INVALID in {
        refusal.code for refusal in report.refusals
    }


@pytest.mark.parametrize("mutation", ["tampered", "missing"])
def test_tampered_or_missing_evidence_cannot_certify(
    tmp_path: Path,
    mutation: str,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    if mutation == "tampered":
        (evidence_root / "representative-report.json").write_text("tampered")
    else:
        (evidence_root / "representative-report.json").unlink()
    report = certify_project(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )
    assert not report.passed
    assert QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED in {
        refusal.code for refusal in report.refusals
    }
    assert workflow.manifest is not None
    assert not workflow.manifest.provenance.certified


def test_fabricated_attestation_cannot_be_recorded(tmp_path: Path) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    project = workflow.qualification
    assert project is not None
    case = project.cases[0]
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    payload = b"evidence"
    (evidence_root / "report.json").write_bytes(payload)
    result = QualificationCaseResult(
        case_id=case.id,
        project_id=project.project_id,
        project_revision=project.revision,
        project_contract_sha256=project.contract_sha256(),
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        environment_contract_sha256=project.environment.contract_sha256(),
        environment_digest=project.environment.environment_digest,
        runtime_version=project.environment.runtime_version,
        runner_id="test-runner",
        runner_capabilities=project.environment.required_capabilities,
        status="passed",
        observed_outcome=case.expected_outcome,
        evidence=[
            EvidenceRef(
                kind="run_report",
                sha256=hashlib.sha256(payload).hexdigest(),
                relative_path="report.json",
            )
        ],
        attestation_key_id="test-runner",
        attestation_signature=b64encode(b"fabricated").decode(),
    )
    with pytest.raises(ValueError, match="case_attestation_invalid"):
        record_case_results(workflow, [result], evidence_root=evidence_root)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("signed_detector_mismatch", QualificationRefusalCode.CASE_NOT_PASSED),
        (
            "signed_mutation_artifact_swap",
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
        ),
    ],
)
def test_signed_passed_status_cannot_bypass_fault_artifact_verification(
    tmp_path: Path,
    mutation: str,
    expected_code: QualificationRefusalCode,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    case = next(item for item in project.cases if item.id == "fault-ambiguity")
    result = case.results[-1]

    if mutation == "signed_detector_mismatch":
        path = evidence_root / "fault-ambiguity.report.json"
        payload = json.loads(path.read_text())
        payload["results"][0]["safety_refusal_evidence"]["detector_input_sha256"] = (
            "9" * 64
        )
        changed_bytes = json.dumps(payload, separators=(",", ":")).encode()
        path.write_bytes(changed_bytes)
        changed_kind = "run_report"
    else:
        path = evidence_root / "fault-ambiguity.mutation.bin"
        changed_bytes = b"different signed-run mutation artifact"
        path.write_bytes(changed_bytes)
        changed_kind = "fault_mutation"

    changed_digest = hashlib.sha256(changed_bytes).hexdigest()
    changed_refs = [
        ref.model_copy(update={"sha256": changed_digest})
        if ref.kind == changed_kind
        else ref
        for ref in result.evidence
    ]
    case.results[-1] = sign_case_result(
        result.model_copy(
            update={"evidence": changed_refs, "attestation_signature": ""}
        ),
        private_key=_RUNNER_PRIVATE_BYTES,
    )

    report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )
    assert not report.passed
    assert expected_code in {refusal.code for refusal in report.refusals}


def test_signed_passed_status_cannot_disagree_with_representative_run(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    case = next(item for item in project.cases if item.id == "representative-1")
    result = case.results[-1]

    path = evidence_root / "representative-report.json"
    payload = json.loads(path.read_text())
    payload["execution_outcome"] = "HALTED"
    payload["execution_completed"] = False
    payload["success"] = False
    changed_bytes = json.dumps(payload, separators=(",", ":")).encode()
    path.write_bytes(changed_bytes)
    changed_digest = hashlib.sha256(changed_bytes).hexdigest()
    changed_refs = [
        ref.model_copy(update={"sha256": changed_digest})
        if ref.kind == "run_report"
        else ref
        for ref in result.evidence
    ]
    case.results[-1] = sign_case_result(
        result.model_copy(
            update={"evidence": changed_refs, "attestation_signature": ""}
        ),
        private_key=_RUNNER_PRIVATE_BYTES,
    )

    report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )
    assert not report.passed
    assert QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED in {
        refusal.code for refusal in report.refusals
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "empty_results",
        "run_id_swap",
        "workflow_contract_swap",
        "delivery_missing",
        "identity_missing",
        "identity_policy_mode_swap",
        "effect_evidence_missing",
        "outcome_envelope_missing",
        "outcome_envelope_weakened",
        "actuation_path_swap",
        "authorization_path_map_swap",
    ],
)
def test_signed_representative_claim_cannot_replace_exact_step_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    case = next(item for item in project.cases if item.id == "representative-1")
    result = case.results[-1]
    path = evidence_root / "representative-report.json"
    payload = json.loads(path.read_text())
    if mutation == "empty_results":
        payload["results"] = []
    elif mutation == "run_id_swap":
        payload["run_id_sha256"] = "f" * 64
    elif mutation == "workflow_contract_swap":
        payload["workflow_contract_sha256"] = "f" * 64
    elif mutation == "delivery_missing":
        payload["results"][0]["delivery_attempted"] = False
    elif mutation == "identity_missing":
        payload["results"][0]["identity"] = None
    elif mutation == "identity_policy_mode_swap":
        payload["results"][0]["identity"] = {
            "status": "verified",
            "mode": "signal_quorum",
            "coverage": 1.0,
            "expected": "",
            "observed": "",
            "param": None,
            "signal_evidence": [
                {
                    "signal": "record_id",
                    "source": "structured",
                    "verdict": "verified",
                    "evidence_class": "application_structured_text",
                    "match": "exact",
                }
            ],
            "quorum_required": 1,
            "quorum_verified": 1,
        }
    elif mutation == "actuation_path_swap":
        payload["results"][0]["actuation"] = "api"
    elif mutation == "authorization_path_map_swap":
        payload["governed_qualification_case_action_paths"] = {"save": "api"}
    elif mutation == "outcome_envelope_missing":
        payload["outcome_envelope"] = None
    elif mutation == "outcome_envelope_weakened":
        payload["outcome_envelope"]["required_contracts"] = {
            "authorization": 1,
            "identity": 0,
            "postcondition": 0,
            "effect": 0,
        }
        payload["outcome_envelope"]["passed_contracts"] = {
            "authorization": 1,
            "identity": 0,
            "postcondition": 0,
            "effect": 0,
        }
        payload["outcome_envelope"]["postcondition_evidence"] = []
        payload["outcome_envelope"]["evidence_classes"] = ["authorization"]
    else:
        payload["results"][0]["effect_evidence"] = []
    changed_bytes = json.dumps(payload, separators=(",", ":")).encode()
    path.write_bytes(changed_bytes)
    changed_digest = hashlib.sha256(changed_bytes).hexdigest()
    changed_refs = [
        ref.model_copy(update={"sha256": changed_digest})
        if ref.kind == "run_report"
        else ref
        for ref in result.evidence
    ]
    case.results[-1] = sign_case_result(
        result.model_copy(
            update={"evidence": changed_refs, "attestation_signature": ""}
        ),
        private_key=_RUNNER_PRIVATE_BYTES,
    )

    report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )

    assert not report.passed
    expected_code = QualificationRefusalCode.CASE_ATTESTATION_INVALID
    assert expected_code in {refusal.code for refusal in report.refusals}


@pytest.mark.parametrize(
    "actuation",
    [None, "human_attended", "human_attended_skip", "future_driver"],
)
def test_representative_case_rejects_non_automated_gui_actuation(
    tmp_path: Path,
    actuation: str | None,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    _replace_representative_report(
        workflow,
        evidence_root,
        lambda payload: payload["results"][0].__setitem__("actuation", actuation),
    )

    report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )

    assert not report.passed
    assert {
        QualificationRefusalCode.CASE_ATTESTATION_INVALID,
        QualificationRefusalCode.CASE_NOT_PASSED,
    }.intersection(refusal.code for refusal in report.refusals)


def test_duplicate_effect_hashes_require_one_evidence_tier_per_policy_index(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    workflow.steps[0].effects.append(workflow.steps[0].effects[0].model_copy(deep=True))
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    set_effect_policy(
        workflow,
        step_id="save",
        effect_index=1,
        tier=VerificationTier.PERSISTED_STATE_REACQUISITION,
    )
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)

    # Both effect instances have the same resolved contract hash. A Tier 3
    # evidence record cannot be reused as the Tier 1 proof required by index 0.
    _replace_representative_report(
        workflow,
        evidence_root,
        lambda payload: payload["results"][0]["effect_evidence"][0].__setitem__(
            "verification_tier", 3
        ),
    )

    report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )

    assert not report.passed
    assert QualificationRefusalCode.CASE_ATTESTATION_INVALID in {
        refusal.code for refusal in report.refusals
    }


def test_case_scope_setter_versions_and_invalidates_certification() -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    project = workflow.qualification
    assert project is not None
    project.last_certification = QualificationCertification(
        project_revision=project.revision,
        project_contract_sha256=project.contract_sha256(),
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        environment_contract_sha256=project.environment.contract_sha256(),
        policy_name="clinical-write",
        policy_contract_sha256="b" * 64,
        passed=True,
        report_sha256="c" * 64,
        case_evidence_contract_sha256="a" * 64,
        certified_at="2026-07-28T00:00:00Z",
    )
    revision = project.revision
    input_sha256 = runtime_inputs_digest(workflow, None, None)

    set_case_scope(
        workflow,
        case_id="fault-ambiguity",
        runtime_input_sha256=input_sha256,
        action_targets=[
            QualificationActionTarget(step_id="save", actuation_path="gui")
        ],
    )

    assert project.revision == revision + 1
    assert project.last_certification is None
    case = next(item for item in project.cases if item.id == "fault-ambiguity")
    assert case.runtime_input_sha256 == input_sha256
    assert case.action_targets == [
        QualificationActionTarget(step_id="save", actuation_path="gui")
    ]
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind="representative",
            expected_outcome="verified",
        ),
    )
    with pytest.raises(
        ValueError, match="representative cases require an action scope"
    ):
        set_case_scope(
            workflow,
            case_id="representative-1",
            runtime_input_sha256=input_sha256,
            action_targets=[],
        )


def test_required_cases_cannot_hide_a_second_qualified_write() -> None:
    workflow = _workflow()
    second = workflow.steps[0].model_copy(deep=True)
    second.id = "send"
    second.intent = "Send the second qualified write"
    workflow.steps.append(second)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="send",
            classification="irreversible",
            explanation="The second action changes source-of-record state",
            operator_confirmed=True,
        ),
    )
    set_identity_policy(
        workflow,
        IdentityPolicy(step_id="send", enforcement="canonical_ladder"),
    )
    set_effect_policy(
        workflow,
        step_id="send",
        effect_index=0,
        tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    project = workflow.qualification
    assert project is not None
    for case in list(project.cases):
        set_case_scope(
            workflow,
            case_id=case.id,
            runtime_input_sha256=input_sha256,
            action_targets=[
                QualificationActionTarget(step_id="save", actuation_path="gui")
            ],
        )
    add_case(
        workflow,
        QualificationCase(
            id="representative-save",
            kind="representative",
            runtime_input_sha256=input_sha256,
            action_targets=[
                QualificationActionTarget(step_id="save", actuation_path="gui")
            ],
            expected_outcome="verified",
        ),
    )
    add_case(
        workflow,
        QualificationCase(
            id="optional-representative-send",
            kind="representative",
            runtime_input_sha256=input_sha256,
            action_targets=[
                QualificationActionTarget(step_id="send", actuation_path="gui")
            ],
            expected_outcome="verified",
            required=False,
        ),
    )

    report = evaluate_qualification(workflow)

    assert any(
        refusal.code is QualificationRefusalCode.REPRESENTATIVE_ACTION_UNCOVERED
        and refusal.step_id == "send"
        for refusal in report.refusals
    )
    assert any(
        refusal.code is QualificationRefusalCode.FAULT_ACTION_UNCOVERED
        and refusal.step_id == "send"
        for refusal in report.refusals
    )


def test_one_representative_case_cannot_claim_both_paths_for_one_step() -> None:
    with pytest.raises(ValueError, match="only one path per step"):
        QualificationCase(
            id="representative-both-paths",
            kind="representative",
            action_targets=[
                QualificationActionTarget(step_id="save", actuation_path="gui"),
                QualificationActionTarget(step_id="save", actuation_path="api"),
            ],
            expected_outcome="verified",
        )


def test_api_effect_path_requires_its_own_representative_and_fault_cases() -> None:
    workflow = _workflow()
    api_effect = workflow.steps[0].effects[0].model_copy(deep=True)
    workflow.steps[0].api_binding = ApiBinding(
        method="POST",
        url_template="/records/{record_id}",
        effects=[api_effect],
    )
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    set_effect_policy(
        workflow,
        step_id="save",
        effect_index=0,
        tier=VerificationTier.INDEPENDENT_SYSTEM,
        actuation_path="api",
    )
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    project = workflow.qualification
    assert project is not None
    for case in project.cases:
        set_case_scope(
            workflow,
            case_id=case.id,
            runtime_input_sha256=input_sha256,
            action_targets=[
                QualificationActionTarget(step_id="save", actuation_path="gui")
            ],
        )
    add_case(
        workflow,
        QualificationCase(
            id="representative-gui",
            kind="representative",
            runtime_input_sha256=input_sha256,
            action_targets=[
                QualificationActionTarget(step_id="save", actuation_path="gui")
            ],
            expected_outcome="verified",
        ),
    )

    report = evaluate_qualification(workflow)

    api_refusals = [
        refusal
        for refusal in report.refusals
        if refusal.details.get("actuation_path") == "api"
    ]
    assert any(
        refusal.code is QualificationRefusalCode.REPRESENTATIVE_ACTION_UNCOVERED
        for refusal in api_refusals
    )
    assert {
        refusal.details.get("kind")
        for refusal in api_refusals
        if refusal.code is QualificationRefusalCode.FAULT_ACTION_UNCOVERED
    } == {"weak_effect", "missing_effect"}

    set_case_scope(
        workflow,
        case_id="fault-ambiguity",
        runtime_input_sha256=input_sha256,
        action_targets=[
            QualificationActionTarget(step_id="save", actuation_path="api")
        ],
    )
    report = evaluate_qualification(workflow)
    assert any(
        refusal.code is QualificationRefusalCode.CASE_TARGET_INVALID
        and refusal.case_id == "fault-ambiguity"
        for refusal in report.refusals
    )


def test_qualification_authorization_cannot_omit_project_identity_scope(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind="representative",
            runtime_input_sha256=input_sha256,
            action_targets=[
                QualificationActionTarget(step_id="save", actuation_path="gui")
            ],
            expected_outcome="verified",
        ),
    )
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    project = workflow.qualification
    assert project is not None and workflow.manifest is not None
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=input_sha256,
        admitted_policy_name="clinical-write",
        admitted_policy_contract_sha256="d" * 64,
        execution_profile="standard",
        minimum_effect_tier=3,
        required_identity_step_ids=(),
        approval_source="qualification-campaign",
        qualification_project_id=project.project_id,
        qualification_project_revision=project.revision,
        qualification_project_contract_sha256=project.contract_sha256(),
        qualification_case_id="representative-1",
        qualification_campaign_id_sha256="e" * 64,
        qualification_case_input_sha256=input_sha256,
        qualification_run_id_sha256="f" * 64,
        qualification_case_kind="representative",
        qualification_case_action_paths={"save": "gui"},
        qualification_campaign_permit_id="00000000-0000-4000-8000-000000000001",
        qualification_campaign_permit_sha256="1" * 64,
        qualification_campaign_signer_registry_sha256="2" * 64,
        qualification_campaign_signer_registry_revision=1,
        qualification_campaign_signer_registry_expires_at=("2099-01-01T00:00:00Z"),
        qualification_campaign_authority_sha256="3" * 64,
    )

    assert authorization.validate_workflow(workflow) == (
        "qualification-run authorization omits required identity steps: save"
    )


def test_fault_case_cannot_target_a_read_only_decoy() -> None:
    workflow = _workflow()
    workflow.steps.append(
        Step(id="inspect", intent="Inspect the result", action=ActionKind.WAIT)
    )
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    set_case_scope(
        workflow,
        case_id="fault-ambiguity",
        runtime_input_sha256=input_sha256,
        action_targets=[
            QualificationActionTarget(step_id="inspect", actuation_path="gui")
        ],
    )

    report = evaluate_qualification(workflow)

    assert any(
        refusal.code is QualificationRefusalCode.CASE_TARGET_INVALID
        and refusal.case_id == "fault-ambiguity"
        for refusal in report.refusals
    )


def test_signed_fault_receipt_cannot_swap_its_case_target(tmp_path: Path) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    case = next(item for item in project.cases if item.id == "fault-ambiguity")
    result = case.results[-1]
    receipt_path = evidence_root / "fault-ambiguity.receipt.json"
    report_path = evidence_root / "fault-ambiguity.report.json"
    receipt = FaultMutationReceipt.model_validate_json(receipt_path.read_bytes())
    swapped_step_sha256 = sha256_bytes(b"read-only-decoy")
    swapped_receipt = sign_fault_mutation_receipt(
        receipt.model_copy(
            update={
                "step_id_sha256": swapped_step_sha256,
                "attestation_signature": "",
            }
        ),
        private_key=_RUNNER_PRIVATE_BYTES,
    )
    receipt_bytes = swapped_receipt.artifact_bytes()
    receipt_path.write_bytes(receipt_bytes)
    retained_report = RunReport.model_validate_json(report_path.read_bytes())
    changed_report = retained_report.model_copy(
        update={
            "governed_qualification_fault_step_id_sha256": swapped_step_sha256,
            "qualification_fault_mutations": [swapped_receipt],
        }
    )
    report_bytes = changed_report.model_dump_json().encode()
    report_path.write_bytes(report_bytes)
    changed_digests = {
        "run_report": hashlib.sha256(report_bytes).hexdigest(),
        "fault_receipt": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    changed_refs = [
        ref.model_copy(update={"sha256": changed_digests[ref.kind]})
        if ref.kind in changed_digests
        else ref
        for ref in result.evidence
    ]
    case.results[-1] = sign_case_result(
        result.model_copy(
            update={"evidence": changed_refs, "attestation_signature": ""}
        ),
        private_key=_RUNNER_PRIVATE_BYTES,
    )

    qualification_report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )

    assert QualificationRefusalCode.CASE_ATTESTATION_INVALID in {
        refusal.code for refusal in qualification_report.refusals
    }


def test_signed_case_result_cannot_move_to_another_case(tmp_path: Path) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    representative = next(
        case
        for case in project.cases
        if case.kind is QualificationCaseKind.REPRESENTATIVE
    )
    target = next(case for case in project.cases if case.id == "fault-ambiguity")
    target.results = [representative.results[-1]]

    report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )

    assert any(
        refusal.case_id == target.id
        and refusal.code is QualificationRefusalCode.CASE_ATTESTATION_INVALID
        for refusal in report.refusals
    )


def test_signed_case_result_cannot_change_the_expected_outcome(tmp_path: Path) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    case = next(item for item in project.cases if item.id == "fault-ambiguity")
    case.results[-1] = sign_case_result(
        case.results[-1].model_copy(
            update={
                "observed_outcome": QualificationOutcome.VERIFIED,
                "attestation_signature": "",
            }
        ),
        private_key=_RUNNER_PRIVATE_BYTES,
    )

    report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )

    assert any(
        refusal.case_id == case.id
        and refusal.code is QualificationRefusalCode.CASE_ATTESTATION_INVALID
        for refusal in report.refusals
    )


@pytest.mark.parametrize(
    ("receipt_update", "report_update", "expected_code"),
    [
        (
            {"actuation_path": "api"},
            {"governed_qualification_case_action_paths": {"save": "api"}},
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
        ),
        (
            {"fault_kind": "missing_effect"},
            {"governed_qualification_case_kind": "missing_effect"},
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
        ),
    ],
)
def test_signed_fault_receipt_cannot_swap_its_path_or_kind(
    tmp_path: Path,
    receipt_update: dict[str, object],
    report_update: dict[str, object],
    expected_code: QualificationRefusalCode,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    case = next(item for item in project.cases if item.id == "fault-ambiguity")
    result = case.results[-1]
    receipt_path = evidence_root / "fault-ambiguity.receipt.json"
    report_path = evidence_root / "fault-ambiguity.report.json"
    receipt = FaultMutationReceipt.model_validate_json(receipt_path.read_bytes())
    swapped_receipt = sign_fault_mutation_receipt(
        receipt.model_copy(update={**receipt_update, "attestation_signature": ""}),
        private_key=_RUNNER_PRIVATE_BYTES,
    )
    receipt_bytes = swapped_receipt.artifact_bytes()
    receipt_path.write_bytes(receipt_bytes)
    retained_report = RunReport.model_validate_json(report_path.read_bytes())
    changed_report = retained_report.model_copy(
        update={
            **report_update,
            "qualification_fault_mutations": [swapped_receipt],
        }
    )
    report_bytes = changed_report.model_dump_json().encode()
    report_path.write_bytes(report_bytes)
    changed_digests = {
        "run_report": hashlib.sha256(report_bytes).hexdigest(),
        "fault_receipt": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    changed_refs = [
        ref.model_copy(update={"sha256": changed_digests[ref.kind]})
        if ref.kind in changed_digests
        else ref
        for ref in result.evidence
    ]
    case.results[-1] = sign_case_result(
        result.model_copy(
            update={"evidence": changed_refs, "attestation_signature": ""}
        ),
        private_key=_RUNNER_PRIVATE_BYTES,
    )

    qualification_report = evaluate_qualification(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )

    assert expected_code in {refusal.code for refusal in qualification_report.refusals}


def test_requalification_condition_advances_version_and_invalidates_certification() -> (
    None
):
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    project = workflow.qualification
    assert project is not None
    revision = project.revision
    previous_digest = project.revision_digest()
    project.last_certification = None

    add_requalification_condition(
        workflow,
        RequalificationCondition(
            kind="application_version_changed",
            description="Re-run qualification when the application version changes",
        ),
    )
    assert project.revision == revision + 1
    assert project.previous_revision_sha256 == previous_digest
    assert project.requalification_conditions[0].kind == ("application_version_changed")


def test_minimum_effect_tier_versions_round_trips_and_invalidates_certification(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    report = certify_project(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )
    assert report.passed
    assert workflow.manifest is not None
    assert workflow.manifest.provenance.certified
    project = workflow.qualification
    assert project is not None
    previous_revision = project.revision
    previous_digest = project.revision_digest()

    set_minimum_effect_tier(
        workflow,
        VerificationTier.INDEPENDENT_SESSION,
    )

    assert project.minimum_effect_tier is VerificationTier.INDEPENDENT_SESSION
    assert project.revision == previous_revision + 1
    assert project.previous_revision_sha256 == previous_digest
    assert project.last_certification is None
    assert not workflow.manifest.provenance.certified

    set_minimum_effect_tier(
        workflow,
        VerificationTier.INDEPENDENT_SESSION,
    )
    assert project.revision == previous_revision + 1

    from openadapt_flow.qualification import save_qualified_workflow

    save_qualified_workflow(workflow, bundle)
    loaded = Workflow.load(bundle)
    assert loaded.qualification is not None
    assert loaded.qualification.minimum_effect_tier == (
        VerificationTier.INDEPENDENT_SESSION
    )
    assert loaded.qualification.revision == previous_revision + 1


def test_full_campaign_certifies_through_existing_policy_and_round_trips(
    tmp_path: Path,
) -> None:
    from openadapt_flow.compiler.codegen import render_workflow_py

    workflow = _workflow()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    (bundle / "workflow.py").write_text(render_workflow_py(workflow), encoding="utf-8")
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    report = certify_project(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
    )
    assert report.passed
    assert report.identity_covered_action_count == 1
    assert report.effect_covered_action_count == 1
    assert workflow.manifest is not None
    assert workflow.manifest.provenance.certified

    from openadapt_flow.qualification import save_qualified_workflow

    save_qualified_workflow(workflow, bundle)
    loaded = Workflow.load(bundle)
    assert (bundle / "workflow.py").read_text(encoding="utf-8") == (
        render_workflow_py(loaded)
    )
    assert loaded.qualification is not None
    assert loaded.qualification.schema_version == "openadapt.qualification-project/v1"
    assert loaded.qualification.last_certification is not None
    assert loaded.qualification.last_certification.report_sha256 == (
        report.report_sha256()
    )
    assert loaded.qualification.last_certification.workflow_contract_sha256 == (
        workflow_contract_sha256(loaded)
    )
    assert loaded.manifest is not None
    template = loaded.manifest.provenance.governed_authorization_template
    assert template is not None
    assert template.bundle_content_digest == loaded.manifest.content_digest
    assert template.template_sha256 == template.computed_sha256()
    assert template.execution_profile == "standard"
    policy = load_policy("clinical-write")
    policy_digest = policy_contract_sha256(policy)
    authorization = GovernedRunAuthorization(
        bundle_content_digest=loaded.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(loaded, None, None),
        admitted_policy_name=policy.name,
        admitted_policy_contract_sha256=policy_digest,
        execution_profile="standard",
        qualified_effect_requirements=qualified_effect_requirements(
            loaded, ExecutionProfile.STANDARD
        ),
    )
    assert authorization.validate_workflow(loaded) is None
    assert "no exact policy digest" in (
        authorization.model_copy(
            update={"admitted_policy_contract_sha256": None}
        ).validate_workflow(loaded)
        or ""
    )
    assert "policy digest does not match" in (
        authorization.model_copy(
            update={"admitted_policy_contract_sha256": "0" * 64}
        ).validate_workflow(loaded)
        or ""
    )


def test_governed_authorization_template_is_strict_and_value_free(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    workflow.params["hidden_value"] = "Jane Sample 1980-01-15"
    workflow.params["secret_token"] = "super-secret-token"
    workflow.secret_params.append("secret_token")
    workflow.secret_params.append("secret_only")
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    assert certify_project(
        workflow, policy=load_policy("clinical-write"), evidence_root=evidence_root
    ).passed
    from openadapt_flow.qualification import save_qualified_workflow

    save_qualified_workflow(workflow, bundle)
    loaded = Workflow.load(bundle)
    assert loaded.manifest is not None
    template = loaded.manifest.provenance.governed_authorization_template
    assert template is not None
    rendered = template.model_dump_json()
    assert "Jane Sample" not in rendered
    assert "super-secret-token" not in rendered
    parameters = {parameter.name: parameter for parameter in template.parameters}
    assert parameters["hidden_value"].has_default is True
    assert parameters["hidden_value"].type.value == "string"
    assert parameters["hidden_value"].required is False
    assert parameters["secret_token"].secret is True
    assert parameters["secret_token"].has_default is True
    assert parameters["secret_only"].secret is True
    assert parameters["secret_only"].has_default is False
    payload = template.model_dump(mode="json")
    with pytest.raises(ValueError, match="hash"):
        GovernedAuthorizationTemplate.model_validate(
            {**payload, "policy_name": "mutated"}
        )
    with pytest.raises(ValueError):
        GovernedAuthorizationTemplate.model_validate({**payload, "unknown": True})


def test_governed_authorization_template_rejects_manifest_replacement(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    assert certify_project(
        workflow, policy=load_policy("clinical-write"), evidence_root=evidence_root
    ).passed
    from openadapt_flow.qualification import save_qualified_workflow

    save_qualified_workflow(workflow, bundle)
    raw = json.loads((bundle / "workflow.json").read_text())
    template = GovernedAuthorizationTemplate.model_validate(
        raw["manifest"]["provenance"]["governed_authorization_template"]
    )
    replacement = GovernedAuthorizationTemplate.create(
        **{
            key: getattr(template, key)
            for key in type(template).model_fields
            if key not in {"template_sha256", "minimum_effect_tier"}
        },
        minimum_effect_tier=1,
    )
    raw["manifest"]["provenance"]["governed_authorization_template"] = (
        replacement.model_dump(mode="json")
    )
    (bundle / "workflow.json").write_text(json.dumps(raw))

    with pytest.raises(BundleIntegrityError, match="does not match"):
        Workflow.load(bundle)


def test_failed_requalification_clears_governed_authorization_template(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    policy = load_policy("clinical-write")
    assert certify_project(workflow, policy=policy, evidence_root=evidence_root).passed
    from openadapt_flow.qualification import save_qualified_workflow

    save_qualified_workflow(workflow, bundle)
    assert workflow.manifest is not None
    assert workflow.manifest.provenance.governed_authorization_template is not None
    assert workflow.qualification is not None
    for case in workflow.qualification.cases:
        case.results = []

    assert not certify_project(
        workflow, policy=policy, evidence_root=evidence_root
    ).passed
    assert workflow.manifest.provenance.governed_authorization_template is None


def test_governed_authorization_template_uses_canonical_legacy_parameter_schema(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    workflow.params["legacy_value"] = "example"
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    assert certify_project(
        workflow, policy=load_policy("clinical-write"), evidence_root=evidence_root
    ).passed
    from openadapt_flow.qualification import (
        build_governed_authorization_template,
    )

    template = build_governed_authorization_template(workflow)
    canonical = {
        item["name"]: item for item in build_runtime_parameter_schema(workflow)
    }
    legacy = next(item for item in template.parameters if item.name == "legacy_value")
    assert legacy.required is canonical["legacy_value"]["required"] is False
    assert legacy.secret is canonical["legacy_value"]["secret"] is False
    assert legacy.type.value == canonical["legacy_value"]["type"]
    assert template.parameter_contract_sha256 == compute_parameter_schema_digest(
        workflow
    )


def test_governed_authorization_template_fixed_vector() -> None:
    template = GovernedAuthorizationTemplate.create(
        bundle_content_digest="1" * 64,
        workflow_contract_sha256="2" * 64,
        qualification_project_id="project-1",
        qualification_project_revision=1,
        qualification_project_contract_sha256="3" * 64,
        qualification_environment_contract_sha256="4" * 64,
        qualification_report_sha256="5" * 64,
        qualification_case_evidence_contract_sha256="6" * 64,
        policy_name="clinical-write",
        policy_contract_sha256="7" * 64,
        execution_profile="standard",
        minimum_effect_tier=3,
        required_identity_step_ids=("step-1",),
        identity_contract_sha256="8" * 64,
        parameters=(
            GovernedAuthorizationParameter(
                name="customer_id",
                type="string",
                required=True,
                secret=False,
                has_default=True,
                choices_count=0,
                choices_sha256="9" * 64,
            ),
        ),
        parameter_contract_sha256="a" * 64,
    )
    assert (
        template.template_sha256
        == "f9c605a8cbb575332bf53015a6c89b35a6dbd8390774730415bef8595d1afcd5"
    )


@pytest.mark.parametrize("name", ("", "x" * 129))
def test_governed_authorization_template_parameter_name_bounds(name: str) -> None:
    with pytest.raises(ValueError):
        GovernedAuthorizationParameter(
            name=name,
            type="string",
            required=True,
            secret=False,
            has_default=False,
            choices_count=0,
            choices_sha256="0" * 64,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("qualification_project_id", "invalid project id"),
        ("qualification_project_id", "x" * 129),
        ("policy_name", ""),
        ("policy_name", "x" * 129),
    ),
)
def test_governed_authorization_template_project_and_policy_bounds(
    field: str,
    value: str,
) -> None:
    values: dict[str, Any] = {
        "bundle_content_digest": "1" * 64,
        "workflow_contract_sha256": "2" * 64,
        "qualification_project_id": "project-1",
        "qualification_project_revision": 1,
        "qualification_project_contract_sha256": "3" * 64,
        "qualification_environment_contract_sha256": "4" * 64,
        "qualification_report_sha256": "5" * 64,
        "qualification_case_evidence_contract_sha256": "6" * 64,
        "policy_name": "clinical-write",
        "policy_contract_sha256": "7" * 64,
        "execution_profile": "standard",
        "minimum_effect_tier": 3,
        "identity_contract_sha256": "8" * 64,
        "parameter_contract_sha256": "a" * 64,
    }
    values[field] = value
    with pytest.raises(ValueError):
        GovernedAuthorizationTemplate.create(**values)


def test_governed_authorization_template_survives_encrypted_save_without_digest_change(
    tmp_path: Path,
) -> None:
    key = "qualification-template-test-key"
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle, encrypt=True, key=key)
    workflow = Workflow.load(bundle, key=key)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    assert certify_project(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
        execution_profile="regulated",
    ).passed
    from openadapt_flow.qualification import save_qualified_workflow

    save_qualified_workflow(workflow, bundle, key=key)
    loaded = Workflow.load(bundle, key=key)
    assert loaded.manifest is not None
    template = loaded.manifest.provenance.governed_authorization_template
    assert template is not None
    assert template.execution_profile == "regulated"
    digest = loaded.manifest.content_digest
    loaded.save(bundle, encrypt=True, key=key)
    reloaded = Workflow.load(bundle, key=key)
    assert reloaded.manifest is not None
    assert reloaded.manifest.content_digest == digest


def test_persisted_certification_is_recomputed_and_policy_digest_bound(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    policy = load_policy("clinical-write")
    report = certify_project(workflow, policy=policy, evidence_root=evidence_root)
    assert report.passed
    policy_digest = policy_contract_sha256(policy)
    assert current_certification_matches(workflow, policy=policy)
    assert current_certification_matches(
        workflow,
        policy_contract_digest=policy_digest,
    )
    assert not current_certification_matches(
        workflow,
        policy_contract_digest="0" * 64,
    )
    assert not current_certification_matches(workflow)

    project = workflow.qualification
    assert project is not None and project.last_certification is not None
    original = project.last_certification.model_copy(deep=True)

    project.last_certification.report_sha256 = "f" * 64
    assert not current_certification_matches(workflow, policy=policy)
    project.last_certification = original.model_copy(deep=True)

    wrong_policy = load_policy("permissive")
    assert not current_certification_matches(workflow, policy=wrong_policy)

    same_name_mutation = Policy.model_validate(policy.model_dump(mode="json"))
    same_name_mutation.prohibit_unarmed_clicks = not policy.prohibit_unarmed_clicks
    assert same_name_mutation.name == policy.name
    assert not current_certification_matches(workflow, policy=same_name_mutation)

    # The exact case runs are policy-bound. A mutable bundle-local policy
    # rewrite therefore fails before it can appoint itself as authority.
    forged_policy = Policy.model_validate(policy.model_dump(mode="json"))
    forged_policy.description += " (mutated bundle-local policy)"
    forged_report = evaluate_qualification(
        workflow,
        policy=forged_policy,
        evidence_root=evidence_root,
    )
    assert not forged_report.passed
    assert QualificationRefusalCode.CASE_ATTESTATION_INVALID in {
        refusal.code for refusal in forged_report.refusals
    }
    project.last_certification = original.model_copy(deep=True)
    project.last_certification.policy_contract = forged_policy.model_dump(mode="json")
    project.last_certification.policy_contract_sha256 = policy_contract_sha256(
        forged_policy
    )
    project.last_certification.report_sha256 = forged_report.report_sha256()
    assert not current_certification_matches(workflow)
    assert not current_certification_matches(workflow, policy=policy)


def test_persisted_certification_refuses_expired_or_invalid_bundle_expiry(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    policy = load_policy("clinical-write")
    assert certify_project(workflow, policy=policy, evidence_root=evidence_root).passed
    assert workflow.manifest is not None
    assert current_certification_matches(workflow, policy=policy)

    workflow.manifest.provenance.expires_at = "2000-01-01T00:00:00Z"
    assert not current_certification_matches(workflow, policy=policy)

    workflow.manifest.provenance.expires_at = "invalid"
    assert not current_certification_matches(workflow, policy=policy)

    workflow.manifest.provenance.expires_at = "2099-01-01T00:00:00Z"
    assert current_certification_matches(workflow, policy=policy)


def test_judgment_evidence_certification_reproduces_and_saves(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    workflow.subflows["review_subflow"] = ProgramGraph(
        entry="done",
        states={
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)

    decision = BusinessDecisionSpec(
        question="Which reviewed path should continue?",
        authorized_roles=("operator",),
        output_param="review_outcome",
        options=(
            BusinessDecisionOption(
                id="priority", label="Priority", value="priority", target="done"
            ),
            BusinessDecisionOption(
                id="standard", label="Standard", value="standard", target="done"
            ),
        ),
        revalidation=(
            Predicate(kind=PredicateKind.TEXT_PRESENT, text="Ready for review"),
        ),
    )
    workflow = set_business_decision(
        workflow,
        graph_id="review_subflow",
        state_id="review",
        decision=decision,
        insert_before_state_id="done",
    )
    schema = JudgmentFactSchemaV1(
        fields={
            "urgency": JudgmentFactFieldV1(
                type=JudgmentFactType.ENUM,
                allowed_values=("high", "normal"),
            )
        }
    )
    case_payload = b"reviewed judgment evidence"
    case_ref = LocalEvidenceRefV1(
        relative_path="judgment/human-1.json",
        sha256=hashlib.sha256(case_payload).hexdigest(),
        kind="report",
    )
    case = JudgmentCaseV1(
        id="human-1",
        decision=JudgmentDecisionBindingV1(
            graph_id="review_subflow",
            state_id="review",
            workflow_contract_sha256=workflow_contract_sha256(workflow),
            decision_contract_sha256=decision.contract_sha256(),
        ),
        fact_schema_sha256=schema.contract_sha256(),
        facts={"urgency": "high"},
        local_evidence=(case_ref,),
        provenance=JudgmentCaseProvenanceV1(
            source="policy_review",
            source_ref_sha256="c" * 64,
            reviewer_role="operator",
            reviewer_principal_ref_sha256="d" * 64,
        ),
        disposition=JudgmentDisposition.HUMAN_NODE,
    )
    set_judgment_cases(
        workflow,
        schemas=(
            JudgmentFactSchemaBindingV1(
                graph_id="review_subflow",
                state_id="review",
                fact_schema=schema,
            ),
        ),
        cases=(case,),
    )

    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    judgment_dir = evidence_root / "judgment"
    judgment_dir.mkdir(parents=True)
    (judgment_dir / "human-1.json").write_bytes(case_payload)
    policy = load_policy("clinical-write")
    assert certify_project(
        workflow,
        policy=policy,
        evidence_root=evidence_root,
    ).passed
    assert current_certification_matches(workflow, policy=policy)
    save_qualified_workflow(workflow, bundle)

    project = workflow.qualification
    assert project is not None
    project.judgment_cases = [
        case.model_copy(
            update={
                "local_evidence": (case_ref.model_copy(update={"sha256": "0" * 64}),)
            }
        )
    ]
    assert not current_certification_matches(workflow, policy=policy)


def test_forged_passed_bit_cannot_turn_a_failed_campaign_into_certification(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    project = workflow.qualification
    assert project is not None
    representative = next(
        case
        for case in project.cases
        if case.kind is QualificationCaseKind.REPRESENTATIVE
    )
    representative.results = []
    policy = load_policy("clinical-write")

    report = certify_project(workflow, policy=policy, evidence_root=evidence_root)
    assert not report.passed
    assert project is not None and project.last_certification is not None
    project.last_certification.passed = True

    assert not current_certification_matches(workflow, policy=policy)


def test_valid_certification_authorizes_only_its_exact_reviewed_risk_policy(
    tmp_path: Path,
) -> None:
    workflow = Workflow(
        name="reviewed-navigation",
        steps=[
            Step(
                id="continue",
                intent="Continue to the review screen",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/continue.png",
                    region=(10, 10, 40, 20),
                    click_point=(30, 20),
                    ocr_text="Continue",
                    context_text="Synthetic review record",
                ),
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Review",
                    )
                ],
                risk="irreversible",
                risk_review_required=True,
            )
        ],
    )
    init_project(workflow, environment=_environment())
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="continue",
            classification=ActionRiskClass.READ_ONLY,
            explanation="Opens review without changing business state",
            operator_confirmed=True,
        ),
    )
    set_trusted_runner_key(
        workflow,
        key_id="test-runner",
        public_key_base64=_RUNNER_PUBLIC_BASE64,
    )
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    policy = load_policy("clinical-write")
    assert certify_project(
        workflow,
        policy=policy,
        evidence_root=evidence_root,
    ).passed

    step = workflow.steps[0]
    assert (
        effective_step_risk(
            step,
            workflow,
            require_current_certification=True,
            certifying_policy=policy,
        )
        == "reversible"
    )
    same_name_mutation = Policy.model_validate(policy.model_dump(mode="json"))
    same_name_mutation.description += " (mutated after certification)"
    assert (
        effective_step_risk(
            step,
            workflow,
            require_current_certification=True,
            certifying_policy=same_name_mutation,
        )
        == "irreversible"
    )


def test_api_and_gui_effect_paths_require_independent_qualification() -> None:
    workflow = _workflow()
    workflow.steps[0].api_binding = ApiBinding(
        url_template="/api/records",
        effects=[workflow.steps[0].effects[0].model_copy(deep=True)],
    )
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)

    missing_api = evaluate_qualification(workflow)
    assert any(
        refusal.code is QualificationRefusalCode.EFFECT_POLICY_MISSING
        and ".api." in refusal.path
        for refusal in missing_api.refusals
    )

    set_effect_policy(
        workflow,
        step_id="save",
        actuation_path="api",
        effect_index=0,
        tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    complete = evaluate_qualification(workflow)
    assert not any(
        refusal.code
        in {
            QualificationRefusalCode.EFFECT_CONTRACT_MISSING,
            QualificationRefusalCode.EFFECT_POLICY_MISSING,
            QualificationRefusalCode.EFFECT_CONTRACT_CHANGED,
        }
        for refusal in complete.refusals
    )
    assert complete.effect_covered_action_count == 1


def test_cli_initializes_project_without_raw_manifest_editing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow = Workflow(
        name="simple",
        steps=[Step(id="wait", intent="Wait", action=ActionKind.WAIT)],
    )
    bundle = tmp_path / "bundle"
    workflow.save(bundle)

    assert (
        main(
            [
                "qualify",
                "init",
                str(bundle),
                "--target",
                "rdp",
                "--application",
                "Reference app",
                "--application-version",
                "1",
                "--environment-digest",
                "c" * 64,
            ]
        )
        == 0
    )
    capsys.readouterr()
    loaded = Workflow.load(bundle)
    assert loaded.qualification is not None
    assert loaded.qualification.environment.target_kind == "rdp"

    # The CLI emits a stable machine report and refuses certification until a
    # representative campaign exists.
    assert main(["qualify", "explain", str(bundle), "--json"]) == 2
    payload = capsys.readouterr().out
    assert '"representative_case_missing"' in payload
    assert json.loads(payload)["entity_label_options"] == entity_label_options()


def test_cli_entity_label_notice_tracks_real_mutations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    init_project(workflow, environment=_environment())
    workflow.save(bundle)

    set_args = [
        "qualify",
        "label",
        "set",
        str(bundle),
        "--step",
        "save",
        "--label",
        "insurance claim",
    ]
    invalid_args = [*set_args]
    invalid_args[invalid_args.index("insurance claim")] = "Patient Record"
    with pytest.raises(SystemExit, match="invalid entity label"):
        main(invalid_args)
    assert Workflow.load(bundle).qualification.entity_labels == {}

    assert main(set_args) == 0
    first = capsys.readouterr()
    assert json.loads(first.out)["entity_labels"]["save"]["label"] == "insurance claim"
    assert first.err

    assert main(set_args) == 0
    unchanged = capsys.readouterr()
    assert json.loads(unchanged.out)["entity_labels"]["save"]["fallback"] == "item"
    assert not unchanged.err

    assert (
        main(
            [
                "qualify",
                "label",
                "set",
                str(bundle),
                "--step",
                "save",
                "--label",
                "appointment request",
                "--fallback",
                "item",
            ]
        )
        == 0
    )
    custom = capsys.readouterr()
    custom_payload = json.loads(custom.out)["entity_labels"]["save"]
    assert custom_payload == {
        "step_id": "save",
        "label": "appointment request",
        "fallback": "item",
    }
    assert custom.err

    assert main(["qualify", "label", "remove", str(bundle), "--step", "save"]) == 0
    removed = capsys.readouterr()
    assert json.loads(removed.out)["entity_labels"] == {}
    assert removed.err


def test_cli_identity_extract_pattern_round_trips_exactly(tmp_path: Path) -> None:
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    init_project(workflow, environment=_environment())
    workflow.save(bundle)
    pattern = r"record (?P<value>identity)(?=$)"

    assert (
        main(
            [
                "qualify",
                "set-identity",
                str(bundle),
                "--step",
                "save",
                "--signal",
                "record_id=structured:exact",
                "--signal-extract",
                f"record_id={pattern}",
                "--quorum",
                "1",
            ]
        )
        == 0
    )

    loaded = Workflow.load(bundle)
    assert loaded.qualification is not None
    policy = loaded.qualification.identity_policies["save"]
    assert policy.signals[0].extract_pattern == pattern


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        ("application=application:exact", "accuro"),
        ("session=session:exact", "a" * 64),
        ("workflow_state=workflow_state:exact", "patient-chart"),
    ],
)
def test_cli_identity_expected_value_round_trips(
    tmp_path: Path, signal: str, expected: str
) -> None:
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    init_project(workflow, environment=_environment())
    workflow.save(bundle)
    key = signal.split("=", 1)[0]

    assert (
        main(
            [
                "qualify",
                "set-identity",
                str(bundle),
                "--step",
                "save",
                "--signal",
                signal,
                "--signal-expected",
                f"{key}={expected}",
                "--quorum",
                "1",
            ]
        )
        == 0
    )

    loaded = Workflow.load(bundle)
    policy = loaded.qualification.identity_policies["save"]
    assert policy.signals[0].expected_value == expected


def test_cli_identity_expected_value_rejects_invalid_bindings(tmp_path: Path) -> None:
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    init_project(workflow, environment=_environment())
    workflow.save(bundle)

    for expected_args, message in (
        (["--signal-expected", "unknown=accuro"], "unknown signal"),
        (
            [
                "--signal-expected",
                "application=accuro",
                "--signal-expected",
                "application=accuro",
            ],
            "repeats signal key",
        ),
        (["--signal-expected", "application"], "expects KEY=VALUE"),
    ):
        with pytest.raises(SystemExit, match=message):
            main(
                [
                    "qualify",
                    "set-identity",
                    str(bundle),
                    "--step",
                    "save",
                    "--signal",
                    "application=application:exact",
                    *expected_args,
                    "--quorum",
                    "1",
                ]
            )


@pytest.mark.parametrize(
    ("signal", "extract_args", "message"),
    [
        (
            "record_id=structured:exact",
            ["--signal-extract", "unknown=(?P<value>.+)"],
            "unknown signal",
        ),
        (
            "record_id=structured:exact",
            [
                "--signal-extract",
                "record_id=(?P<value>.+)",
                "--signal-extract",
                "record_id=(?P<value>.+)",
            ],
            "repeats signal key",
        ),
        (
            "record_id=structured:exact",
            ["--signal-extract", "record_id"],
            "expects KEY=REGEX",
        ),
        (
            "record_id=structured:exact",
            [],
            "structured/context identity signals require extract_pattern",
        ),
        (
            "record_id=identifier_region:exact",
            [
                "--signal-region",
                "record_id=10,10,20,20",
                "--signal-extract",
                "record_id=(?P<value>.+)",
            ],
            "extract_pattern applies only",
        ),
    ],
)
def test_cli_identity_extract_rejects_invalid_bindings(
    tmp_path: Path,
    signal: str,
    extract_args: list[str],
    message: str,
) -> None:
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    init_project(workflow, environment=_environment())
    workflow.save(bundle)

    with pytest.raises(SystemExit, match=message):
        main(
            [
                "qualify",
                "set-identity",
                str(bundle),
                "--step",
                "save",
                "--signal",
                signal,
                *extract_args,
                "--quorum",
                "1",
            ]
        )
