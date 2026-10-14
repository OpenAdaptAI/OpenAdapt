"""Build content-addressed references for public production evidence.

The public evidence registry stores immutable JSON objects in ``OpenAdaptAI/.github``.
This module does not publish those objects.  It builds the canonical bytes, semantic
identities, registry-entry commitments, and exact v2 references that a publisher can
write after it has obtained the current registry metadata.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any, Mapping, Protocol, Sequence

CENTRAL_TRUST_CONTRACT_COMMIT = "989681f6f475616b7e2cb72360c716db0927f7ad"

OBJECT_REFERENCE_SCHEMA = "openadapt.production-evidence-object-reference/v2"
EVIDENCE_REPOSITORY = "OpenAdaptAI/.github"
EVIDENCE_REPOSITORY_ID = "858454062"
EVIDENCE_REPOSITORY_OWNER_ID = "132681217"
SIGSTORE_BUNDLE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
OBJECT_PATH_PREFIX = "production-evidence/objects/sha256"

REGISTRY_ENTRY_DIGEST_DOMAIN = b"OpenAdapt production evidence registry entry v1\0"
REGULAR_SEMANTIC_IDENTITY_DOMAIN = b"OpenAdapt production evidence semantic identity v1\0"
BUNDLE_SEMANTIC_IDENTITY_DOMAIN = b"OpenAdapt production evidence Sigstore bundle identity v1\0"

SIGNER_REGISTRY_POINTER_SCHEMA = "openadapt.qualification-signer-registry-pointer/v1"
SIGNER_REGISTRY_IDENTITY_DOMAIN = b"OpenAdapt qualification signer registry v2\0"
PRODUCTION_ACCEPTANCE_MANIFEST_SCHEMA = "openadapt.production-acceptance/v3"
PRODUCTION_ACCEPTANCE_SUMMARY_SCHEMA = "openadapt.production-lifecycle-evidence-summary/v3"
PRODUCTION_EVIDENCE_IDENTITY_DOMAIN = b"OpenAdapt production acceptance evidence identity v3\0"
PUBLICATION_STAGING_SCHEMA = "openadapt.production-release-staging-evidence/v1"
PUBLICATION_STAGING_DIGEST_DOMAIN = b"OpenAdapt production release staging evidence v1\0"
TAG_RULESET_SCHEMA = "openadapt.production-release-tag-ruleset/v1"
TAG_RULESETS_DIGEST_DOMAIN = b"OpenAdapt production release tag rulesets v1\0"
IMMUTABLE_RELEASES_DIGEST_DOMAIN = b"OpenAdapt production immutable releases response v1\0"
TAG_REF_STATE_DIGEST_DOMAIN = b"OpenAdapt production release tag ref state v1\0"
RELEASE_CANDIDATE_DIGEST_DOMAIN = b"OpenAdapt production release candidate v1\0"
ARTIFACT_INVENTORY_DIGEST_DOMAIN = b"OpenAdapt production release artifact inventory v1\0"
DECISION_RECEIPT_SCHEMA = "openadapt.qualification-evidence-decision-receipt/v2"
DECISION_RECEIPT_SIGNATURE_DOMAIN = b"OpenAdapt qualification evidence decision receipt v2\0"
DECISION_RECEIPT_IDENTITY_DOMAIN = b"OpenAdapt qualification decision receipt series identity v1\0"
DECISION_CAMPAIGN_SUMMARY_SCHEMA = "openadapt.qualification-evidence-decision-campaign-summary/v1"
QUALIFICATION_ADMISSION_SCHEMA = "openadapt.qualification-admission/v4"
QUALIFICATION_ADMISSION_DIGEST_DOMAIN = b"OpenAdapt qualification admission v4\0"
QUALIFICATION_RELEASE_SCHEMA = "openadapt.qualification-release/v2"
RELEASE_VERIFICATION_RECEIPT_SCHEMA = "openadapt.qualification-release-verification-receipt/v1"
RELEASE_VERIFICATION_RECEIPT_DOMAIN = b"OpenAdapt qualification release verification receipt v1\0"

_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_HEX40 = re.compile(r"^[a-f0-9]{40}$")
_KIND = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_KEY_ID = re.compile(r"^qa-ed25519-[0-9a-f]{16}$")
_ENTITY_CLASS = re.compile(r"^[a-z][a-z0-9 -]{0,63}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]{0,9})\.(0|[1-9][0-9]{0,9})\."
    r"(0|[1-9][0-9]{0,9})(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$"
)

_TARGET_RELEASE_CONTRACTS: Mapping[str, tuple[str, str, str, str]] = {
    "agent": ("production_agent", "OpenAdaptAI/openadapt-agent", "1136136670", "package"),
    "capture": (
        "production_capture",
        "OpenAdaptAI/openadapt-capture",
        "1115283835",
        "package",
    ),
    "cloud": ("production_cloud", "OpenAdaptAI/openadapt-cloud", "1300570990", "deployment"),
    "desktop": (
        "production_desktop",
        "OpenAdaptAI/openadapt-desktop",
        "1171291730",
        "package",
    ),
    "docs": ("production_docs", "OpenAdaptAI/openadapt-ops", "1172011294", "deployment"),
    "flow": ("production_flow", "OpenAdaptAI/openadapt-flow", "1291376938", "package"),
    "openadapt": ("production_openadapt", "OpenAdaptAI/OpenAdapt", "627024850", "package"),
}
_TARGET_ARTIFACT_CONTRACTS: Mapping[str, Mapping[str, tuple[str, tuple[str, ...]]]] = {
    "agent": {
        "python-sdist": ("application/gzip", ("github-release", "pypi")),
        "python-wheel": ("application/zip", ("github-release", "pypi")),
    },
    "capture": {
        "chrome-extension-zip": ("application/zip", ("github-release",)),
        "python-sdist": ("application/gzip", ("github-release", "pypi")),
        "python-wheel": ("application/zip", ("github-release", "pypi")),
        "spdx-sbom": ("application/spdx+json", ("github-release",)),
    },
    "cloud": {
        "deployment-manifest": (
            "application/vnd.openadapt.production-deployment-manifest+json;version=1",
            ("deployment",),
        ),
    },
    "desktop": {
        "cyclonedx-sbom": ("application/vnd.cyclonedx+json", ("github-release",)),
        "linux-appimage": ("application/vnd.appimage", ("github-release",)),
        "linux-deb": ("application/vnd.debian.binary-package", ("github-release",)),
        "macos-dmg-arm64": ("application/x-apple-diskimage", ("github-release",)),
        "macos-dmg-x86-64": ("application/x-apple-diskimage", ("github-release",)),
        "python-sdist": ("application/gzip", ("github-release", "pypi")),
        "python-wheel": ("application/zip", ("github-release", "pypi")),
        "release-checksums": ("text/plain", ("github-release",)),
        "verification-metadata-linux-x86-64": (
            "application/vnd.openadapt.desktop-platform-verification+json;version=1",
            ("github-release",),
        ),
        "verification-metadata-macos-arm64": (
            "application/vnd.openadapt.desktop-platform-verification+json;version=1",
            ("github-release",),
        ),
        "verification-metadata-macos-x86-64": (
            "application/vnd.openadapt.desktop-platform-verification+json;version=1",
            ("github-release",),
        ),
        "verification-metadata-windows-x86-64": (
            "application/vnd.openadapt.desktop-platform-verification+json;version=1",
            ("github-release",),
        ),
        "windows-msi": ("application/x-msi", ("github-release",)),
        "windows-nsis": (
            "application/vnd.microsoft.portable-executable",
            ("github-release",),
        ),
    },
    "docs": {
        "deployment-manifest": (
            "application/vnd.openadapt.production-deployment-manifest+json;version=1",
            ("deployment",),
        ),
        "site-archive": ("application/gzip", ("deployment",)),
    },
    "flow": {
        "python-sdist": ("application/gzip", ("github-release", "pypi")),
        "python-wheel": ("application/zip", ("github-release", "pypi")),
    },
    "openadapt": {
        "python-sdist": ("application/gzip", ("github-release", "pypi")),
        "python-wheel": ("application/zip", ("github-release", "pypi")),
    },
}


@dataclass(frozen=True)
class EvidenceKind:
    """The approved schema and media type for one regular evidence object."""

    schema_version: str
    media_type: str


@dataclass(frozen=True)
class EvidenceObjectPair:
    """One regular object followed by its raw Sigstore bundle."""

    objects: tuple[bytes, bytes]
    references: tuple[dict[str, Any], dict[str, Any]]


class DecisionReceiptVerifier(Protocol):
    """Verify a v2 receipt against issuer-owned current trust material."""

    def verify(
        self,
        receipt: Mapping[str, Any],
        *,
        reference: Mapping[str, Any],
        bundle_reference: Mapping[str, Any],
        object_sha256: str,
        semantic_identity_sha256: str,
        authority_state_sha256: str,
        signer_registry_sha256: str,
        revocation_state_sha256: str,
        evaluation_time: datetime,
    ) -> None: ...

    def verify_registered_object(
        self,
        value: Mapping[str, Any],
        *,
        reference: Mapping[str, Any],
        bundle_reference: Mapping[str, Any],
        kind: str,
        authority_state_sha256: str,
        signer_registry_sha256: str,
        revocation_state_sha256: str,
        evaluation_time: datetime,
    ) -> None: ...


REGULAR_EVIDENCE_KINDS: Mapping[str, EvidenceKind] = {
    "qualification-release": EvidenceKind(
        QUALIFICATION_RELEASE_SCHEMA,
        "application/vnd.openadapt.qualification-release+json;version=2",
    ),
    "production-acceptance-manifest": EvidenceKind(
        PRODUCTION_ACCEPTANCE_MANIFEST_SCHEMA,
        "application/vnd.openadapt.production-acceptance+json;version=3",
    ),
    "production-acceptance-summary": EvidenceKind(
        PRODUCTION_ACCEPTANCE_SUMMARY_SCHEMA,
        "application/vnd.openadapt.production-lifecycle-evidence-summary+json;version=3",
    ),
    "qualification-authority-state-receipt": EvidenceKind(
        "openadapt.qualification-authority-state-receipt/v2",
        "application/vnd.openadapt.qualification-authority-state-receipt+json;version=2",
    ),
    "qualification-revocation-state-receipt": EvidenceKind(
        "openadapt.qualification-revocation-state-receipt/v1",
        "application/vnd.openadapt.qualification-revocation-state-receipt+json;version=1",
    ),
    "production-current-default": EvidenceKind(
        "openadapt.production-current-default/v1",
        "application/vnd.openadapt.production-current-default+json;version=1",
    ),
    "production-deployment-observation": EvidenceKind(
        "openadapt.production-deployment-observation/v1",
        "application/vnd.openadapt.production-deployment-observation+json;version=1",
    ),
    "production-lifecycle-checkpoint": EvidenceKind(
        "openadapt.production-lifecycle-checkpoint/v2",
        "application/vnd.openadapt.production-lifecycle-checkpoint+json;version=2",
    ),
    "production-cloud-deploy-authorization": EvidenceKind(
        "openadapt.production-cloud-deploy-authorization/v1",
        "application/vnd.openadapt.production-cloud-deploy-authorization+json;version=1",
    ),
    "production-cloud-deployment-result": EvidenceKind(
        "openadapt.production-cloud-deployment-result/v1",
        "application/vnd.openadapt.production-cloud-deployment-result+json;version=1",
    ),
    "qualification-campaign-permit-policy": EvidenceKind(
        "openadapt.qualification-campaign-permit-policy/v3",
        "application/vnd.openadapt.qualification-campaign-permit-policy+json;version=3",
    ),
    "qualification-campaign-permit-request": EvidenceKind(
        "openadapt.qualification-campaign-permit-request/v3",
        "application/vnd.openadapt.qualification-campaign-permit-request+json;version=3",
    ),
    "qualification-campaign-permit": EvidenceKind(
        "openadapt.qualification-campaign-permit/v3",
        "application/vnd.openadapt.qualification-campaign-permit+json;version=3",
    ),
    "qualification-campaign-permit-receipt": EvidenceKind(
        "openadapt.qualification-campaign-permit-receipt/v3",
        "application/vnd.openadapt.qualification-campaign-permit-receipt+json;version=3",
    ),
    "qualification-evidence-decision-receipt": EvidenceKind(
        DECISION_RECEIPT_SCHEMA,
        "application/vnd.openadapt.qualification-evidence-decision-receipt+json;version=2",
    ),
    "qualification-admission": EvidenceKind(
        QUALIFICATION_ADMISSION_SCHEMA,
        "application/vnd.openadapt.qualification-admission+json;version=4",
    ),
    "support-release-admission": EvidenceKind(
        "openadapt.support-release-admission/v1",
        "application/vnd.openadapt.support-release-admission+json;version=1",
    ),
}

REFERENCE_KEYS = frozenset(
    {
        "schema_version",
        "repository",
        "repository_id",
        "repository_owner_id",
        "registry_source_commit",
        "registry_revision",
        "registry_head_sha256",
        "registry_entry_sha256",
        "kind",
        "object_schema_version",
        "object_path",
        "object_sha256",
        "size_bytes",
        "object_media_type",
        "semantic_identity_sha256",
        "subject_sha256",
    }
)

REGISTRY_ENTRY_KEYS: Sequence[str] = (
    "kind",
    "object_media_type",
    "object_path",
    "object_schema_version",
    "object_sha256",
    "semantic_identity_sha256",
    "size_bytes",
    "subject_sha256",
)

_SUMMARY_KEYS = frozenset(
    {
        "schema_version",
        "target",
        "verdict",
        "claim_scope",
        "acceptance_policy_sha256",
        "lifecycle_policy_sha256",
        "release_identity",
        "release_sha256",
        "artifact_inventory_sha256",
        "publication_staging",
        "publication_staging_sha256",
        "evidence_identity_sha256",
        "qualification_evidence_decision_receipt_reference",
        "qualification_evidence_decision_receipt_bundle_reference",
        "qualification_admission_reference",
        "qualification_admission_bundle_reference",
        "production_acceptance_manifest_reference",
        "production_acceptance_manifest_bundle_reference",
        "campaign_summary",
        "authority_state_sha256",
        "revocation_state_sha256",
        "signer_registry_sha256",
        "issued_at",
        "not_before",
        "expires_at",
        "issuer",
    }
)
_RELEASE_IDENTITY_KEYS = frozenset(
    {"schema_version", "channel", "sequence", "previous_admission_sha256"}
)
_CAMPAIGN_CLASSES = frozenset(
    {
        "healthy",
        "safe_halt",
        "idempotency_replay",
        "uncertain_delivery",
        "declared_attended",
        "governed_repair",
    }
)
_CAMPAIGN_CLASS_SUMMARY_KEYS = frozenset(
    {
        "task_condition_cell_count",
        "minimum_trials_per_cell",
        "observed_trial_count",
        "silent_incorrect_success_count",
        "over_halt_count",
        "unsafe_effect_count",
        "blind_retry_count",
        "replay_dispatch_count",
        "model_call_count",
        "unplanned_intervention_count",
        "reconciliation_required_count",
        "authenticated_bound_decision_count",
        "live_target_revalidation_count",
        "policy_approved_repair_count",
        "approved_repair_count",
        "retained_repair_evidence_count",
        "unverified_direct_action_count",
    }
)
_WHOLE_SECOND_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_DECIMAL_ID = re.compile(r"^[1-9][0-9]*$")
_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$")
_PUBLICATION_STAGING_KEYS = frozenset(
    {
        "schema_version",
        "repository",
        "repository_id",
        "draft_release_id",
        "tag",
        "target_commitish",
        "draft",
        "prerelease",
        "release_app_id",
        "release_app_installation_id",
        "release_app_bot_user_id",
        "release_author_login",
        "assets",
        "immutable_releases",
        "immutable_releases_sha256",
        "tag_rulesets",
        "tag_rulesets_sha256",
        "tag_ref_state",
        "tag_ref_state_sha256",
        "observed_at",
    }
)
_ASSET_KEYS = frozenset(
    {
        "asset_id",
        "name",
        "kind",
        "sha256",
        "size_bytes",
        "media_type",
        "publish_destinations",
        "uploader_id",
        "uploader_login",
    }
)
_EXPECTED_ASSET_KEYS = frozenset(
    {"name", "kind", "sha256", "size_bytes", "media_type", "publish_destinations"}
)
_TAG_RULESET_KEYS = frozenset(
    {
        "schema_version",
        "role",
        "repository",
        "repository_id",
        "ruleset_id",
        "name",
        "target",
        "enforcement",
        "bypass_actors",
        "conditions",
        "rules",
    }
)
_BYPASS_ACTOR_KEYS = frozenset({"actor_id", "actor_type", "bypass_mode"})
_CONDITION_KEYS = frozenset({"ref_name"})
_REF_NAME_KEYS = frozenset({"include", "exclude"})
_RULE_KEYS = frozenset({"type"})
_UPDATE_RULE_KEYS = frozenset({"type", "parameters"})
_UPDATE_PARAMETERS_KEYS = frozenset({"update_allows_fetch_and_merge"})
_DECISION_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "evidence_class",
        "decision_identity_sha256",
        "decision_revision",
        "decision_commitment_sha256",
        "evidence_manifest_sha256",
        "evidence_manifest_readback_sha256",
        "campaign_artifact_sha256",
        "organization_id_sha256",
        "workflow_id_sha256",
        "workflow_version_id_sha256",
        "bundle_version",
        "bundle_sha256",
        "admitted_runtime_sha256",
        "action_contract_sha256",
        "application_contract_sha256",
        "effect_contract_sha256",
        "environment_contract_sha256",
        "evidence_authority_contract_sha256",
        "identity_contract_sha256",
        "input_contract_sha256",
        "policy_contract_sha256",
        "campaign_permit_sha256",
        "signer_registry_sha256",
        "revocation_state_sha256",
        "entity_class",
        "campaign_summary",
        "verdict",
        "issued_at",
        "not_before",
        "expires_at",
        "issuer_key_id",
        "algorithm",
        "signing_statement",
        "signature",
        "issuer",
    }
)
_DECISION_CAMPAIGN_SUMMARY_KEYS = frozenset(
    {"schema_version", "minimum_trials_per_task_condition", "task_count", "classes"}
)
_SIGNING_STATEMENT_KEYS = frozenset(
    {
        "schema_version",
        "object_schema_version",
        "signature_domain",
        "unsigned_object_sha256",
        "unsigned_size_bytes",
        "commitment_scheme",
    }
)
_ISSUER_KEYS = frozenset(
    {
        "repository",
        "repository_id",
        "repository_owner_id",
        "workflow",
        "ref",
        "source_commit",
        "environment",
    }
)
_RELEASE_VERIFICATION_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "verification_id_sha256",
        "verdict",
        "evidence_class",
        "target",
        "claim_scope",
        "admission_object_sha256",
        "admission_bundle_object_sha256",
        "admission_id_sha256",
        "release_sha256",
        "artifact_inventory_sha256",
        "release_identity",
        "source_repository",
        "source_repository_id",
        "source_commit",
        "version",
        "tag",
        "draft_release_id",
        "publication_staging_sha256",
        "authority_state_sha256",
        "revocation_state_sha256",
        "signer_registry_sha256",
        "acceptance_summary_object_sha256",
        "acceptance_manifest_object_sha256",
        "decision_receipt_object_sha256",
        "qualification_admission_object_sha256",
        "qualification_admission_id_sha256",
        "workflow_version_id_sha256",
        "workflow_bundle_sha256",
        "admitted_runtime_sha256",
        "verified_at",
        "expires_at",
        "registry_source_commit",
        "registry_revision",
        "registry_head_sha256",
        "trust_state_source_commit",
    }
)
_QUALIFICATION_ADMISSION_KEYS = frozenset(
    {
        "schema_version",
        "admission_id_sha256",
        "evidence_class",
        "organization_id_sha256",
        "workflow_id_sha256",
        "workflow_version_id_sha256",
        "bundle_version",
        "bundle_sha256",
        "admitted_runtime_sha256",
        "application_contract_sha256",
        "environment_contract_sha256",
        "input_contract_sha256",
        "action_contract_sha256",
        "identity_contract_sha256",
        "effect_contract_sha256",
        "policy_contract_sha256",
        "evidence_authority_sha256",
        "campaign_artifact_sha256",
        "campaign_permit_sha256",
        "decision_receipt_reference",
        "decision_receipt_bundle_reference",
        "signer_registry_sha256",
        "revocation_state_sha256",
        "entity_class",
        "campaign_summary",
        "local_identity_opening",
        "verdict",
        "issued_at",
        "not_before",
        "expires_at",
        "issuer",
    }
)
_PRODUCTION_ACCEPTANCE_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "target",
        "verdict",
        "claim_scope",
        "acceptance_policy_sha256",
        "lifecycle_policy_sha256",
        "release_identity",
        "release",
        "release_sha256",
        "artifact_inventory",
        "artifact_inventory_sha256",
        "publication_staging",
        "publication_staging_sha256",
        "qualification_evidence_decision_receipt_reference",
        "qualification_evidence_decision_receipt_bundle_reference",
        "qualification_admission_reference",
        "qualification_admission_bundle_reference",
        "campaign_summary",
        "authority_state_sha256",
        "revocation_state_sha256",
        "signer_registry_sha256",
        "issued_at",
        "not_before",
        "expires_at",
        "issuer",
    }
)
_RELEASE_CANDIDATE_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "source_repository",
        "source_repository_id",
        "source_commit",
        "version",
        "tag",
        "deployment_id",
        "deployment_sha256",
        "artifacts",
    }
)
_ARTIFACT_INVENTORY_KEYS = frozenset({"schema_version", "target", "claim_scope", "artifacts"})


class ProductionEvidenceError(ValueError):
    """A public evidence object or reference violates the frozen contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical compact JSON encoding used by evidence digests."""

    _validate_json_value(value, "object")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_digest(payload: bytes) -> str:
    """Return a lowercase, algorithm-prefixed SHA-256 digest."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def signer_registry_identity(registry: Mapping[str, Any]) -> str:
    """Commit to a signer registry with the approved v2 identity domain."""

    return sha256_digest(SIGNER_REGISTRY_IDENTITY_DOMAIN + canonical_json_bytes(dict(registry)))


