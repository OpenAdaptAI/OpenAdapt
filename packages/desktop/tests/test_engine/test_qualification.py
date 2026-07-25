from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("openadapt_flow")

from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    Anchor,
    ParamSpec,
    Step,
    Workflow,
    lift_to_program,
)
from openadapt_flow.runtime.effects.effect import (  # noqa: E402
    Effect,
    EffectKind,
)
from openadapt_flow.traversal import iter_workflow_steps  # noqa: E402

from engine.config import EngineConfig
from engine.db import IndexDB
from engine.dispatch import EngineDispatcher, EngineServices
from engine.qualification import (
    QualificationError,
    arm_action_identity,
    bind_action_effect,
    certify_bundle,
    inspect_bundle,
    set_action_risk,
)


def _bundle(path: Path, *steps: Step) -> Path:
    workflow = Workflow(name="qualification-test", steps=list(steps))
    workflow.save(path)
    return path


def test_ambiguous_step_id_refuses_without_changing_bundle(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="duplicate", intent="Wait once", action=ActionKind.WAIT),
        Step(id="duplicate", intent="Wait twice", action=ActionKind.WAIT),
    )
    before = {
        path.relative_to(bundle): path.read_bytes() for path in bundle.rglob("*") if path.is_file()
    }

    with pytest.raises(QualificationError, match="ambiguous"):
        set_action_risk(
            bundle,
            workflow_id="wf-1",
            step_id="duplicate",
            risk="irreversible",
        )

    after = {
        path.relative_to(bundle): path.read_bytes() for path in bundle.rglob("*") if path.is_file()
    }
    assert after == before


def test_risk_change_invalidates_certification_and_reseals(tmp_path: Path) -> None:
    step = Step(id="review", intent="Review state", action=ActionKind.WAIT)
    linear = Workflow(name="qualification-program-test", steps=[step])
    workflow = linear.model_copy(
        update={
            "steps": [],
            "program": lift_to_program(linear),
        },
    )
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    workflow.stamp_certification("permissive", True)
    workflow.save(bundle)
    previous_digest = Workflow.load(bundle).manifest.content_digest
    project = inspect_bundle(bundle, workflow_id="wf-1")
    action_ref = next(node["id"] for node in project["graph"]["nodes"] if node["kind"] == "action")

    result = set_action_risk(
        bundle,
        workflow_id="wf-1",
        step_id=action_ref,
        risk="irreversible",
    )

    persisted = Workflow.load(bundle)
    assert [step.risk for step in iter_workflow_steps(persisted)] == ["irreversible"]
    assert persisted.manifest.content_digest != previous_digest
    assert persisted.manifest.provenance.certified is False
    assert persisted.manifest.provenance.policy_name is None
    assert persisted.manifest.provenance.certification_status is None
    assert result["graph"]["bundle"]["provenance"]["content_digest"] == (
        persisted.manifest.content_digest
    )


def test_risk_change_keeps_bound_effects_on_the_reviewed_classification(
    tmp_path: Path,
) -> None:
    step = Step(
        id="save",
        intent="Save encounter",
        action=ActionKind.CLICK,
        risk="reversible",
        effects=[
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"patient_id": "P-42"},
                risk="reversible",
            )
        ],
    )
    bundle = _bundle(tmp_path / "bundle", step)

    set_action_risk(
        bundle,
        workflow_id="wf-risk",
        step_id="save",
        risk="irreversible",
    )

    persisted = next(iter_workflow_steps(Workflow.load(bundle)))
    assert persisted.risk == "irreversible"
    assert [effect.risk for effect in persisted.effects] == ["irreversible"]


def test_successful_certification_persists_exact_provenance(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="settle", intent="Wait for settled state", action=ActionKind.WAIT),
    )

    result = certify_bundle(
        bundle,
        workflow_id="wf-1",
        policy_source="clinical-write",
    )

    persisted = Workflow.load(bundle)
    provenance = persisted.manifest.provenance
    assert result["certification_attempt"]["passed"] is True
    assert provenance.certified is True
    assert provenance.policy_name == "clinical-write"
    assert provenance.certification_status == "certified"
    assert provenance.certified_at
    assert result["graph"]["bundle"]["provenance"]["content_digest"] == (
        persisted.manifest.content_digest
    )


def test_sealed_certification_must_match_live_policy(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path / "bundle",
        Step(id="settle", intent="Wait for settled state", action=ActionKind.WAIT),
    )
    workflow = Workflow.load(bundle)
    workflow.stamp_certification("permissive", True)
    workflow.save(bundle)

    result = inspect_bundle(
        bundle,
        workflow_id="wf-1",
        policy_source="clinical-write",
    )

    assert result["certification"]["passed"] is True
    assert result["provenance"]["certified"] is True
    assert result["certification_current"] is False


