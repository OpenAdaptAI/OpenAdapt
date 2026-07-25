from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

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
from openadapt_flow.runtime.effects.effect import Effect, EffectKind  # noqa: E402
from openadapt_flow.traversal import iter_workflow_steps  # noqa: E402

from engine.config import EngineConfig
from engine.db import IndexDB
from engine.dispatch import EngineDispatcher, EngineServices
from engine.qualification import (
    QualificationError,
    arm_action_identity,
    bind_action_effect,
    certify_bundle,
    environment_digest_from_identifier,
    initialize_qualification,
    inspect_bundle,
    set_action_effect_verification,
    set_action_identity_policy,
    set_action_risk,
    set_project_minimum_effect_tier,
)


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
                "field": "patient_id",
                "source": "structured",
                "match": "exact",
                "normalizers": [],
            },
            {
                "field": "patient_banner",
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
    assert [signal.field for signal in policy.signals] == [
        "patient_id",
        "patient_banner",
    ]
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
    assert "identity_policy_unenforced" in {
        refusal["code"] for refusal in result["report"]["refusals"]
    }


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
                    "field": "patient_banner",
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
                        "field": "patient_id",
                        "source": "structured",
                        "match": "normalized",
                        "normalizers": ["unicode_nfkc", "casefold"],
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