def _regular_semantic_identity(
    *, kind: str, object_schema_version: str, object_value: Mapping[str, Any]
) -> str:
    if kind == "qualification-evidence-decision-receipt":
        evidence_class = object_value.get("evidence_class")
        decision_identity = object_value.get("decision_identity_sha256")
        decision_revision = object_value.get("decision_revision")
        if evidence_class not in {"private-customer", "remote-safe-synthetic"}:
            raise ProductionEvidenceError("decision receipt evidence_class is invalid")
        _require_digest(decision_identity, "decision_identity_sha256")
        _require_positive_int(decision_revision, "decision_revision")
        payload = {
            "decision_identity_sha256": decision_identity,
            "decision_revision": decision_revision,
            "evidence_class": evidence_class,
        }
        return sha256_digest(DECISION_RECEIPT_IDENTITY_DOMAIN + canonical_json_bytes(payload))
    return sha256_digest(
        REGULAR_SEMANTIC_IDENTITY_DOMAIN
        + canonical_json_bytes(
            {
                "kind": kind,
                "object_schema_version": object_schema_version,
                "object": dict(object_value),
            }
        )
    )


def build_signer_registry_pointer(
    *,
    object_path: str,
    object_sha256: str,
    registry_identity_sha256: str,
    registry_revision: int,
) -> dict[str, Any]:
    """Build the exact public signer-registry pointer projection."""

    _validate_object_path(object_path)
    _require_digest(object_sha256, "object_sha256")
    _require_digest(registry_identity_sha256, "registry_identity_sha256")
    _require_positive_int(registry_revision, "registry_revision")
    return {
        "schema_version": SIGNER_REGISTRY_POINTER_SCHEMA,
        "object_path": object_path,
        "object_sha256": object_sha256,
        "registry_identity_sha256": registry_identity_sha256,
        "registry_revision": registry_revision,
    }


