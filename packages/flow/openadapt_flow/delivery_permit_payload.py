"""Closed signed payloads for historical v2 and deadline-bounded v3 permits.

A v3 deadline limits the START of one input. Completion and acknowledgment
may arrive later and remain audit evidence; parsing never grants live input.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Final, Literal

from pydantic import Field, StrictInt, model_validator

from openadapt_flow.qualification_admission_v2 import (
    MAX_PERMIT_SNAPSHOT_AGE,
    ClosedSignedModel,
    canonical_json,
)

PERMIT_PAYLOAD_DOMAIN: Final[bytes] = (
    b"openadapt.production-delivery-permit-payload.v2\0"
)
PERMIT_V3_PAYLOAD_DOMAIN: Final[bytes] = (
    b"openadapt.production-delivery-permit-payload.v3\0"
)
PERMIT_PAYLOAD_SCHEMA: Final[
    Literal["openadapt.production-delivery-permit-payload/v2"]
] = "openadapt.production-delivery-permit-payload/v2"
PERMIT_V3_PAYLOAD_SCHEMA: Final[
    Literal["openadapt.production-delivery-permit-payload/v3"]
] = "openadapt.production-delivery-permit-payload/v3"
PERMIT_ARTIFACT_SCHEMA: Final[
    Literal["openadapt.production-delivery-permit-artifact/v2"]
] = "openadapt.production-delivery-permit-artifact/v2"
PERMIT_V3_ARTIFACT_SCHEMA: Final[
    Literal["openadapt.production-delivery-permit-artifact/v3"]
] = "openadapt.production-delivery-permit-artifact/v3"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,199}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
JS_MAX_SAFE_INTEGER: Final[int] = 9_007_199_254_740_991


def _parse_utc(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical UTC timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)


class _PermitPayload(ClosedSignedModel):
    schema_version: str
    execution_authority_id: str = Field(pattern=_UUID_RE)
    execution_authority_sha256: str = Field(pattern=_SHA256_RE)
    permit_id: str = Field(pattern=_ID_RE)
    run_id: str = Field(pattern=_UUID_RE)
    flow_run_id_sha256: str = Field(pattern=_SHA256_RE)
    run_request_sha256: str = Field(pattern=_SHA256_RE)
    action_request_sha256: str = Field(pattern=_SHA256_RE)
    admission_artifact_sha256: str = Field(pattern=_SHA256_RE)
    evidence_identity_sha256: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_revision: StrictInt = Field(
        ge=1, le=JS_MAX_SAFE_INTEGER
    )
    qualification_signer_registry_checked_at: str
    qualification_signer_registry_expires_at: str
    input_edge_sequence: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    authority_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    issued_at: str

    @model_validator(mode="after")
    def _valid_permit_time(self) -> "_PermitPayload":
        checked = _parse_utc(
            self.qualification_signer_registry_checked_at,
            field="permit registry checked_at",
        )
        expires = _parse_utc(
            self.qualification_signer_registry_expires_at,
            field="permit registry expires_at",
        )
        issued = _parse_utc(self.issued_at, field="permit issued_at")
        if not checked <= issued < expires:
            raise ValueError("permit was not issued under a fresh signer registry")
        if issued - checked > MAX_PERMIT_SNAPSHOT_AGE:
            raise ValueError(
                "permit registry check is stale; the issuing authority must recheck the signer registry within 60 seconds of permit issue"
            )
        if (
            hashlib.sha256(self.run_id.encode("utf-8")).hexdigest()
            != self.flow_run_id_sha256
        ):
            raise ValueError("permit run identity digest is invalid")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    def signing_bytes(self) -> bytes:
        domain = (
            PERMIT_V3_PAYLOAD_DOMAIN
            if self.schema_version == PERMIT_V3_PAYLOAD_SCHEMA
            else PERMIT_PAYLOAD_DOMAIN
        )
        return domain + self.canonical_bytes()

    def payload_sha256(self) -> str:
        return hashlib.sha256(self.signing_bytes()).hexdigest()


class ProductionDeliveryPermitPayload(_PermitPayload):
    """Immutable historical payload. Its canonical bytes and hash stay v2."""

    schema_version: Literal["openadapt.production-delivery-permit-payload/v2"] = (
        PERMIT_PAYLOAD_SCHEMA
    )


class ProductionDeliveryPermitV3Payload(_PermitPayload):
    """The exclusive signed deadline authorizing an input's start."""

    schema_version: Literal["openadapt.production-delivery-permit-payload/v3"] = (
        PERMIT_V3_PAYLOAD_SCHEMA
    )
    delivery_not_after: str

    @model_validator(mode="after")
    def _exclusive_start_deadline(self) -> "ProductionDeliveryPermitV3Payload":
        issued = _parse_utc(self.issued_at, field="permit issued_at")
        deadline = _parse_utc(
            self.delivery_not_after, field="permit delivery_not_after"
        )
        expires = _parse_utc(
            self.qualification_signer_registry_expires_at,
            field="permit registry expires_at",
        )
        if not issued < deadline <= expires:
            raise ValueError(
                "permit start deadline is not within its registry authority"
            )
        return self


AnyProductionDeliveryPermitPayload = (
    ProductionDeliveryPermitPayload | ProductionDeliveryPermitV3Payload
)


def parse_delivery_permit_payload(value: object) -> AnyProductionDeliveryPermitPayload:
    """Select an exact closed historical or current grammar, never guess one."""
    schema = value.get("schema_version") if isinstance(value, dict) else None
    if schema == PERMIT_V3_PAYLOAD_SCHEMA:
        return ProductionDeliveryPermitV3Payload.model_validate(value)
    return ProductionDeliveryPermitPayload.model_validate(value)
