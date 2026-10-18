"""Portable contracts for code capabilities and process evidence.

The models in this module contain no customer paths, program bytes, secrets,
or verifier recipes.  They identify immutable artifacts and the policy
contracts that a runner must enforce.  A digest proves identity.  It does not
prove correctness; qualification and an independent oracle provide that
evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, b64encode
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from openadapt_types.execute import ExecuteTerminalOutcomeV1, OracleTierV1

ARTIFACT_REF_SCHEMA: Literal["openadapt.artifact-ref/v1"] = "openadapt.artifact-ref/v1"
CODE_CAPABILITY_MANIFEST_SCHEMA: Literal["openadapt.code-capability-manifest/v1"] = (
    "openadapt.code-capability-manifest/v1"
)
CODE_CAPABILITY_ADMISSION_SCHEMA: Literal["openadapt.code-capability-admission/v1"] = (
    "openadapt.code-capability-admission/v1"
)
PROCESS_EVIDENCE_RECEIPT_SCHEMA: Literal["openadapt.process-evidence-receipt/v1"] = (
    "openadapt.process-evidence-receipt/v1"
)

_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,127}$"
_SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$"
_MEDIA_TYPE_PATTERN = (
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}$"
)
_HOST_PATTERN = (
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_MAX_ADMISSION_LIFETIME = timedelta(days=30)


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactStorageBoundary(str, Enum):
    LOCAL_PROTECTED = "local_protected"
    CUSTOMER_CONTROLLED = "customer_controlled"
    HOSTED_PRIVATE = "hosted_private"


class ArtifactDataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    REGULATED = "regulated"


class ArtifactVerificationState(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REFUTED = "refuted"
    INDETERMINATE = "indeterminate"


class CodeRuntimeKind(str, Enum):
    PYTHON = "python"


class CodeIsolationProfile(str, Enum):
    """Execution isolation that the runner can prove for one run."""

    TRUSTED_LOCAL = "trusted_local"
    OS_SANDBOX = "os_sandbox"
    CONTAINER = "container"
    VIRTUAL_MACHINE = "virtual_machine"


class CodeNetworkMode(str, Enum):
    NONE = "none"
    ALLOWLIST = "allowlist"


def _parse_timestamp(value: str, field_name: str) -> datetime:
    if re.fullmatch(_TIMESTAMP_PATTERN, value) is None:
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the language-neutral canonical JSON form for these contracts."""

    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest_payload(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _ordered_unique(values: tuple[Any, ...], field_name: str) -> tuple[Any, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values, key=str))