def build_evidence_object_pair(
    *,
    kind: str,
    object_value: Mapping[str, Any],
    sigstore_bundle: bytes,
    registry_source_commit: str,
    registry_revision: int,
    registry_head_sha256: str,
) -> EvidenceObjectPair:
    """Build one regular reference and its immediately following bundle reference."""

    profile = REGULAR_EVIDENCE_KINDS.get(kind)
    if profile is None:
        raise ProductionEvidenceError(f"evidence kind is not approved: {kind!r}")
    if object_value.get("schema_version") != profile.schema_version:
        raise ProductionEvidenceError(f"{kind} schema_version must be {profile.schema_version!r}")

    regular_bytes = canonical_json_bytes(dict(object_value)) + b"\n"
    bundle_bytes = _validate_raw_json_object(sigstore_bundle, "sigstore_bundle")
    regular_sha256 = sha256_digest(regular_bytes)
    regular_identity = _regular_semantic_identity(
        kind=kind,
        object_schema_version=profile.schema_version,
        object_value=object_value,
    )
    regular = _build_reference(
        kind=kind,
        object_schema_version=profile.schema_version,
        object_media_type=profile.media_type,
        object_bytes=regular_bytes,
        object_sha256=regular_sha256,
        semantic_identity_sha256=regular_identity,
        subject_sha256=None,
        registry_source_commit=registry_source_commit,
        registry_revision=registry_revision,
        registry_head_sha256=registry_head_sha256,
    )

    bundle_kind = f"{kind}-sigstore-bundle"
    bundle_sha256 = sha256_digest(bundle_bytes)
    bundle_identity = sha256_digest(
        BUNDLE_SEMANTIC_IDENTITY_DOMAIN
        + canonical_json_bytes(
            {
                "kind": bundle_kind,
                "object_sha256": bundle_sha256,
                "subject_sha256": regular_sha256,
            }
        )
    )
    bundle = _build_reference(
        kind=bundle_kind,
        object_schema_version=SIGSTORE_BUNDLE_MEDIA_TYPE,
        object_media_type=SIGSTORE_BUNDLE_MEDIA_TYPE,
        object_bytes=bundle_bytes,
        object_sha256=bundle_sha256,
        semantic_identity_sha256=bundle_identity,
        subject_sha256=regular_sha256,
        registry_source_commit=registry_source_commit,
        registry_revision=registry_revision,
        registry_head_sha256=registry_head_sha256,
    )
    return EvidenceObjectPair(
        objects=(regular_bytes, bundle_bytes),
        references=(regular, bundle),
    )


def build_production_acceptance_summary(
    *,
    target: str,
    claim_scope: str,
    acceptance_policy_sha256: str,
    lifecycle_policy_sha256: str,
    release_identity: Mapping[str, Any],
    release_sha256: str,
    artifact_inventory_sha256: str,
    publication_staging: Mapping[str, Any],
    expected_publication_assets: Sequence[Mapping[str, Any]],
    qualification_evidence_decision_receipt: Mapping[str, Any],
    qualification_evidence_decision_receipt_references: Sequence[Mapping[str, Any]],
    qualification_admission: Mapping[str, Any],
    qualification_admission_references: Sequence[Mapping[str, Any]],
    production_acceptance_manifest: Mapping[str, Any],
    production_acceptance_manifest_references: Sequence[Mapping[str, Any]],
    authority_state_sha256: str,
    revocation_state_sha256: str,
    signer_registry_sha256: str,
    issued_at: str,
    not_before: str,
    expires_at: str,
    issuer: Mapping[str, Any],
    decision_receipt_verifier: DecisionReceiptVerifier,
    evaluation_time: datetime,
) -> dict[str, Any]:
    """Build the remote-safe v3 summary after all three input pairs exist."""

    decision_pair = _validated_pair(
        qualification_evidence_decision_receipt_references,
        "qualification-evidence-decision-receipt",
    )
    admission_pair = _validated_pair(qualification_admission_references, "qualification-admission")
    manifest_pair = _validated_pair(
        production_acceptance_manifest_references, "production-acceptance-manifest"
    )
    release = dict(release_identity)
    receipt = dict(qualification_evidence_decision_receipt)
    campaign = _validate_decision_receipt(
        receipt,
        reference=decision_pair[0],
        bundle_reference=decision_pair[1],
        expected_authority_state_sha256=authority_state_sha256,
        expected_signer_registry_sha256=signer_registry_sha256,
        expected_revocation_state_sha256=revocation_state_sha256,
        verifier=decision_receipt_verifier,
        evaluation_time=evaluation_time,
    )
    staging = dict(publication_staging)
    staging_sha256 = _validate_publication_staging(
        staging, expected_assets=expected_publication_assets
    )
    summary: dict[str, Any] = {
        "schema_version": PRODUCTION_ACCEPTANCE_SUMMARY_SCHEMA,
        "target": target,
        "verdict": "accepted",
        "claim_scope": claim_scope,
        "acceptance_policy_sha256": acceptance_policy_sha256,
        "lifecycle_policy_sha256": lifecycle_policy_sha256,
        "release_identity": release,
        "release_sha256": release_sha256,
        "artifact_inventory_sha256": artifact_inventory_sha256,
        "publication_staging": staging,
        "publication_staging_sha256": staging_sha256,
        "evidence_identity_sha256": "sha256:" + "0" * 64,
        "qualification_evidence_decision_receipt_reference": decision_pair[0],
        "qualification_evidence_decision_receipt_bundle_reference": decision_pair[1],
        "qualification_admission_reference": admission_pair[0],
        "qualification_admission_bundle_reference": admission_pair[1],
        "production_acceptance_manifest_reference": manifest_pair[0],
        "production_acceptance_manifest_bundle_reference": manifest_pair[1],
        "campaign_summary": campaign,
        "authority_state_sha256": authority_state_sha256,
        "revocation_state_sha256": revocation_state_sha256,
        "signer_registry_sha256": signer_registry_sha256,
        "issued_at": issued_at,
        "not_before": not_before,
        "expires_at": expires_at,
        "issuer": dict(issuer),
    }
    summary["evidence_identity_sha256"] = _production_evidence_identity(summary)
    validate_production_acceptance_summary(
        summary,
        qualification_evidence_decision_receipt=receipt,
        qualification_admission=qualification_admission,
        production_acceptance_manifest=production_acceptance_manifest,
        expected_publication_assets=expected_publication_assets,
        decision_receipt_verifier=decision_receipt_verifier,
        evaluation_time=evaluation_time,
    )
    return summary


