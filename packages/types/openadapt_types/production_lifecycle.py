"""Simple, signed Production admission registry contracts.

The registry is the one current authority for admission state. Its entries are
active or revoked. A consumer verifies the registry signature, dereferences the
immutable admission object, and checks every bound product field before use.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_REPOSITORY_PATTERN = r"^OpenAdaptAI/[A-Za-z0-9._-]+$"
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_ARTIFACT_NAME_PATTERN = r"^[^/\\]{1,255}$"
_SIGNATURE_PATTERN = r"^[A-Za-z0-9_-]{86}$"
_DECIMAL_ID_PATTERN = r"^[1-9][0-9]*$"

_ARTIFACT_INVENTORY_DOMAIN = b"OpenAdapt production release artifact inventory v1\0"
_RELEASE_DOMAIN = b"OpenAdapt production release candidate v1\0"
_RELEASE_ADMISSION_DOMAIN = b"OpenAdapt qualification release admission v2\0"

PRODUCTION_EVIDENCE_OBJECT_REFERENCE_SCHEMA: Literal[
    "openadapt.production-evidence-object-reference/v2"
] = "openadapt.production-evidence-object-reference/v2"
PRODUCTION_LIFECYCLE_TARGET_SCHEMA: Literal[
    "openadapt.production-lifecycle-target/v2"
] = "openadapt.production-lifecycle-target/v2"
PRODUCTION_LIFECYCLE_ADMISSION_BINDING_SCHEMA: Literal[
    "openadapt.production-lifecycle-admission-binding/v2"
] = "openadapt.production-lifecycle-admission-binding/v2"
PRODUCTION_ADMISSION_REGISTRY_STATE_SCHEMA: Literal[
    "openadapt.production-admission-registry-state/v1"
] = "openadapt.production-admission-registry-state/v1"


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductionTargetIdV2(str, Enum):
    AGENT = "agent"
    CAPTURE = "capture"
    CLOUD = "cloud"
    DESKTOP = "desktop"
    DOCS = "docs"
    FLOW = "flow"
    OPENADAPT = "openadapt"


class ProductionReleaseKindV2(str, Enum):
    PACKAGE = "package"
    DEPLOYMENT = "deployment"
    HYBRID = "hybrid"


class ProductionArtifactKindV2(str, Enum):
    PYTHON_SDIST = "python-sdist"
    PYTHON_WHEEL = "python-wheel"
    DEPLOYMENT_MANIFEST = "deployment-manifest"


class ProductionArtifactAuthorityV2(str, Enum):
    PYPI = "pypi"
    GITHUB_RELEASE = "github_release"
    MANAGED_EVIDENCE = "managed_evidence"


class ProductionAdmissionStateV1(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


_PACKAGE_ARTIFACTS = (
    ProductionArtifactKindV2.PYTHON_SDIST,
    ProductionArtifactKindV2.PYTHON_WHEEL,
)
_DEPLOYMENT_ARTIFACTS = (ProductionArtifactKindV2.DEPLOYMENT_MANIFEST,)
_TARGET_CONTRACTS = {
    ProductionTargetIdV2.AGENT: (
        "OpenAdaptAI/openadapt-agent",
        "1136136670",
        ProductionReleaseKindV2.PACKAGE,
        "production_agent",
        _PACKAGE_ARTIFACTS,
        "openadapt-agent",
    ),
    ProductionTargetIdV2.CAPTURE: (
        "OpenAdaptAI/openadapt-capture",
        "1115283835",
        ProductionReleaseKindV2.PACKAGE,
        "production_capture",
        _PACKAGE_ARTIFACTS,
        "openadapt-capture",
    ),
    ProductionTargetIdV2.CLOUD: (
        "OpenAdaptAI/openadapt-cloud",
        "1300570990",
        ProductionReleaseKindV2.DEPLOYMENT,
        "production_cloud",
        _DEPLOYMENT_ARTIFACTS,
        None,
    ),
    ProductionTargetIdV2.DESKTOP: (
        "OpenAdaptAI/openadapt-desktop",
        "1171291730",
        ProductionReleaseKindV2.PACKAGE,
        "production_desktop",
        _PACKAGE_ARTIFACTS,
        "openadapt-desktop",
    ),
    ProductionTargetIdV2.DOCS: (
        "OpenAdaptAI/openadapt-ops",
        "1172011294",
        ProductionReleaseKindV2.DEPLOYMENT,
        "production_docs",
        _DEPLOYMENT_ARTIFACTS,
        None,
    ),
    ProductionTargetIdV2.FLOW: (
        "OpenAdaptAI/openadapt-flow",
        "1291376938",
        ProductionReleaseKindV2.PACKAGE,
        "production_flow",
        _PACKAGE_ARTIFACTS,
        "openadapt-flow",
    ),
    ProductionTargetIdV2.OPENADAPT: (
        "OpenAdaptAI/OpenAdapt",
        "627024850",
        ProductionReleaseKindV2.PACKAGE,
        "production_openadapt",
        _PACKAGE_ARTIFACTS,
        "openadapt",
    ),
}
_EXPECTED_ARTIFACT_AUTHORITIES = {
    ProductionArtifactKindV2.PYTHON_SDIST: ProductionArtifactAuthorityV2.PYPI,
    ProductionArtifactKindV2.PYTHON_WHEEL: ProductionArtifactAuthorityV2.PYPI,
    ProductionArtifactKindV2.DEPLOYMENT_MANIFEST: (
        ProductionArtifactAuthorityV2.MANAGED_EVIDENCE
    ),
}


def _parse_utc(value: str, field_name: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be a canonical UTC RFC 3339 timestamp"
        ) from exc


class ProductionArtifactAuthoritiesV2(_StrictContract):
    """The closed authority map for the supported artifact kinds."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    python_sdist: ProductionArtifactAuthorityV2 | None = Field(
        default=None, alias="python-sdist"
    )
    python_wheel: ProductionArtifactAuthorityV2 | None = Field(
        default=None, alias="python-wheel"
    )
    deployment_manifest: ProductionArtifactAuthorityV2 | None = Field(
        default=None, alias="deployment-manifest"
    )

    @property
    def declared_kinds(self) -> frozenset[ProductionArtifactKindV2]:
        return frozenset(
            ProductionArtifactKindV2(kind)
            for kind in self.model_dump(by_alias=True, exclude_none=True)
        )

    @model_validator(mode="after")
    def _authorities_match_artifacts(self) -> "ProductionArtifactAuthoritiesV2":
        for name, authority in self.model_dump(
            by_alias=True, exclude_none=True
        ).items():
            if (
                authority
                is not _EXPECTED_ARTIFACT_AUTHORITIES[ProductionArtifactKindV2(name)]
            ):
                raise ValueError(f"{name} has the wrong artifact authority")
        return self


