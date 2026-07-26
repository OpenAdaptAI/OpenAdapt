from __future__ import annotations

import hashlib
import json
from base64 import b64encode
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engine.config import EngineConfig
from engine.db import IndexDB
from engine.dispatch import EngineDispatcher, EngineServices
from engine.qualification import (
    QualificationError,
    add_qualification_case,
    arm_action_identity,
    bind_action_effect,
    certify_bundle,
    environment_digest_from_identifier,
    initialize_qualification,
    inspect_bundle,
    prepare_local_qualification_runner,
    record_local_qualification_result,
    seal_qualification_bundle,
    set_action_effect_verification,
    set_action_identity_policy,
    set_action_risk,
    set_project_minimum_effect_tier,
)
from engine.qualification_lifecycle import retain_run_evidence

pytest.importorskip("openadapt_flow.qualification")

from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    Anchor,
    Landmark,
    ParamSpec,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.qualification import IdentitySignalPolicy  # noqa: E402
from openadapt_flow.runtime.effects.effect import Effect, EffectKind  # noqa: E402
from openadapt_flow.traversal import iter_workflow_steps  # noqa: E402


def _bundle(path: Path, *steps: Step, params: dict[str, str] | None = None) -> Path:
    workflow = Workflow(
        name="qualification-test",
        params=params or {},
        param_specs={
            name: ParamSpec(name=name) for name in (params or {})
        },
        steps=list(steps),
    )
    workflow.save(path)
    return path


def _initialize(bundle: Path, *, target_kind: str = "web") -> dict:
    return initialize_qualification(
        bundle,
        workflow_id="wf-1",
        target_kind=target_kind,
        application="Reference app",
        application_version="1.0",
        environment_label="reference-test-environment",
        required_capabilities=["structural_observation", "actuation"],
        minimum_effect_tier=3,
    )


def test_existing_bundle_initializes_canonical_project_and_invalidates_legacy_certification(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="settle", intent="Wait for settled state", action=ActionKind.WAIT),
    )
    workflow = Workflow.load(bundle)
    workflow.stamp_certification("clinical-write", True)
    workflow.save(bundle)

    before = inspect_bundle(bundle, workflow_id="wf-1")
    result = _initialize(bundle, target_kind="citrix")
    persisted = Workflow.load(bundle)

    assert before["migration_required"] is True
    assert before["report"]["refusals"][0]["code"] == "project_missing"
    assert result["qualification_schema"] == "openadapt.qualification-project/v1"
    assert result["report"]["schema_version"] == "openadapt.qualification-report/v1"
    assert result["migration_required"] is False
    assert result["project"]["environment"]["target_kind"] == "citrix"
    assert result["project"]["environment"]["environment_digest"] == hashlib.sha256(
        b"reference-test-environment"
    ).hexdigest()
    assert persisted.qualification is not None
    assert persisted.manifest.provenance.certified is False
    assert persisted.manifest.provenance.policy_name is None


def test_environment_identifier_digest_is_reproducible_but_explicitly_operator_defined(
    tmp_path: Path,
) -> None:
    digest = environment_digest_from_identifier("  clinic-test-citrix-vda  ")
    assert digest == hashlib.sha256(b"clinic-test-citrix-vda").hexdigest()
    assert digest == environment_digest_from_identifier("clinic-test-citrix-vda")
    assert digest != environment_digest_from_identifier("clinic-prod-citrix-vda")

    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="settle", intent="Wait", action=ActionKind.WAIT),
    )
    exact_measured_digest = "a" * 64
    result = initialize_qualification(
        bundle,
        workflow_id="wf-1",
        target_kind="citrix",
        application="Reference app",
        application_version="1",
        environment_digest=exact_measured_digest,
        required_capabilities=[],
    )
    assert result["project"]["environment"]["environment_digest"] == exact_measured_digest