def validate_production_acceptance_summary(
    summary: Mapping[str, Any],
    *,
    qualification_evidence_decision_receipt: Mapping[str, Any],
    qualification_admission: Mapping[str, Any],
    production_acceptance_manifest: Mapping[str, Any],
    expected_publication_assets: Sequence[Mapping[str, Any]],
    decision_receipt_verifier: DecisionReceiptVerifier,
    evaluation_time: datetime,
) -> None:
    """Validate a v3 summary without reading any private campaign payload."""

    value = dict(summary)
    if set(value) != _SUMMARY_KEYS:
        missing = sorted(_SUMMARY_KEYS - set(value))
        extra = sorted(set(value) - _SUMMARY_KEYS)
        raise ProductionEvidenceError(
            f"production summary keys differ: missing={missing}, extra={extra}"
        )
    if value["schema_version"] != PRODUCTION_ACCEPTANCE_SUMMARY_SCHEMA:
        raise ProductionEvidenceError("production summary schema_version is invalid")
    for field in ("target", "claim_scope"):
        if not isinstance(value[field], str) or not value[field]:
            raise ProductionEvidenceError(f"production summary {field} is invalid")
    target_contract = _TARGET_RELEASE_CONTRACTS.get(value["target"])
    if target_contract is None or value["claim_scope"] != target_contract[0]:
        raise ProductionEvidenceError("production summary target or claim_scope is invalid")
    if value["verdict"] != "accepted":
        raise ProductionEvidenceError("production summary verdict must be accepted")
    for field in (
        "acceptance_policy_sha256",
        "lifecycle_policy_sha256",
        "release_sha256",
        "artifact_inventory_sha256",
        "publication_staging_sha256",
        "evidence_identity_sha256",
        "authority_state_sha256",
        "revocation_state_sha256",
        "signer_registry_sha256",
    ):
        _require_digest(value[field], field)
    _validate_release_identity(value["release_identity"])
    _validate_acceptance_issuer(value["issuer"])
    _validate_summary_reference_fields(value)
    staging_sha256 = _validate_publication_staging(
        value["publication_staging"], expected_assets=expected_publication_assets
    )
    if value["publication_staging_sha256"] != staging_sha256:
        raise ProductionEvidenceError("publication_staging_sha256 is invalid")
    receipt_summary = _validate_decision_receipt(
        qualification_evidence_decision_receipt,
        reference=value["qualification_evidence_decision_receipt_reference"],
        bundle_reference=value["qualification_evidence_decision_receipt_bundle_reference"],
        expected_authority_state_sha256=value["authority_state_sha256"],
        expected_signer_registry_sha256=value["signer_registry_sha256"],
        expected_revocation_state_sha256=value["revocation_state_sha256"],
        verifier=decision_receipt_verifier,
        evaluation_time=evaluation_time,
    )
    _validate_campaign_summary(value["campaign_summary"])
    _validate_qualification_admission_binding(
        qualification_admission,
        receipt=qualification_evidence_decision_receipt,
        receipt_reference=value["qualification_evidence_decision_receipt_reference"],
        receipt_bundle_reference=value["qualification_evidence_decision_receipt_bundle_reference"],
        admission_reference=value["qualification_admission_reference"],
    )
    decision_receipt_verifier.verify_registered_object(
        qualification_admission,
        reference=value["qualification_admission_reference"],
        bundle_reference=value["qualification_admission_bundle_reference"],
        kind="qualification-admission",
        authority_state_sha256=value["authority_state_sha256"],
        signer_registry_sha256=value["signer_registry_sha256"],
        revocation_state_sha256=value["revocation_state_sha256"],
        evaluation_time=evaluation_time,
    )
    _validate_acceptance_manifest_binding(
        production_acceptance_manifest,
        summary=value,
        receipt=qualification_evidence_decision_receipt,
        qualification_admission=qualification_admission,
        expected_publication_assets=expected_publication_assets,
    )
    decision_receipt_verifier.verify_registered_object(
        production_acceptance_manifest,
        reference=value["production_acceptance_manifest_reference"],
        bundle_reference=value["production_acceptance_manifest_bundle_reference"],
        kind="production-acceptance-manifest",
        authority_state_sha256=value["authority_state_sha256"],
        signer_registry_sha256=value["signer_registry_sha256"],
        revocation_state_sha256=value["revocation_state_sha256"],
        evaluation_time=evaluation_time,
    )
    if value["campaign_summary"] != receipt_summary:
        raise ProductionEvidenceError("campaign_summary differs from the public decision receipt")
    issued = _validate_timestamp(value["issued_at"], "issued_at")
    not_before = _validate_timestamp(value["not_before"], "not_before")
    expires = _validate_timestamp(value["expires_at"], "expires_at")
    if not (
        not_before <= issued < expires
        and (expires - not_before).total_seconds() <= 7 * 24 * 60 * 60
    ):
        raise ProductionEvidenceError(
            "production summary validity must satisfy not_before <= issued_at < expires_at"
        )
    receipt_issued = _validate_timestamp(
        qualification_evidence_decision_receipt["issued_at"],
        "decision receipt issued_at",
    )
    receipt_not_before = _validate_timestamp(
        qualification_evidence_decision_receipt["not_before"],
        "decision receipt not_before",
    )
    receipt_expires = _validate_timestamp(
        qualification_evidence_decision_receipt["expires_at"],
        "decision receipt expires_at",
    )
    if not (
        receipt_not_before <= receipt_issued <= issued
        and not_before >= receipt_not_before
        and expires <= receipt_expires
    ):
        raise ProductionEvidenceError(
            "production summary validity exceeds the public decision receipt"
        )
    manifest_issued = _validate_timestamp(
        production_acceptance_manifest["issued_at"], "manifest issued_at"
    )
    manifest_not_before = _validate_timestamp(
        production_acceptance_manifest["not_before"], "manifest not_before"
    )
    manifest_expires = _validate_timestamp(
        production_acceptance_manifest["expires_at"], "manifest expires_at"
    )
    if not (
        not_before >= manifest_not_before
        and issued >= manifest_issued
        and expires <= manifest_expires
    ):
        raise ProductionEvidenceError("production summary validity exceeds the manifest")
    if value["evidence_identity_sha256"] != _production_evidence_identity(value):
        raise ProductionEvidenceError("evidence_identity_sha256 is invalid")
    if not_before > evaluation_time or evaluation_time >= expires:
        raise ProductionEvidenceError("production summary is not active at evaluation_time")


def validate_release_verification_receipt(receipt: Mapping[str, Any]) -> None:
    """Validate the final Flow-only release verification receipt v1."""

    value = dict(receipt)
    if set(value) != _RELEASE_VERIFICATION_RECEIPT_KEYS:
        missing = sorted(_RELEASE_VERIFICATION_RECEIPT_KEYS - set(value))
        extra = sorted(set(value) - _RELEASE_VERIFICATION_RECEIPT_KEYS)
        raise ProductionEvidenceError(
            f"release verification receipt keys differ: missing={missing}, extra={extra}"
        )
    expected = {
        "schema_version": RELEASE_VERIFICATION_RECEIPT_SCHEMA,
        "verdict": "verified",
        "evidence_class": "remote-safe-synthetic",
        "target": "flow",
        "claim_scope": "production_flow",
        "source_repository": "OpenAdaptAI/openadapt-flow",
        "source_repository_id": "1291376938",
    }
    for field, expected_value in expected.items():
        if value[field] != expected_value:
            raise ProductionEvidenceError(f"release verification receipt {field} is invalid")
    for field in _RELEASE_VERIFICATION_RECEIPT_KEYS:
        if field.endswith("_sha256"):
            _require_digest(value[field], f"release verification receipt {field}")
    _validate_release_identity(value["release_identity"])
    for field in (
        "source_commit",
        "registry_source_commit",
        "trust_state_source_commit",
    ):
        if not isinstance(value[field], str) or _HEX40.fullmatch(value[field]) is None:
            raise ProductionEvidenceError(f"release verification receipt {field} is invalid")
    if not isinstance(value["version"], str) or _SEMVER.fullmatch(value["version"]) is None:
        raise ProductionEvidenceError("release verification receipt version is invalid")
    if value["tag"] != f"v{value['version']}":
        raise ProductionEvidenceError("release verification receipt tag differs from version")
    _require_decimal_id(value["draft_release_id"], "release verification draft_release_id")
    _require_positive_int(value["registry_revision"], "release verification registry_revision")
    verified_at = _validate_timestamp(value["verified_at"], "release verification verified_at")
    expires_at = _validate_timestamp(value["expires_at"], "release verification expires_at")
    if verified_at >= expires_at:
        raise ProductionEvidenceError("release verification receipt is expired at verification")
    projection = dict(value)
    verification_id = projection.pop("verification_id_sha256")
    expected_id = sha256_digest(
        RELEASE_VERIFICATION_RECEIPT_DOMAIN + canonical_json_bytes(projection)
    )
    if verification_id != expected_id:
        raise ProductionEvidenceError("release verification receipt id is invalid")


def validate_reference_pair(
    references: Sequence[Mapping[str, Any]],
    *,
    expected_regular_kind: str | None = None,
) -> None:
    """Reject a reference pair that changes kind, order, digest, or subject binding."""

    if len(references) != 2:
        raise ProductionEvidenceError(
            "an evidence pair must contain one regular object and one bundle"
        )
    regular = dict(references[0])
    bundle = dict(references[1])
    validate_object_reference(regular)
    validate_object_reference(bundle)
    kind = regular["kind"]
    if expected_regular_kind is not None and kind != expected_regular_kind:
        raise ProductionEvidenceError(f"regular evidence kind must be {expected_regular_kind!r}")
    if bundle["kind"] != f"{kind}-sigstore-bundle":
        raise ProductionEvidenceError("the Sigstore bundle must immediately follow its subject")
    if regular["subject_sha256"] is not None:
        raise ProductionEvidenceError("a regular evidence object subject must be null")
    if bundle["subject_sha256"] != regular["object_sha256"]:
        raise ProductionEvidenceError("the Sigstore bundle subject does not bind its object")
    for field in (
        "registry_source_commit",
        "registry_revision",
        "registry_head_sha256",
    ):
        if bundle[field] != regular[field]:
            raise ProductionEvidenceError(f"the evidence pair does not share {field}")


def validate_object_reference(reference: Mapping[str, Any]) -> None:
    """Validate the complete 16-key v2 public object-reference contract."""

    value = dict(reference)
    if set(value) != REFERENCE_KEYS:
        missing = sorted(REFERENCE_KEYS - set(value))
        extra = sorted(set(value) - REFERENCE_KEYS)
        raise ProductionEvidenceError(
            f"object reference keys differ: missing={missing}, extra={extra}"
        )
    expected_constants = {
        "schema_version": OBJECT_REFERENCE_SCHEMA,
        "repository": EVIDENCE_REPOSITORY,
        "repository_id": EVIDENCE_REPOSITORY_ID,
        "repository_owner_id": EVIDENCE_REPOSITORY_OWNER_ID,
    }
    for field, expected in expected_constants.items():
        if value[field] != expected:
            raise ProductionEvidenceError(f"object reference {field} is invalid")
    if not isinstance(value["registry_source_commit"], str) or not _HEX40.fullmatch(
        value["registry_source_commit"]
    ):
        raise ProductionEvidenceError("registry_source_commit must be 40 lowercase hex")
    _require_positive_int(value["registry_revision"], "registry_revision")
    for field in (
        "registry_head_sha256",
        "registry_entry_sha256",
        "object_sha256",
        "semantic_identity_sha256",
    ):
        _require_digest(value[field], field)
    _require_positive_int(value["size_bytes"], "size_bytes")
    _validate_object_path(value["object_path"])

    kind = value["kind"]
    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        raise ProductionEvidenceError("kind is invalid")
    if kind.endswith("-sigstore-bundle"):
        regular_kind = kind.removesuffix("-sigstore-bundle")
        if regular_kind not in REGULAR_EVIDENCE_KINDS:
            raise ProductionEvidenceError("bundle kind has no approved regular kind")
        if (
            value["object_schema_version"] != SIGSTORE_BUNDLE_MEDIA_TYPE
            or value["object_media_type"] != SIGSTORE_BUNDLE_MEDIA_TYPE
        ):
            raise ProductionEvidenceError("raw Sigstore bundle schema or media type is invalid")
        _require_digest(value["subject_sha256"], "subject_sha256")
    else:
        profile = REGULAR_EVIDENCE_KINDS.get(kind)
        if profile is None:
            raise ProductionEvidenceError(f"evidence kind is not approved: {kind!r}")
        if value["object_schema_version"] != profile.schema_version:
            raise ProductionEvidenceError("regular object schema version is invalid")
        if value["object_media_type"] != profile.media_type:
            raise ProductionEvidenceError("regular object media type is invalid")
        if value["subject_sha256"] is not None:
            raise ProductionEvidenceError("a regular object subject must be null")

    raw_digest = value["object_sha256"].removeprefix("sha256:")
    expected_path = f"{OBJECT_PATH_PREFIX}/{raw_digest[:2]}/{raw_digest}.{kind}.json"
    if value["object_path"] != expected_path:
        raise ProductionEvidenceError("object_path is not content-addressed by object_sha256")
    expected_entry = _registry_entry_sha256(value)
    if value["registry_entry_sha256"] != expected_entry:
        raise ProductionEvidenceError("registry_entry_sha256 is invalid")