class QualificationReleaseReferenceV2(_StrictContract):
    """An immutable registry index entry with no target assertion."""

    schema_version: Literal["openadapt.production-evidence-object-reference/v2"] = (
        PRODUCTION_EVIDENCE_OBJECT_REFERENCE_SCHEMA
    )
    repository: Literal["OpenAdaptAI/.github"] = "OpenAdaptAI/.github"
    repository_id: Literal["858454062"] = "858454062"
    repository_owner_id: Literal["132681217"] = "132681217"
    registry_source_commit: StrictStr = Field(pattern=_COMMIT_PATTERN)
    registry_revision: StrictInt = Field(ge=1)
    registry_head_sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    registry_entry_sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    kind: Literal["qualification-release"] = "qualification-release"
    object_schema_version: Literal["openadapt.qualification-release/v2"] = (
        "openadapt.qualification-release/v2"
    )
    object_path: StrictStr = Field(
        pattern=(
            r"^production-evidence/objects/sha256/[0-9a-f]{2}/"
            r"[0-9a-f]{64}\.qualification-release\.json$"
        )
    )
    object_sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    size_bytes: StrictInt = Field(ge=1)
    object_media_type: Literal[
        "application/vnd.openadapt.qualification-release+json;version=2"
    ] = "application/vnd.openadapt.qualification-release+json;version=2"
    semantic_identity_sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    subject_sha256: None = None

    @model_validator(mode="after")
    def _path_binds_digest(self) -> "QualificationReleaseReferenceV2":
        digest = self.object_sha256.removeprefix("sha256:")
        path = PurePosixPath(self.object_path)
        if (
            path.parent.name != digest[:2]
            or path.name != f"{digest}.qualification-release.json"
        ):
            raise ValueError("object_path must bind object_sha256")
        return self