def _validate_relative_path(value: str, field_name: str) -> str:
    if "\\" in value or "\x00" in value:
        raise ValueError(f"{field_name} must be a portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or not value or value in {".", ".."} or ".." in path.parts:
        raise ValueError(f"{field_name} must be a portable relative path")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError(f"{field_name} must be canonical")
    return value


def _validate_signature(value: str, size: int, field_name: str) -> str:
    try:
        decoded = b64decode(value, validate=True)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be base64") from exc
    if len(decoded) != size or b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{field_name} is invalid")
    return value


class ArtifactRefV1(_StrictContract):
    """A path-free reference to one immutable process artifact."""

    schema_version: Literal["openadapt.artifact-ref/v1"] = ARTIFACT_REF_SCHEMA
    artifact_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    content_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    size_bytes: StrictInt = Field(ge=0, le=9_007_199_254_740_991)
    media_type: StrictStr = Field(
        min_length=3,
        max_length=192,
        pattern=_MEDIA_TYPE_PATTERN,
    )
    logical_name: StrictStr = Field(pattern=_NAME_PATTERN)
    producer_execution_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    producer_output_name: StrictStr = Field(pattern=_NAME_PATTERN)
    storage_boundary: ArtifactStorageBoundary
    data_classification: ArtifactDataClassification
    verification_state: ArtifactVerificationState
    verifier_receipt_digest: StrictStr | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    metadata_digest: StrictStr | None = Field(default=None, pattern=_SHA256_PATTERN)
    created_at: StrictStr = Field(
        min_length=20,
        max_length=40,
        pattern=_TIMESTAMP_PATTERN,
    )

    @model_validator(mode="after")
    def _validate_verification(self) -> ArtifactRefV1:
        _parse_timestamp(self.created_at, "created_at")
        if self.verification_state is ArtifactVerificationState.PENDING:
            if self.verifier_receipt_digest is not None:
                raise ValueError("a pending artifact cannot carry a verifier receipt")
        elif self.verifier_receipt_digest is None:
            raise ValueError("a terminal artifact state requires a verifier receipt")
        return self


class CodeArtifactOutputV1(_StrictContract):
    name: StrictStr = Field(pattern=_NAME_PATTERN)
    relative_path: StrictStr = Field(min_length=1, max_length=512)
    media_type: StrictStr = Field(
        min_length=3,
        max_length=192,
        pattern=_MEDIA_TYPE_PATTERN,
    )
    required: StrictBool = True

    @field_validator("relative_path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return _validate_relative_path(value, "relative_path")


class CodePermissionContractV1(_StrictContract):
    """Declared and admission-bound permissions for one code capability."""

    isolation_profile: CodeIsolationProfile
    readable_mounts: tuple[StrictStr, ...] = Field(
        default_factory=tuple,
        max_length=64,
        json_schema_extra={"uniqueItems": True},
    )
    writable_mounts: tuple[StrictStr, ...] = Field(
        default_factory=tuple,
        max_length=64,
        json_schema_extra={"uniqueItems": True},
    )
    secret_names: tuple[StrictStr, ...] = Field(
        default_factory=tuple,
        max_length=64,
        json_schema_extra={"uniqueItems": True},
    )
    network_mode: CodeNetworkMode = CodeNetworkMode.NONE
    allowed_network_hosts: tuple[StrictStr, ...] = Field(
        default_factory=tuple,
        max_length=64,
        json_schema_extra={"uniqueItems": True},
    )
    allow_process_spawn: StrictBool = False
    timeout_seconds: StrictInt = Field(ge=1, le=86_400)
    memory_limit_mb: StrictInt = Field(ge=16, le=1_048_576)
    output_limit_bytes: StrictInt = Field(ge=0, le=9_007_199_254_740_991)

    @field_validator("readable_mounts", "writable_mounts", "secret_names")
    @classmethod
    def _canonical_names(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        if any(re.fullmatch(_NAME_PATTERN, value) is None for value in values):
            raise ValueError(f"{info.field_name} contains an invalid name")
        return _ordered_unique(values, str(info.field_name))

    @field_validator("allowed_network_hosts")
    @classmethod
    def _canonical_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_HOST_PATTERN, value) is None for value in values):
            raise ValueError("allowed_network_hosts contains an invalid host")
        lowered = tuple(value.lower() for value in values)
        return _ordered_unique(lowered, "allowed_network_hosts")

    @model_validator(mode="after")
    def _network_contract(self) -> CodePermissionContractV1:
        if self.network_mode is CodeNetworkMode.NONE and self.allowed_network_hosts:
            raise ValueError("network mode none cannot allow network hosts")
        if (
            self.network_mode is CodeNetworkMode.ALLOWLIST
            and not self.allowed_network_hosts
        ):
            raise ValueError("network allowlist mode requires at least one host")
        return self

    @property
    def digest(self) -> str:
        return _digest_payload(self.model_dump(mode="json"))


class CodeCapabilityManifestV1(_StrictContract):
    """One immutable Python program with typed I/O and declared permissions."""

    schema_version: Literal["openadapt.code-capability-manifest/v1"] = (
        CODE_CAPABILITY_MANIFEST_SCHEMA
    )
    capability_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    capability_version_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    runtime_kind: Literal[CodeRuntimeKind.PYTHON] = CodeRuntimeKind.PYTHON
    runtime_version: StrictStr = Field(
        min_length=3,
        max_length=32,
        pattern=r"^3\.(10|11|12|13)(?:\.[0-9]{1,3})?$",
    )
    source_archive_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    lockfile_path: StrictStr = Field(min_length=1, max_length=512)
    lockfile_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    entrypoint: tuple[StrictStr, ...] = Field(min_length=1, max_length=64)
    input_schema_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    output_schema_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    outputs: tuple[CodeArtifactOutputV1, ...] = Field(
        max_length=64,
        json_schema_extra={"uniqueItems": True},
    )
    permissions: CodePermissionContractV1
    effect_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    oracle_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    qualification_campaign_digest: StrictStr = Field(pattern=_SHA256_PATTERN)

    @field_validator("lockfile_path")
    @classmethod
    def _portable_lockfile(cls, value: str) -> str:
        return _validate_relative_path(value, "lockfile_path")

    @field_validator("entrypoint")
    @classmethod
    def _direct_entrypoint(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or "\x00" in value for value in values):
            raise ValueError("entrypoint contains an invalid argument")
        _validate_relative_path(values[0], "entrypoint executable")
        return values

    @model_validator(mode="after")
    def _unique_outputs(self) -> CodeCapabilityManifestV1:
        names = tuple(item.name for item in self.outputs)
        paths = tuple(item.relative_path for item in self.outputs)
        if len(names) != len(set(names)):
            raise ValueError("code output names must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("code output paths must be unique")
        return self

    @property
    def digest(self) -> str:
        return _digest_payload(self.model_dump(mode="json"))


class CodeCapabilityAdmissionPayloadV1(_StrictContract):
    """Expiring authority for one exact code capability manifest."""

    schema_version: Literal["openadapt.code-capability-admission/v1"] = (
        CODE_CAPABILITY_ADMISSION_SCHEMA
    )
    admission_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    tenant_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    capability_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    capability_version_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    manifest_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    runtime_environment_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    permission_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    input_schema_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    output_schema_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    effect_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    oracle_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    operator_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    qualification_campaign_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    issuer_workflow: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    issuer_ref: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    issued_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    not_before: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    expires_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)

    @model_validator(mode="after")
    def _validity_interval(self) -> CodeCapabilityAdmissionPayloadV1:
        issued = _parse_timestamp(self.issued_at, "issued_at")
        starts = _parse_timestamp(self.not_before, "not_before")
        expires = _parse_timestamp(self.expires_at, "expires_at")
        if not issued - timedelta(minutes=5) <= starts <= issued + timedelta(minutes=5):
            raise ValueError("code admission start is outside issue skew")
        if expires <= starts:
            raise ValueError("code admission expiry must follow its start")
        if expires > issued + _MAX_ADMISSION_LIFETIME:
            raise ValueError("code admission lifetime exceeds 30 days")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


class CodeCapabilityAdmissionEnvelopeV1(_StrictContract):
    payload: CodeCapabilityAdmissionPayloadV1
    algorithm: Literal["ed25519"] = "ed25519"
    signature: StrictStr = Field(min_length=88, max_length=88)

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        return _validate_signature(value, 64, "code admission signature")

    @property
    def artifact_digest(self) -> str:
        return _digest_payload(self.model_dump(mode="json"))


class ProcessEvidenceReceiptV1(_StrictContract):
    """Signed root receipt for one process execution.

    Child receipts and evidence remain authoritative.  This receipt binds
    their digests into one terminal process result without carrying evidence
    bytes or customer data.
    """

    schema_version: Literal["openadapt.process-evidence-receipt/v1"] = (
        PROCESS_EVIDENCE_RECEIPT_SCHEMA
    )
    receipt_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    process_execution_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    process_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    app_package_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    environment_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    runner_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    outcome: ExecuteTerminalOutcomeV1
    oracle_tier: OracleTierV1
    delivery_uncertain: StrictBool
    model_used: StrictBool
    external_network_used: StrictBool
    child_receipt_digests: tuple[StrictStr, ...] = Field(
        min_length=1,
        max_length=1024,
        json_schema_extra={"uniqueItems": True},
    )
    human_receipt_digests: tuple[StrictStr, ...] = Field(
        default_factory=tuple,
        max_length=1024,
        json_schema_extra={"uniqueItems": True},
    )
    artifact_graph_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    evidence_root_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    issued_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature: StrictStr = Field(min_length=88, max_length=88)

    @field_validator("child_receipt_digests", "human_receipt_digests")
    @classmethod
    def _canonical_digests(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        if any(re.fullmatch(_SHA256_PATTERN, value) is None for value in values):
            raise ValueError(f"{info.field_name} contains an invalid digest")
        return _ordered_unique(values, str(info.field_name))

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        return _validate_signature(value, 64, "process receipt signature")

    @model_validator(mode="after")
    def _terminal_contract(self) -> ProcessEvidenceReceiptV1:
        _parse_timestamp(self.issued_at, "issued_at")
        verified = self.outcome in {
            ExecuteTerminalOutcomeV1.VERIFIED,
            ExecuteTerminalOutcomeV1.ROLLED_BACK_VERIFIED,
        }
        if verified:
            if self.delivery_uncertain:
                raise ValueError("a verified process cannot have uncertain delivery")
            if self.model_used:
                raise ValueError("a verified process requires a zero-model run")
            if self.oracle_tier < 2:
                raise ValueError("a verified process requires oracle tier 2 or 3")
        if (
            self.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
            and not self.delivery_uncertain
        ):
            raise ValueError(
                "reconciliation_required requires uncertain delivery or effect"
            )
        return self

    def unsigned_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"signature", "signature_algorithm"},
        )

    @property
    def digest(self) -> str:
        return _digest_payload(self.model_dump(mode="json"))
