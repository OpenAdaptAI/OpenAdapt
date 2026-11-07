"""Promoted repair selection models; no browser or calling model is run.

Store/candidate/bundle/report models are real. Admission signing and the
canary receipt are separate component boundaries in selection mutations;
the final tests exercise the ordinary receipt validator itself.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.compile_agent import repair_selection as selection
from openadapt_flow.ir import ActionKind, Anchor, RunReport, Step, Workflow
from openadapt_flow.jit import lifecycle
from openadapt_flow.repair.candidate import (
    ApprovalRecord,
    CanaryMetrics,
    RepairProvenance,
    TaughtStep,
)
from openadapt_flow.repair.lifecycle import ActivePointer, RepairStore
from openadapt_flow.repair.registration import build_candidate
from tests.test_skill_cache_runtime import (
    PROMPT,  # noqa: F401 -- shared component fixture
    model,
    prepare_repair,
)


@pytest.fixture
def promoted(model, monkeypatch):
    desk, halted = prepare_repair(model, monkeypatch)
    prior = Workflow.load(model.jit.bundle_dir)
    prior.params = {"value": ""}
    prior.save(model.jit.bundle_dir)
    # Update the actual halt to this fixture's full parameter schema.
    digest = Workflow.load(model.jit.bundle_dir).manifest.content_digest
    report = RunReport.model_validate_json((halted / "report.json").read_text())
    report.bundle_content_digest = digest
    (halted / "report.json").write_text(report.model_dump_json())
    stop = model.compiler.dir / "skill-stop.json"
    stopped = json.loads(stop.read_text())
    stopped["bundle_digest"] = digest
    stop.write_text(json.dumps(stopped))
    proposed = model.compiler.dir / "proposed"
    shutil.copytree(model.jit.bundle_dir, proposed)
    workflow = Workflow.load(proposed)
    workflow.steps.insert(
        0,
        Step(
            id="one_navigation",
            intent="Open the synthetic panel",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="target.png", region=(0, 0, 2, 2), click_point=(1, 1)
            ),
        ),
    )
    workflow.save(proposed)
    candidate = build_candidate(model.jit.bundle_dir, proposed, source="teach")
    assert candidate.state != "rejected"
    candidate.provenance = RepairProvenance(
        agent_id="agent:component",
        halted_run_id=halted.name,
        halted_step_id="save",
        repair_token_sha256="a" * 64,
        screen_access_enabled_by="Synthetic Operator",
    )
    candidate.taught_steps = [
        TaughtStep(step_id="one_navigation", frame_sha256="b" * 64)
    ]
    candidate.state = "active"
    candidate.reviewed_by = "Synthetic Reviewer"
    candidate.approval = ApprovalRecord(
        approved_by="Synthetic Reviewer",
        prior_content_digest=candidate.prior_content_digest,
        proposed_content_digest=candidate.proposed_content_digest,
    )
    # Actual campaign/canary execution is outside these selection models.
    from openadapt_flow.repair.candidate import CampaignResult

    candidate.replay_campaign = CampaignResult(
        campaign="replay", passed=True, total=1, failed=0
    )
    candidate.fault_campaign = CampaignResult(
        campaign="fault", passed=True, total=1, failed=0
    )
    candidate.canary_metrics = CanaryMetrics(runs_completed=5, verified_runs=5)
    store = RepairStore(desk.store_for(workflow.name))
    store.save_candidate(candidate)
    store._stage_bundle(model.jit.bundle_dir, candidate.prior_content_digest)
    target = store._stage_bundle(proposed, candidate.proposed_content_digest)
    pointer = ActivePointer(
        workflow_name=workflow.name,
        candidate_id=candidate.candidate_id,
        active_digest=candidate.proposed_content_digest,
        active_bundle=f"bundles/{candidate.proposed_content_digest}",
        prior_digest=candidate.prior_content_digest,
        mode="active",
        activated_at="2026-01-01T00:00:00Z",
    )
    store._write_pointer(pointer)
    key = model.cache.key(PROMPT, {"value": "one"})
    model.jit.task = SimpleNamespace(key="synthetic-task-key", params={"value": ""})
    model.jit.task_dir = model.compiler.dir
    model.jit.key_dir = None
    model.jit.policy = "clinical-write"

    def admission(path, **_):
        digest = Workflow.load(path).manifest.content_digest
        return {
            "digest": digest,
            "admission_mode": "repair",
            "task": {"key": model.jit.task.key},
            "policy": model.jit.policy,
            "repair_of": {
                "candidate_id": candidate.candidate_id,
                "prior_digest": candidate.prior_content_digest,
            },
        }

    monkeypatch.setattr(lifecycle, "verify_admission", admission)
    verified = model.jit.runs_dir / "verified-canary"
    verified.mkdir()
    proof = RunReport(
        workflow_name=workflow.name,
        started_at="2026-01-01T00:00:01Z",
        execution_outcome="VERIFIED",
        transaction_outcome="VERIFIED",
        bundle_content_digest=candidate.proposed_content_digest,
        params={"value": "one"},
    )
    (verified / "report.json").write_text(proof.model_dump_json())
    receipt = model.compiler.dir / "receipts" / "canary" / "effect-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}")
    monkeypatch.setattr(selection, "_proof", lambda *_: proof)
    selected = dict(
        version=1,
        store=str(store.root.resolve()),
        candidate_id=candidate.candidate_id,
        prior_digest=candidate.prior_content_digest,
        halted_run=halted.name,
        step_id="save",
        verified_run=verified.name,
        receipt="canary/effect-receipt.json",
    )
    model.rows.append({"id": "canary", "kind": "synthetic", "value": "one"})
    return SimpleNamespace(
        model=model,
        desk=desk,
        store=store,
        candidate=candidate,
        selected=selected,
        target=target,
        proof=proof,
        verified=verified,
        receipt=receipt,
        key=key,
    )


def test_explicit_adoption_selects_existing_store_bundle_and_retains_halt(promoted):
    p = promoted
    result = p.model.cache.adopt_repair(
        PROMPT,
        {"value": "one"},
        repair_desk=p.desk,
        candidate_id=p.candidate.candidate_id,
        verified_run_dir=p.verified,
        receipt=p.receipt,
    )
    assert result == p.target
    assert not (p.model.compiler.dir / "skill-stop.json").exists()
    assert (
        p.model.compiler.dir / f"skill-stop-repaired-{p.candidate.candidate_id}.json"
    ).is_file()
    assert selection.selected_bundle(p.model.jit) == p.target
    assert p.model.sent == []


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_pointer",
        "rolled_back",
        "canary",
        "wrong_candidate",
        "wrong_prior",
        "wrong_path",
        "unapproved",
        "regressed",
        "uncertain",
        "wrong_step",
        "foreign_halt",
        "two_steps",
        "other_task",
        "probe",
        "changed_bundle",
    ],
)
def test_every_selected_use_revalidates_normal_promotion(
    promoted, monkeypatch, mutation
):
    p = promoted
    assert selection.validate_selection(p.model.jit, p.selected) == p.target
    pointer = p.store.active_pointer()
    if mutation == "missing_pointer":
        p.store.active_path.unlink()
    elif mutation == "rolled_back":
        p.candidate.state = "rolled_back"
    elif mutation == "canary":
        pointer.mode = "canary"
        p.store._write_pointer(pointer)
    elif mutation == "wrong_candidate":
        pointer.candidate_id = "another-candidate"
        p.store._write_pointer(pointer)
    elif mutation == "wrong_prior":
        p.candidate.approval.prior_content_digest = "f" * 64
    elif mutation == "wrong_path":
        pointer.active_bundle = "../proposed"
        p.store._write_pointer(pointer)
    elif mutation == "unapproved":
        p.candidate.approval = None
    elif mutation == "regressed":
        p.candidate.canary_metrics.silent_incorrect = 1
    elif mutation == "uncertain":
        report = RunReport.model_validate_json(
            (
                p.model.jit.runs_dir / p.selected["halted_run"] / "report.json"
            ).read_text()
        )
        report.transaction_outcome = "RECONCILIATION_REQUIRED"
        (p.model.jit.runs_dir / p.selected["halted_run"] / "report.json").write_text(
            report.model_dump_json()
        )
    elif mutation == "wrong_step":
        p.candidate.provenance.halted_step_id = "foreign"
    elif mutation == "foreign_halt":
        p.selected["halted_run"] = "../foreign"
    elif mutation == "two_steps":
        p.candidate.taught_steps.append(
            TaughtStep(step_id="second", frame_sha256="c" * 64)
        )
    elif mutation in ("other_task", "probe"):
        normal = lifecycle.verify_admission

        def changed(path, **kwargs):
            record = normal(path, **kwargs)
            if mutation == "other_task":
                record["task"] = {"key": "foreign"}
            else:
                record["admission_mode"] = "repair_probe"
            return record

        monkeypatch.setattr(lifecycle, "verify_admission", changed)
    else:
        (p.target / "target.png").write_bytes(b"changed")
    p.store.save_candidate(p.candidate)
    with pytest.raises(selection.RepairSelectionError):
        selection.validate_selection(p.model.jit, p.selected)
    assert (p.model.compiler.dir / "skill-stop.json").is_file()
    assert p.model.sent == []


def test_fresh_independent_read_required_at_adoption(promoted):
    from openadapt_flow.agent.repair import RepairError

    p = promoted
    p.model.rows[0]["value"] = "wrong"
    with pytest.raises(RepairError):
        p.model.cache.adopt_repair(
            PROMPT,
            {"value": "one"},
            repair_desk=p.desk,
            candidate_id=p.candidate.candidate_id,
            verified_run_dir=p.verified,
            receipt=p.receipt,
        )
    assert (p.model.compiler.dir / "skill-stop.json").is_file()
    assert not (p.model.compiler.dir / selection.SELECTION_FILE).exists()


def test_invalid_selection_is_a_hold_before_agent_or_tool(model, monkeypatch):
    def broken(*_):
        raise selection.RepairSelectionError("component boundary refusal")

    monkeypatch.setattr(model.cache, "_server", broken)
    result = model.cache.run(PROMPT, {"value": "one"}, object())
    assert result.status == "CACHE_HALTED" and not result.verified
    assert model.sent == []


def test_normal_receipt_proof_refuses_missing_before_any_factory(model):
    workflow = Workflow.load(model.jit.bundle_dir)
    model.jit.task_dir = model.compiler.dir
    with pytest.raises(FileNotFoundError):
        selection._proof(
            model.jit,
            {"verified_run": "missing"},
            workflow,
            SimpleNamespace(started_at="2026-01-01T00:00:00Z"),
        )


@pytest.mark.parametrize(
    "count,known", [(0, True), (None, False), (False, False), (1, False), (-1, False)]
)
def test_normal_replay_projects_only_observed_zero_usage(count, known):
    from openadapt_flow.compile_agent.pipeline import ReplayResult

    result = ReplayResult(
        status="HALTED", verified=False, model_calls=count, seconds=0, run_dir=None
    ).to_dict()
    assert result["usage_known"] is known
    assert result["usage"] == ([] if known else None)
    assert not result["verified"]


@pytest.mark.parametrize(
    "reply,expected",
    [
        ({"model_calls": 0}, (0, None, None, False)),
        ({"model_calls": 0, "usage": [], "usage_known": True}, (0, 0.0, [], True)),
        (
            {
                "model_calls": 1,
                "usage": [{"calls": 1, "usd": 0.02}],
                "usage_known": True,
            },
            (1, 0.02, [{"calls": 1, "usd": 0.02}], True),
        ),
        (
            {
                "model_calls": 1,
                "usage": [{"calls": 1, "usd": None}],
                "usage_known": False,
            },
            (1, None, [{"calls": 1, "usd": None}], False),
        ),
        (
            {
                "model_calls": 0,
                "usage": [{"calls": 1, "usd": 0.02}],
                "usage_known": True,
            },
            (0, None, [{"calls": 1, "usd": 0.02}], False),
        ),
    ],
)
def test_skill_usage_preserves_unknown_and_actual_repair_calls(reply, expected):
    from openadapt_flow.integrations.agents.skills import _skill_usage

    assert _skill_usage(reply) == expected


@pytest.mark.parametrize("error", [
    "integrity", "structure", "crypto", "missing_key", "decryption",
])
def test_native_bundle_load_refusals_remain_a_closed_cache_hold(promoted, monkeypatch, error):
    from openadapt_flow.bundle_validation import BundleIntegrityError, BundleValidationError, ValidationReport
    from openadapt_flow.crypto import CryptoError, MissingKeyError, DecryptionError

    failures = {
        "integrity": BundleIntegrityError("component corruption"),
        "structure": BundleValidationError(ValidationReport(workflow_name="component"), []),
        "crypto": CryptoError("component encrypted bundle"),
        "missing_key": MissingKeyError("component missing key"),
        "decryption": DecryptionError("component decryption refusal"),
    }
    failure = failures[error]

    def refused(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(Workflow, "load", refused)
    with pytest.raises(selection.RepairSelectionError) as caught:
        selection.validate_selection(promoted.model.jit, promoted.selected)
    assert caught.value.__cause__ is failure
    assert (promoted.model.compiler.dir / "skill-stop.json").is_file()
    assert promoted.model.sent == []