def _build_reference(
    *,
    kind: str,
    object_schema_version: str,
    object_media_type: str,
    object_bytes: bytes,
    object_sha256: str,
    semantic_identity_sha256: str,
    subject_sha256: str | None,
    registry_source_commit: str,
    registry_revision: int,
    registry_head_sha256: str,
) -> dict[str, Any]:
    if not _HEX40.fullmatch(registry_source_commit):
        raise ProductionEvidenceError("registry_source_commit must be 40 lowercase hex")
    _require_positive_int(registry_revision, "registry_revision")
    _require_digest(registry_head_sha256, "registry_head_sha256")
    _require_digest(object_sha256, "object_sha256")
    _require_digest(semantic_identity_sha256, "semantic_identity_sha256")
    if subject_sha256 is not None:
        _require_digest(subject_sha256, "subject_sha256")
    raw_digest = object_sha256.removeprefix("sha256:")
    reference: dict[str, Any] = {
        "schema_version": OBJECT_REFERENCE_SCHEMA,
        "repository": EVIDENCE_REPOSITORY,
        "repository_id": EVIDENCE_REPOSITORY_ID,
        "repository_owner_id": EVIDENCE_REPOSITORY_OWNER_ID,
        "registry_source_commit": registry_source_commit,
        "registry_revision": registry_revision,
        "registry_head_sha256": registry_head_sha256,
        "registry_entry_sha256": "sha256:" + "0" * 64,
        "kind": kind,
        "object_schema_version": object_schema_version,
        "object_path": (f"{OBJECT_PATH_PREFIX}/{raw_digest[:2]}/{raw_digest}.{kind}.json"),
        "object_sha256": object_sha256,
        "size_bytes": len(object_bytes),
        "object_media_type": object_media_type,
        "semantic_identity_sha256": semantic_identity_sha256,
        "subject_sha256": subject_sha256,
    }
    reference["registry_entry_sha256"] = _registry_entry_sha256(reference)
    validate_object_reference(reference)
    return reference


def _registry_entry_sha256(reference: Mapping[str, Any]) -> str:
    projection = {field: reference[field] for field in REGISTRY_ENTRY_KEYS}
    return sha256_digest(REGISTRY_ENTRY_DIGEST_DOMAIN + canonical_json_bytes(projection))


