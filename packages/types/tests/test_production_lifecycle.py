"""Tests for the simple signed Production admission registry."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError

from openadapt_types import (
    ProductionAdmissionRegistryStateV1,
    ProductionLifecycleAdmissionBindingV2,
    ProductionLifecycleTargetV2,
    QualificationReleaseReferenceV2,
    production_registry_signing_payload,
    project_production_lifecycle_target_v3,
    validate_production_admission,
)

_A = "a" * 64
_B = "b" * 64
_OBJECT_COMMIT = "b" * 40
_REGISTRY_COMMIT = "c" * 40
_ARTIFACT_INVENTORY_DOMAIN = b"OpenAdapt production release artifact inventory v1\0"
_RELEASE_DOMAIN = b"OpenAdapt production release candidate v1\0"
_RELEASE_ADMISSION_DOMAIN = b"OpenAdapt qualification release admission v2\0"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _domain_digest(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical(value)).hexdigest()


def _policy_targets() -> list[dict[str, object]]:
    return [
        {
            "id": "agent",
            "display_name": "OpenAdapt Agent",
            "source_repository": "OpenAdaptAI/openadapt-agent",
            "source_repository_id": "1136136670",
            "release_kind": "package",
            "claim_scope": "production_agent",
            "required_artifact_kinds": ["python-sdist", "python-wheel"],
            "package_index_project": "openadapt-agent",
        },
        {
            "id": "capture",
            "display_name": "OpenAdapt Capture",
            "source_repository": "OpenAdaptAI/openadapt-capture",
            "source_repository_id": "1115283835",
            "release_kind": "package",
            "claim_scope": "production_capture",
            "required_artifact_kinds": ["python-sdist", "python-wheel"],
            "package_index_project": "openadapt-capture",
        },
        {
            "id": "cloud",
            "display_name": "OpenAdapt Cloud",
            "source_repository": "OpenAdaptAI/openadapt-cloud",
            "source_repository_id": "1300570990",
            "release_kind": "deployment",
            "claim_scope": "production_cloud",
            "required_artifact_kinds": ["deployment-manifest"],
            "package_index_project": None,
        },
        {
            "id": "desktop",
            "display_name": "OpenAdapt Desktop",
            "source_repository": "OpenAdaptAI/openadapt-desktop",
            "source_repository_id": "1171291730",
            "release_kind": "package",
            "claim_scope": "production_desktop",
            "required_artifact_kinds": ["python-sdist", "python-wheel"],
            "package_index_project": "openadapt-desktop",
        },
        {
            "id": "docs",
            "display_name": "OpenAdapt Documentation",
            "source_repository": "OpenAdaptAI/openadapt-ops",
            "source_repository_id": "1172011294",
            "release_kind": "deployment",
            "claim_scope": "production_docs",
            "required_artifact_kinds": ["deployment-manifest"],
            "package_index_project": None,
        },
        {
            "id": "flow",
            "display_name": "OpenAdapt Flow",
            "source_repository": "OpenAdaptAI/openadapt-flow",
            "source_repository_id": "1291376938",
            "release_kind": "package",
            "claim_scope": "production_flow",
            "required_artifact_kinds": ["python-sdist", "python-wheel"],
            "package_index_project": "openadapt-flow",
        },
        {
            "id": "openadapt",
            "display_name": "OpenAdapt",
            "source_repository": "OpenAdaptAI/OpenAdapt",
            "source_repository_id": "627024850",
            "release_kind": "package",
            "claim_scope": "production_openadapt",
            "required_artifact_kinds": ["python-sdist", "python-wheel"],
            "package_index_project": "openadapt",
        },
    ]


def _policy(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "$schema": "schemas/production-lifecycle-policy.schema.json",
        "schema_version": "openadapt.production-lifecycle-policy/v3",
        "revision": 7,
        "admission_validity": "until_revoked",
        "maximum_release_admission_days": None,
        "maximum_workflow_admission_days": None,
        "object_reference_schema_version": (
            "openadapt.production-evidence-object-reference/v2"
        ),
        "release_admission_schema_version": "openadapt.qualification-release/v2",
        "workflow_admission_schema_version": "openadapt.qualification-admission/v4",
        "lifecycle_checkpoint_schema_version": (
            "openadapt.production-lifecycle-checkpoint/v2"
        ),
        "lifecycle_feed_schema_version": "openadapt.production-lifecycle-feed/v2",
        "lifecycle_feed_ref": "refs/heads/production-lifecycle-feed",
        "targets": _policy_targets(),
    }
    value.update(updates)
    return value


def _target(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "openadapt.production-lifecycle-target/v2",
        "id": "flow",
        "source_repository": "OpenAdaptAI/openadapt-flow",
        "source_repository_id": "1291376938",
        "release_kind": "package",
        "claim_scope": "production_flow",
        "required_artifact_kinds": ["python-sdist", "python-wheel"],
        "package_index_project": "openadapt-flow",
        "artifact_authority_by_kind": {
            "python-sdist": "pypi",
            "python-wheel": "pypi",
        },
    }
    payload.update(updates)
    return payload


def _release_artifacts() -> list[dict[str, object]]:
    return [
        {
            "name": "openadapt_flow-1.34.0.tar.gz",
            "kind": "python-sdist",
            "sha256": _digest("sdist"),
            "size_bytes": 120,
            "media_type": "application/gzip",
            "publish_destinations": ["github-release", "pypi"],
        },
        {
            "name": "openadapt_flow-1.34.0-py3-none-any.whl",
            "kind": "python-wheel",
            "sha256": _digest("wheel"),
            "size_bytes": 140,
            "media_type": "application/zip",
            "publish_destinations": ["github-release", "pypi"],
        },
    ]


def _release(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "openadapt.production-release-candidate/v1",
        "kind": "package",
        "source_repository": "OpenAdaptAI/openadapt-flow",
        "source_repository_id": "1291376938",
        "source_commit": "d" * 40,
        "version": "1.34.0",
        "tag": "v1.34.0",
        "deployment_id": None,
        "deployment_sha256": None,
        "artifacts": _release_artifacts(),
    }
    value.update(updates)
    return value


def _qualification_release(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "openadapt.qualification-release/v2",
        "evidence_class": "remote-safe-synthetic",
        "target": "flow",
        "verdict": "accepted",
        "claim_scope": "production_flow",
        "release_identity": {
            "schema_version": "openadapt.monotonic-production-release/v1",
            "channel": "production",
            "sequence": 1,
            "previous_admission_sha256": None,
        },
        "release": _release(),
        "publication_staging": {"schema_version": "test-staging/v1"},
        "publication_staging_sha256": _digest("staging"),
        "production_acceptance_summary_reference": {"kind": "summary"},
        "production_acceptance_summary_bundle_reference": {"kind": "bundle"},
        "authority_state_sha256": _digest("authority"),
        "revocation_state_sha256": _digest("revocation"),
        "signer_registry_sha256": _digest("signer-registry"),
        "publication_policy_sha256": _digest("publication-policy"),
        "issued_at": "2026-09-03T12:00:00Z",
        "not_before": "2026-09-03T12:00:00Z",
        "expires_at": None,
        "issuer": {"repository": "OpenAdaptAI/.github"},
    }
    value.update(updates)
    release = value["release"]
    assert isinstance(release, dict)
    artifacts = release["artifacts"]
    value.setdefault(
        "release_sha256",
        _domain_digest(
            _RELEASE_DOMAIN,
            {
                "target": value["target"],
                "claim_scope": value["claim_scope"],
                "release": release,
            },
        ),
    )
    value.setdefault(
        "artifact_inventory_sha256",
        _domain_digest(
            _ARTIFACT_INVENTORY_DOMAIN,
            {
                "target": value["target"],
                "claim_scope": value["claim_scope"],
                "artifacts": artifacts,
            },
        ),
    )
    unsigned = dict(value)
    unsigned.pop("admission_id_sha256", None)
    value.setdefault(
        "admission_id_sha256",
        _domain_digest(_RELEASE_ADMISSION_DOMAIN, unsigned),
    )
    return value


def _object_bytes(**updates: object) -> bytes:
    return _canonical(_qualification_release(**updates)) + b"\n"


def _reference(
    object_bytes: bytes | None = None, **updates: object
) -> dict[str, object]:
    raw = object_bytes if object_bytes is not None else _object_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    payload: dict[str, object] = {
        "schema_version": "openadapt.production-evidence-object-reference/v2",
        "repository": "OpenAdaptAI/.github",
        "repository_id": "858454062",
        "repository_owner_id": "132681217",
        "registry_source_commit": _OBJECT_COMMIT,
        "registry_revision": 1,
        "registry_head_sha256": f"sha256:{_A}",
        "registry_entry_sha256": f"sha256:{_B}",
        "kind": "qualification-release",
        "object_schema_version": "openadapt.qualification-release/v2",
        "object_path": (
            f"production-evidence/objects/sha256/{digest[:2]}/"
            f"{digest}.qualification-release.json"
        ),
        "object_sha256": f"sha256:{digest}",
        "size_bytes": len(raw),
        "object_media_type": (
            "application/vnd.openadapt.qualification-release+json;version=2"
        ),
        "semantic_identity_sha256": f"sha256:{_B}",
        "subject_sha256": None,
    }
    payload.update(updates)
    return payload


def _registry(
    state: str = "active",
    *,
    revision: int = 7,
    reference: dict[str, object] | None = None,
    **updates: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "openadapt.production-admission-registry-state/v1",
        "repository": "OpenAdaptAI/.github",
        "source_commit": _REGISTRY_COMMIT,
        "revision": revision,
        "issued_at": "2026-09-03T12:05:00Z",
        "entries": [
            {
                "reference": reference or _reference(),
                "state": state,
                "state_changed_at": "2026-09-03T12:04:00Z",
            }
        ],
        "signer_key_sha256": _digest("signer"),
        "signature_algorithm": "ed25519",
        "signature_b64url": "A" * 86,
    }
    payload.update(updates)
    payload["signed_payload_sha256"] = (
        "sha256:"
        + hashlib.sha256(production_registry_signing_payload(payload)).hexdigest()
    )
    return payload


def _validate(
    *,
    object_bytes: bytes | None = None,
    reference: QualificationReleaseReferenceV2 | None = None,
    registry: ProductionAdmissionRegistryStateV1 | None = None,
    signature_ok: bool = True,
    minimum_registry_revision: int = 7,
    at: str = "2036-09-03T12:00:00Z",
) -> ProductionLifecycleAdmissionBindingV2:
    raw = object_bytes if object_bytes is not None else _object_bytes()
    resolved_reference = reference or QualificationReleaseReferenceV2.model_validate(
        _reference(raw)
    )
    resolved_registry = registry or ProductionAdmissionRegistryStateV1.model_validate(
        _registry(reference=resolved_reference.model_dump(mode="json"))
    )
    return validate_production_admission(
        registry=resolved_registry,
        verify_registry_signature=lambda _state: signature_ok,
        minimum_registry_revision=minimum_registry_revision,
        reference=resolved_reference,
        qualification_release_bytes=raw,
        target=project_production_lifecycle_target_v3(_policy(), "flow"),
        at=at,
    )


def test_null_expiry_stays_active_until_the_signed_registry_revokes_it() -> None:
    raw = _object_bytes()
    reference = QualificationReleaseReferenceV2.model_validate(_reference(raw))
    assert _validate(object_bytes=raw, reference=reference, at="2099-09-03T12:00:00Z")

    revoked = ProductionAdmissionRegistryStateV1.model_validate(
        _registry(
            "revoked",
            revision=8,
            reference=reference.model_dump(mode="json"),
        )
    )
    with pytest.raises(ValueError, match="revoked"):
        _validate(
            object_bytes=raw,
            reference=reference,
            registry=revoked,
            minimum_registry_revision=8,
        )


def test_registry_revision_high_water_mark_rejects_older_active_replay() -> None:
    raw = _object_bytes()
    reference = QualificationReleaseReferenceV2.model_validate(_reference(raw))
    old_active = ProductionAdmissionRegistryStateV1.model_validate(
        _registry(reference=reference.model_dump(mode="json"), revision=7)
    )
    with pytest.raises(ValueError, match="older than the trusted minimum"):
        _validate(
            object_bytes=raw,
            reference=reference,
            registry=old_active,
            minimum_registry_revision=8,
        )


def test_supplied_expiry_has_no_policy_cap_and_is_enforced() -> None:
    raw = _object_bytes(expires_at="2036-09-20T12:00:00Z")
    assert _validate(object_bytes=raw, at="2036-09-20T11:59:59Z")
    with pytest.raises(ValueError, match="expired"):
        _validate(object_bytes=raw, at="2036-09-20T12:00:00Z")


def test_registry_signature_and_exact_object_bytes_are_required() -> None:
    raw = _object_bytes()
    with pytest.raises(ValueError, match="signature verification failed"):
        _validate(object_bytes=raw, signature_ok=False)

    reference = QualificationReleaseReferenceV2.model_validate(_reference(raw))
    with pytest.raises(ValueError, match="object digest"):
        _validate(object_bytes=raw + b" ", reference=reference)

    wrong_size = QualificationReleaseReferenceV2.model_validate(
        _reference(raw, size_bytes=len(raw) + 1)
    )
    registry = ProductionAdmissionRegistryStateV1.model_validate(
        _registry(reference=wrong_size.model_dump(mode="json"))
    )
    with pytest.raises(ValueError, match="object size"):
        _validate(
            object_bytes=raw,
            reference=wrong_size,
            registry=registry,
        )


def test_registry_has_one_current_state_for_each_admission() -> None:
    duplicate = _registry()
    duplicate_entries = cast(list[dict[str, Any]], duplicate["entries"])
    duplicate["entries"] = [duplicate_entries[0], duplicate_entries[0]]
    with pytest.raises(ValidationError, match="one current registry state"):
        production_registry_signing_payload(duplicate)

    future = _registry()
    future_entries = cast(list[dict[str, Any]], future["entries"])
    future_entries[0]["state_changed_at"] = "2026-09-03T12:05:01Z"
    with pytest.raises(ValidationError, match="must not follow"):
        production_registry_signing_payload(future)


def test_registry_revision_can_advance_without_mutating_immutable_reference() -> None:
    reference = _reference()
    assert reference["registry_revision"] == 1
    state = ProductionAdmissionRegistryStateV1.model_validate(
        _registry("revoked", revision=8, reference=reference)
    )
    assert state.revision == 8
    assert state.entries[0].reference.registry_revision == 1


def test_signed_payload_digest_covers_the_complete_registry_state() -> None:
    registry = _registry()
    entries = cast(list[dict[str, Any]], registry["entries"])
    entries[0]["state"] = "revoked"
    with pytest.raises(ValidationError, match="does not bind"):
        ProductionAdmissionRegistryStateV1.model_validate(registry)


def test_signing_payload_is_identical_for_mapping_and_model_defaults() -> None:
    sparse_reference = _reference()
    for field in (
        "schema_version",
        "repository",
        "repository_id",
        "repository_owner_id",
        "kind",
        "object_schema_version",
        "object_media_type",
        "subject_sha256",
    ):
        sparse_reference.pop(field)
    sparse = _registry(reference=sparse_reference)
    for field in ("schema_version", "repository", "signature_algorithm"):
        sparse.pop(field)
    sparse["signed_payload_sha256"] = (
        "sha256:"
        + hashlib.sha256(production_registry_signing_payload(sparse)).hexdigest()
    )
    model = ProductionAdmissionRegistryStateV1.model_validate(sparse)
    assert production_registry_signing_payload(sparse) == (
        production_registry_signing_payload(model)
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_repository", "OpenAdaptAI/OpenAdapt", "source_repository"),
        ("source_repository_id", "627024850", "source_repository_id"),
        ("release_kind", "deployment", "release_kind"),
        ("claim_scope", "production_agent", "claim_scope"),
        (
            "required_artifact_kinds",
            ["python-wheel", "python-sdist"],
            "required_artifact_kinds",
        ),
        ("package_index_project", "openadapt", "package_index_project"),
    ],
)
def test_target_contract_is_exact(field: str, value: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ProductionLifecycleTargetV2.model_validate(_target(**{field: value}))


def test_policy_v3_targets_project_to_exact_compatibility_contracts() -> None:
    policy = _policy()
    targets = [
        project_production_lifecycle_target_v3(policy, item["id"])
        for item in _policy_targets()
    ]
    assert [target.id.value for target in targets] == [
        "agent",
        "capture",
        "cloud",
        "desktop",
        "docs",
        "flow",
        "openadapt",
    ]
    assert targets[2].artifact_authority_by_kind.deployment_manifest.value == (
        "managed_evidence"
    )
    assert targets[5].artifact_authority_by_kind.python_wheel.value == "pypi"


def test_policy_v3_projection_rejects_expiry_cap_and_target_drift() -> None:
    with pytest.raises(ValueError, match="must not cap"):
        project_production_lifecycle_target_v3(
            _policy(maximum_release_admission_days=30), "flow"
        )

    policy = _policy()
    targets = cast(list[dict[str, Any]], policy["targets"])
    targets[5]["source_repository_id"] = "627024850"
    with pytest.raises(ValidationError, match="source_repository_id"):
        project_production_lifecycle_target_v3(policy, "flow")


def test_verified_bytes_project_canonical_qualification_release_v2_fields() -> None:
    raw = _object_bytes()
    admission = _validate(object_bytes=raw)
    assert admission.target.value == "flow"
    assert [artifact.kind.value for artifact in admission.artifacts] == [
        "python-sdist",
        "python-wheel",
    ]
    assert {artifact.authority.value for artifact in admission.artifacts} == {"pypi"}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target", "agent", "target"),
        ("claim_scope", "production_agent", "claim scope"),
    ],
)
def test_verified_object_projection_rejects_target_drift(
    field: str, value: object, message: str
) -> None:
    raw = _object_bytes(**{field: value})
    with pytest.raises(ValueError, match=message):
        _validate(object_bytes=raw)


def test_verified_object_projection_rejects_repository_and_artifact_drift() -> None:
    wrong_repository = _release(source_repository="OpenAdaptAI/OpenAdapt")
    with pytest.raises(ValueError, match="repository"):
        _validate(object_bytes=_object_bytes(release=wrong_repository))

    wrong_artifacts = _release(artifacts=_release_artifacts()[1:])
    with pytest.raises(ValueError, match="artifacts"):
        _validate(object_bytes=_object_bytes(release=wrong_artifacts))


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("release_sha256", "candidate digest"),
        ("artifact_inventory_sha256", "artifact inventory digest"),
        ("admission_id_sha256", "admission digest"),
    ],
)
def test_verified_object_projection_recomputes_internal_digests(
    field: str, message: str
) -> None:
    raw = _object_bytes(**{field: f"sha256:{_A}"})
    with pytest.raises(ValueError, match=message):
        _validate(object_bytes=raw)


def test_reference_is_only_an_index_and_binds_its_object_path() -> None:
    raw = _object_bytes()
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QualificationReleaseReferenceV2.model_validate(_reference(raw, target="flow"))
    with pytest.raises(ValidationError, match="object_path must bind"):
        QualificationReleaseReferenceV2.model_validate(
            _reference(raw, object_sha256=f"sha256:{_B}")
        )


@pytest.mark.parametrize(
    ("model", "payload", "filename"),
    [
        (
            QualificationReleaseReferenceV2,
            _reference(),
            "qualification-release-reference-v2.json",
        ),
        (
            ProductionLifecycleTargetV2,
            _target(),
            "production-lifecycle-target-v2.json",
        ),
        (
            ProductionLifecycleAdmissionBindingV2,
            {
                "target": "flow",
                "source_repository": "OpenAdaptAI/openadapt-flow",
                "release_kind": "package",
                "claim_scope": "production_flow",
                "release_sha256": _digest("release"),
                "artifact_inventory_sha256": _digest("inventory"),
                "artifacts": [
                    {
                        "name": item["name"],
                        "kind": item["kind"],
                        "sha256": item["sha256"],
                        "size_bytes": item["size_bytes"],
                        "authority": "pypi",
                    }
                    for item in _release_artifacts()
                ],
                "issued_at": "2026-09-03T12:00:00Z",
                "not_before": "2026-09-03T12:00:00Z",
                "expires_at": None,
                "verdict": "accepted",
            },
            "production-lifecycle-admission-binding-v2.json",
        ),
        (
            ProductionAdmissionRegistryStateV1,
            _registry(),
            "production-admission-registry-state-v1.json",
        ),
    ],
)
def test_packaged_schemas_are_closed_and_current(
    model: type[BaseModel],
    payload: dict[str, object],
    filename: str,
) -> None:
    schema = model.model_json_schema()
    assert schema["additionalProperties"] is False
    for definition in schema.get("$defs", {}).values():
        if isinstance(definition, dict) and definition.get("type") == "object":
            assert definition.get("additionalProperties") is False
    parsed = model.model_validate(payload)
    assert parsed is not None
    packaged = files("openadapt_types.schemas").joinpath(filename)
    assert json.loads(packaged.read_text(encoding="utf-8")) == schema