def test_local_case_run_is_signed_and_bound_to_exact_retained_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="settle", intent="Wait", action=ActionKind.WAIT),
    )
    _initialize(bundle)
    added = add_qualification_case(
        bundle,
        workflow_id="wf-1",
        case_id="representative-local",
        kind="representative",
        input_ref="desktop-input://wf-1/representative-local",
    )

    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(
        "engine.qualification_keys.qualification_signer",
        lambda: (private_raw, b64encode(public_raw).decode("ascii")),
    )
    prepared = prepare_local_qualification_runner(bundle, workflow_id="wf-1")
    revision = prepared["project"]["revision"]
    evidence_path = (
        bundle
        / "qualification-evidence"
        / "representative-local"
        / "run-1"
        / "report.json"
    )
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text('{"execution_outcome":"VERIFIED"}', encoding="utf-8")
    result = record_local_qualification_result(
        bundle,
        workflow_id="wf-1",
        case_id="representative-local",
        observed_outcome="verified",
        evidence=[
            {
                "kind": "run_report",
                "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                "relative_path": "representative-local/run-1/report.json",
            }
        ],
        runner_capabilities=["structural_observation", "actuation"],
    )
    case = next(
        item
        for item in result["project"]["cases"]
        if item["id"] == "representative-local"
    )

    assert added["project"]["cases"][-1]["input_ref"].startswith("desktop-input://")
    assert result["project"]["revision"] == revision
    assert case["results"][0]["status"] == "passed"
    assert case["results"][0]["attestation_signature"]


def test_retained_case_receipt_binds_report_without_copying_sensitive_body(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    run.mkdir()
    report = {
        "execution_outcome": "VERIFIED",
        "production_eligible": True,
        "execution_completed": True,
        "patient_name": "Sensitive Person",
        "outcome_envelope": {
            "required_contracts": {"effect": 1},
            "passed_contracts": {"effect": 1},
            "evidence_classes": ["independent_effect"],
            "external_network_calls": "none",
        },
    }
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")

    references = retain_run_evidence(
        bundle,
        case_id="representative-1",
        run_id="run-1",
        run_dir=run,
    )
    receipt = (bundle / "qualification-evidence" / references[0]["relative_path"]).read_text()

    assert "Sensitive Person" not in receipt
    assert hashlib.sha256(receipt.encode()).hexdigest() == references[0]["sha256"]


def test_inspection_exposes_durable_target_evidence_without_flattening_to_coordinates(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(
            id="submit",
            intent="click at (815, 369)",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="submit.png",
                region=(800, 350, 30, 38),
                click_point=(815, 369),
                structural=StructuralLocator(
                    automation_id="submit-claim",
                    role="button",
                    name="Submit",
                ),
                ocr_text="Submit",
                landmarks=[
                    Landmark(
                        relation="above",
                        ocr_text="Eligibility",
                        distance_px=120,
                    )
                ],
            ),
        ),
    )

    node = inspect_bundle(bundle, workflow_id="wf-1")["graph"]["nodes"][0]
    assert node["resolution"]["top_rung"] == "structural"
    evidence = {
        rung["name"]: (rung["present"], rung["detail"])
        for rung in node["resolution"]["rungs"]
    }
    assert evidence["structural"] == (True, "submit-claim")
    assert evidence["template"] == (True, "submit.png")
    assert evidence["ocr"] == (True, "Submit")
    assert evidence["landmarks"] == (True, "1 landmark(s)")
    assert "click_point" not in node["resolution"]