class ProductionLifecycleTargetV2(_StrictContract):
    """One exact policy-native Production target."""

    schema_version: Literal["openadapt.production-lifecycle-target/v2"] = (
        PRODUCTION_LIFECYCLE_TARGET_SCHEMA
    )
    id: ProductionTargetIdV2
    source_repository: StrictStr = Field(pattern=_REPOSITORY_PATTERN)
    source_repository_id: StrictStr = Field(pattern=_DECIMAL_ID_PATTERN)
    release_kind: ProductionReleaseKindV2
    claim_scope: StrictStr = Field(pattern=r"^production_[a-z]+$")
    required_artifact_kinds: tuple[ProductionArtifactKindV2, ...] = Field(min_length=1)
    package_index_project: StrictStr | None
    artifact_authority_by_kind: ProductionArtifactAuthoritiesV2

    @model_validator(mode="after")
    def _matches_canonical_target(self) -> "ProductionLifecycleTargetV2":
        (
            repository,
            repository_id,
            release_kind,
            claim_scope,
            artifact_kinds,
            package_index_project,
        ) = _TARGET_CONTRACTS[self.id]
        if self.source_repository != repository:
            raise ValueError("source_repository does not match the target")
        if self.source_repository_id != repository_id:
            raise ValueError("source_repository_id does not match the target")
        if self.release_kind is not release_kind:
            raise ValueError("release_kind does not match the target")
        if self.claim_scope != claim_scope:
            raise ValueError("claim_scope does not match the target")
        if self.required_artifact_kinds != artifact_kinds:
            raise ValueError("required_artifact_kinds do not match the target")
        if self.package_index_project != package_index_project:
            raise ValueError("package_index_project does not match the target")
        if self.artifact_authority_by_kind.declared_kinds != frozenset(artifact_kinds):
            raise ValueError("every required artifact kind must have one authority")
        return self


def project_production_lifecycle_target_v3(
    policy: Mapping[str, Any], target_id: ProductionTargetIdV2 | str
) -> ProductionLifecycleTargetV2:
    """Project one exact target from the canonical lifecycle policy v3."""

    if policy.get("schema_version") != "openadapt.production-lifecycle-policy/v3":
        raise ValueError("Production lifecycle policy v3 is required")
    if policy.get("admission_validity") != "until_revoked":
        raise ValueError("the lifecycle policy must use until-revoked admissions")
    if (
        "maximum_release_admission_days" not in policy
        or policy.get("maximum_release_admission_days") is not None
    ):
        raise ValueError("the lifecycle policy must not cap release admission expiry")
    try:
        expected_id = ProductionTargetIdV2(target_id)
    except ValueError as exc:
        raise ValueError("the Production target is not supported") from exc
    targets = policy.get("targets")
    if not isinstance(targets, list):
        raise ValueError("the lifecycle policy targets must be an array")
    matching = [
        item
        for item in targets
        if isinstance(item, Mapping) and item.get("id") == expected_id.value
    ]
    if len(matching) != 1:
        raise ValueError("the lifecycle policy must contain one exact target")
    target = matching[0]
    raw_kinds = target.get("required_artifact_kinds")
    if not isinstance(raw_kinds, list):
        raise ValueError("required_artifact_kinds must be an array")
    try:
        kinds = tuple(ProductionArtifactKindV2(kind) for kind in raw_kinds)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "the lifecycle policy has an unsupported artifact kind"
        ) from exc
    authorities = {
        kind.value: _EXPECTED_ARTIFACT_AUTHORITIES[kind].value for kind in kinds
    }
    return ProductionLifecycleTargetV2.model_validate(
        {
            "id": target.get("id"),
            "source_repository": target.get("source_repository"),
            "source_repository_id": target.get("source_repository_id"),
            "release_kind": target.get("release_kind"),
            "claim_scope": target.get("claim_scope"),
            "required_artifact_kinds": raw_kinds,
            "package_index_project": target.get("package_index_project"),
            "artifact_authority_by_kind": authorities,
        }
    )


class ProductionReleaseArtifactBindingV2(_StrictContract):
    """One exact artifact and its verification authority."""

    name: StrictStr = Field(pattern=_ARTIFACT_NAME_PATTERN)
    kind: ProductionArtifactKindV2
    sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    size_bytes: StrictInt = Field(ge=1)
    authority: ProductionArtifactAuthorityV2

    @model_validator(mode="after")
    def _authority_matches_kind(self) -> "ProductionReleaseArtifactBindingV2":
        if self.authority is not _EXPECTED_ARTIFACT_AUTHORITIES[self.kind]:
            raise ValueError("artifact authority does not match its kind")
        return self