def _validated_pair(
    references: Sequence[Mapping[str, Any]], expected_kind: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_reference_pair(references, expected_regular_kind=expected_kind)
    return dict(references[0]), dict(references[1])


def _validate_summary_reference_fields(value: Mapping[str, Any]) -> None:
    fields = (
        (
            "qualification_evidence_decision_receipt_reference",
            "qualification_evidence_decision_receipt_bundle_reference",
            "qualification-evidence-decision-receipt",
        ),
        (
            "qualification_admission_reference",
            "qualification_admission_bundle_reference",
            "qualification-admission",
        ),
        (
            "production_acceptance_manifest_reference",
            "production_acceptance_manifest_bundle_reference",
            "production-acceptance-manifest",
        ),
    )
    for regular_field, bundle_field, kind in fields:
        regular = value[regular_field]
        bundle = value[bundle_field]
        if not isinstance(regular, Mapping) or not isinstance(bundle, Mapping):
            raise ProductionEvidenceError(f"{kind} references must be objects")
        validate_reference_pair((regular, bundle), expected_regular_kind=kind)


def _validate_release_identity(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _RELEASE_IDENTITY_KEYS:
        raise ProductionEvidenceError("release_identity keys are invalid")
    if value["schema_version"] != "openadapt.monotonic-production-release/v1":
        raise ProductionEvidenceError("release_identity schema_version is invalid")
    if value["channel"] != "production":
        raise ProductionEvidenceError("release_identity channel must be production")
    sequence = _require_positive_int(value["sequence"], "release_identity.sequence")
    previous = value["previous_admission_sha256"]
    if sequence == 1:
        if previous is not None:
            raise ProductionEvidenceError(
                "the first release_identity previous_admission_sha256 must be null"
            )
    else:
        _require_digest(previous, "release_identity.previous_admission_sha256")


def _validate_decision_receipt(
    value: Mapping[str, Any],
    *,
    reference: Mapping[str, Any],
    bundle_reference: Mapping[str, Any],
    expected_authority_state_sha256: str,
    expected_signer_registry_sha256: str,
    expected_revocation_state_sha256: str,
    verifier: DecisionReceiptVerifier,
    evaluation_time: datetime,
) -> dict[str, Any]:
    receipt = dict(value)
    if set(receipt) != _DECISION_RECEIPT_KEYS:
        missing = sorted(_DECISION_RECEIPT_KEYS - set(receipt))
        extra = sorted(set(receipt) - _DECISION_RECEIPT_KEYS)
        raise ProductionEvidenceError(
            f"decision receipt keys differ: missing={missing}, extra={extra}"
        )
    if receipt["schema_version"] != DECISION_RECEIPT_SCHEMA:
        raise ProductionEvidenceError("decision receipt schema_version is invalid")
    if receipt["evidence_class"] not in {"private-customer", "remote-safe-synthetic"}:
        raise ProductionEvidenceError("decision receipt evidence_class is invalid")
    for field in _DECISION_RECEIPT_KEYS:
        if field.endswith("_sha256"):
            _require_digest(receipt[field], f"decision receipt {field}")
    _require_positive_int(receipt["decision_revision"], "decision receipt decision_revision")
    if (
        len(
            {
                receipt["decision_commitment_sha256"],
                receipt["evidence_manifest_sha256"],
                receipt["evidence_manifest_readback_sha256"],
                receipt["campaign_artifact_sha256"],
            }
        )
        != 4
    ):
        raise ProductionEvidenceError("decision receipt commitments must be distinct")
    if receipt["signer_registry_sha256"] != expected_signer_registry_sha256:
        raise ProductionEvidenceError("decision receipt signer registry differs")
    if receipt["revocation_state_sha256"] != expected_revocation_state_sha256:
        raise ProductionEvidenceError("decision receipt revocation state differs")
    if (
        not isinstance(receipt["bundle_version"], str)
        or _SEMVER.fullmatch(receipt["bundle_version"]) is None
    ):
        raise ProductionEvidenceError("decision receipt bundle_version is invalid")
    if receipt["entity_class"] not in {
        "insurance claim",
        "item",
        "loan application",
        "patient record",
        "record",
    }:
        raise ProductionEvidenceError("decision receipt entity_class is not remote-safe")
    if receipt["verdict"] != "ADMIT":
        raise ProductionEvidenceError("decision receipt verdict must be ADMIT")
    if receipt["algorithm"] != "ed25519":
        raise ProductionEvidenceError("decision receipt algorithm must be ed25519")
    if (
        not isinstance(receipt["issuer_key_id"], str)
        or _KEY_ID.fullmatch(receipt["issuer_key_id"]) is None
    ):
        raise ProductionEvidenceError("decision receipt issuer_key_id is invalid")
    try:
        signature = base64.b64decode(receipt["signature"], validate=True)
    except (binascii.Error, TypeError) as exc:
        raise ProductionEvidenceError("decision receipt signature is invalid base64") from exc
    if len(signature) != 64:
        raise ProductionEvidenceError("decision receipt signature is not 64-byte Ed25519")
    _validate_decision_signing_statement(receipt)
    _validate_decision_issuer(receipt["issuer"], evidence_class=receipt["evidence_class"])
    issued = _validate_timestamp(receipt["issued_at"], "decision receipt issued_at")
    not_before = _validate_timestamp(receipt["not_before"], "decision receipt not_before")
    expires = _validate_timestamp(receipt["expires_at"], "decision receipt expires_at")
    if not (not_before <= issued < expires):
        raise ProductionEvidenceError("decision receipt validity is invalid")
    if (expires - not_before).total_seconds() > 7 * 24 * 60 * 60:
        raise ProductionEvidenceError("decision receipt validity exceeds seven days")
    if not_before > evaluation_time or evaluation_time >= expires:
        raise ProductionEvidenceError("decision receipt is not active at evaluation_time")
    if not isinstance(reference, Mapping):
        raise ProductionEvidenceError("decision receipt reference must be an object")
    validate_object_reference(reference)
    if reference["kind"] != "qualification-evidence-decision-receipt":
        raise ProductionEvidenceError("decision receipt reference kind is invalid")
    object_sha256 = sha256_digest(canonical_json_bytes(receipt) + b"\n")
    if reference["object_sha256"] != object_sha256:
        raise ProductionEvidenceError("decision receipt does not match its public reference")
    semantic_identity_sha256 = _regular_semantic_identity(
        kind="qualification-evidence-decision-receipt",
        object_schema_version=DECISION_RECEIPT_SCHEMA,
        object_value=receipt,
    )
    if reference["semantic_identity_sha256"] != semantic_identity_sha256:
        raise ProductionEvidenceError("decision receipt semantic identity differs")
    verifier.verify(
        receipt,
        reference=reference,
        bundle_reference=bundle_reference,
        object_sha256=object_sha256,
        semantic_identity_sha256=semantic_identity_sha256,
        authority_state_sha256=expected_authority_state_sha256,
        signer_registry_sha256=expected_signer_registry_sha256,
        revocation_state_sha256=expected_revocation_state_sha256,
        evaluation_time=evaluation_time,
    )

    campaign_summary = receipt["campaign_summary"]
    if not isinstance(campaign_summary, Mapping) or set(campaign_summary) != (
        _DECISION_CAMPAIGN_SUMMARY_KEYS
    ):
        raise ProductionEvidenceError("decision receipt campaign_summary keys are invalid")
    if campaign_summary["schema_version"] != DECISION_CAMPAIGN_SUMMARY_SCHEMA:
        raise ProductionEvidenceError("decision receipt campaign_summary schema_version is invalid")
    minimum = _require_positive_int(
        campaign_summary["minimum_trials_per_task_condition"],
        "decision receipt minimum_trials_per_task_condition",
    )
    task_count = _require_positive_int(
        campaign_summary["task_count"], "decision receipt task_count"
    )
    if minimum != 3:
        raise ProductionEvidenceError("decision receipt minimum trial count must be exactly three")
    classes = campaign_summary["classes"]
    _validate_campaign_summary(classes)
    if any(counts["task_condition_cell_count"] < task_count for counts in classes.values()):
        raise ProductionEvidenceError("decision receipt omits a task/class cell")
    return {qualification_class: dict(counts) for qualification_class, counts in classes.items()}


def _validate_decision_signing_statement(receipt: Mapping[str, Any]) -> None:
    statement = receipt["signing_statement"]
    if not isinstance(statement, Mapping) or set(statement) != _SIGNING_STATEMENT_KEYS:
        raise ProductionEvidenceError("decision receipt signing_statement keys are invalid")
    unsigned = dict(receipt)
    unsigned.pop("signature")
    unsigned.pop("signing_statement")
    unsigned_bytes = canonical_json_bytes(unsigned) + b"\n"
    expected = {
        "schema_version": "openadapt.qualification-evidence-signing-statement/v1",
        "object_schema_version": DECISION_RECEIPT_SCHEMA,
        "signature_domain": DECISION_RECEIPT_SIGNATURE_DOMAIN.decode("utf-8"),
        "unsigned_object_sha256": sha256_digest(unsigned_bytes),
        "unsigned_size_bytes": len(unsigned_bytes),
        "commitment_scheme": "sha256-canonical-json-lf",
    }
    if dict(statement) != expected:
        raise ProductionEvidenceError("decision receipt signing_statement is invalid")


def _validate_decision_issuer(value: Any, *, evidence_class: str) -> None:
    if not isinstance(value, Mapping) or set(value) != _ISSUER_KEYS:
        raise ProductionEvidenceError("decision receipt issuer keys are invalid")
    expected = (
        {
            "repository": "OpenAdaptAI/openadapt-internal",
            "repository_id": "1170060695",
            "repository_owner_id": "132681217",
            "workflow": ".github/workflows/issue-private-qualification-evidence-decision.yml",
            "ref": "refs/heads/main",
            "environment": "private-qualification-evidence-decision",
        }
        if evidence_class == "private-customer"
        else {
            "repository": "OpenAdaptAI/.github",
            "repository_id": "858454062",
            "repository_owner_id": "132681217",
            "workflow": ".github/workflows/issue-synthetic-qualification-evidence-decision.yml",
            "ref": "refs/heads/main",
            "environment": "synthetic-qualification-evidence-decision",
        }
    )
    actual = dict(value)
    source_commit = actual.pop("source_commit")
    if (
        actual != expected
        or not isinstance(source_commit, str)
        or _HEX40.fullmatch(source_commit) is None
    ):
        raise ProductionEvidenceError("decision receipt issuer is invalid")


def _validate_acceptance_issuer(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _ISSUER_KEYS:
        raise ProductionEvidenceError("production acceptance issuer keys are invalid")
    expected = {
        "repository": "OpenAdaptAI/openadapt-evals",
        "repository_id": "1135998197",
        "repository_owner_id": "132681217",
        "workflow": ".github/workflows/issue-production-acceptance.yml",
        "ref": "refs/heads/main",
        "environment": "production-acceptance",
    }
    actual = dict(value)
    source_commit = actual.pop("source_commit")
    if (
        actual != expected
        or not isinstance(source_commit, str)
        or _HEX40.fullmatch(source_commit) is None
    ):
        raise ProductionEvidenceError("production acceptance issuer is invalid")


def _validate_qualification_admission_binding(
    value: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any],
    receipt_reference: Mapping[str, Any],
    receipt_bundle_reference: Mapping[str, Any],
    admission_reference: Mapping[str, Any],
) -> None:
    admission = dict(value)
    if set(admission) != _QUALIFICATION_ADMISSION_KEYS:
        raise ProductionEvidenceError("qualification admission keys are invalid")
    if (
        admission["schema_version"] != QUALIFICATION_ADMISSION_SCHEMA
        or admission["verdict"] != "accepted"
        or admission["evidence_class"] != receipt["evidence_class"]
    ):
        raise ProductionEvidenceError("qualification admission identity is invalid")
    for field in _QUALIFICATION_ADMISSION_KEYS:
        if field.endswith("_sha256"):
            _require_digest(admission[field], f"qualification admission {field}")
    if (
        not isinstance(admission["bundle_version"], str)
        or _SEMVER.fullmatch(admission["bundle_version"]) is None
        or len(admission["bundle_version"]) > 64
    ):
        raise ProductionEvidenceError("qualification admission bundle_version is invalid")
    if (
        not isinstance(admission["entity_class"], str)
        or _ENTITY_CLASS.fullmatch(admission["entity_class"]) is None
    ):
        raise ProductionEvidenceError("qualification admission entity_class is invalid")
    bindings = {
        "organization_id_sha256": "organization_id_sha256",
        "workflow_id_sha256": "workflow_id_sha256",
        "workflow_version_id_sha256": "workflow_version_id_sha256",
        "bundle_version": "bundle_version",
        "bundle_sha256": "bundle_sha256",
        "admitted_runtime_sha256": "admitted_runtime_sha256",
        "application_contract_sha256": "application_contract_sha256",
        "environment_contract_sha256": "environment_contract_sha256",
        "input_contract_sha256": "input_contract_sha256",
        "action_contract_sha256": "action_contract_sha256",
        "identity_contract_sha256": "identity_contract_sha256",
        "effect_contract_sha256": "effect_contract_sha256",
        "policy_contract_sha256": "policy_contract_sha256",
        "evidence_authority_sha256": "evidence_authority_contract_sha256",
        "campaign_artifact_sha256": "campaign_artifact_sha256",
        "campaign_permit_sha256": "campaign_permit_sha256",
        "signer_registry_sha256": "signer_registry_sha256",
        "revocation_state_sha256": "revocation_state_sha256",
        "entity_class": "entity_class",
    }
    if any(admission[left] != receipt[right] for left, right in bindings.items()):
        raise ProductionEvidenceError("qualification admission differs from decision receipt")
    if admission["campaign_summary"] != receipt["campaign_summary"]["classes"]:
        raise ProductionEvidenceError("qualification admission campaign summary differs")
    if admission["decision_receipt_reference"] != dict(receipt_reference) or admission[
        "decision_receipt_bundle_reference"
    ] != dict(receipt_bundle_reference):
        raise ProductionEvidenceError("qualification admission receipt references differ")
    local_identity = admission["local_identity_opening"]
    normalized_local_identity = (
        dict(local_identity) if isinstance(local_identity, Mapping) else None
    )
    if normalized_local_identity != {
        "schema_version": "openadapt.qualification-local-identity-opening/v1",
        "algorithm": "hmac-sha256",
        "required": True,
        "customer_controlled_secret_required": True,
        "exact_contract_match_required": True,
        "revalidation_before_actuation": True,
        "maximum_age_seconds": 60,
    }:
        raise ProductionEvidenceError("qualification admission local identity opening differs")
    _validate_qualification_admission_issuer(admission["issuer"])
    admission_issued = _validate_timestamp(admission["issued_at"], "admission issued_at")
    admission_not_before = _validate_timestamp(admission["not_before"], "admission not_before")
    admission_expires = _validate_timestamp(admission["expires_at"], "admission expires_at")
    receipt_issued = _validate_timestamp(receipt["issued_at"], "receipt issued_at")
    receipt_not_before = _validate_timestamp(receipt["not_before"], "receipt not_before")
    receipt_expires = _validate_timestamp(receipt["expires_at"], "receipt expires_at")
    if not (
        receipt_not_before <= receipt_issued <= admission_issued < admission_expires
        and admission_not_before >= receipt_not_before
        and admission_expires <= receipt_expires
        and (admission_expires - admission_not_before).total_seconds() <= 7 * 24 * 60 * 60
    ):
        raise ProductionEvidenceError("qualification admission validity exceeds receipt")
    projection = dict(admission)
    admission_id = projection.pop("admission_id_sha256")
    if admission_id != sha256_digest(
        QUALIFICATION_ADMISSION_DIGEST_DOMAIN + canonical_json_bytes(projection)
    ):
        raise ProductionEvidenceError("qualification admission id is invalid")
    if admission_reference["object_sha256"] != sha256_digest(
        canonical_json_bytes(admission) + b"\n"
    ):
        raise ProductionEvidenceError("qualification admission reference differs")


def _validate_qualification_admission_issuer(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _ISSUER_KEYS:
        raise ProductionEvidenceError("qualification admission issuer keys are invalid")
    expected = {
        "repository": "OpenAdaptAI/.github",
        "repository_id": "858454062",
        "repository_owner_id": "132681217",
        "workflow": ".github/workflows/issue-qualification-admission.yml",
        "ref": "refs/heads/main",
        "environment": "qualification-admission",
    }
    actual = dict(value)
    source_commit = actual.pop("source_commit")
    if (
        actual != expected
        or not isinstance(source_commit, str)
        or _HEX40.fullmatch(source_commit) is None
    ):
        raise ProductionEvidenceError("qualification admission issuer is invalid")


def _validate_acceptance_manifest_binding(
    value: Mapping[str, Any],
    *,
    summary: Mapping[str, Any],
    receipt: Mapping[str, Any],
    qualification_admission: Mapping[str, Any],
    expected_publication_assets: Sequence[Mapping[str, Any]],
) -> None:
    manifest = dict(value)
    if set(manifest) != _PRODUCTION_ACCEPTANCE_MANIFEST_KEYS:
        raise ProductionEvidenceError("production acceptance manifest keys are invalid")
    if manifest["schema_version"] != PRODUCTION_ACCEPTANCE_MANIFEST_SCHEMA:
        raise ProductionEvidenceError("production acceptance manifest schema is invalid")
    _validate_acceptance_issuer(manifest["issuer"])
    common_fields = (
        "target",
        "verdict",
        "claim_scope",
        "acceptance_policy_sha256",
        "lifecycle_policy_sha256",
        "release_identity",
        "release_sha256",
        "artifact_inventory_sha256",
        "publication_staging",
        "publication_staging_sha256",
        "qualification_evidence_decision_receipt_reference",
        "qualification_evidence_decision_receipt_bundle_reference",
        "qualification_admission_reference",
        "qualification_admission_bundle_reference",
        "campaign_summary",
        "authority_state_sha256",
        "revocation_state_sha256",
        "signer_registry_sha256",
        "issuer",
    )
    if any(manifest[field] != summary[field] for field in common_fields):
        raise ProductionEvidenceError("production acceptance summary differs from manifest")
    if manifest["campaign_summary"] != qualification_admission["campaign_summary"]:
        raise ProductionEvidenceError("production acceptance manifest campaign differs")
    release = manifest["release"]
    if not isinstance(release, Mapping) or set(release) != _RELEASE_CANDIDATE_KEYS:
        raise ProductionEvidenceError("production release candidate keys are invalid")
    target_contract = _TARGET_RELEASE_CONTRACTS[manifest["target"]]
    if (
        release["schema_version"] != "openadapt.production-release-candidate/v1"
        or release["kind"] != target_contract[3]
        or release["source_repository"] != target_contract[1]
        or release["source_repository_id"] != target_contract[2]
    ):
        raise ProductionEvidenceError("production release candidate identity is invalid")
    if release["kind"] == "package":
        if (
            not isinstance(release["version"], str)
            or not release["version"]
            or len(release["version"]) > 80
            or not isinstance(release["tag"], str)
            or not release["tag"]
            or len(release["tag"]) > 120
            or release["deployment_id"] is not None
            or release["deployment_sha256"] is not None
        ):
            raise ProductionEvidenceError("production package release candidate profile is invalid")
    elif (
        release["version"] is not None
        or release["tag"] is not None
        or not isinstance(release["deployment_id"], str)
        or _DECIMAL_ID.fullmatch(release["deployment_id"]) is None
        or not isinstance(release["deployment_sha256"], str)
        or _SHA256.fullmatch(release["deployment_sha256"]) is None
    ):
        raise ProductionEvidenceError("production deployment release candidate profile is invalid")
    artifact_profile = _TARGET_ARTIFACT_CONTRACTS[manifest["target"]]
    if [item.get("kind") for item in expected_publication_assets] != sorted(artifact_profile):
        raise ProductionEvidenceError("production artifact kinds differ from target profile")
    if list(expected_publication_assets) != sorted(
        expected_publication_assets,
        key=lambda item: (item["kind"], item["name"], item["sha256"]),
    ):
        raise ProductionEvidenceError("production artifacts are not canonically ordered")
    for artifact in expected_publication_assets:
        media_type, destinations = artifact_profile[artifact["kind"]]
        if artifact["media_type"] != media_type or artifact["publish_destinations"] != list(
            destinations
        ):
            raise ProductionEvidenceError(
                "production artifact media type or destinations differ from target profile"
            )
    if manifest["target"] == "desktop":
        if any(
            token in artifact["name"].casefold()
            for artifact in expected_publication_assets
            for token in ("beta", "candidate", "adhoc", "unsigned")
        ):
            raise ProductionEvidenceError(
                "Desktop release filenames contain a non-production label"
            )
        version = release["version"]
        expected_names = {
            "verification-metadata-linux-x86-64": (
                f"OpenAdapt-Desktop-v{version}-linux-x86_64-verification.json"
            ),
            "verification-metadata-macos-arm64": (
                f"OpenAdapt-Desktop-v{version}-macos-arm64-verification.json"
            ),
            "verification-metadata-macos-x86-64": (
                f"OpenAdapt-Desktop-v{version}-macos-x86_64-verification.json"
            ),
            "verification-metadata-windows-x86-64": (
                f"OpenAdapt-Desktop-v{version}-windows-x86_64-verification.json"
            ),
        }
        if any(
            artifact["name"] != expected_names[artifact["kind"]]
            for artifact in expected_publication_assets
            if artifact["kind"] in expected_names
        ):
            raise ProductionEvidenceError(
                "Desktop verification metadata filename differs from target profile"
            )
    if release["artifacts"] != list(expected_publication_assets):
        raise ProductionEvidenceError("production release candidate artifacts differ")
    if (
        not isinstance(release["source_commit"], str)
        or _HEX40.fullmatch(release["source_commit"]) is None
    ):
        raise ProductionEvidenceError("production release candidate source_commit is invalid")
    release_projection = {
        "target": manifest["target"],
        "claim_scope": manifest["claim_scope"],
        "release": dict(release),
    }
    if manifest["release_sha256"] != sha256_digest(
        RELEASE_CANDIDATE_DIGEST_DOMAIN + canonical_json_bytes(release_projection)
    ):
        raise ProductionEvidenceError("production release candidate digest is invalid")
    inventory = manifest["artifact_inventory"]
    if not isinstance(inventory, Mapping) or set(inventory) != _ARTIFACT_INVENTORY_KEYS:
        raise ProductionEvidenceError("production artifact inventory keys are invalid")
    if dict(inventory) != {
        "schema_version": "openadapt.production-release-artifact-inventory/v1",
        "target": manifest["target"],
        "claim_scope": manifest["claim_scope"],
        "artifacts": list(expected_publication_assets),
    }:
        raise ProductionEvidenceError("production artifact inventory differs")
    inventory_projection = {
        "target": manifest["target"],
        "claim_scope": manifest["claim_scope"],
        "artifacts": list(expected_publication_assets),
    }
    if manifest["artifact_inventory_sha256"] != sha256_digest(
        ARTIFACT_INVENTORY_DIGEST_DOMAIN + canonical_json_bytes(inventory_projection)
    ):
        raise ProductionEvidenceError("production artifact inventory digest is invalid")
    staging = manifest["publication_staging"]
    if (
        staging["repository"] != release["source_repository"]
        or staging["repository_id"] != release["source_repository_id"]
        or staging["target_commitish"] != release["source_commit"]
        or (release["tag"] is not None and staging["tag"] != release["tag"])
    ):
        raise ProductionEvidenceError("production publication staging differs from release")
    manifest_issued = _validate_timestamp(manifest["issued_at"], "manifest issued_at")
    manifest_not_before = _validate_timestamp(manifest["not_before"], "manifest not_before")
    manifest_expires = _validate_timestamp(manifest["expires_at"], "manifest expires_at")
    if not (
        manifest_not_before <= manifest_issued < manifest_expires
        and (manifest_expires - manifest_not_before).total_seconds() <= 7 * 24 * 60 * 60
    ):
        raise ProductionEvidenceError("production manifest validity is invalid")
    if _validate_timestamp(staging["observed_at"], "staging observed_at") > manifest_issued:
        raise ProductionEvidenceError("publication staging was observed after manifest issuance")
    for child, label in ((receipt, "receipt"), (qualification_admission, "admission")):
        if (
            manifest_not_before < _validate_timestamp(child["not_before"], f"{label} not_before")
            or manifest_issued < _validate_timestamp(child["issued_at"], f"{label} issued_at")
            or manifest_expires > _validate_timestamp(child["expires_at"], f"{label} expires_at")
        ):
            raise ProductionEvidenceError(f"production manifest validity exceeds {label}")
    manifest_reference = summary["production_acceptance_manifest_reference"]
    if manifest_reference["object_sha256"] != sha256_digest(canonical_json_bytes(manifest) + b"\n"):
        raise ProductionEvidenceError("production acceptance manifest reference differs")


def _validate_publication_staging(
    value: Any, *, expected_assets: Sequence[Mapping[str, Any]]
) -> str:
    if not isinstance(value, Mapping) or set(value) != _PUBLICATION_STAGING_KEYS:
        raise ProductionEvidenceError("publication_staging keys are invalid")
    staging = dict(value)
    if staging["schema_version"] != PUBLICATION_STAGING_SCHEMA:
        raise ProductionEvidenceError("publication_staging schema_version is invalid")
    if (
        not isinstance(staging["repository"], str)
        or re.fullmatch(r"OpenAdaptAI/[A-Za-z0-9._-]+", staging["repository"]) is None
    ):
        raise ProductionEvidenceError("publication_staging repository is invalid")
    for field in (
        "repository_id",
        "draft_release_id",
        "release_app_id",
        "release_app_installation_id",
        "release_app_bot_user_id",
    ):
        _require_decimal_id(staging[field], f"publication_staging {field}")
    if (
        staging["release_app_id"] != "4730708"
        or staging["release_app_installation_id"] != "156835568"
        or staging["release_app_bot_user_id"] != "321543906"
        or staging["release_author_login"] != "openadapt-release[bot]"
    ):
        raise ProductionEvidenceError("publication_staging release App identity is invalid")
    if staging["draft"] is not True or staging["prerelease"] is not False:
        raise ProductionEvidenceError("publication_staging must be a non-prerelease draft")
    immutable_releases = staging["immutable_releases"]
    if (
        not isinstance(immutable_releases, Mapping)
        or set(immutable_releases) != {"enabled", "enforced_by_owner"}
        or immutable_releases["enabled"] is not True
        or not isinstance(immutable_releases["enforced_by_owner"], bool)
    ):
        raise ProductionEvidenceError("immutable GitHub releases evidence is invalid")
    immutable_releases_sha256 = sha256_digest(
        IMMUTABLE_RELEASES_DIGEST_DOMAIN + canonical_json_bytes(dict(immutable_releases))
    )
    if staging["immutable_releases_sha256"] != immutable_releases_sha256:
        raise ProductionEvidenceError("immutable GitHub releases digest is invalid")
    if not isinstance(staging["tag"], str) or _TAG.fullmatch(staging["tag"]) is None:
        raise ProductionEvidenceError("publication_staging tag is invalid")
    if (
        not isinstance(staging["target_commitish"], str)
        or _HEX40.fullmatch(staging["target_commitish"]) is None
    ):
        raise ProductionEvidenceError("publication_staging target_commitish is invalid")
    tag_ref_state = staging["tag_ref_state"]
    if not isinstance(tag_ref_state, Mapping) or dict(tag_ref_state) != {
        "ref": f"refs/tags/{staging['tag']}",
        "exists": False,
    }:
        raise ProductionEvidenceError("publication_staging tag_ref_state is invalid")
    tag_ref_state_sha256 = sha256_digest(
        TAG_REF_STATE_DIGEST_DOMAIN + canonical_json_bytes(dict(tag_ref_state))
    )
    if staging["tag_ref_state_sha256"] != tag_ref_state_sha256:
        raise ProductionEvidenceError("publication_staging tag_ref_state_sha256 is invalid")
    _validate_timestamp(staging["observed_at"], "publication_staging observed_at")
    _validate_publication_assets(staging["assets"], expected_assets=expected_assets)
    tag_rulesets_sha256 = _validate_tag_rulesets(
        staging["tag_rulesets"],
        repository=staging["repository"],
        repository_id=staging["repository_id"],
        tag=staging["tag"],
    )
    if staging["tag_rulesets_sha256"] != tag_rulesets_sha256:
        raise ProductionEvidenceError("publication_staging tag_rulesets_sha256 is invalid")
    return sha256_digest(PUBLICATION_STAGING_DIGEST_DOMAIN + canonical_json_bytes(staging))


def _validate_publication_assets(
    value: Any, *, expected_assets: Sequence[Mapping[str, Any]]
) -> None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ProductionEvidenceError("publication_staging assets must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    folded_names: set[str] = set()
    asset_ids: set[str] = set()
    for index, raw_asset in enumerate(value):
        if not isinstance(raw_asset, Mapping) or set(raw_asset) != _ASSET_KEYS:
            raise ProductionEvidenceError(f"publication asset {index} keys are invalid")
        asset = dict(raw_asset)
        _require_decimal_id(asset["asset_id"], f"publication asset {index} asset_id")
        _require_decimal_id(asset["uploader_id"], f"publication asset {index} uploader_id")
        if (
            asset["uploader_id"] != "321543906"
            or asset["uploader_login"] != "openadapt-release[bot]"
        ):
            raise ProductionEvidenceError(f"publication asset {index} uploader is invalid")
        if not isinstance(asset["name"], str) or _ASSET_NAME.fullmatch(asset["name"]) is None:
            raise ProductionEvidenceError(f"publication asset {index} name is invalid")
        if not isinstance(asset["kind"], str) or _KIND.fullmatch(asset["kind"]) is None:
            raise ProductionEvidenceError(f"publication asset {index} kind is invalid")
        _require_digest(asset["sha256"], f"publication asset {index} sha256")
        _require_positive_int(asset["size_bytes"], f"publication asset {index} size_bytes")
        if (
            not isinstance(asset["media_type"], str)
            or "/" not in asset["media_type"]
            or len(asset["media_type"]) > 200
        ):
            raise ProductionEvidenceError(f"publication asset {index} media_type is invalid")
        destinations = asset["publish_destinations"]
        if (
            not isinstance(destinations, list)
            or not destinations
            or destinations != sorted(set(destinations))
            or any(item not in {"deployment", "github-release", "pypi"} for item in destinations)
        ):
            raise ProductionEvidenceError(
                f"publication asset {index} publish_destinations are invalid"
            )
        if (
            asset["name"] in names
            or asset["name"].casefold() in folded_names
            or asset["asset_id"] in asset_ids
        ):
            raise ProductionEvidenceError("publication assets contain a duplicate")
        names.add(asset["name"])
        folded_names.add(asset["name"].casefold())
        asset_ids.add(asset["asset_id"])
        normalized.append(asset)
    if normalized != sorted(normalized, key=lambda item: (item["name"], item["asset_id"])):
        raise ProductionEvidenceError("publication assets are not canonically ordered")

    expected: list[dict[str, Any]] = []
    for index, raw_asset in enumerate(expected_assets):
        if not isinstance(raw_asset, Mapping) or set(raw_asset) != _EXPECTED_ASSET_KEYS:
            raise ProductionEvidenceError(f"expected publication asset {index} keys are invalid")
        asset = dict(raw_asset)
        if not isinstance(asset["name"], str) or _ASSET_NAME.fullmatch(asset["name"]) is None:
            raise ProductionEvidenceError(f"expected publication asset {index} name is invalid")
        if not isinstance(asset["kind"], str) or _KIND.fullmatch(asset["kind"]) is None:
            raise ProductionEvidenceError(f"expected publication asset {index} kind is invalid")
        _require_digest(asset["sha256"], f"expected publication asset {index} sha256")
        _require_positive_int(asset["size_bytes"], f"expected publication asset {index} size_bytes")
        if not isinstance(asset["media_type"], str) or "/" not in asset["media_type"]:
            raise ProductionEvidenceError(
                f"expected publication asset {index} media_type is invalid"
            )
        destinations = asset["publish_destinations"]
        if (
            not isinstance(destinations, list)
            or not destinations
            or destinations != sorted(set(destinations))
            or any(item not in {"deployment", "github-release", "pypi"} for item in destinations)
        ):
            raise ProductionEvidenceError(
                f"expected publication asset {index} publish_destinations are invalid"
            )
        expected.append(asset)
    actual_projection = [
        {field: asset[field] for field in _EXPECTED_ASSET_KEYS} for asset in normalized
    ]

    def asset_key(item: Mapping[str, Any]) -> tuple[Any, Any, Any]:
        return item["kind"], item["name"], item["sha256"]

    if sorted(actual_projection, key=asset_key) != sorted(expected, key=asset_key):
        raise ProductionEvidenceError("publication assets differ from the release candidate")


def _validate_tag_rulesets(value: Any, *, repository: str, repository_id: str, tag: str) -> str:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProductionEvidenceError("publication tag_rulesets must be an array")
    rulesets = [dict(item) if isinstance(item, Mapping) else {} for item in value]
    if [item.get("role") for item in rulesets] != ["creation_authority", "immutability"]:
        raise ProductionEvidenceError("publication tag_rulesets roles or order are invalid")
    for index, ruleset in enumerate(rulesets):
        if set(ruleset) != _TAG_RULESET_KEYS:
            raise ProductionEvidenceError(f"publication tag ruleset {index} keys are invalid")
        if ruleset["schema_version"] != TAG_RULESET_SCHEMA:
            raise ProductionEvidenceError(f"publication tag ruleset {index} schema is invalid")
        if ruleset["repository"] != repository or ruleset["repository_id"] != repository_id:
            raise ProductionEvidenceError(f"publication tag ruleset {index} repository differs")
        _require_decimal_id(ruleset["ruleset_id"], f"tag ruleset {index} ruleset_id")
        expected_name = (
            "OpenAdapt policy: release tag creation"
            if ruleset["role"] == "creation_authority"
            else "OpenAdapt policy: immutable release tags"
        )
        if ruleset["name"] != expected_name:
            raise ProductionEvidenceError(f"publication tag ruleset {index} name is invalid")
        if ruleset["target"] != "tag" or ruleset["enforcement"] != "active":
            raise ProductionEvidenceError(f"publication tag ruleset {index} is not active")
        _validate_tag_conditions(ruleset["conditions"], tag=tag, index=index)

        bypass = ruleset["bypass_actors"]
        if not isinstance(bypass, list):
            raise ProductionEvidenceError(f"publication tag ruleset {index} bypass is invalid")
        expected_bypass = (
            [
                {
                    "actor_id": "4730708",
                    "actor_type": "Integration",
                    "bypass_mode": "always",
                }
            ]
            if ruleset["role"] == "creation_authority"
            else []
        )
        if bypass != expected_bypass or any(
            not isinstance(item, Mapping) or set(item) != _BYPASS_ACTOR_KEYS for item in bypass
        ):
            raise ProductionEvidenceError(f"publication tag ruleset {index} bypass differs")

        rules = ruleset["rules"]
        expected_rules = (
            [{"type": "creation"}]
            if ruleset["role"] == "creation_authority"
            else [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {
                    "type": "update",
                    "parameters": {"update_allows_fetch_and_merge": False},
                },
            ]
        )
        if not isinstance(rules, list) or rules != expected_rules:
            raise ProductionEvidenceError(f"publication tag ruleset {index} rules differ")
        for rule in rules:
            expected_keys = _UPDATE_RULE_KEYS if rule["type"] == "update" else _RULE_KEYS
            if not isinstance(rule, Mapping) or set(rule) != expected_keys:
                raise ProductionEvidenceError(f"publication tag ruleset {index} rules are invalid")
            if rule["type"] == "update":
                parameters = rule["parameters"]
                if not isinstance(parameters, Mapping) or set(parameters) != (
                    _UPDATE_PARAMETERS_KEYS
                ):
                    raise ProductionEvidenceError(
                        f"publication tag ruleset {index} update parameters are invalid"
                    )
    return sha256_digest(TAG_RULESETS_DIGEST_DOMAIN + canonical_json_bytes(rulesets))


def _validate_tag_conditions(value: Any, *, tag: str, index: int) -> None:
    if not isinstance(value, Mapping) or set(value) != _CONDITION_KEYS:
        raise ProductionEvidenceError(f"publication tag ruleset {index} conditions are invalid")
    ref_name = value["ref_name"]
    if not isinstance(ref_name, Mapping) or set(ref_name) != _REF_NAME_KEYS:
        raise ProductionEvidenceError(f"publication tag ruleset {index} ref_name is invalid")
    include = ref_name["include"]
    exclude = ref_name["exclude"]
    for label, patterns in (("include", include), ("exclude", exclude)):
        if (
            not isinstance(patterns, list)
            or any(not isinstance(pattern, str) or not pattern for pattern in patterns)
            or patterns != sorted(set(patterns))
        ):
            raise ProductionEvidenceError(
                f"publication tag ruleset {index} {label} patterns are invalid"
            )
    if {"include": include, "exclude": exclude} != {
        "include": ["refs/tags/v*"],
        "exclude": [],
    }:
        raise ProductionEvidenceError(
            f"publication tag ruleset {index} must match exactly refs/tags/v*"
        )
    if not fnmatchcase(f"refs/tags/{tag}", include[0]):
        raise ProductionEvidenceError(f"publication tag ruleset {index} does not match the tag")


def _require_decimal_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DECIMAL_ID.fullmatch(value) is None:
        raise ProductionEvidenceError(f"{field} must be a positive decimal string")
    return value


def _validate_campaign_summary(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _CAMPAIGN_CLASSES:
        raise ProductionEvidenceError(
            "campaign_summary must contain exactly the six qualification classes"
        )
    for qualification_class, raw_counts in value.items():
        if not isinstance(raw_counts, Mapping) or set(raw_counts) != _CAMPAIGN_CLASS_SUMMARY_KEYS:
            raise ProductionEvidenceError(
                f"campaign_summary.{qualification_class} keys are invalid"
            )
        for field, count in raw_counts.items():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ProductionEvidenceError(
                    f"campaign_summary.{qualification_class}.{field} must be non-negative"
                )
        cells = raw_counts["task_condition_cell_count"]
        minimum = raw_counts["minimum_trials_per_cell"]
        observed = raw_counts["observed_trial_count"]
        if cells < 1 or minimum < 3 or observed < cells * minimum:
            raise ProductionEvidenceError(
                f"campaign_summary.{qualification_class} lacks three trials per cell"
            )
        for field in (
            "silent_incorrect_success_count",
            "over_halt_count",
            "unsafe_effect_count",
            "blind_retry_count",
        ):
            if raw_counts[field] != 0:
                raise ProductionEvidenceError(
                    f"accepted campaign has nonzero {qualification_class}.{field}"
                )
    healthy = value["healthy"]
    idempotency = value["idempotency_replay"]
    uncertain = value["uncertain_delivery"]
    attended = value["declared_attended"]
    repair = value["governed_repair"]
    for qualification_class, counts in (
        ("healthy", healthy),
        ("idempotency_replay", idempotency),
    ):
        for field in ("model_call_count", "unplanned_intervention_count"):
            if counts[field] != 0:
                raise ProductionEvidenceError(f"{qualification_class}.{field} must be zero")
    if idempotency["replay_dispatch_count"] != 0:
        raise ProductionEvidenceError("idempotency_replay.replay_dispatch_count must be zero")
    if (
        uncertain["reconciliation_required_count"] != uncertain["observed_trial_count"]
        or uncertain["blind_retry_count"] != 0
        or uncertain["replay_dispatch_count"] != 0
    ):
        raise ProductionEvidenceError(
            "each uncertain-delivery trial must require reconciliation without redispatch"
        )
    if (
        attended["authenticated_bound_decision_count"] != attended["observed_trial_count"]
        or attended["live_target_revalidation_count"] != attended["observed_trial_count"]
    ):
        raise ProductionEvidenceError(
            "each declared-attended trial must bind a decision and target revalidation"
        )
    for field in (
        "policy_approved_repair_count",
        "approved_repair_count",
        "retained_repair_evidence_count",
        "live_target_revalidation_count",
    ):
        if repair[field] != repair["observed_trial_count"]:
            raise ProductionEvidenceError(f"each governed-repair trial must provide {field}")
    if repair["unverified_direct_action_count"] != 0:
        raise ProductionEvidenceError("governed-repair trials cannot use unverified direct action")


def _production_evidence_identity(value: Mapping[str, Any]) -> str:
    projection = {
        field: value[field]
        for field in (
            "target",
            "claim_scope",
            "release_identity",
            "release_sha256",
            "artifact_inventory_sha256",
            "publication_staging_sha256",
            "qualification_evidence_decision_receipt_reference",
            "qualification_admission_reference",
            "production_acceptance_manifest_reference",
            "campaign_summary",
            "authority_state_sha256",
            "revocation_state_sha256",
            "signer_registry_sha256",
            "issuer",
        )
    }
    return sha256_digest(PRODUCTION_EVIDENCE_IDENTITY_DOMAIN + canonical_json_bytes(projection))


def _validate_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not _WHOLE_SECOND_UTC.fullmatch(value):
        raise ProductionEvidenceError(f"{field} must be whole-second UTC")
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00").astimezone(timezone.utc)


def _validate_json_value(value: Any, context: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        raise ProductionEvidenceError(f"{context} contains a floating-point value")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProductionEvidenceError(f"{context} contains a non-string key")
            _validate_json_value(item, f"{context}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{context}[{index}]")
        return
    raise ProductionEvidenceError(f"{context} contains a non-JSON value")


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ProductionEvidenceError(f"{field} must be sha256:<64-lowercase-hex>")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProductionEvidenceError(f"{field} must be a positive integer")
    return value


def _validate_object_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ProductionEvidenceError("object_path is invalid")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}", value) is None:
        raise ProductionEvidenceError("object_path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProductionEvidenceError("object_path is invalid")
    return value


def _validate_raw_json_object(value: Any, context: str) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise ProductionEvidenceError(f"{context} must be non-empty raw bytes")
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionEvidenceError(f"{context} must contain one JSON object") from exc
    if not isinstance(decoded, dict):
        raise ProductionEvidenceError(f"{context} must contain one JSON object")
    return value
