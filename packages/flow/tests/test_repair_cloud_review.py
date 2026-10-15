"""Cloud repair review bridge: digest-only projection, decisions never skip gates."""

from __future__ import annotations

import json
import re

import pytest

from openadapt_flow.repair.candidate import Actor
from openadapt_flow.repair.cloud_review import apply_cloud_decision, ingest_projection
from openadapt_flow.repair.lifecycle import RepairLifecycleError, RepairStore
from tests.test_repair_lifecycle import bundles, registered  # noqa: F401

WORKFLOW = "0b5c5f1e-8a0d-4c4e-9d3f-2a1b3c4d5e6f"
SHA = re.compile(r"^[a-f0-9]{64}$")


def _decision(candidate, decision="approve", **over):
    return {
        "schema": "openadapt.repair-decision/v1",
        "candidate_id": candidate.candidate_id,
        "decision": decision,
        "prior_content_digest": candidate.prior_content_digest,
        "proposed_content_digest": candidate.proposed_content_digest,
        "decided_by": "user-1234",
        **over,
    }


def test_projection_is_digest_only_and_closed(bundles, tmp_path):  # noqa: F811
    prior, proposed, run_dir = bundles
    candidate = registered(RepairStore(tmp_path / "s"), prior, proposed, run_dir)
    body = ingest_projection(candidate, workflow_id=WORKFLOW)
    assert set(body) == {
        "schema",
        "candidate_id",
        "workflow_id",
        "run_id",
        "source",
        "prior_content_digest",
        "proposed_content_digest",
        "consequential",
        "binding_changes",
        "failure_fingerprints",
    }
    raw = json.dumps(body)
    assert "Submit Encounter" not in raw and str(tmp_path) not in raw
    for change in body["binding_changes"]:
        for key in ("old_sha256", "new_sha256"):
            assert change[key] is None or SHA.match(change[key])
    assert isinstance(body["consequential"], bool)


def test_unknown_risk_is_consequential(bundles, tmp_path):  # noqa: F811
    prior, proposed, run_dir = bundles
    candidate = registered(RepairStore(tmp_path / "s"), prior, proposed, run_dir)
    missing = candidate.model_copy(update={"proposed_bundle_path": None})
    assert ingest_projection(missing, workflow_id=WORKFLOW)["consequential"] is True


def test_cloud_approval_is_only_the_review_gate(bundles, tmp_path):  # noqa: F811
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "s")
    candidate = registered(store, prior, proposed, run_dir)
    reviewed = apply_cloud_decision(store, _decision(candidate))
    assert reviewed.state == "reviewed"
    assert reviewed.reviewed_by == "cloud:user-1234"
    # Approving again never skips replay/fault/approve/canary.
    assert apply_cloud_decision(store, _decision(candidate)).state == "reviewed"
    with pytest.raises(RepairLifecycleError):
        store.approve(candidate.candidate_id, Actor(kind="human", id="x"))
    assert store.active_pointer() is None


def test_cloud_rejection_and_refusals(bundles, tmp_path):  # noqa: F811
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "s")
    candidate = registered(store, prior, proposed, run_dir)
    with pytest.raises(RepairLifecycleError, match="different bundle bytes"):
        apply_cloud_decision(
            store, _decision(candidate, proposed_content_digest="f" * 64)
        )
    with pytest.raises(RepairLifecycleError, match="name the person"):
        apply_cloud_decision(store, _decision(candidate, decided_by=""))
    with pytest.raises(RepairLifecycleError, match="schema"):
        apply_cloud_decision(store, _decision(candidate, schema="x"))
    assert (
        apply_cloud_decision(store, _decision(candidate, "reject")).state == "rejected"
    )