class _QualificationReleaseArtifactV2(_StrictContract):
    """The canonical artifact fields needed for release digest verification."""

    name: StrictStr = Field(pattern=_ARTIFACT_NAME_PATTERN)
    kind: ProductionArtifactKindV2
    sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    size_bytes: StrictInt = Field(ge=1)
    media_type: StrictStr = Field(pattern=r"^[^/]+/[^/]+$", max_length=200)
    publish_destinations: tuple[
        Literal["deployment", "github-release", "pypi"], ...
    ] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def _destinations_are_unique(self) -> "_QualificationReleaseArtifactV2":
        if len(frozenset(self.publish_destinations)) != len(self.publish_destinations):
            raise ValueError("artifact publish destinations must be unique")
        return self


class _QualificationReleaseCandidateV1(_StrictContract):
    """The canonical release candidate embedded in qualification release v2."""

    schema_version: Literal["openadapt.production-release-candidate/v1"]
    kind: ProductionReleaseKindV2
    source_repository: StrictStr = Field(pattern=_REPOSITORY_PATTERN)
    source_repository_id: StrictStr = Field(pattern=_DECIMAL_ID_PATTERN)
    source_commit: StrictStr = Field(pattern=_COMMIT_PATTERN)
    version: StrictStr | None
    tag: StrictStr | None
    deployment_id: StrictStr | None = Field(pattern=_DECIMAL_ID_PATTERN)
    deployment_sha256: StrictStr | None = Field(pattern=_DIGEST_PATTERN)
    artifacts: tuple[_QualificationReleaseArtifactV2, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _release_identity_matches_kind(self) -> "_QualificationReleaseCandidateV1":
        if self.kind is ProductionReleaseKindV2.PACKAGE:
            if self.version is None or self.tag is None:
                raise ValueError("a package release requires a version and tag")
            if self.deployment_id is not None or self.deployment_sha256 is not None:
                raise ValueError("a package release cannot contain deployment identity")
        elif self.kind is ProductionReleaseKindV2.DEPLOYMENT:
            if self.version is not None or self.tag is not None:
                raise ValueError("a deployment release cannot contain package identity")
            if self.deployment_id is None or self.deployment_sha256 is None:
                raise ValueError("a deployment release requires deployment identity")
        elif any(
            value is None
            for value in (
                self.version,
                self.tag,
                self.deployment_id,
                self.deployment_sha256,
            )
        ):
            raise ValueError(
                "a hybrid release requires package and deployment identity"
            )
        return self


class ProductionLifecycleAdmissionBindingV2(_StrictContract):
    """Fields read only after an admission object passes its digest check."""

    schema_version: Literal["openadapt.production-lifecycle-admission-binding/v2"] = (
        PRODUCTION_LIFECYCLE_ADMISSION_BINDING_SCHEMA
    )
    target: ProductionTargetIdV2
    source_repository: StrictStr = Field(pattern=_REPOSITORY_PATTERN)
    release_kind: ProductionReleaseKindV2
    claim_scope: StrictStr = Field(pattern=r"^production_[a-z]+$")
    release_sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    artifact_inventory_sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    artifacts: tuple[ProductionReleaseArtifactBindingV2, ...] = Field(min_length=1)
    issued_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    not_before: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    expires_at: StrictStr | None = Field(default=None, pattern=_TIMESTAMP_PATTERN)
    verdict: Literal["accepted"] = "accepted"

    @model_validator(mode="after")
    def _valid_fields(self) -> "ProductionLifecycleAdmissionBindingV2":
        issued_at = _parse_utc(self.issued_at, "issued_at")
        not_before = _parse_utc(self.not_before, "not_before")
        if not_before < issued_at:
            raise ValueError("not_before must not precede issued_at")
        if (
            self.expires_at is not None
            and _parse_utc(self.expires_at, "expires_at") <= not_before
        ):
            raise ValueError("expires_at must be after not_before")
        kinds = tuple(artifact.kind for artifact in self.artifacts)
        names = tuple(artifact.name for artifact in self.artifacts)
        if len(frozenset(kinds)) != len(kinds):
            raise ValueError("artifact kinds must be unique")
        if len(frozenset(names)) != len(names):
            raise ValueError("artifact names must be unique")
        return self


class ProductionAdmissionRegistryEntryV1(_StrictContract):
    """The current state of one immutable admission reference."""

    reference: QualificationReleaseReferenceV2
    state: ProductionAdmissionStateV1
    state_changed_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)


