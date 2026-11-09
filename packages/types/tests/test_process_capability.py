"""Contract tests for process artifacts and admitted code capabilities."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types.process_capability import (
    ArtifactRefV1,
    CodeCapabilityAdmissionEnvelopeV1,
    CodeCapabilityAdmissionPayloadV1,
    CodeCapabilityManifestV1,
    ProcessEvidenceReceiptV1,
)

_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64
_SIGNATURE = "A" * 86 + "=="


def _manifest_payload() -> dict[str, object]:
    return {
        "schema_version": "openadapt.code-capability-manifest/v1",
        "capability_id": "capability.parse_document",
        "capability_version_id": "version.parse_document.0001",
        "runtime_kind": "python",
        "runtime_version": "3.12.5",
        "source_archive_digest": _DIGEST,
        "lockfile_path": "requirements.lock",
        "lockfile_digest": _OTHER_DIGEST,
        "entrypoint": ["bin/parse", "--input", "inputs/source"],
        "input_schema_digest": _DIGEST,
        "output_schema_digest": _OTHER_DIGEST,
        "outputs": [
            {
                "name": "normalized_rows",
                "relative_path": "outputs/rows.json",
                "media_type": "application/json",
                "required": True,
            }
        ],
        "permissions": {
            "isolation_profile": "trusted_local",
            "readable_mounts": ["inputs"],
            "writable_mounts": ["outputs"],
            "secret_names": [],
            "network_mode": "none",
            "allowed_network_hosts": [],
            "allow_process_spawn": False,
            "timeout_seconds": 300,
            "memory_limit_mb": 512,
            "output_limit_bytes": 10_000_000,
        },
        "effect_contract_digest": _DIGEST,
        "oracle_contract_digest": _OTHER_DIGEST,
        "qualification_campaign_digest": _DIGEST,
    }


def _admission_payload() -> dict[str, object]:
    manifest = CodeCapabilityManifestV1.model_validate(_manifest_payload())
    return {
        "schema_version": "openadapt.code-capability-admission/v1",
        "admission_id": "admission.parse_document.0001",
        "tenant_id": "tenant.reference.0001",
        "capability_id": manifest.capability_id,
        "capability_version_id": manifest.capability_version_id,
        "manifest_digest": manifest.digest,
        "runtime_environment_digest": _DIGEST,
        "permission_contract_digest": manifest.permissions.digest,
        "input_schema_digest": manifest.input_schema_digest,
        "output_schema_digest": manifest.output_schema_digest,
        "effect_contract_digest": manifest.effect_contract_digest,
        "oracle_contract_digest": manifest.oracle_contract_digest,
        "operator_contract_digest": _OTHER_DIGEST,
        "qualification_campaign_digest": manifest.qualification_campaign_digest,
        "issuer_key_id": "key.reference.0001",
        "issuer_workflow": "workflow.reference.0001",
        "issuer_ref": "refs.main.abcdef01",
        "issued_at": "2026-08-30T12:00:00Z",
        "not_before": "2026-08-30T12:00:00Z",
        "expires_at": "2026-09-20T12:00:00Z",
    }


def _process_receipt_payload() -> dict[str, object]:
    return {
        "schema_version": "openadapt.process-evidence-receipt/v1",
        "receipt_id": "receipt.process.0001",
        "process_execution_id": "execution.process.0001",
        "process_digest": _DIGEST,
        "app_package_digest": _OTHER_DIGEST,
        "environment_id": "environment.local.0001",
        "runner_id": "runner.local.0001",
        "outcome": "verified",
        "oracle_tier": 2,
        "delivery_uncertain": False,
        "model_used": False,
        "external_network_used": False,
        "child_receipt_digests": [_DIGEST, _OTHER_DIGEST],
        "human_receipt_digests": [],
        "artifact_graph_digest": _DIGEST,
        "evidence_root_digest": _OTHER_DIGEST,
        "issued_at": "2026-08-30T12:00:00Z",
        "issuer_key_id": "key.reference.0001",
        "signature_algorithm": "ed25519",
        "signature": _SIGNATURE,
    }


def test_manifest_is_closed_and_has_a_stable_digest() -> None:
    manifest = CodeCapabilityManifestV1.model_validate(_manifest_payload())
    assert manifest.digest.startswith("sha256:")
    assert (
        manifest.digest
        == CodeCapabilityManifestV1.model_validate(
            json.loads(manifest.model_dump_json())
        ).digest
    )

    payload = _manifest_payload()
    payload["shell"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CodeCapabilityManifestV1.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lockfile_path", "/etc/passwd"),
        ("lockfile_path", "../requirements.lock"),
        ("entrypoint", ["../bin/parse"]),
        ("runtime_version", "3.9.19"),
    ],
)
def test_manifest_refuses_unsafe_runtime_inputs(field: str, value: object) -> None:
    payload = _manifest_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        CodeCapabilityManifestV1.model_validate(payload)


def test_permissions_refuse_undeclared_network_access() -> None:
    payload = _manifest_payload()
    payload["permissions"]["allowed_network_hosts"] = ["bank.example"]
    with pytest.raises(ValidationError, match="network mode none"):
        CodeCapabilityManifestV1.model_validate(payload)

    payload["permissions"]["network_mode"] = "allowlist"
    manifest = CodeCapabilityManifestV1.model_validate(payload)
    assert manifest.permissions.allowed_network_hosts == ("bank.example",)


def test_artifact_ref_never_carries_a_path_and_binds_verification() -> None:
    artifact = ArtifactRefV1(
        artifact_id="artifact.source.0001",
        content_digest=_DIGEST,
        size_bytes=42,
        media_type="application/pdf",
        logical_name="bank_statement",
        producer_execution_id="execution.export.0001",
        producer_output_name="statement",
        storage_boundary="local_protected",
        data_classification="sensitive",
        verification_state="verified",
        verifier_receipt_digest=_OTHER_DIGEST,
        created_at="2026-08-30T12:00:00Z",
    )
    assert "path" not in artifact.model_dump()

    payload = artifact.model_dump(mode="json")
    payload["verification_state"] = "pending"
    with pytest.raises(ValidationError, match="pending artifact"):
        ArtifactRefV1.model_validate(payload)


def test_code_admission_binds_the_manifest_and_expires() -> None:
    payload = _admission_payload()
    admission = CodeCapabilityAdmissionPayloadV1.model_validate(payload)
    envelope = CodeCapabilityAdmissionEnvelopeV1(
        payload=admission,
        signature=_SIGNATURE,
    )
    assert envelope.artifact_digest.startswith("sha256:")

    payload["expires_at"] = "2026-10-01T12:00:00Z"
    with pytest.raises(ValidationError, match="exceeds 30 days"):
        CodeCapabilityAdmissionPayloadV1.model_validate(payload)


def test_process_verified_requires_independent_oracle_and_zero_model() -> None:
    payload = _process_receipt_payload()
    receipt = ProcessEvidenceReceiptV1.model_validate(payload)
    assert receipt.outcome.value == "verified"

    payload["oracle_tier"] = 1
    with pytest.raises(ValidationError, match="oracle tier 2 or 3"):
        ProcessEvidenceReceiptV1.model_validate(payload)

    payload = _process_receipt_payload()
    payload["model_used"] = True
    with pytest.raises(ValidationError, match="zero-model"):
        ProcessEvidenceReceiptV1.model_validate(payload)

    payload = _process_receipt_payload()
    payload["outcome"] = "rolled_back_verified"
    payload["oracle_tier"] = 1
    with pytest.raises(ValidationError, match="oracle tier 2 or 3"):
        ProcessEvidenceReceiptV1.model_validate(payload)


def test_process_reconciliation_requires_uncertainty() -> None:
    payload = _process_receipt_payload()
    payload["outcome"] = "reconciliation_required"
    with pytest.raises(ValidationError, match="requires uncertain"):
        ProcessEvidenceReceiptV1.model_validate(payload)

    payload["delivery_uncertain"] = True
    receipt = ProcessEvidenceReceiptV1.model_validate(payload)
    assert receipt.delivery_uncertain is True


@pytest.mark.parametrize(
    ("model", "filename"),
    [
        (ArtifactRefV1, "artifact-ref-v1.json"),
        (CodeCapabilityManifestV1, "code-capability-manifest-v1.json"),
        (
            CodeCapabilityAdmissionEnvelopeV1,
            "code-capability-admission-v1.json",
        ),
        (ProcessEvidenceReceiptV1, "process-evidence-receipt-v1.json"),
    ],
)
def test_packaged_schemas_match_models(model: object, filename: str) -> None:
    packaged = json.loads(
        files("openadapt_types.schemas").joinpath(filename).read_text(encoding="utf-8")
    )
    assert packaged == model.model_json_schema()