def test_encrypted_bundle_inspect_mutate_and_reseal_stays_ciphertext_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "desktop-qualification-test-key"
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="save", intent="Save", action=ActionKind.CLICK),
    )
    workflow = Workflow.load(bundle)
    workflow.save(bundle, encrypt=True, key=key)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", key)

    assert not (bundle / "workflow.json").exists()
    assert (bundle / "workflow.json.enc").is_file()
    before = (bundle / "workflow.json.enc").read_bytes()

    assert inspect_bundle(bundle, workflow_id="wf-encrypted")["migration_required"] is True
    _initialize(bundle)
    set_action_risk(
        bundle,
        workflow_id="wf-encrypted",
        step_id="save",
        risk="consequential",
    )

    assert not (bundle / "workflow.json").exists()
    assert (bundle / "workflow.json.enc").read_bytes() != before
    assert not list((bundle / "templates").glob("*.png"))
    persisted = Workflow.load(bundle, key=key)
    assert persisted.encrypted is True
    assert persisted.manifest.encrypted is True
    assert persisted.qualification.action_classifications[
        "save"
    ].classification.value == "consequential"


def test_encrypted_bundle_without_configured_key_refuses_without_disk_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "desktop-qualification-test-key"
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="save", intent="Save", action=ActionKind.CLICK),
    )
    workflow = Workflow.load(bundle)
    workflow.save(bundle, encrypt=True, key=key)
    monkeypatch.delenv("OPENADAPT_BUNDLE_KEY", raising=False)
    before = {
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    }

    with pytest.raises(QualificationError, match="Cannot open the sealed workflow"):
        inspect_bundle(bundle, workflow_id="wf-encrypted")
    with pytest.raises(QualificationError, match="Cannot open the sealed workflow"):
        set_action_risk(
            bundle,
            workflow_id="wf-encrypted",
            step_id="save",
            risk="consequential",
        )

    after = {
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (bundle / "workflow.json").exists()


def test_seal_creates_ciphertext_version_and_preserves_plaintext_source(
    tmp_path: Path,
) -> None:
    source = _bundle(
        tmp_path / "source",
        Step(id="save", intent="Save", action=ActionKind.CLICK),
    )
    original = (source / "workflow.json").read_bytes()
    destination = tmp_path / "sealed"

    result = seal_qualification_bundle(
        source,
        destination,
        workflow_id="wf-sealed",
        destination_key="desktop-sealed-version-key",
    )

    assert result["graph"]["bundle"]["encrypted"] is True
    assert (source / "workflow.json").read_bytes() == original
    assert (destination / "workflow.json.enc").is_file()
    assert not (destination / "workflow.json").exists()
    assert Workflow.load(destination, key="desktop-sealed-version-key").encrypted is True


def test_canonical_risk_review_reseals_and_preserves_executable_irreversibility(
    tmp_path: Path,
) -> None:
    step = Step(
        id="save",
        intent="Save encounter",
        action=ActionKind.CLICK,
        effects=[
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"patient_id": "P-42"},
            )
        ],
    )
    bundle = _bundle(tmp_path / "bundle", step)
    _initialize(bundle)
    previous_digest = Workflow.load(bundle).manifest.content_digest

    result = set_action_risk(
        bundle,
        workflow_id="wf-1",
        step_id="save",
        risk="irreversible",
        explanation="Final source-of-record submission",
    )
    persisted = Workflow.load(bundle)
    action = next(iter_workflow_steps(persisted))
    classification = persisted.qualification.action_classifications["save"]

    assert action.risk == "irreversible"
    assert [effect.risk for effect in action.effects] == ["irreversible"]
    assert classification.classification.value == "irreversible"
    assert classification.operator_confirmed is True
    assert persisted.manifest.content_digest != previous_digest
    assert result["controls"]["actions"]["save"]["classification"]["classification"] == (
        "irreversible"
    )

    before = (bundle / "workflow.json").read_bytes()
    with pytest.raises(QualificationError, match="cannot be down-classified"):
        set_action_risk(
            bundle,
            workflow_id="wf-1",
            step_id="save",
            risk="read_only",
        )
    assert (bundle / "workflow.json").read_bytes() == before