class _ProductionAdmissionRegistryPayloadV1(_StrictContract):
    """The normalized registry fields covered by the one signature."""

    schema_version: Literal["openadapt.production-admission-registry-state/v1"] = (
        PRODUCTION_ADMISSION_REGISTRY_STATE_SCHEMA
    )
    repository: Literal["OpenAdaptAI/.github"] = "OpenAdaptAI/.github"
    source_commit: StrictStr = Field(pattern=_COMMIT_PATTERN)
    revision: StrictInt = Field(ge=1)
    issued_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    entries: tuple[ProductionAdmissionRegistryEntryV1, ...] = Field(min_length=1)
    signer_key_sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    signature_algorithm: Literal["ed25519"] = "ed25519"

    @model_validator(mode="after")
    def _closed_current_state(self) -> "_ProductionAdmissionRegistryPayloadV1":
        issued_at = _parse_utc(self.issued_at, "issued_at")
        object_digests: set[str] = set()
        for entry in self.entries:
            object_digest = entry.reference.object_sha256
            if object_digest in object_digests:
                raise ValueError(
                    "an admission can have only one current registry state"
                )
            object_digests.add(object_digest)
            if _parse_utc(entry.state_changed_at, "state_changed_at") > issued_at:
                raise ValueError("state_changed_at must not follow issued_at")
        return self


def production_registry_signing_payload(
    value: Mapping[str, Any] | "ProductionAdmissionRegistryStateV1",
) -> bytes:
    """Return normalized bytes, including defaults, covered by the signature."""

    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", by_alias=True)
    else:
        payload = dict(value)
    payload.pop("signed_payload_sha256", None)
    payload.pop("signature_b64url", None)
    normalized = _ProductionAdmissionRegistryPayloadV1.model_validate(payload)
    return json.dumps(
        normalized.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class ProductionAdmissionRegistryStateV1(_ProductionAdmissionRegistryPayloadV1):
    """The single signed active/revoked registry state."""

    signed_payload_sha256: StrictStr = Field(pattern=_DIGEST_PATTERN)
    signature_b64url: StrictStr = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _signed_payload_matches(self) -> "ProductionAdmissionRegistryStateV1":
        expected = (
            "sha256:"
            + hashlib.sha256(production_registry_signing_payload(self)).hexdigest()
        )
        if self.signed_payload_sha256 != expected:
            raise ValueError("signed_payload_sha256 does not bind the registry state")
        return self


RegistrySignatureVerifier = Callable[[ProductionAdmissionRegistryStateV1], bool]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _domain_digest(domain: bytes, value: Any) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json(value)).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"qualification release contains duplicate key {key!r}")
        value[key] = item
    return value


def _validate_admission_target(
    admission: ProductionLifecycleAdmissionBindingV2,
    target: ProductionLifecycleTargetV2,
) -> None:
    if admission.target is not target.id:
        raise ValueError("admission target does not match the policy target")
    if admission.source_repository != target.source_repository:
        raise ValueError("admission repository does not match the policy target")
    if admission.release_kind is not target.release_kind:
        raise ValueError("admission release kind does not match the policy target")
    if admission.claim_scope != target.claim_scope:
        raise ValueError("admission claim scope does not match the policy target")
    admitted_kinds = tuple(artifact.kind for artifact in admission.artifacts)
    if admitted_kinds != target.required_artifact_kinds:
        raise ValueError("admission artifacts do not match the policy target")
    authorities = target.artifact_authority_by_kind.model_dump(
        by_alias=True, exclude_none=True
    )
    for artifact in admission.artifacts:
        if authorities[artifact.kind.value] != artifact.authority:
            raise ValueError("admission artifact authority does not match the policy")


