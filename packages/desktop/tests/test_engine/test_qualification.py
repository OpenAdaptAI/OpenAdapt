from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("openadapt_flow")

from openadapt_flow.ir import ActionKind, Step, Workflow, lift_to_program  # noqa: E402
from openadapt_flow.traversal import iter_workflow_steps  # noqa: E402

from engine.config import EngineConfig
from engine.db import IndexDB
from engine.dispatch import EngineDispatcher, EngineServices
from engine.qualification import (
    QualificationError,
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
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    }

    with pytest.raises(QualificationError, match="ambiguous"):
        set_action_risk(
            bundle,
            workflow_id="wf-1",
            step_id="duplicate",
            risk="irreversible",
        )

    after = {
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
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
    action_ref = next(
        node["id"] for node in project["graph"]["nodes"] if node["kind"] == "action"
    )

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