def test_risk_review_requires_project_without_mutating_legacy_bundle(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="save", intent="Save", action=ActionKind.CLICK),
    )
    before = (bundle / "workflow.json").read_bytes()

    with pytest.raises(QualificationError, match="Initialize the qualification"):
        set_action_risk(
            bundle,
            workflow_id="wf-1",
            step_id="save",
            risk="consequential",
        )

    assert (bundle / "workflow.json").read_bytes() == before


def test_identity_control_arms_runtime_and_canonical_policy_together(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(
            id="save",
            intent="Save encounter",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="save.png",
                region=(0, 0, 20, 20),
                click_point=(10, 10),
                structured_identity="patient_id P-42",
            ),
            identity_armed=False,
            identity_unarmed_reason="operator review required",
        ),
    )
    _initialize(bundle)
    set_action_risk(
        bundle,
        workflow_id="wf-1",
        step_id="save",
        risk="consequential",
    )

    result = arm_action_identity(
        bundle,
        workflow_id="wf-1",
        step_id="save",
    )
    persisted = Workflow.load(bundle)
    action = next(iter_workflow_steps(persisted))
    policy = persisted.qualification.identity_policies["save"]

    assert action.identity_armed is True
    assert action.identity_unarmed_reason is None
    assert policy.enforcement.value == "canonical_ladder"
    assert result["controls"]["actions"]["save"]["identity"]["policy"][
        "enforcement"
    ] == "canonical_ladder"


def test_identity_control_refuses_missing_evidence_without_mutation(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="save", intent="Save", action=ActionKind.CLICK),
    )
    _initialize(bundle)
    before = (bundle / "workflow.json").read_bytes()

    with pytest.raises(QualificationError, match="no retained structured identity"):
        arm_action_identity(
            bundle,
            workflow_id="wf-1",
            step_id="save",
        )

    assert (bundle / "workflow.json").read_bytes() == before


def test_identity_editor_round_trips_exact_normalized_region_and_quorum(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(
            id="save",
            intent="Save encounter",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="save.png",
                region=(0, 0, 20, 20),
                click_point=(10, 10),
                structured_identity="patient_id P-42",
                identifier_crop="patient-id.png",
                identifier_region=(4, 6, 120, 24),
            ),
            identity_armed=False,
            identity_unarmed_reason="operator review required",
        ),
    )
    _initialize(bundle)
    set_action_risk(
        bundle,
        workflow_id="wf-1",
        step_id="save",
        risk="consequential",
    )
    before = Workflow.load(bundle)
    assert before.qualification is not None
    previous_revision = before.qualification.revision
    previous_digest = before.qualification.revision_digest()

    result = set_action_identity_policy(
        bundle,
        workflow_id="wf-1",
        step_id="save",
        enforcement="signal_quorum",
        signals=[
            {
                "key": "record_id",
                "source": "structured",
                "match": "exact",
                "normalizers": [],
                "extract_pattern": r"patient_id (?P<value>[A-Z0-9-]+)",
            },
            {
                "key": "secondary_identifier",
                "source": "identifier_region",
                "match": "normalized",
                "normalizers": ["unicode_nfkc", "collapse_whitespace"],
            },
        ],
        quorum=2,
    )

    persisted = Workflow.load(bundle)
    assert persisted.qualification is not None
    policy = persisted.qualification.identity_policies["save"]
    assert policy.enforcement.value == "signal_quorum"
    assert policy.quorum == 2
    signal_keys = [
        getattr(signal, "key", getattr(signal, "field", None))
        for signal in policy.signals
    ]
    assert [
        value.value if hasattr(value, "value") else value for value in signal_keys
    ] == ["record_id", "secondary_identifier"]
    assert policy.signals[0].match.value == "exact"
    assert policy.signals[0].normalizers == []
    assert policy.signals[1].match.value == "normalized"
    assert [item.value for item in policy.signals[1].normalizers] == [
        "unicode_nfkc",
        "collapse_whitespace",
    ]
    assert policy.signals[1].region == (4, 6, 120, 24)
    assert persisted.qualification.revision == previous_revision + 1
    assert persisted.qualification.previous_revision_sha256 == previous_digest
    assert result["controls"]["actions"]["save"]["identity"]["policy"]["quorum"] == 2
    assert result["controls"]["actions"]["save"]["identity"]["policy"]["signals"][1]["region"] == [
        4,
        6,
        120,
        24,
    ]
    refusal_codes = {
        refusal["code"] for refusal in result["report"]["refusals"]
    }
    if "key" in IdentitySignalPolicy.model_fields:
        assert "identity_policy_unenforced" not in refusal_codes
    else:
        assert "identity_policy_unenforced" in refusal_codes


