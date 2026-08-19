"""Desktop consumption of Flow's exact openadapt.push-result/v1 contract."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from engine.qualification_lifecycle import (
    parse_flow_push,
    persist_deployment_handoff,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
WORKFLOW_ID = "123e4567-e89b-42d3-a456-426614174000"
INGEST_ID = "223e4567-e89b-42d3-a456-426614174000"
ORG_ID = "323e4567-e89b-42d3-a456-426614174000"
BUNDLE_VERSION_ID = "423e4567-e89b-42d3-a456-426614174000"
RUNTIME_VALIDATION_ID = "523e4567-e89b-42d3-a456-426614174000"


def push_document(*, status: str, kind: str = "recording") -> dict:
    document = {
        "schema": "openadapt.push-result/v1",
        "status": status,
        "workflow_id": None,
        "artifact_ingest_id": None,
        "review": None,
        "attestation": None,
        "binding": {
            "kind": kind,
            "source_tree_sha256": SHA_A,
            "derivative_tree_sha256": SHA_B,
            "approved_archive_sha256": None,
            "artifact_sha256": None,
            "bundle_sha256": None,
            "source_recording_sha256": None,
            "sanitization_policy": "outbound-phi-v1",
            "certification_policy": None,
            "certification_evidence_sha256": None,
            "governed_authorization_template_sha256": None,
            "parameter_schema_sha256": None,
            "attested_run_report_sha256": None,
            "resolves_run_id": None,
            "organization_id": None,
            "bundle_version_id": None,
            "bundle_version": None,
            "runtime_validation_id": None,
        },
        "next_action": None,
        "dashboard_url": None,
        "delivery": {"attempted": False, "certainty": "not_attempted"},
        "error": None,
    }
    if status == "paused_for_review":
        document["review"] = {
            "id": SHA_C,
            "scope": "local_non_authoritative",
            "sanitized_path": "/private/sanitized/artifact",
            "command": "openadapt-flow review-sanitized /private/sanitized/artifact",
        }
        document["next_action"] = "review_local"
    elif status == "accepted_for_ingest":
        document["artifact_ingest_id"] = INGEST_ID
        document["review"] = {
            "id": SHA_C,
            "scope": "local_non_authoritative",
            "sanitized_path": None,
            "command": None,
        }
        document["binding"]["approved_archive_sha256"] = SHA_D
        document["binding"]["artifact_sha256"] = SHA_D
        document["delivery"] = {"attempted": True, "certainty": "accepted"}
        if kind == "recording":
            document["next_action"] = "validate_runtime"
        else:
            document["workflow_id"] = WORKFLOW_ID
            document["attestation"] = {
                "id": "challenge-7",
                "schema": "openadapt.runtime-validation/v3",
            }
            document["binding"].update(
                {
                    "bundle_sha256": SHA_D,
                    "source_recording_sha256": SHA_A,
                    "certification_policy": "regulated",
                    "certification_evidence_sha256": SHA_B,
                    "governed_authorization_template_sha256": SHA_C,
                    "parameter_schema_sha256": SHA_C,
                    "attested_run_report_sha256": SHA_D,
                    "organization_id": ORG_ID,
                    "bundle_version_id": BUNDLE_VERSION_ID,
                    "bundle_version": 3,
                    "runtime_validation_id": RUNTIME_VALIDATION_ID,
                }
            )
            document["next_action"] = "open_dashboard"
            document["dashboard_url"] = (
                f"https://app.openadapt.ai/dashboard/workflows/{WORKFLOW_ID}"
            )
    elif status == "delivery_uncertain":
        document["next_action"] = "reconcile"
        document["delivery"] = {"attempted": True, "certainty": "unknown"}
        document["error"] = {
            "code": "delivery_uncertain",
            "message": "Reconcile the exact artifact before any retry.",
        }
    return document


def test_pause_preserves_exact_review_and_binding() -> None:
    document = push_document(status="paused_for_review")
    result = parse_flow_push(json.dumps(document), "", ok=True)
    assert result["ok"] is True
    assert result["pending_review"] is True
    assert result["artifact_ingest_id"] is None
    assert result["binding"]["source_tree_sha256"] == SHA_A
    assert result["sanitized_path"] == "/private/sanitized/artifact"


def test_accepted_recording_keeps_server_ingest_id_without_workflow() -> None:
    document = push_document(status="accepted_for_ingest")
    result = parse_flow_push(json.dumps(document), "", ok=True)
    assert result["accepted_for_ingest"] is True
    assert result["deployed"] is False
    assert result["workflow_id"] is None
    assert result["artifact_ingest_id"] == INGEST_ID
    assert result["next_action"] == "validate_runtime"


def test_accepted_bundle_requires_attested_exact_hashes_and_dashboard() -> None:
    document = push_document(status="accepted_for_ingest", kind="bundle")
    result = parse_flow_push(json.dumps(document), "", ok=True)
    assert result["deployed"] is True
    assert result["workflow_id"] == WORKFLOW_ID
    assert result["artifact_ingest_id"] == INGEST_ID
    assert result["binding"]["bundle_sha256"] == SHA_D
    assert result["binding"]["organization_id"] == ORG_ID
    assert result["binding"]["bundle_version_id"] == BUNDLE_VERSION_ID
    assert result["binding"]["runtime_validation_id"] == RUNTIME_VALIDATION_ID
    assert result["attestation"]["id"] == "challenge-7"


def test_bundle_rejects_invalid_retained_server_binding() -> None:
    document = push_document(status="accepted_for_ingest", kind="bundle")
    document["binding"]["runtime_validation_id"] = None
    result = parse_flow_push(json.dumps(document), "", ok=True)
    assert result["ok"] is False
    assert result["delivery_uncertain"] is True


def test_uncertain_state_rejects_retained_server_binding() -> None:
    document = push_document(status="delivery_uncertain")
    document["binding"]["runtime_validation_id"] = RUNTIME_VALIDATION_ID

    result = parse_flow_push(json.dumps(document), "", ok=False)

    assert result["ok"] is False
    assert result["delivery_uncertain"] is True
    assert result["error_code"] == "invalid_ingest_response"


def test_failed_state_rejects_uncertain_delivery_shape() -> None:
    document = push_document(status="delivery_uncertain")
    document["status"] = "failed"
    document["error"] = {
        "code": "push_failed",
        "message": "The artifact was not accepted for ingest.",
    }

    result = parse_flow_push(json.dumps(document), "", ok=False)

    assert result["ok"] is False
    assert result["delivery_uncertain"] is True
    assert result["error_code"] == "invalid_ingest_response"


def test_missing_server_ingest_id_is_never_success() -> None:
    document = push_document(status="accepted_for_ingest")
    document["artifact_ingest_id"] = None
    result = parse_flow_push(json.dumps(document), "", ok=True)
    assert result["ok"] is False
    assert result["delivery_uncertain"] is True
    assert result["next_action"] == "reconcile"


def test_server_ids_must_match_the_closed_uuid_schema() -> None:
    document = push_document(status="accepted_for_ingest")
    document["artifact_ingest_id"] = "223e4567-e89b-02d3-a456-426614174000"
    result = parse_flow_push(json.dumps(document), "", ok=True)
    assert result["ok"] is False
    assert result["delivery_uncertain"] is True


def test_process_status_conflict_is_never_success() -> None:
    document = push_document(status="accepted_for_ingest")
    result = parse_flow_push(json.dumps(document), "", ok=False)
    assert result["ok"] is False
    assert result["error_code"] == "invalid_ingest_response"


def test_invalid_output_does_not_reflect_child_diagnostics() -> None:
    secret = "Jane Doe /private/captures/raw.sqlite oai_ingest_secret"
    result = parse_flow_push("not-json", secret, ok=False)
    assert result["delivery_uncertain"] is True
    assert secret not in repr(result)


def test_bundle_dashboard_query_is_rejected() -> None:
    document = push_document(status="accepted_for_ingest", kind="bundle")
    document["dashboard_url"] += "?redirect=https://evil.example"
    result = parse_flow_push(json.dumps(document), "", ok=True)
    assert result["ok"] is False
    assert result["delivery_uncertain"] is True


def test_bundle_dashboard_must_match_the_requested_host() -> None:
    document = push_document(status="accepted_for_ingest", kind="bundle")
    result = parse_flow_push(
        json.dumps(document), "", ok=True, expected_host="https://other.openadapt.ai"
    )
    assert result["ok"] is False
    assert result["delivery_uncertain"] is True


def test_handoff_persists_exact_server_id_with_private_permissions(tmp_path: Path) -> None:
    document = push_document(status="accepted_for_ingest")
    result = parse_flow_push(json.dumps(document), "", ok=True)
    path = persist_deployment_handoff(
        tmp_path, local_workflow_id="local-workflow-1", result=result
    )
    saved = json.loads(path.read_text())
    assert saved["state"] == "accepted_recording"
    assert saved["push_result"]["artifact_ingest_id"] == INGEST_ID
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    else:
        # Windows does not expose POSIX owner/group bits, so `chmod(0o600)`
        # cannot be asserted there. The handoff is written inside Desktop's
        # per-user data directory and inherits its ACL.
        assert path.is_file()


def test_invalid_child_result_persists_reconcile_state(tmp_path: Path) -> None:
    result = parse_flow_push("bad", "raw local error", ok=False)
    path = persist_deployment_handoff(
        tmp_path, local_workflow_id="local-workflow-1", result=result
    )
    saved = json.loads(path.read_text())
    assert saved["state"] == "delivery_uncertain"
    assert saved["push_result"] is None
    assert saved["error_code"] == "invalid_ingest_response"
