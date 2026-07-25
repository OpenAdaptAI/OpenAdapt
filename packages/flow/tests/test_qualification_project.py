"""Durable contracts for the versioned qualification project.

These tests exercise schema round-trip, coverage refusal, case evidence, and
the existing policy/certification seam.  They do not pin CLI prose or UI copy.
"""

from __future__ import annotations

import hashlib
from base64 import b64encode
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.__main__ import main
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.policy import load_policy
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    EvidenceRef,
    IdentityEnforcement,
    IdentityPolicy,
    IdentitySignalPolicy,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationOutcome,
    QualificationRefusalCode,
    RequalificationCondition,
    VerificationTier,
    add_case,
    add_requalification_condition,
    certify_project,
    evaluate_qualification,
    init_project,
    record_case_results,
    set_action_classification,
    set_effect_policy,
    set_identity_policy,
    set_minimum_effect_tier,
    set_trusted_runner_key,
    sign_case_result,
    workflow_contract_sha256,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

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
        application_version="1",
        environment_digest="b" * 64,
        runtime_version="1.20.2",
        required_capabilities=["pixel_observation", "effect_verification"],
    )


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


def _record_passing_campaign(workflow: Workflow, evidence_root: Path) -> None:
    project = workflow.qualification
    assert project is not None
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind=QualificationCaseKind.REPRESENTATIVE,
            input_ref="fixtures/representative-1",
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    evidence_bytes = b'{"outcome":"verified"}'
    (evidence_root / "report.json").write_bytes(evidence_bytes)
    evidence = [
        EvidenceRef(
            kind="run_report",
            sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            relative_path="report.json",
        )
    ]
    results = [
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
                evidence=evidence,
                attestation_key_id="test-runner",
            ),
            private_key=_RUNNER_PRIVATE_BYTES,
        )
        for case in project.cases
    ]
    record_case_results(workflow, results, evidence_root=evidence_root)


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


def test_state_changing_is_not_mislabeled_or_identity_gated() -> None:
    workflow = Workflow(
        name="reversible-write",
        params={"record_id": "1"},
        steps=[
            Step(
                id="draft",
                intent="Update a reversible draft",
                action=ActionKind.TYPE,
                text="draft",
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
        (evidence_root / "report.json").write_text("tampered")
    else:
        (evidence_root / "report.json").unlink()
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
    (bundle / "templates" / "save.png").write_bytes(b"fixture")
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
    workflow = _workflow()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(b"fixture")
    workflow.save(bundle)
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
    assert loaded.qualification is not None
    assert loaded.qualification.schema_version == "openadapt.qualification-project/v1"
    assert loaded.qualification.last_certification is not None
    assert loaded.qualification.last_certification.report_sha256 == (
        report.report_sha256()
    )
    assert loaded.qualification.last_certification.workflow_contract_sha256 == (
        workflow_contract_sha256(loaded)
    )


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