@pytest.mark.skipif(
    "key" not in IdentitySignalPolicy.model_fields,
    reason="requires the Flow 1.23 semantic identity contract",
)
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("application", "reference.application"),
        ("session", "a" * 64),
        ("workflow_state", "record.review"),
    ],
)
def test_identity_editor_authors_live_context_expected_values(
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(
            id="save",
            intent="Save",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="save.png",
                region=(0, 0, 20, 20),
                click_point=(10, 10),
            ),
        ),
    )
    _initialize(bundle)

    result = set_action_identity_policy(
        bundle,
        workflow_id="wf-1",
        step_id="save",
        enforcement="signal_quorum",
        signals=[
            {
                "key": source,
                "source": source,
                "match": "exact",
                "normalizers": [],
                "expected_value": expected,
            }
        ],
        quorum=1,
    )

    signal = result["controls"]["actions"]["save"]["identity"]["policy"]["signals"][0]
    assert signal["key"] == source
    assert signal["expected_value"] == expected


def test_identity_editor_refuses_unavailable_source_without_mutation(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(
            id="save",
            intent="Save",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="save.png",
                region=(0, 0, 20, 20),
                click_point=(10, 10),
                structured_identity="patient_id P-42",
            ),
        ),
    )
    _initialize(bundle)
    before = (bundle / "workflow.json").read_bytes()

    with pytest.raises(QualificationError, match="unavailable evidence"):
        set_action_identity_policy(
            bundle,
            workflow_id="wf-1",
            step_id="save",
            enforcement="signal_quorum",
            signals=[
                {
                    "key": "record_id",
                    "source": "identifier_region",
                    "match": "exact",
                    "normalizers": [],
                }
            ],
            quorum=1,
        )

    assert (bundle / "workflow.json").read_bytes() == before


def test_effect_control_writes_executable_contract_and_canonical_tier(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(
            id="save",
            intent="Save encounter",
            action=ActionKind.CLICK,
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={"__unbound__": "__operator_required__"},
                    needs_operator_confirmation=True,
                )
            ],
        ),
        params={"patient_id": "P-42", "request_id": "req-1"},
    )
    _initialize(bundle)

    result = bind_action_effect(
        bundle,
        workflow_id="wf-1",
        step_id="save",
        kind="record_written",
        match_field="patient_id",
        match_param="patient_id",
        idempotency_param="request_id",
        key_field="request_id",
        count_new_only=True,
        verification_tier=2,
    )
    persisted = Workflow.load(bundle)
    action = next(iter_workflow_steps(persisted))
    binding = persisted.qualification.effect_policies[0]

    assert action.effects[0].match["patient_id"].param == "patient_id"
    assert action.effects[0].idempotency_key.param == "request_id"
    assert action.effects[0].needs_operator_confirmation is False
    assert binding.step_id == "save"
    assert binding.effect_index == 0
    assert int(binding.tier) == 2
    assert binding.effect_contract_hash == action.effects[0].contract_hash()
    assert result["controls"]["actions"]["save"]["effects"][0][
        "verification_tier"
    ] == 2
    assert persisted.qualification.action_classifications[
        "save"
    ].classification.value == "state_changing"