def _parse_qualification_release_v2(
    *,
    reference: QualificationReleaseReferenceV2,
    object_bytes: bytes,
    target: ProductionLifecycleTargetV2,
) -> ProductionLifecycleAdmissionBindingV2:
    """Verify and project one canonical qualification-release/v2 object."""

    if not isinstance(object_bytes, bytes):
        raise ValueError("qualification release object_bytes must be bytes")
    object_digest = "sha256:" + hashlib.sha256(object_bytes).hexdigest()
    if object_digest != reference.object_sha256:
        raise ValueError("qualification release object digest does not match reference")
    if len(object_bytes) != reference.size_bytes:
        raise ValueError("qualification release object size does not match reference")
    try:
        decoded = object_bytes.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("qualification release object is not canonical JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("qualification release object must be a JSON object")
    if value.get("schema_version") != "openadapt.qualification-release/v2":
        raise ValueError("qualification release object schema is not supported")

    admission_id = value.get("admission_id_sha256")
    unsigned_admission = dict(value)
    unsigned_admission.pop("admission_id_sha256", None)
    if admission_id != _domain_digest(_RELEASE_ADMISSION_DOMAIN, unsigned_admission):
        raise ValueError("qualification release admission digest is invalid")

    release = _QualificationReleaseCandidateV1.model_validate(value.get("release"))
    release_value = release.model_dump(mode="json")
    target_value = value.get("target")
    claim_scope = value.get("claim_scope")
    if value.get("release_sha256") != _domain_digest(
        _RELEASE_DOMAIN,
        {
            "target": target_value,
            "claim_scope": claim_scope,
            "release": release_value,
        },
    ):
        raise ValueError("qualification release candidate digest is invalid")
    artifact_values = [
        artifact.model_dump(mode="json") for artifact in release.artifacts
    ]
    if value.get("artifact_inventory_sha256") != _domain_digest(
        _ARTIFACT_INVENTORY_DOMAIN,
        {
            "target": target_value,
            "claim_scope": claim_scope,
            "artifacts": artifact_values,
        },
    ):
        raise ValueError("qualification release artifact inventory digest is invalid")

    authorities = target.artifact_authority_by_kind.model_dump(
        by_alias=True, exclude_none=True
    )
    artifact_kinds = tuple(artifact.kind for artifact in release.artifacts)
    if artifact_kinds != target.required_artifact_kinds:
        raise ValueError(
            "qualification release artifacts do not match the policy target"
        )
    admission = ProductionLifecycleAdmissionBindingV2.model_validate(
        {
            "target": target_value,
            "source_repository": release.source_repository,
            "release_kind": release.kind,
            "claim_scope": claim_scope,
            "release_sha256": value.get("release_sha256"),
            "artifact_inventory_sha256": value.get("artifact_inventory_sha256"),
            "artifacts": [
                {
                    "name": artifact.name,
                    "kind": artifact.kind,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "authority": authorities[artifact.kind.value],
                }
                for artifact in release.artifacts
            ],
            "issued_at": value.get("issued_at"),
            "not_before": value.get("not_before"),
            "expires_at": value.get("expires_at"),
            "verdict": value.get("verdict"),
        }
    )
    if release.source_repository_id != target.source_repository_id:
        raise ValueError("admission repository id does not match the policy target")
    _validate_admission_target(admission, target)
    return admission


def validate_production_admission(
    *,
    registry: ProductionAdmissionRegistryStateV1,
    verify_registry_signature: RegistrySignatureVerifier,
    minimum_registry_revision: int,
    reference: QualificationReleaseReferenceV2,
    qualification_release_bytes: bytes,
    target: ProductionLifecycleTargetV2,
    at: str | datetime | None = None,
) -> ProductionLifecycleAdmissionBindingV2:
    """Return one active admission after all registry and object checks pass."""

    if not verify_registry_signature(registry):
        raise ValueError("registry signature verification failed")
    if (
        isinstance(minimum_registry_revision, bool)
        or not isinstance(minimum_registry_revision, int)
        or minimum_registry_revision < 1
    ):
        raise ValueError("minimum_registry_revision must be a positive integer")
    if registry.revision < minimum_registry_revision:
        raise ValueError("registry revision is older than the trusted minimum")
    matching = [
        entry
        for entry in registry.entries
        if entry.reference.object_sha256 == reference.object_sha256
    ]
    if len(matching) != 1 or matching[0].reference != reference:
        raise ValueError("admission reference is not in the signed registry")
    if matching[0].state is ProductionAdmissionStateV1.REVOKED:
        raise ValueError("admission is revoked")
    admission = _parse_qualification_release_v2(
        reference=reference,
        object_bytes=qualification_release_bytes,
        target=target,
    )

    check_time = (
        datetime.now(timezone.utc)
        if at is None
        else (_parse_utc(at, "at") if isinstance(at, str) else at)
    )
    if not isinstance(check_time, datetime) or check_time.tzinfo is None:
        raise ValueError(
            "at must be an offset-aware datetime or canonical UTC timestamp"
        )
    not_before = _parse_utc(admission.not_before, "not_before")
    if check_time < not_before:
        raise ValueError("admission is not active yet")
    if admission.expires_at is not None:
        expires_at = _parse_utc(admission.expires_at, "expires_at")
        if check_time >= expires_at:
            raise ValueError("admission has expired")
    return admission