def test_symlinked_bundle_alias_is_not_writable(tmp_path: Path) -> None:
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    bundle_root = config.data_dir / "bundles"
    target = _bundle(
        bundle_root / "target",
        Step(id="settle", intent="Wait for settled state", action=ActionKind.WAIT),
    )
    alias = bundle_root / "alias"
    alias.symlink_to(target, target_is_directory=True)
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    db.insert_bundle("wf-1", str(alias))
    dispatcher = EngineDispatcher(config, services=EngineServices(config, db=db))
    try:
        result = dispatcher.get_qualification(
            workflow_id="wf-1",
            policy="clinical-write",
        )
    finally:
        db.close()

    assert result["ok"] is False
    assert "symbolic link" in result["error"]


def test_identity_arming_uses_retained_flow_evidence_and_invalidates_certification(
    tmp_path: Path,
) -> None:
    step = Step(
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
    )
    bundle = _bundle(tmp_path / "bundle", step)
    workflow = Workflow.load(bundle)
    workflow.stamp_certification("permissive", True)
    workflow.save(bundle)
    old_digest = Workflow.load(bundle).manifest.content_digest

    result = arm_action_identity(
        bundle,
        workflow_id="wf-identity",
        step_id="save",
    )

    persisted = Workflow.load(bundle)
    armed = next(iter_workflow_steps(persisted))
    assert armed.identity_armed is True
    assert armed.identity_unarmed_reason is None
    assert persisted.manifest.content_digest != old_digest
    assert persisted.manifest.provenance.certified is False
    controls = result["controls"]["actions"]["save"]["identity"]
    assert controls["armed"] is True
    assert [source["kind"] for source in controls["sources"]] == ["structured"]


def test_identity_arming_refuses_when_no_runtime_identity_source_exists(
    tmp_path: Path,
) -> None:
    step = Step(
        id="save",
        intent="Save encounter",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="save.png",
            region=(0, 0, 20, 20),
            click_point=(10, 10),
        ),
        identity_armed=False,
        identity_unarmed_reason="no retained identity evidence",
    )
    bundle = _bundle(tmp_path / "bundle", step)
    before = (bundle / "workflow.json").read_bytes()

    with pytest.raises(QualificationError, match="no retained structured identity"):
        arm_action_identity(
            bundle,
            workflow_id="wf-identity",
            step_id="save",
        )

    assert (bundle / "workflow.json").read_bytes() == before


def test_effect_binding_replaces_placeholder_with_parameterized_flow_contract(
    tmp_path: Path,
) -> None:
    step = Step(
        id="save",
        intent="Save encounter",
        action=ActionKind.CLICK,
        risk="irreversible",
        effects=[
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"__unbound__": "__operator_required__"},
                risk="irreversible",
                needs_operator_confirmation=True,
            )
        ],
    )
    workflow = Workflow(
        name="qualification-test",
        params={
            "patient_id": "P-42",
            "request_id": "req-1",
        },
        param_specs={
            "patient_id": ParamSpec(name="patient_id"),
            "request_id": ParamSpec(name="request_id"),
        },
        steps=[step],
    )
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    loaded = Workflow.load(bundle)
    loaded.stamp_certification("permissive", True)
    loaded.save(bundle)
    old_digest = Workflow.load(bundle).manifest.content_digest

    result = bind_action_effect(
        bundle,
        workflow_id="wf-effect",
        step_id="save",
        kind="record_written",
        match_field="patient_id",
        match_param="patient_id",
        idempotency_param="request_id",
        key_field="request_id",
        count_new_only=True,
    )

    persisted = Workflow.load(bundle)
    bound = next(iter_workflow_steps(persisted)).effects
    assert len(bound) == 1
    assert bound[0].kind is EffectKind.RECORD_WRITTEN
    assert bound[0].match["patient_id"].param == "patient_id"
    assert bound[0].idempotency_key is not None
    assert bound[0].idempotency_key.param == "request_id"
    assert bound[0].count_new_only is True
    assert bound[0].needs_operator_confirmation is False
    assert persisted.manifest.content_digest != old_digest
    assert persisted.manifest.provenance.certified is False
    controls = result["controls"]["actions"]["save"]["effects"]
    assert controls[0]["match"]["patient_id"] == {
        "source": "parameter",
        "value": "patient_id",
    }


def test_effect_binding_refuses_unknown_parameter_without_changing_bundle(
    tmp_path: Path,
) -> None:
    workflow = Workflow(
        name="qualification-test",
        params={"patient_id": "P-42"},
        steps=[
            Step(
                id="save",
                intent="Save encounter",
                action=ActionKind.CLICK,
                risk="irreversible",
            )
        ],
    )
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    before = (bundle / "workflow.json").read_bytes()

    with pytest.raises(QualificationError, match="unknown workflow parameter"):
        bind_action_effect(
            bundle,
            workflow_id="wf-effect",
            step_id="save",
            kind="record_written",
            match_field="patient_id",
            match_param="missing",
        )

    assert (bundle / "workflow.json").read_bytes() == before