def test_effect_control_refuses_unknown_parameter_without_mutation(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="save", intent="Save", action=ActionKind.CLICK),
        params={"patient_id": "P-42"},
    )
    _initialize(bundle)
    before = (bundle / "workflow.json").read_bytes()

    with pytest.raises(QualificationError, match="unknown workflow parameter"):
        bind_action_effect(
            bundle,
            workflow_id="wf-1",
            step_id="save",
            kind="record_written",
            match_field="patient_id",
            match_param="missing",
        )

    assert (bundle / "workflow.json").read_bytes() == before


def test_existing_effect_verification_and_project_minimum_round_trip_by_revision(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(
            id="save",
            intent="Save encounter",
            action=ActionKind.CLICK,
            effects=[
                Effect(
                    kind=EffectKind.FIELD_EQUALS,
                    match={"patient_id": "P-42"},
                    field="status",
                    value="complete",
                )
            ],
        ),
    )
    _initialize(bundle)
    before = Workflow.load(bundle)
    assert before.qualification is not None
    effect_before = before.steps[0].effects[0].model_dump(mode="json")
    previous_revision = before.qualification.revision
    previous_digest = before.qualification.revision_digest()

    verified = set_action_effect_verification(
        bundle,
        workflow_id="wf-1",
        step_id="save",
        effect_index=0,
        verification_tier=2,
    )
    persisted = Workflow.load(bundle)
    assert persisted.qualification is not None
    assert persisted.steps[0].effects[0].model_dump(mode="json") == effect_before
    assert int(persisted.qualification.effect_policies[0].tier) == 2
    assert persisted.qualification.revision == previous_revision + 1
    assert persisted.qualification.previous_revision_sha256 == previous_digest
    assert verified["controls"]["actions"]["save"]["effects"][0]["verification_tier"] == 2

    previous_revision = persisted.qualification.revision
    previous_digest = persisted.qualification.revision_digest()
    updated = set_project_minimum_effect_tier(
        bundle,
        workflow_id="wf-1",
        minimum_effect_tier=2,
    )
    persisted = Workflow.load(bundle)
    assert persisted.qualification is not None
    assert int(persisted.qualification.minimum_effect_tier) == 2
    assert persisted.qualification.revision == previous_revision + 1
    assert persisted.qualification.previous_revision_sha256 == previous_digest
    assert updated["project"]["minimum_effect_tier"] == 2
    assert updated["report"]["minimum_effect_tier"] == 2

    unchanged = set_project_minimum_effect_tier(
        bundle,
        workflow_id="wf-1",
        minimum_effect_tier=2,
    )
    assert unchanged["project"]["revision"] == persisted.qualification.revision


def test_certification_uses_canonical_refusals_instead_of_policy_only_success(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="settle", intent="Wait for settled state", action=ActionKind.WAIT),
    )
    _initialize(bundle)

    result = certify_bundle(bundle, workflow_id="wf-1")
    persisted = Workflow.load(bundle)

    assert result["certification_attempt"]["passed"] is False
    assert result["certification_current"] is False
    assert {
        refusal["code"] for refusal in result["certification_attempt"]["refusals"]
    } >= {"representative_case_missing", "case_not_passed"}
    assert persisted.qualification.last_certification is not None
    assert persisted.qualification.last_certification.passed is False
    assert persisted.manifest.provenance.certified is False


def test_dispatcher_initialization_persists_bundle_status_and_contract(
    tmp_path: Path,
) -> None:
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    bundle = _bundle(
        config.data_dir / "bundles" / "wf-1",
        Step(id="settle", intent="Wait", action=ActionKind.WAIT),
    )
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    db.insert_bundle("wf-1", str(bundle))
    dispatcher = EngineDispatcher(config, services=EngineServices(config, db=db))
    try:
        result = dispatcher.initialize_qualification(
            workflow_id="wf-1",
            target_kind="rdp",
            application="Legacy app",
            application_version="5",
            environment_label="test-rdp-session",
            required_capabilities=["pixel_observation", "session_continuity"],
            minimum_effect_tier=3,
        )
        row = db.get_bundle("wf-1")
    finally:
        db.close()

    assert result["ok"] is True
    assert result["project"]["environment"]["target_kind"] == "rdp"
    assert row["status"] == "qualification_pending"


