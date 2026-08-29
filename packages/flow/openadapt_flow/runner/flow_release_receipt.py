"""Exact central Flow release verification receipt used by hosted runners."""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, b64encode
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from openadapt_flow.qualification_admission_v2 import canonical_json

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")
_MAX_RECEIPT_BYTES = 64 * 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
_VERIFICATION_ID_DOMAIN = b"OpenAdapt qualification release verification receipt v1\0"


class _Closed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


def _utc_seconds(value: str, *, label: str) -> datetime:
    if not _UTC_RE.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise ValueError(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


class MonotonicProductionReleaseIdentity(_Closed):
    schema_version: Literal["openadapt.monotonic-production-release/v1"]
    channel: Literal["production"]
    sequence: StrictInt = Field(ge=1, le=_MAX_SAFE_INTEGER)
    previous_admission_sha256: str | None = Field(default=None, pattern=_SHA256_RE)


class FlowReleaseVerificationReceipt(_Closed):
    schema_version: Literal["openadapt.qualification-release-verification-receipt/v1"]
    verification_id_sha256: str = Field(pattern=_SHA256_RE)
    verdict: Literal["verified"]
    evidence_class: Literal["remote-safe-synthetic"]
    target: Literal["flow"]
    claim_scope: Literal["production_flow"]
    admission_object_sha256: str = Field(pattern=_SHA256_RE)
    admission_bundle_object_sha256: str = Field(pattern=_SHA256_RE)
    admission_id_sha256: str = Field(pattern=_SHA256_RE)
    release_sha256: str = Field(pattern=_SHA256_RE)
    artifact_inventory_sha256: str = Field(pattern=_SHA256_RE)
    release_identity: MonotonicProductionReleaseIdentity
    source_repository: Literal["OpenAdaptAI/openadapt-flow"]
    source_repository_id: Literal["1291376938"]
    source_commit: str = Field(pattern=_COMMIT_RE)
    version: str = Field(pattern=_VERSION_RE)
    tag: str
    draft_release_id: str = Field(pattern=_POSITIVE_INTEGER_RE)
    publication_staging_sha256: str = Field(pattern=_SHA256_RE)
    authority_state_sha256: str = Field(pattern=_SHA256_RE)
    revocation_state_sha256: str = Field(pattern=_SHA256_RE)
    signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    acceptance_summary_object_sha256: str = Field(pattern=_SHA256_RE)
    acceptance_manifest_object_sha256: str = Field(pattern=_SHA256_RE)
    decision_receipt_object_sha256: str = Field(pattern=_SHA256_RE)
    qualification_admission_object_sha256: str = Field(pattern=_SHA256_RE)
    qualification_admission_id_sha256: str = Field(pattern=_SHA256_RE)
    workflow_version_id_sha256: str = Field(pattern=_SHA256_RE)
    workflow_bundle_sha256: str = Field(pattern=_SHA256_RE)
    admitted_runtime_sha256: str = Field(pattern=_SHA256_RE)
    verified_at: str
    expires_at: str
    registry_source_commit: str = Field(pattern=_COMMIT_RE)
    registry_revision: StrictInt = Field(ge=1, le=_MAX_SAFE_INTEGER)
    registry_head_sha256: str = Field(pattern=_SHA256_RE)
    trust_state_source_commit: str = Field(pattern=_COMMIT_RE)

    @model_validator(mode="after")
    def _self_binding(self) -> "FlowReleaseVerificationReceipt":
        if self.tag != f"v{self.version}":
            raise ValueError("Flow receipt tag differs from its version")
        projection = self.model_dump(mode="json")
        projection.pop("verification_id_sha256")
        expected = (
            "sha256:"
            + hashlib.sha256(
                _VERIFICATION_ID_DOMAIN + canonical_json(projection)
            ).hexdigest()
        )
        if self.verification_id_sha256 != expected:
            raise ValueError("Flow receipt verification digest is invalid")
        _utc_seconds(self.verified_at, label="Flow receipt verified_at")
        _utc_seconds(self.expires_at, label="Flow receipt expires_at")
        return self

    def require_current(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("Flow receipt current time is naive")
        current = current.astimezone(timezone.utc)
        if _utc_seconds(self.verified_at, label="Flow receipt verified_at") > current:
            raise ValueError("Flow receipt verification is in the future")
        if _utc_seconds(self.expires_at, label="Flow receipt expires_at") <= current:
            raise ValueError("Flow receipt is expired")


class HostedFlowReleaseIdentity(_Closed):
    schema_version: Literal["openadapt.hosted-flow-release/v1"] = (
        "openadapt.hosted-flow-release/v1"
    )
    verification_receipt_object_sha256: str = Field(pattern=_SHA256_RE)
    release_sha256: str = Field(pattern=_SHA256_RE)
    source_commit: str = Field(pattern=_COMMIT_RE)
    version: str = Field(pattern=_VERSION_RE)

    @classmethod
    def from_receipt(
        cls,
        receipt: FlowReleaseVerificationReceipt,
        *,
        object_sha256: str,
    ) -> "HostedFlowReleaseIdentity":
        return cls(
            verification_receipt_object_sha256=object_sha256,
            release_sha256=receipt.release_sha256,
            source_commit=receipt.source_commit,
            version=receipt.version,
        )


class FlowReleaseVerificationReceiptArtifactBytes(_Closed):
    artifact_bytes_base64: str = Field(max_length=87_384)
    artifact_sha256: str = Field(pattern=_SHA256_RE)

    def _raw_bytes(self) -> bytes:
        try:
            raw = b64decode(self.artifact_bytes_base64, validate=True)
        except ValueError as exc:
            raise ValueError("Flow receipt object bytes are invalid") from exc
        if (
            len(raw) < 2
            or len(raw) > _MAX_RECEIPT_BYTES
            or b64encode(raw).decode("ascii") != self.artifact_bytes_base64
            or "sha256:" + hashlib.sha256(raw).hexdigest() != self.artifact_sha256
        ):
            raise ValueError("Flow receipt object bytes or digest differ")
        return raw

    @model_validator(mode="after")
    def _bytes_match_digest(self) -> "FlowReleaseVerificationReceiptArtifactBytes":
        self._raw_bytes()
        return self

    def decode(
        self,
        *,
        now: datetime | None = None,
    ) -> FlowReleaseVerificationReceipt:
        raw = self._raw_bytes()
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Flow receipt object is not JSON") from exc
        receipt = FlowReleaseVerificationReceipt.model_validate(payload)
        receipt.require_current(now=now)
        return receipt

    def identity(
        self,
        *,
        now: datetime | None = None,
    ) -> HostedFlowReleaseIdentity:
        return HostedFlowReleaseIdentity.from_receipt(
            self.decode(now=now),
            object_sha256=self.artifact_sha256,
        )


def assert_hosted_flow_release(
    local: HostedFlowReleaseIdentity,
    artifact: FlowReleaseVerificationReceiptArtifactBytes,
    *,
    now: datetime | None = None,
) -> FlowReleaseVerificationReceipt:
    receipt = artifact.decode(now=now)
    if local != HostedFlowReleaseIdentity.from_receipt(
        receipt,
        object_sha256=artifact.artifact_sha256,
    ):
        raise ValueError("local Flow release differs from the verified Flow release")
    return receipt
