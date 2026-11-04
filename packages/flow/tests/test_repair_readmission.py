"""Re-qualify a repaired bundle and re-admit it with signed lineage.

openadapt_flow/repair/readmission.py. A repair's certified copy becomes the
next pinned version only when it passed its parent's campaign under the same
policy and cases, kept every consequential step's write target, effects,
identity rule and record check, and carries a lineage record signed by the
local admission key. A pin to the parent accepts it only through a chain of
such records: at most three hops, no cycle, no revoked key, every bundle
staged. Anything else refuses and never falls back.

The bundles are made-up; the campaign runner is a stand-in that certifies a
copy the way ``openadapt-flow qualify`` does.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.policy import load_policy
from openadapt_flow.qualification import certify_project, save_qualified_workflow
from openadapt_flow.repair import readmission
from openadapt_flow.repair.readmission import (
    MAX_HOPS,
    ReadmissionRefused,
    Requalification,
    admit,
    fixed_contract_refusal,
    lineage_refusal,
    record_path,
    requalify,
)
from openadapt_flow.verification import VerificationTier
from tests.test_qualification_project import (
    _configure,
    _qualification_visual_fixture,
    _record_passing_campaign,
    _workflow,
)


def _certified(bundle: Path, *, label: str = "Save", field: str = "note") -> Path:
    """A certified bundle whose save button reads ``label``."""

    workflow = _workflow()
    save = workflow.steps[0]
    anchor = save.anchor.model_copy(update={"ocr_text": label})
    effect = save.effects[0].model_copy(update={"field": field})
    workflow.steps[0] = save.model_copy(update={"anchor": anchor, "effects": [effect]})
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence = bundle.parent / f"{bundle.name}-evidence"
    _record_passing_campaign(workflow, evidence)
    assert certify_project(
        workflow, policy=load_policy("clinical-write"), evidence_root=evidence
    ).passed
    save_qualified_workflow(workflow, bundle)
    return bundle


def _candidate(parent: Path, out: Path, *, label: str) -> Path:
    """The unqualified repair: the parent with its save button relabelled."""

    shutil.copytree(parent, out)
    workflow = Workflow.load(out)
    save = workflow.steps[0]
    workflow.steps[0] = save.model_copy(
        update={"anchor": save.anchor.model_copy(update={"ocr_text": label})}
    )
    # As self-repair writes a patch: uncertified until re-qualified.
    from openadapt_flow.repair.self_repair import _uncertify_patch

    _uncertify_patch(workflow)
    workflow.save(out)
    return out


class Campaign:
    """Stands in for ``qualify``: certifies a copy of the candidate."""

    def __init__(self, *, passes: bool = True, field: str = "note") -> None:
        self.passes, self.field, self.calls = passes, field, []

    def __call__(
        self, candidate, config, *, out, cases, trials, certify, hooks, run_id_prefix
    ):
        self.calls.append(
            {
                "cases": cases,
                "certify": certify,
                "trials": trials,
                "run_id_prefix": run_id_prefix,
            }
        )
        if not self.passes:
            raise RuntimeError("the representative case halted")
        label = Workflow.load(candidate).steps[0].anchor.ocr_text
        out.mkdir(parents=True)
        bundle = _certified(out / "bundle", label=label, field=self.field)
        return SimpleNamespace(
            passed=True, certified=True, bundle_dir=bundle, result={"verdict": "pass"}
        )


@pytest.fixture
def keys(tmp_path) -> Path:
    from openadapt_flow.execute.keys import load_or_create_private_key

    load_or_create_private_key(tmp_path / "admission")
    return tmp_path / "admission"


def _digest(bundle: Path) -> str:
    manifest = Workflow.load(bundle).manifest
    assert manifest is not None
    return manifest.content_digest


def _stage(store: Path, *bundles: Path) -> None:
    for bundle in bundles:
        target = store / "bundles" / _digest(bundle)
        if not target.exists():
            shutil.copytree(bundle, target)


def _repair(
    tmp_path: Path,
    store: Path,
    parent: Path,
    keys: Path,
    *,
    label: str,
    campaign: Optional[Campaign] = None,
) -> tuple[Any, dict[str, Any]]:
    candidate = _candidate(parent, tmp_path / f"candidate-{label}", label=label)
    requalified = requalify(
        parent,
        candidate,
        tmp_path / f"requalify-{label}",
        Requalification(config=None, run=campaign or Campaign()),
    )
    _stage(store, parent, requalified.bundle_dir)
    record = admit(
        store,
        parent_bundle=parent,
        candidate_digest=_digest(candidate),
        candidate_id=f"candidate-{label}",
        requalified=requalified,
        approved_by="clinic.lead@example.com",
        route="phone",
        key_dir=keys,
    )
    return requalified, record


def test_a_repair_is_requalified_and_readmitted_with_lineage(tmp_path, keys):
    store = tmp_path / "store"
    root = _certified(tmp_path / "root")
    campaign = Campaign()
    requalified, record = _repair(
        tmp_path, store, root, keys, label="Save record", campaign=campaign
    )

    # The parent's own campaign: same policy, same cases, under fresh run ids
    # (each case's authorization id is its run id, consumed once per process).
    [call] = campaign.calls
    assert call.pop("run_id_prefix").startswith("requalify-")
    assert call == {
        "cases": list(requalified.case_ids),
        "certify": "clinical-write",
        "trials": 1,
    }
    assert requalified.digest != _digest(root)
    assert record["hop"] == 1 and record["parent_digest"] == _digest(root)
    assert record["policy_name"] == "clinical-write"
    assert record["parent_qualification_report_sha256"]
    assert record["qualification_report_sha256"]
    assert (
        lineage_refusal(store, _digest(root), requalified.digest, key_dir=keys) is None
    )


def test_a_repair_that_fails_requalification_is_refused(tmp_path, keys):
    root = _certified(tmp_path / "root")
    candidate = _candidate(root, tmp_path / "candidate", label="Save record")
    with pytest.raises(ReadmissionRefused, match="failed re-qualification"):
        requalify(
            root,
            candidate,
            tmp_path / "requalify",
            Requalification(config=None, run=Campaign(passes=False)),
        )


def test_a_repair_that_changes_what_is_written_needs_a_fresh_admission(tmp_path, keys):
    root = _certified(tmp_path / "root")
    candidate = _candidate(root, tmp_path / "candidate", label="Save record")
    with pytest.raises(ReadmissionRefused, match="fresh admission"):
        requalify(
            root,
            candidate,
            tmp_path / "requalify",
            Requalification(config=None, run=Campaign(field="other_field")),
        )


def test_the_fixed_contract_allows_only_on_screen_changes(tmp_path):
    root = Workflow.load(_certified(tmp_path / "root"))
    relabelled = Workflow.load(_certified(tmp_path / "relabelled", label="Save it"))
    other = Workflow.load(_certified(tmp_path / "other", field="other_field"))
    assert fixed_contract_refusal(root, relabelled) is None
    assert fixed_contract_refusal(root, other) is not None


def test_a_pin_accepts_only_a_verified_descendant(tmp_path, keys):
    from openadapt_flow.run_gate import _gate_manifest

    store = tmp_path / "store"
    root = _certified(tmp_path / "root")
    requalified, _ = _repair(tmp_path, store, root, keys, label="Save record")
    child = Workflow.load(requalified.bundle_dir)
    check = readmission.lineage_check(store, key_dir=keys)

    passed = _gate_manifest(child, requalified.bundle_dir, _digest(root), None, check)
    assert passed.passed, passed.detail
    assert "re-admitted with signed lineage" in passed.detail
    # Without the lineage, the pin refuses the new version.
    assert not _gate_manifest(child, requalified.bundle_dir, _digest(root), None).passed
    # A pin to some other bundle refuses it too.
    other = _certified(tmp_path / "other", label="Unrelated")
    assert not _gate_manifest(
        child, requalified.bundle_dir, _digest(other), None, check
    ).passed


def _record(store: Path, digest: str) -> dict[str, Any]:
    return json.loads(record_path(store, digest).read_text())


def test_a_tampered_record_refuses(tmp_path, keys):
    store = tmp_path / "store"
    root = _certified(tmp_path / "root")
    requalified, _ = _repair(tmp_path, store, root, keys, label="Save record")
    path = record_path(store, requalified.digest)
    record = _record(store, requalified.digest)
    record["approved_by"] = "someone.else@example.com"
    path.write_text(json.dumps(record))
    refusal = lineage_refusal(store, _digest(root), requalified.digest, key_dir=keys)
    assert refusal is not None and "does not verify" in refusal


def test_a_record_signed_by_another_key_refuses(tmp_path, keys):
    from openadapt_flow.execute.keys import load_or_create_private_key

    store = tmp_path / "store"
    root = _certified(tmp_path / "root")
    requalified, _ = _repair(tmp_path, store, root, keys, label="Save record")
    other = tmp_path / "other-admission"
    load_or_create_private_key(other)
    refusal = lineage_refusal(store, _digest(root), requalified.digest, key_dir=other)
    assert refusal is not None and "does not verify" in refusal


def test_a_revoked_key_invalidates_its_descendants(tmp_path, keys):
    store = tmp_path / "store"
    root = _certified(tmp_path / "root")
    requalified, record = _repair(tmp_path, store, root, keys, label="Save record")
    (store / "readmissions" / "revoked-keys.json").write_text(
        json.dumps([record["signing_key"]])
    )
    refusal = lineage_refusal(store, _digest(root), requalified.digest, key_dir=keys)
    assert refusal is not None and "revoked" in refusal
    # An unreadable revocation list revokes everything.
    (store / "readmissions" / "revoked-keys.json").write_text("{not json")
    assert lineage_refusal(store, _digest(root), requalified.digest, key_dir=keys)


def test_the_chain_stops_after_three_repairs(tmp_path, keys):
    store = tmp_path / "store"
    root = _certified(tmp_path / "root")
    parent = root
    for hop in range(1, MAX_HOPS + 1):
        requalified, record = _repair(
            tmp_path, store, parent, keys, label=f"Save {hop}"
        )
        assert record["hop"] == hop
        assert (
            lineage_refusal(store, _digest(root), requalified.digest, key_dir=keys)
            is None
        )
        parent = requalified.bundle_dir
    with pytest.raises(ReadmissionRefused, match="fresh admission"):
        _repair(tmp_path, store, parent, keys, label="Save again")


def test_a_cycle_refuses(tmp_path, keys):
    from openadapt_flow.execute.keys import load_or_create_private_key, sign_bytes

    store = tmp_path / "store"
    root = _certified(tmp_path / "root")
    requalified, record = _repair(tmp_path, store, root, keys, label="Save record")
    # A forged record that makes the root a "repair" of its own child.
    body = {
        **{k: v for k, v in record.items() if k != "signature"},
        "digest": _digest(root),
        "parent_digest": requalified.digest,
        "hop": 2,
        "parent_record_sha256": None,
    }
    private = load_or_create_private_key(keys)
    record_path(store, _digest(root)).write_text(
        json.dumps(
            {**body, "signature": sign_bytes(private, readmission._canonical(body))}
        )
    )
    other = _certified(tmp_path / "other", label="Unrelated")
    refusal = lineage_refusal(store, _digest(other), requalified.digest, key_dir=keys)
    assert refusal is not None


def test_a_chain_with_a_missing_bundle_refuses(tmp_path, keys):
    store = tmp_path / "store"
    root = _certified(tmp_path / "root")
    requalified, _ = _repair(tmp_path, store, root, keys, label="Save record")
    shutil.rmtree(store / "bundles" / _digest(root))
    refusal = lineage_refusal(store, _digest(root), requalified.digest, key_dir=keys)
    assert refusal is not None and "not staged" in refusal


def test_an_uncertified_parent_cant_be_requalified(tmp_path):
    workflow = _workflow()
    bundle = tmp_path / "plain"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    with pytest.raises(ReadmissionRefused, match="not certified"):
        requalify(bundle, bundle, tmp_path / "out", Requalification(config=None))


# -- wired into self-repair ----------------------------------------------------------


def _self_repair(tmp_path: Path, keys: Path, campaign: Campaign) -> Any:
    from openadapt_flow.repair.self_repair import ScreenAccess, SelfRepair

    agent = SimpleNamespace(
        dialect="anthropic",
        agent_id="test-agent",
        requires_model_key=False,
        propose=lambda request: None,
    )
    return SelfRepair(
        agent,
        target=None,
        store=tmp_path / "store",
        screen_access=ScreenAccess(enabled_by="ops-lead"),
        requalification=Requalification(config=None, run=campaign),
        admission_key_dir=keys,
    )


def _patch(tmp_path: Path, root: Path) -> Any:
    from openadapt_flow.repair.registration import build_candidate
    from openadapt_flow.repair.self_repair import _Patch

    candidate_dir = _candidate(root, tmp_path / "candidate", label="Save record")
    workflow = Workflow.load(candidate_dir)
    return _Patch(
        attempt=1,
        step=workflow.steps[0],
        workflow=workflow,
        bundle_dir=candidate_dir,
        evidence_dir=tmp_path / "evidence",
        candidate=build_candidate(root, candidate_dir, source="model_suggestion"),
        target_region=(0, 0, 10, 10),
    )


def test_self_repair_runs_the_certified_copy_not_the_candidate(tmp_path, keys):
    root = _certified(tmp_path / "root")
    patch = _patch(tmp_path, root)
    repair = _self_repair(tmp_path, keys, Campaign())

    requalified_patch, requalified = repair._requalify(root, patch, tmp_path / "work")

    assert requalified_patch.bundle_dir == requalified.bundle_dir
    candidate = requalified_patch.candidate
    assert candidate.prior_content_digest == _digest(root)
    assert candidate.proposed_content_digest == requalified.digest
    assert requalified.candidate_digest == patch.candidate.proposed_content_digest
    assert candidate.state != "rejected"


def test_a_fix_that_fails_requalification_saves_nothing(tmp_path, keys):
    from openadapt_flow.ir import RunReport, SelfRepairEvidence

    root = _certified(tmp_path / "root")
    patch = _patch(tmp_path, root)
    repair = _self_repair(tmp_path, keys, Campaign(passes=False))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = repair._govern_and_resume(
        Workflow.load(root),
        root,
        patch,
        probe=None,
        halted_report=RunReport(
            workflow_name="qualified-write", started_at="2026-10-20T00:00:00+00:00"
        ),
        run_dir=run_dir,
        work=run_dir / "self-repair",
        evidence=SelfRepairEvidence(status="not_needed"),
    )

    assert result.status == "no_fix"
    assert "re-qualification" in result.evidence.reason
    assert result.evidence.steps[-1].stage == "requalify"
    # Nothing entered the repair store, and the pinned bundle is untouched.
    assert repair.store.list_candidates() == []
    assert not (tmp_path / "store" / "readmissions").exists()
    assert Workflow.load(root).manifest.content_digest == _digest(root)


def test_the_readmission_step_signs_lineage_after_approval(tmp_path, keys):
    from openadapt_flow.ir import SelfRepairEvidence

    store = tmp_path / "store"
    root = _certified(tmp_path / "root")
    patch = _patch(tmp_path, root)
    repair = _self_repair(tmp_path, keys, Campaign())
    patch, requalified = repair._requalify(root, patch, tmp_path / "work")
    _stage(store, root, requalified.bundle_dir)
    evidence = SelfRepairEvidence(status="not_needed")

    repair._readmit(
        root,
        patch.candidate,
        requalified,
        approved_by="clinic.lead@example.com",
        route="phone",
        evidence=evidence,
    )

    assert evidence.readmitted_bundle_digest == requalified.digest
    assert evidence.lineage_hop == 1
    assert evidence.steps[-1].stage == "readmit"
    assert (
        lineage_refusal(store, _digest(root), requalified.digest, key_dir=keys) is None
    )


def test_each_requalification_names_its_runs_afresh(tmp_path, keys):
    # A process that qualified the parent refuses its run ids again, so two
    # re-qualifications must never share a run-id prefix.
    root = _certified(tmp_path / "root")
    campaign = Campaign()
    for label in ("Save record", "Store record"):
        _repair(
            tmp_path,
            tmp_path / f"store-{label}",
            root,
            keys,
            label=label,
            campaign=campaign,
        )
    prefixes = [call["run_id_prefix"] for call in campaign.calls]
    assert len(set(prefixes)) == 2