def test_dispatcher_round_trips_editable_identity_and_effect_contracts(
    tmp_path: Path,
) -> None:
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    bundle = _bundle(
        config.data_dir / "bundles" / "wf-1",
        Step(
            id="save",
            intent="Save encounter",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="save.png",
                region=(0, 0, 20, 20),
                click_point=(10, 10),
                structured_identity="patient_id P-42",
            ),
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={"patient_id": "P-42"},
                )
            ],
        ),
    )
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    db.insert_bundle("wf-1", str(bundle))
    dispatcher = EngineDispatcher(config, services=EngineServices(config, db=db))
    try:
        initialized = dispatcher.dispatch(
            "initialize_qualification",
            {
                "workflow_id": "wf-1",
                "target_kind": "rdp",
                "application": "Reference app",
                "application_version": "1",
                "environment_label": "test-rdp-session",
                "minimum_effect_tier": 3,
            },
        )
        identity = dispatcher.dispatch(
            "set_qualification_identity",
            {
                "workflow_id": "wf-1",
                "step_id": "save",
                "enforcement": "signal_quorum",
                "signals": [
                    {
                        "key": "record_id",
                        "source": "structured",
                        "match": "normalized",
                        "normalizers": ["unicode_nfkc", "casefold"],
                        "extract_pattern": r"patient_id (?P<value>[A-Z0-9-]+)",
                    }
                ],
                "quorum": 1,
            },
        )
        effect = dispatcher.dispatch(
            "set_qualification_effect_verification",
            {
                "workflow_id": "wf-1",
                "step_id": "save",
                "effect_index": 0,
                "verification_tier": 2,
            },
        )
        minimum = dispatcher.dispatch(
            "set_qualification_minimum_effect_tier",
            {
                "workflow_id": "wf-1",
                "minimum_effect_tier": 2,
            },
        )
        row = db.get_bundle("wf-1")
    finally:
        db.close()

    assert initialized["ok"] is True
    assert identity["controls"]["actions"]["save"]["identity"]["policy"]["signals"][0][
        "normalizers"
    ] == ["unicode_nfkc", "casefold"]
    assert effect["controls"]["actions"]["save"]["effects"][0]["verification_tier"] == 2
    assert minimum["project"]["minimum_effect_tier"] == 2
    assert minimum["project"]["revision"] == initialized["project"]["revision"] + 3
    assert row["status"] == "qualification_pending"


def test_dispatcher_versions_exact_bundle_without_mutating_source(tmp_path: Path) -> None:
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    bundle = _bundle(
        config.data_dir / "bundles" / "wf-1",
        Step(id="settle", intent="Wait", action=ActionKind.WAIT),
    )
    original = (bundle / "workflow.json").read_bytes()
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    db.insert_bundle("wf-1", str(bundle))
    db.update_bundle("wf-1", workflow_name="Versioned workflow", version=1, steps=1)
    dispatcher = EngineDispatcher(config, services=EngineServices(config, db=db))
    try:
        result = dispatcher.dispatch(
            "version_qualification_workflow", {"workflow_id": "wf-1"}
        )
        version = db.get_bundle(result["workflow_id"])
    finally:
        db.close()

    assert result["ok"] is True
    assert result["workflow_id"] != "wf-1"
    assert version is not None
    assert version["version"] == 2
    assert Path(version["bundle_path"]).joinpath("workflow.json").read_bytes() == original
    assert (bundle / "workflow.json").read_bytes() == original
