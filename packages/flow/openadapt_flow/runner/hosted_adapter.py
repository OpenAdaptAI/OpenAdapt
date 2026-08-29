"""Strict Flow-owned bridge between a hosted lease and governed execution.

The Desktop host owns HTTP and credential storage. This module owns every
decision that can authorize or classify execution: admission verification,
local trust, input resolution, one-use reservation, managed child execution,
evidence projection, and terminal verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from base64 import b64decode, b64encode
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping, Protocol, Union
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.bundle_validation import compute_parameter_schema_digest
from openadapt_flow.execution_profiles import stamp_execution_outcome
from openadapt_flow.ir import (
    ManagedResultLossEvidence,
    RunReport,
    StepResult,
    Workflow,
    managed_result_loss_idempotency_sha256,
)
from openadapt_flow.private_file import (
    PrivateFileAclError,
    windows_descriptor_has_private_acl,
)
from openadapt_flow.production_qualification import (
    ProductionQualificationAuthority,
    ProductionQualificationGuard,
    _read_private_json,
)
from openadapt_flow.qualification import workflow_contract_sha256
from openadapt_flow.qualification_admission_v2 import (
    QualificationAdmissionEnvelope,
    QualificationAdmissionExpected,
    QualificationSignerRegistry,
    canonical_json,
    contract_sha256,
    verify_qualification_admission,
)
from openadapt_flow.runner.commands import build_run_argv
from openadapt_flow.runner.config import RunnerConfig, load_runner_config
from openadapt_flow.runner.dispatch_envelope import write_managed_dispatch_envelope
from openadapt_flow.runner.evidence import failure_events, refusal_events, report_events
from openadapt_flow.runner.flow_release_receipt import (
    FlowReleaseVerificationReceiptArtifactBytes,
    HostedFlowReleaseIdentity,
    assert_hosted_flow_release,
)
from openadapt_flow.runner.inputs import resolve_admitted_params
from openadapt_flow.runner.product_release import (
    ProductReleaseAdmissionArtifact,
    ProductReleaseAdmissionPayload,
    load_product_release_signer_trust,
    verify_product_release_admission,
)
from openadapt_flow.runner.protocol import (
    DispatchParamsValues,
    RunnerDispatchPayload,
    validate_runtime_param_name,
)
from openadapt_flow.runner.protocol import (
    dispatch_binding_sha256 as governed_dispatch_binding_sha256,
)
from openadapt_flow.runner.verify import Refusal, RefusalCode, verify_dispatch
from openadapt_flow.runtime.authorization import RuntimeParamScalar
from openadapt_flow.runtime.durable.authority import (
    REMOTE_AUTHORITY_TOKEN_ENV,
    REMOTE_AUTHORITY_URL_ENV,
    REMOTE_DISPATCH_SESSION_ID_ENV,
    DurableAuthority,
)
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
from openadapt_flow.terminal_verification_v2 import (
    RESULT_LOSS_CLOSURE_REQUEST_DOMAIN,
    ProductionDeliveryPermitChain,
    ProductionDeliveryResultLossClosureArtifact,
    ProductionTerminalVerificationContext,
    ProductionTerminalVerificationEnvelope,
    ProductionTerminalVerificationEnvelopeV2,
    ProductionTerminalVerificationExpected,
    build_production_terminal_verification,
    evidence_runner_signer_sha256,
    prepare_production_terminal_evidence,
    verify_production_delivery_result_loss_closure_binding,
    verify_production_terminal_verification_from_report,
    verify_production_terminal_verification_v2_signature,
    verify_production_terminal_verification_v3_signature,
)
from openadapt_flow.transaction import (
    DuplicateActuation,
    IdempotencyLedger,
    TransactionOutcome,
    classify_transaction_outcome,
)

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_HEX64 = r"^[a-f0-9]{64}$"
_IDEMPOTENCY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$"
_LEASE_TOKEN = r"^oal_[a-f0-9]{64}$"
_RUNNER_TOKEN = r"^oar_[a-f0-9]{64}$"
_SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,199}$"
_UTC_SECONDS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_MAX_RESULT_LOSS_CHAIN_BYTES = 1024 * 1024
_MAX_RESULT_LOSS_SNAPSHOT_BYTES = 3 * 1024 * 1024
_CHILD_START_DOMAIN = b"OpenAdapt managed child start v1\0"
_RUN_STORE_IDENTITY_DOMAIN = b"OpenAdapt managed run store identity v1\0"
_RESULT_LOSS_SNAPSHOT_DOMAIN = b"OpenAdapt managed result loss snapshot v1\0"
_MAX_TERMINAL_STATE_BYTES = 2 * (4 * ((_MAX_ARTIFACT_BYTES + 2) // 3)) + 4096
ManagedResultLossCode = Literal[
    "runner_exception",
    "report_missing",
    "report_invalid",
    "recovered_after_restart",
]

# The current runner credential is never part of a request model. Desktop may
# project it into this header only for POST /api/runners/register on the exact
# protected runner origin.
RUNNER_RENEWAL_HEADER = "x-openadapt-runner-renewal-token"


def registration_renewal_headers(current_runner_token: str | None) -> dict[str, str]:
    """Return the one register-only renewal header without retaining it."""

    if current_runner_token is None or current_runner_token == "":
        return {}
    if re.fullmatch(_RUNNER_TOKEN, current_runner_token) is None:
        raise ValueError("current runner renewal credential is invalid")
    return {RUNNER_RENEWAL_HEADER: current_runner_token}


def _utc_seconds(value: str, *, label: str) -> datetime:
    if _UTC_SECONDS.fullmatch(value) is None:
        raise ValueError(f"{label} is not canonical UTC seconds")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} is not canonical UTC seconds") from exc


class _Closed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class LocalRuntimeReleaseBinding(_Closed):
    target: Literal["flow", "desktop", "capture"]
    admission_id: str = Field(pattern=_UUID)
    admission_sha256: str = Field(pattern=_HEX64)
    release_version: str = Field(pattern=_SAFE_ID)
    release_artifact_sha256: str = Field(pattern=_HEX64)


CapabilityKind = Literal[
    "web", "windows", "macos", "linux", "rdp", "citrix", "rdp_window"
]


class RegisterCapabilities(_Closed):
    backends: tuple[CapabilityKind, ...] = Field(min_length=1, max_length=16)
    attended: bool
    effects_substrates: tuple[CapabilityKind, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _closed_capabilities(self) -> "RegisterCapabilities":
        for label, values in (
            ("backend", self.backends),
            ("effect substrate", self.effects_substrates),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"runner {label} capabilities are invalid")
        return self


class _RegisterRequestCommon(_Closed):
    name: str = Field(min_length=1, max_length=80)
    platform: Literal["windows", "macos", "linux"]
    agent_version: str = Field(min_length=1, max_length=40)
    engine_version: str = Field(min_length=1, max_length=40)
    mode: Literal["attended", "service"]
    capabilities: RegisterCapabilities
    local_runtime_release: dict[
        Literal["flow", "desktop", "capture"], LocalRuntimeReleaseBinding
    ]

    @model_validator(mode="after")
    def _exact_local_targets(self) -> "_RegisterRequestCommon":
        if set(self.local_runtime_release) != {"flow", "desktop", "capture"} or any(
            key != item.target for key, item in self.local_runtime_release.items()
        ):
            raise ValueError(
                "local runtime release targets must be flow, desktop, capture"
            )
        return self


class RegisterRequestV1(_RegisterRequestCommon):
    """Frozen Flow 1.34.0 registration shape."""

    schema_version: Literal["openadapt.hosted-runner-registration/v1"] = (
        "openadapt.hosted-runner-registration/v1"
    )


class RegisterRequestV2(_RegisterRequestCommon):
    schema_version: Literal["openadapt.hosted-runner-registration/v2"] = (
        "openadapt.hosted-runner-registration/v2"
    )
    local_flow_release: HostedFlowReleaseIdentity


RegisterRequest = RegisterRequestV2
RegisterRequestWire = Union[RegisterRequestV1, RegisterRequestV2]


def parse_register_request(
    value: RegisterRequestWire | Mapping[str, object],
) -> RegisterRequestWire:
    if isinstance(value, (RegisterRequestV1, RegisterRequestV2)):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("hosted registration is invalid")
    if value.get("schema_version") == "openadapt.hosted-runner-registration/v1":
        return RegisterRequestV1.model_validate(value)
    if value.get("schema_version") == "openadapt.hosted-runner-registration/v2":
        return RegisterRequestV2.model_validate(value)
    raise ValueError("hosted registration schema is unsupported")


class _RegisterResponseCommon(_Closed):
    runner_id: str = Field(pattern=_UUID)
    tenant_id: str = Field(pattern=_UUID)
    runner_session_id: str = Field(pattern=_UUID)
    runner_token: str = Field(pattern=_RUNNER_TOKEN, repr=False)
    token_expires_at: str

    @model_validator(mode="after")
    def _canonical_expiry(self) -> "_RegisterResponseCommon":
        _utc_seconds(self.token_expires_at, label="runner token expiry")
        return self


class RegisterResponseV1(_RegisterResponseCommon):
    schema_version: Literal["openadapt.hosted-runner-registration-result/v1"]


class RegisterResponseV2(_RegisterResponseCommon):
    schema_version: Literal["openadapt.hosted-runner-registration-result/v2"]


RegisterResponse = RegisterResponseV2
RegisterResponseWire = Union[RegisterResponseV1, RegisterResponseV2]


def parse_register_response(
    value: RegisterResponseWire | Mapping[str, object],
) -> RegisterResponseWire:
    if isinstance(value, (RegisterResponseV1, RegisterResponseV2)):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("hosted registration response is invalid")
    if value.get("schema_version") == "openadapt.hosted-runner-registration-result/v1":
        return RegisterResponseV1.model_validate(value)
    if value.get("schema_version") == "openadapt.hosted-runner-registration-result/v2":
        return RegisterResponseV2.model_validate(value)
    raise ValueError("hosted registration response schema is unsupported")


class PollRequest(_Closed):
    schema_version: Literal["openadapt.hosted-runner-poll/v1"] = (
        "openadapt.hosted-runner-poll/v1"
    )
    runner_session_id: str = Field(pattern=_UUID)
    wait_seconds: int = Field(ge=0, le=25)
    lease_seconds: int = Field(ge=1, le=900)


class AdmissionArtifactBytes(_Closed):
    artifact_bytes_base64: str = Field(min_length=4, max_length=2_796_204)
    artifact_sha256: str = Field(pattern=_HEX64)

    def decode(self) -> bytes:
        try:
            raw = b64decode(self.artifact_bytes_base64, validate=True)
        except ValueError as exc:
            raise ValueError("admission artifact is not canonical base64") from exc
        if len(raw) > _MAX_ARTIFACT_BYTES:
            raise ValueError("admission artifact exceeds the size limit")
        if b64encode(raw).decode("ascii") != self.artifact_bytes_base64:
            raise ValueError("admission artifact is not canonical base64")
        if hashlib.sha256(raw).hexdigest() != self.artifact_sha256:
            raise ValueError("admission artifact digest does not match its bytes")
        return raw

    @model_validator(mode="after")
    def _bytes_match_digest(self) -> "AdmissionArtifactBytes":
        self.decode()
        return self


class _HostedDispatchCommon(_Closed):
    dispatch_id: str = Field(pattern=_UUID)
    tenant_id: str = Field(pattern=_UUID)
    runner_id: str = Field(pattern=_UUID)
    runner_session_id: str = Field(pattern=_UUID)
    dispatch_session_id: str = Field(pattern=_UUID)
    run_id: str = Field(pattern=_UUID)
    workflow_id: str = Field(pattern=_UUID)
    workflow_version_id: str = Field(pattern=_UUID)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY)
    lease_token: str = Field(pattern=_LEASE_TOKEN, repr=False)
    lease_expires_at: str
    workflow_admission: AdmissionArtifactBytes
    managed_delivery_authority_url: str = Field(min_length=1, max_length=2048)
    delivery_authority_token: str = Field(pattern=_HEX64, repr=False)
    payload: RunnerDispatchPayload

    @model_validator(mode="after")
    def _exact_run_binding(self) -> "_HostedDispatchCommon":
        _utc_seconds(self.lease_expires_at, label="hosted lease expiry")
        if (
            self.payload.run_id != self.run_id
            or self.payload.workflow_id != self.workflow_id
        ):
            raise ValueError("hosted lease identity does not match its payload")
        if self.payload.bundle.version_id != self.workflow_version_id:
            raise ValueError("hosted lease workflow version does not match its bundle")
        return self


class HostedDispatchV1(_HostedDispatchCommon):
    """Frozen Flow 1.34.0 dispatch shape."""

    schema_version: Literal["openadapt.hosted-runner/v1"]
    product_release_admission: AdmissionArtifactBytes


class HostedDispatchV2(_HostedDispatchCommon):
    """Current dispatch with explicit delivery-authority identity."""

    schema_version: Literal["openadapt.hosted-runner/v2"] = "openadapt.hosted-runner/v2"
    flow_release_verification_receipt: FlowReleaseVerificationReceiptArtifactBytes
    product_release_admission: AdmissionArtifactBytes
    execution_authority_id: str = Field(pattern=_UUID)
    execution_authority_sha256: str = Field(pattern=_HEX64)
    execution_authority_signer_sha256: str = Field(pattern=_HEX64)


# Keep the established Python API name on the current wire type. Callers that
# negotiate explicitly can use the versioned names.
HostedDispatch = HostedDispatchV2
HostedDispatchWire = Union[HostedDispatchV1, HostedDispatchV2]


def parse_hosted_dispatch(
    value: HostedDispatchWire | Mapping[str, object],
) -> HostedDispatchWire:
    """Read both published dispatch versions without guessing a wire shape."""

    if isinstance(value, (HostedDispatchV1, HostedDispatchV2)):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("hosted dispatch is invalid")
    schema = value.get("schema_version")
    if schema == "openadapt.hosted-runner/v1":
        return HostedDispatchV1.model_validate(value)
    if schema == "openadapt.hosted-runner/v2":
        return HostedDispatchV2.model_validate(value)
    raise ValueError("hosted dispatch schema is unsupported")


class _HostedRecoveryBindingCommon(_Closed):
    """Callback state without params or the delivery-authority credential.

    This projection remains credential-bearing because it retains the lease
    token required for the exact terminal callback.
    """

    dispatch_id: str = Field(pattern=_UUID)
    runner_session_id: str = Field(pattern=_UUID)
    dispatch_session_id: str = Field(pattern=_UUID)
    run_id: str = Field(pattern=_UUID)
    workflow_id: str = Field(pattern=_UUID)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY)
    lease_token: str = Field(pattern=_LEASE_TOKEN, repr=False)
    workflow_admission_sha256: str = Field(pattern=_HEX64)
    bundle_content_digest: str = Field(pattern=_HEX64)
    authorization_id: str = Field(min_length=1, max_length=128)


class HostedRecoveryBindingV1(_HostedRecoveryBindingCommon):
    schema_version: Literal["openadapt.hosted-runner-recovery/v1"] = (
        "openadapt.hosted-runner-recovery/v1"
    )
    product_release_admission_sha256: str = Field(pattern=_HEX64)


class HostedRecoveryBindingV2(_HostedRecoveryBindingCommon):
    schema_version: Literal["openadapt.hosted-runner-recovery/v2"] = (
        "openadapt.hosted-runner-recovery/v2"
    )
    flow_release_verification_receipt_object_sha256: str = Field(
        pattern=r"^sha256:[a-f0-9]{64}$"
    )


HostedRecoveryBinding = HostedRecoveryBindingV2
HostedRecoveryBindingWire = Union[HostedRecoveryBindingV1, HostedRecoveryBindingV2]


class ManagedChildStartEvidence(_Closed):
    """Durable outer-runner boundary written before managed child entry."""

    schema_version: Literal["openadapt.managed-child-start/v1"] = (
        "openadapt.managed-child-start/v1"
    )
    started_at: str
    dispatch_id: str = Field(pattern=_UUID)
    dispatch_session_id: str = Field(pattern=_UUID)
    run_id: str = Field(pattern=_UUID)
    managed_dispatch_binding_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    authenticated_runner_id_sha256: str = Field(pattern=_HEX64)
    authenticated_session_id_sha256: str = Field(pattern=_HEX64)
    execution_authority_id: str = Field(pattern=_UUID)
    execution_authority_sha256: str = Field(pattern=_HEX64)
    execution_authority_signer_sha256: str = Field(pattern=_HEX64)
    run_store_identity_sha256: str = Field(pattern=_HEX64)
    marker_sha256: str = Field(pattern=_HEX64)

    def computed_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("marker_sha256", None)
        return hashlib.sha256(_CHILD_START_DOMAIN + canonical_json(payload)).hexdigest()

    @model_validator(mode="after")
    def _closed_marker(self) -> "ManagedChildStartEvidence":
        _utc_seconds(self.started_at, label="managed child start")
        if self.marker_sha256 != self.computed_sha256():
            raise ValueError("managed child start digest is invalid")
        return self

    @classmethod
    def create(cls, **values: Any) -> "ManagedChildStartEvidence":
        candidate = cls.model_construct(**values, marker_sha256="0" * 64)
        payload = candidate.model_dump(mode="json")
        payload["marker_sha256"] = candidate.computed_sha256()
        return cls.model_validate(payload)


class ProductionDeliveryResultLossClosureRequest(_Closed):
    """Credential-free request for one monotonic hosted result-loss fence."""

    schema_version: Literal[
        "openadapt.production-delivery-result-loss-closure-request/v2"
    ] = "openadapt.production-delivery-result-loss-closure-request/v2"
    child_start_evidence: ManagedChildStartEvidence
    expected_closure_sequence: Literal[0] = 0
    result_loss_observed_at: str

    @model_validator(mode="after")
    def _closed_request(self) -> "ProductionDeliveryResultLossClosureRequest":
        observed = _utc_seconds(
            self.result_loss_observed_at,
            label="result loss observation",
        )
        started = _utc_seconds(
            self.child_start_evidence.started_at,
            label="managed child start",
        )
        if started > observed:
            raise ValueError("result-loss closure request chronology is invalid")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    def request_sha256(self) -> str:
        return hashlib.sha256(
            RESULT_LOSS_CLOSURE_REQUEST_DOMAIN + self.canonical_bytes()
        ).hexdigest()


class ProductionDeliveryResultLossClosureResult(_Closed):
    """Exact Cloud authority closure and its atomically frozen permit chain."""

    schema_version: Literal[
        "openadapt.production-delivery-result-loss-closure-result/v2"
    ] = "openadapt.production-delivery-result-loss-closure-result/v2"
    status: Literal["closed"] = "closed"
    closure_artifact_bytes_base64: str = Field(max_length=2_796_204)
    closure_artifact_sha256: str = Field(pattern=_HEX64)
    permit_chain_bytes_base64: str = Field(max_length=2_796_204)
    permit_chain_sha256: str = Field(pattern=_HEX64)

    @staticmethod
    def _decode_canonical(
        value: str,
        *,
        label: str,
        maximum_bytes: int = _MAX_ARTIFACT_BYTES,
    ) -> bytes:
        try:
            raw = b64decode(value, validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} is not canonical base64") from exc
        if (
            not raw
            or len(raw) > maximum_bytes
            or b64encode(raw).decode("ascii") != value
        ):
            raise ValueError(f"{label} is not canonical base64")
        return raw

    def artifacts(
        self,
    ) -> tuple[
        ProductionDeliveryResultLossClosureArtifact,
        ProductionDeliveryPermitChain,
    ]:
        closure_raw = self._decode_canonical(
            self.closure_artifact_bytes_base64,
            label="result-loss closure artifact",
        )
        chain_raw = self._decode_canonical(
            self.permit_chain_bytes_base64,
            label="result-loss closure permit chain",
            maximum_bytes=_MAX_RESULT_LOSS_CHAIN_BYTES,
        )
        try:
            closure = ProductionDeliveryResultLossClosureArtifact.model_validate_json(
                closure_raw
            )
            chain = ProductionDeliveryPermitChain.model_validate_json(chain_raw)
        except ValueError as exc:
            raise ValueError("result-loss closure response is invalid") from exc
        if (
            canonical_json(closure) != closure_raw
            or canonical_json(chain) != chain_raw
            or hashlib.sha256(closure_raw).hexdigest() != self.closure_artifact_sha256
            or closure.artifact_sha256() != self.closure_artifact_sha256
            or chain.permit_chain_sha256 != self.permit_chain_sha256
            or closure.payload.permit_chain_sha256 != self.permit_chain_sha256
        ):
            raise ValueError("result-loss closure response binding is invalid")
        return closure, chain

    @model_validator(mode="after")
    def _closed_result(self) -> "ProductionDeliveryResultLossClosureResult":
        self.artifacts()
        return self


class ManagedResultLossSnapshot(_Closed):
    """One local CAS record for the loss evidence and exact permit-chain view."""

    schema_version: Literal["openadapt.managed-result-loss-snapshot/v1"] = (
        "openadapt.managed-result-loss-snapshot/v1"
    )
    evidence: ManagedResultLossEvidence
    permit_chain: ProductionDeliveryPermitChain
    closure_artifact: ProductionDeliveryResultLossClosureArtifact
    snapshot_sha256: str = Field(pattern=_HEX64)

    def computed_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("snapshot_sha256", None)
        return hashlib.sha256(
            _RESULT_LOSS_SNAPSHOT_DOMAIN + canonical_json(payload)
        ).hexdigest()

    @model_validator(mode="after")
    def _closed_snapshot(self) -> "ManagedResultLossSnapshot":
        if (
            self.snapshot_sha256 != self.computed_sha256()
            or self.evidence.delivery_result_loss_closure_artifact_sha256
            != self.closure_artifact.artifact_sha256()
            or self.closure_artifact.payload.permit_chain_sha256
            != self.permit_chain.permit_chain_sha256
        ):
            raise ValueError("managed result loss snapshot is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        evidence: ManagedResultLossEvidence,
        permit_chain: ProductionDeliveryPermitChain,
        closure_artifact: ProductionDeliveryResultLossClosureArtifact,
    ) -> "ManagedResultLossSnapshot":
        candidate = cls.model_construct(
            evidence=evidence,
            permit_chain=permit_chain,
            closure_artifact=closure_artifact,
            snapshot_sha256="0" * 64,
        )
        payload = candidate.model_dump(mode="json")
        payload["snapshot_sha256"] = candidate.computed_sha256()
        return cls.model_validate(payload)


class HostedTerminalEventV1(_Closed):
    """Frozen Flow 1.34.0 callback event."""

    schema_version: Literal["openadapt.hosted-runner-terminal/v1"] = (
        "openadapt.hosted-runner-terminal/v1"
    )
    run_id: str = Field(pattern=_UUID)
    outcome: Literal[
        "VERIFIED",
        "HALTED_BEFORE_EFFECT",
        "RECONCILIATION_REQUIRED",
        "FAILED_PLATFORM",
        "CANCELED",
        "REJECTED_POLICY",
        "COMPLETED_UNVERIFIED",
        "ROLLED_BACK",
    ]
    report_sha256: str = Field(pattern=_HEX64)
    started: bool
    uncertain_delivery: bool
    terminal_verification_artifact_bytes_base64: str | None = Field(
        default=None, max_length=2_796_204
    )
    terminal_verification_artifact_sha256: str | None = Field(
        default=None, pattern=_HEX64
    )

    @model_validator(mode="after")
    def _verified_requires_exact_v2_proof(self) -> "HostedTerminalEventV1":
        has_proof = self.terminal_verification_artifact_bytes_base64 is not None
        if has_proof != (self.terminal_verification_artifact_sha256 is not None):
            raise ValueError("terminal verification binding is incomplete")
        if self.outcome == "VERIFIED" and not has_proof:
            raise ValueError("VERIFIED requires exact terminal verification")
        if self.outcome != "VERIFIED" and has_proof:
            raise ValueError("non-VERIFIED callback cannot carry a success proof")
        if has_proof:
            assert self.terminal_verification_artifact_bytes_base64 is not None
            assert self.terminal_verification_artifact_sha256 is not None
            try:
                raw = b64decode(
                    self.terminal_verification_artifact_bytes_base64,
                    validate=True,
                )
                proof = ProductionTerminalVerificationEnvelopeV2.model_validate_json(
                    raw
                )
                verified_sha256 = verify_production_terminal_verification_v2_signature(
                    proof
                )
            except (ValueError, TypeError) as exc:
                raise ValueError("terminal verification artifact is invalid") from exc
            if (
                len(raw) > _MAX_ARTIFACT_BYTES
                or b64encode(raw).decode("ascii")
                != self.terminal_verification_artifact_bytes_base64
                or canonical_json(proof) != raw
                or verified_sha256 != self.terminal_verification_artifact_sha256
            ):
                raise ValueError("terminal verification artifact binding is invalid")
            if (
                proof.payload.run_id != self.run_id
                or proof.payload.run_report_sha256 != self.report_sha256
                or proof.payload.run_report_object_sha256 != self.report_sha256
            ):
                raise ValueError("terminal verification names a different run report")
        return self


class HostedTerminalEventV2(_Closed):
    schema_version: Literal["openadapt.hosted-runner-terminal/v2"] = (
        "openadapt.hosted-runner-terminal/v2"
    )
    run_id: str = Field(pattern=_UUID)
    outcome: Literal[
        "VERIFIED",
        "HALTED_BEFORE_EFFECT",
        "RECONCILIATION_REQUIRED",
        "FAILED_PLATFORM",
        "CANCELED",
        "REJECTED_POLICY",
        "COMPLETED_UNVERIFIED",
        "ROLLED_BACK",
    ]
    report_sha256: str = Field(pattern=_HEX64)
    started: bool
    uncertain_delivery: bool
    terminal_verification_artifact_bytes_base64: str | None = Field(
        default=None, max_length=2_796_204
    )
    terminal_verification_artifact_sha256: str | None = Field(
        default=None, pattern=_HEX64
    )

    @model_validator(mode="after")
    def _terminal_outcome_requires_exact_proof(self) -> "HostedTerminalEventV2":
        has_proof = self.terminal_verification_artifact_bytes_base64 is not None
        if has_proof != (self.terminal_verification_artifact_sha256 is not None):
            raise ValueError("terminal verification binding is incomplete")
        if (
            self.outcome
            in {
                "VERIFIED",
                "HALTED_BEFORE_EFFECT",
                "RECONCILIATION_REQUIRED",
            }
            and not has_proof
        ):
            raise ValueError(f"{self.outcome} requires exact terminal verification")
        if (
            self.outcome
            not in {
                "VERIFIED",
                "HALTED_BEFORE_EFFECT",
                "RECONCILIATION_REQUIRED",
            }
            and has_proof
        ):
            raise ValueError("terminal callback outcome cannot carry a v3 proof")
        if has_proof:
            assert self.terminal_verification_artifact_bytes_base64 is not None
            assert self.terminal_verification_artifact_sha256 is not None
            try:
                raw = b64decode(
                    self.terminal_verification_artifact_bytes_base64,
                    validate=True,
                )
                proof = ProductionTerminalVerificationEnvelope.model_validate_json(raw)
                verified_sha256 = verify_production_terminal_verification_v3_signature(
                    proof
                )
            except (ValueError, TypeError) as exc:
                raise ValueError("terminal verification artifact is invalid") from exc
            if (
                len(raw) > _MAX_ARTIFACT_BYTES
                or b64encode(raw).decode("ascii")
                != self.terminal_verification_artifact_bytes_base64
                or canonical_json(proof) != raw
                or verified_sha256 != self.terminal_verification_artifact_sha256
            ):
                raise ValueError("terminal verification artifact binding is invalid")
            if (
                proof.payload.run_id != self.run_id
                or proof.payload.run_report_sha256 != self.report_sha256
                or proof.payload.run_report_object_sha256 != self.report_sha256
            ):
                raise ValueError("terminal verification names a different run report")
            if proof.payload.run_receipt.transaction_outcome != self.outcome:
                raise ValueError("terminal verification names a different outcome")
            expected_uncertainty = proof.payload.pending_permit_count == 1
        else:
            expected_uncertainty = False
        if self.uncertain_delivery != expected_uncertainty:
            raise ValueError("terminal uncertainty conflicts with its signed proof")
        return self


# Keep the established Python API name on the current wire type.
HostedTerminalEvent = HostedTerminalEventV2
HostedTerminalEventWire = Union[HostedTerminalEventV1, HostedTerminalEventV2]


def parse_hosted_terminal_event(
    value: HostedTerminalEventWire | Mapping[str, object],
) -> HostedTerminalEventWire:
    """Read both terminal event versions through their exact validators."""

    if isinstance(value, (HostedTerminalEventV1, HostedTerminalEventV2)):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("hosted terminal event is invalid")
    schema = value.get("schema_version")
    if schema == "openadapt.hosted-runner-terminal/v1":
        return HostedTerminalEventV1.model_validate(value)
    if schema == "openadapt.hosted-runner-terminal/v2":
        return HostedTerminalEventV2.model_validate(value)
    raise ValueError("hosted terminal event schema is unsupported")


class HostedRunResult(_Closed):
    kind: Literal["result"] = "result"
    dispatch_id: str = Field(pattern=_UUID)
    run_id: str = Field(pattern=_UUID)
    outcome: TransactionOutcome
    evidence_batch: tuple[dict[str, Any], ...]
    terminal_verification: ProductionTerminalVerificationEnvelope | None = None
    started: bool
    uncertain_delivery: bool
    report_sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _closed_terminal(self) -> "HostedRunResult":
        if (
            self.outcome
            in {
                TransactionOutcome.VERIFIED,
                TransactionOutcome.HALTED_BEFORE_EFFECT,
                TransactionOutcome.RECONCILIATION_REQUIRED,
            }
            and self.terminal_verification is None
        ):
            raise ValueError("closed terminal outcome requires a signed v3 proof")
        if self.terminal_verification is not None and self.outcome not in {
            TransactionOutcome.VERIFIED,
            TransactionOutcome.HALTED_BEFORE_EFFECT,
            TransactionOutcome.RECONCILIATION_REQUIRED,
        }:
            raise ValueError("terminal outcome cannot carry a signed v3 proof")
        expected_uncertainty = bool(
            self.terminal_verification is not None
            and self.terminal_verification.payload.pending_permit_count == 1
        )
        if self.uncertain_delivery != expected_uncertainty:
            raise ValueError("uncertain delivery conflicts with its signed proof")
        if self.terminal_verification is not None and (
            self.terminal_verification.payload.run_id != self.run_id
            or self.terminal_verification.payload.run_report_sha256
            != self.report_sha256
            or self.terminal_verification.payload.run_report_object_sha256
            != self.report_sha256
            or self.terminal_verification.payload.run_receipt.transaction_outcome
            != self.outcome.value
        ):
            raise ValueError(
                "terminal verification names a different run report or outcome"
            )
        return self


class HostedDispatchRefusal(_Closed):
    """Local pre-actuation refusal that must not close a v2 Cloud lease.

    The v2 callback accepts only signed governed outcomes. A refusal has no
    terminal proof, so the caller must retain it locally and let Cloud move the
    started lease to reconciliation instead of sending a proofless terminal.
    """

    kind: Literal["refusal"] = "refusal"
    dispatch_id: str | None = None
    run_id: str | None = None
    code: str = Field(min_length=1, max_length=64)
    detail: str = Field(min_length=1, max_length=400)
    evidence_batch: tuple[dict[str, Any], ...] = ()
    started: Literal[False] = False
    uncertain_delivery: Literal[False] = False
    outcome: Literal["REJECTED_POLICY"] = "REJECTED_POLICY"
    report_sha256: str = Field(default="0" * 64, pattern=_HEX64)


class _CallbackRequestCommon(_Closed):
    dispatch_id: str = Field(pattern=_UUID)
    runner_session_id: str = Field(pattern=_UUID)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY)
    lease_token: str = Field(pattern=_LEASE_TOKEN, repr=False)
    workflow_admission_sha256: str = Field(pattern=_HEX64)
    events: tuple[dict[str, Any], ...] = Field(min_length=1, max_length=10_001)

    @model_validator(mode="after")
    def _atomic_terminal_batch(self) -> "_CallbackRequestCommon":
        callback_schema: object = getattr(self, "schema_version", None)
        if not isinstance(callback_schema, str):
            raise ValueError("callback schema is unsupported")
        expected_terminal_schema = {
            "openadapt.hosted-runner-callback/v1": (
                "openadapt.hosted-runner-terminal/v1"
            ),
            "openadapt.hosted-runner-callback/v2": (
                "openadapt.hosted-runner-terminal/v2"
            ),
        }.get(callback_schema)
        if expected_terminal_schema is None:
            raise ValueError("callback schema is unsupported")
        supported_terminal_schemas = {
            "openadapt.hosted-runner-terminal/v1",
            "openadapt.hosted-runner-terminal/v2",
        }
        terminal_indices = tuple(
            index
            for index, event in enumerate(self.events)
            if event.get("schema_version") in supported_terminal_schemas
        )
        if terminal_indices != (len(self.events) - 1,):
            raise ValueError("callback must end in exactly one terminal event")
        if self.events[-1].get("schema_version") != expected_terminal_schema:
            raise ValueError("callback terminal schema does not match its version")
        terminal = parse_hosted_terminal_event(self.events[-1])
        if callback_schema == "openadapt.hosted-runner-callback/v2" and (
            terminal.outcome
            not in {
                "VERIFIED",
                "HALTED_BEFORE_EFFECT",
                "RECONCILIATION_REQUIRED",
            }
        ):
            raise ValueError("callback v2 requires a signed governed terminal proof")
        summaries = tuple(
            event for event in self.events[:-1] if event.get("kind") == "run_summary"
        )
        if len(summaries) != 1:
            raise ValueError("callback must contain exactly one run summary")
        if any(event.get("run_id") != terminal.run_id for event in self.events[:-1]):
            raise ValueError("callback evidence names a different run")
        summary = summaries[0].get("run_summary")
        if not isinstance(summary, dict):
            raise ValueError("callback run summary is invalid")
        expected_status = {
            "VERIFIED": "confirmed",
            "HALTED_BEFORE_EFFECT": "halted-needs-attention",
        }.get(terminal.outcome, "failed")
        if summary.get("status") != expected_status:
            raise ValueError("callback run summary conflicts with terminal outcome")
        return self


class CallbackRequestV1(_CallbackRequestCommon):
    schema_version: Literal["openadapt.hosted-runner-callback/v1"] = (
        "openadapt.hosted-runner-callback/v1"
    )
    product_release_admission_sha256: str = Field(pattern=_HEX64)


class CallbackRequestV2(_CallbackRequestCommon):
    schema_version: Literal["openadapt.hosted-runner-callback/v2"] = (
        "openadapt.hosted-runner-callback/v2"
    )
    flow_release_verification_receipt_object_sha256: str = Field(
        pattern=r"^sha256:[a-f0-9]{64}$"
    )


CallbackRequest = CallbackRequestV2
CallbackRequestWire = Union[CallbackRequestV1, CallbackRequestV2]


def parse_callback_request(
    value: CallbackRequestWire | Mapping[str, object],
) -> CallbackRequestWire:
    if isinstance(value, (CallbackRequestV1, CallbackRequestV2)):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("hosted callback is invalid")
    if value.get("schema_version") == "openadapt.hosted-runner-callback/v1":
        return CallbackRequestV1.model_validate(value)
    if value.get("schema_version") == "openadapt.hosted-runner-callback/v2":
        return CallbackRequestV2.model_validate(value)
    raise ValueError("hosted callback schema is unsupported")


class _CallbackResponseCommon(_Closed):
    status: Literal["accepted", "duplicate"]
    run_id: str = Field(pattern=_UUID)
    outcome: TransactionOutcome
    dispatch_state: Literal["closed"]
    accepted_events: int = Field(ge=0, le=10_001)


class CallbackResponseV1(_CallbackResponseCommon):
    schema_version: Literal["openadapt.hosted-runner-callback-result/v1"]


class CallbackResponseV2(_CallbackResponseCommon):
    schema_version: Literal["openadapt.hosted-runner-callback-result/v2"]


CallbackResponse = CallbackResponseV2
CallbackResponseWire = Union[CallbackResponseV1, CallbackResponseV2]


def parse_callback_response(
    value: CallbackResponseWire | Mapping[str, object],
) -> CallbackResponseWire:
    if isinstance(value, (CallbackResponseV1, CallbackResponseV2)):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("hosted callback response is invalid")
    if value.get("schema_version") == "openadapt.hosted-runner-callback-result/v1":
        return CallbackResponseV1.model_validate_json(json.dumps(dict(value)))
    if value.get("schema_version") == "openadapt.hosted-runner-callback-result/v2":
        return CallbackResponseV2.model_validate_json(json.dumps(dict(value)))
    raise ValueError("hosted callback response schema is unsupported")


class HostedRunnerTransport(Protocol):
    """Desktop-owned HTTP surface. Credentials stay in its transport state."""

    def register(self, request: RegisterRequest) -> RegisterResponseWire: ...

    def poll(self, request: PollRequest) -> HostedDispatchWire | None: ...

    def close_result_loss(
        self,
        run_id: str,
        lease_token: str,
        request: ProductionDeliveryResultLossClosureRequest,
    ) -> ProductionDeliveryResultLossClosureResult: ...

    def callback(
        self, run_id: str, request: CallbackRequest
    ) -> CallbackResponseWire: ...


@dataclass(frozen=True)
class DeliveryAuthority:
    """Run-scoped configuration for the existing per-input-edge authority path."""

    url: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.url)
        except ValueError as exc:
            raise ValueError("managed delivery authority URL is invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != "/api/internal/managed-delivery-permit"
        ):
            raise ValueError(
                "managed delivery authority URL is not a pinned HTTPS edge"
            )
        if re.fullmatch(_HEX64, self.token) is None:
            raise ValueError("managed delivery authority token is invalid")

    def child_environment(self) -> dict[str, str]:
        return {
            REMOTE_AUTHORITY_URL_ENV: self.url,
            REMOTE_AUTHORITY_TOKEN_ENV: self.token,
        }


@dataclass(frozen=True)
class ManagedExecution:
    returncode: int
    report_bytes: bytes | None
    terminal_verification: ProductionTerminalVerificationEnvelope | None = None


ManagedRunner = Callable[[list[str], Path, Mapping[str, str]], ManagedExecution]


def _subprocess_runner(
    argv: list[str], run_dir: Path, child_env: Mapping[str, str]
) -> ManagedExecution:
    process = subprocess.run(  # nosec - argv is built from verified local material
        argv,
        capture_output=True,
        text=True,
        env=dict(child_env),
    )
    report_path = run_dir / "report.json"
    report_bytes = report_path.read_bytes() if report_path.is_file() else None
    return ManagedExecution(process.returncode, report_bytes)


class HostedRunnerAdapter:
    def __init__(
        self,
        ledger_path: Path,
        *,
        runner: ManagedRunner = _subprocess_runner,
    ) -> None:
        self.ledger_path = Path(ledger_path)
        self._ledger = IdempotencyLedger(
            self.ledger_path, namespace="openadapt-hosted-runner/v1"
        )
        self._runner = runner
        self._release_state_path = self.ledger_path.with_suffix(
            self.ledger_path.suffix + ".product-release.json"
        )
        self._flow_release_state_path = self.ledger_path.with_suffix(
            self.ledger_path.suffix + ".flow-release.json"
        )

    @staticmethod
    def _protected_runner_origin(config: RunnerConfig) -> str:
        raw = config.host
        if raw is None:
            raise ValueError("hosted runner requires a protected runner host origin")
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("protected runner host origin is invalid") from exc
        canonical = f"https://{parsed.netloc}"
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname != parsed.hostname.lower()
            or parsed.netloc != parsed.netloc.lower()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or port == 443
            or raw != canonical
        ):
            raise ValueError("protected runner host is not one canonical HTTPS origin")
        return canonical

    def protected_runner_origin(self, runner_config: Path) -> str:
        """Return the origin from one protected, strictly parsed runner config."""

        return self._protected_runner_origin(
            load_runner_config(runner_config, protected=True)
        )

    def registration_request(
        self,
        *,
        runner_config: Path,
        name: str,
        platform: Literal["windows", "macos", "linux"],
        agent_version: str,
        engine_version: str,
        mode: Literal["attended", "service"],
        capabilities: RegisterCapabilities | Mapping[str, object],
    ) -> RegisterRequest:
        config = load_runner_config(runner_config, protected=True)
        self._protected_runner_origin(config)
        releases = config.local_runtime_release
        if tuple(item.target for item in releases) != ("flow", "desktop", "capture"):
            raise ValueError(
                "hosted registration requires exact flow, desktop, and capture releases"
            )
        local_flow_release = config.local_flow_release
        if local_flow_release is None:
            raise ValueError(
                "hosted registration requires the exact verified Flow release"
            )
        installed_flow = next(item for item in releases if item.target == "flow")
        if (
            installed_flow.release_version != local_flow_release.version
            or engine_version != local_flow_release.version
        ):
            raise ValueError(
                "local Flow runtime version differs from its verification receipt"
            )
        if not isinstance(capabilities, RegisterCapabilities):
            if not isinstance(capabilities, Mapping) or set(capabilities) != {
                "backends",
                "attended",
                "effects_substrates",
            }:
                raise ValueError("runner capabilities have an invalid exact shape")
            backends = capabilities["backends"]
            effects = capabilities["effects_substrates"]
            attended = capabilities["attended"]
            if (
                not isinstance(backends, (list, tuple))
                or not isinstance(effects, (list, tuple))
                or type(attended) is not bool
            ):
                raise ValueError("runner capabilities have an invalid exact shape")
            capabilities = RegisterCapabilities(
                backends=tuple(backends),
                attended=attended,
                effects_substrates=tuple(effects),
            )
        return RegisterRequest(
            name=name,
            platform=platform,
            agent_version=agent_version,
            engine_version=engine_version,
            mode=mode,
            capabilities=capabilities,
            local_runtime_release={
                item.target: LocalRuntimeReleaseBinding(**item.__dict__)
                for item in releases
            },
            local_flow_release=local_flow_release,
        )

    @staticmethod
    def _load_json(path: Path) -> object:
        try:
            return _read_private_json(path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"admission trust state {path} is invalid") from exc

    @staticmethod
    def _read_private_bytes(path: Path, *, maximum_bytes: int, label: str) -> bytes:
        """Read one owner-only regular file without following a final link."""

        path = Path(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            path_before = path.lstat()
            if not stat.S_ISREG(path_before.st_mode) or stat.S_ISLNK(
                path_before.st_mode
            ):
                raise ValueError(f"{label} is not a private regular file")
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValueError(f"{label} could not be opened safely") from exc
        try:
            before = os.fstat(descriptor)
            try:
                private_permissions = (
                    windows_descriptor_has_private_acl(descriptor)
                    if os.name == "nt"
                    else (
                        before.st_uid == os.geteuid()
                        and stat.S_IMODE(before.st_mode) == 0o600
                    )
                )
            except PrivateFileAclError as exc:
                raise ValueError(f"{label} ACL could not be verified") from exc
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > maximum_bytes
                or not private_permissions
            ):
                raise ValueError(f"{label} is not a private regular file")
            chunks: list[bytes] = []
            remaining = min(before.st_size, maximum_bytes) + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            try:
                path_after = path.lstat()
            except OSError as exc:
                raise ValueError(f"{label} changed during its protected read") from exc
            if (
                len(raw) != before.st_size
                or len(raw) > maximum_bytes
                or stat.S_ISLNK(path_after.st_mode)
                or (path_before.st_dev, path_before.st_ino)
                != (before.st_dev, before.st_ino)
                or (path_after.st_dev, path_after.st_ino)
                != (before.st_dev, before.st_ino)
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError(f"{label} changed during its protected read")
            return raw
        finally:
            os.close(descriptor)

    def _load_evidence_private_key(self, config: RunnerConfig) -> Ed25519PrivateKey:
        path = config.evidence_runner_private_key
        if path is None:
            raise ValueError("hosted runner has no evidence-runner private key")
        raw = self._read_private_bytes(
            path, maximum_bytes=4096, label="evidence-runner private key"
        )
        try:
            if len(raw) == 32:
                key = Ed25519PrivateKey.from_private_bytes(raw)
            else:
                loaded = serialization.load_pem_private_key(raw, password=None)
                if not isinstance(loaded, Ed25519PrivateKey):
                    raise ValueError("evidence-runner key is not Ed25519")
                key = loaded
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence-runner private key is invalid") from exc
        return key

    def _accept_newest_product_sequence(
        self, payload: ProductReleaseAdmissionPayload, artifact_sha256: str
    ) -> None:
        current: dict[str, object] | None = None
        if self._release_state_path.exists():
            metadata = self._release_state_path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or (
                os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError("product release sequence ledger is unsafe")
            loaded = self._load_json(self._release_state_path)
            if not isinstance(loaded, dict):
                raise ValueError("product release sequence ledger is invalid")
            current = loaded
        if current is not None:
            sequence = current.get("sequence")
            digest = current.get("artifact_sha256")
            if not isinstance(sequence, int) or not isinstance(digest, str):
                raise ValueError("product release sequence ledger is invalid")
            if payload.sequence < sequence:
                raise ValueError("product release admission sequence is stale")
            if payload.sequence == sequence and artifact_sha256 != digest:
                raise ValueError("product release admission changed at one sequence")
            if payload.sequence == sequence:
                return
        raw = json.dumps(
            {"sequence": payload.sequence, "artifact_sha256": artifact_sha256},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._release_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._release_state_path.with_suffix(
            self._release_state_path.suffix + ".tmp"
        )
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(tmp, self._release_state_path)
        if os.name != "nt":
            os.chmod(self._release_state_path, 0o600)

    def _verify_product_release(
        self, dispatch: HostedDispatch, config: RunnerConfig
    ) -> ProductReleaseAdmissionPayload:
        trust_files = config.product_release_admission
        if trust_files is None:
            raise ValueError("hosted runner has no product release admission trust")
        raw = dispatch.product_release_admission.decode()
        artifact = ProductReleaseAdmissionArtifact.model_validate_json(raw)
        if (
            artifact.artifact_sha256()
            != dispatch.product_release_admission.artifact_sha256
        ):
            raise ValueError("product release artifact canonical digest changed")
        trust = load_product_release_signer_trust(
            self._load_json(trust_files.signer_registry)
        )
        state = self._load_json(trust_files.state)
        if not isinstance(state, dict) or set(state) != {
            "newest_sequence",
            "revoked_set_ids",
        }:
            raise ValueError("product release authority state is invalid")
        newest = state["newest_sequence"]
        revoked = state["revoked_set_ids"]
        if (
            not isinstance(newest, int)
            or not isinstance(revoked, list)
            or any(not isinstance(item, str) for item in revoked)
        ):
            raise ValueError("product release authority state is invalid")
        payload = verify_product_release_admission(
            artifact,
            trusted_signers=trust,
            newest_sequence=newest,
            revoked_set_ids=frozenset(revoked),
        )
        local = {item.target: item for item in config.local_runtime_release}
        if set(local) != {"flow", "desktop", "capture"}:
            raise ValueError("hosted runner local release inventory is incomplete")
        admitted = {item.target: item for item in payload.targets}
        for target, installed in local.items():
            item = admitted[target]
            if (
                installed.admission_id,
                installed.admission_sha256,
                installed.release_version,
                installed.release_artifact_sha256,
            ) != (
                item.admission_id,
                item.admission_sha256,
                item.release_id,
                item.release_artifact_sha256,
            ):
                raise ValueError(f"local {target} release is not exactly admitted")
        if isinstance(dispatch, HostedDispatchV2):
            local_flow_release = config.local_flow_release
            if local_flow_release is None:
                raise ValueError("hosted runner has no verified Flow release identity")
            flow_receipt = assert_hosted_flow_release(
                local_flow_release,
                dispatch.flow_release_verification_receipt,
            )
            admitted_flow = admitted["flow"]
            if (
                admitted_flow.release_id != flow_receipt.version
                or f"sha256:{admitted_flow.release_artifact_sha256}"
                != flow_receipt.release_sha256
            ):
                raise ValueError(
                    "signed product admission and Flow receipt name different releases"
                )
        self._accept_newest_product_sequence(
            payload, dispatch.product_release_admission.artifact_sha256
        )
        return payload

    def _verify_workflow_admission(
        self,
        dispatch: HostedDispatch,
        config: RunnerConfig,
        *,
        evidence_private_key: Ed25519PrivateKey,
    ) -> tuple[ProductionQualificationAuthority, bytes]:
        trust_files = config.workflow_admission
        if trust_files is None:
            raise ValueError("hosted runner has no workflow admission trust")
        raw = dispatch.workflow_admission.decode()
        envelope = QualificationAdmissionEnvelope.model_validate_json(raw)
        canonical = json.dumps(
            envelope.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if canonical != raw or (
            envelope.artifact_sha256() != dispatch.workflow_admission.artifact_sha256
        ):
            raise ValueError("workflow admission canonical digest changed")
        authorization = dispatch.payload.authorization
        local_fields = tuple(
            name
            for name in authorization.model_fields
            if name.startswith("production_qualification_")
        ) + ("qualification_admission", "qualification_admission_sha256")
        if any(getattr(authorization, name) is not None for name in local_fields):
            raise ValueError("dispatch authorization supplies runner-local authority")
        state = self._load_json(trust_files.state)
        if not isinstance(state, dict) or set(state) != {"revoked_admission_ids"}:
            raise ValueError("workflow admission authority state is invalid")
        revoked = state["revoked_admission_ids"]
        if not isinstance(revoked, list) or any(
            not isinstance(item, str) for item in revoked
        ):
            raise ValueError("workflow admission authority state is invalid")
        registry_raw = self._load_json(trust_files.signer_registry)
        registry = QualificationSignerRegistry.model_validate(registry_raw)
        if registry.model_dump(mode="json") != registry_raw:
            raise ValueError("workflow signer registry is not canonical")
        expected_raw = self._load_json(trust_files.expected_bindings)
        expected = QualificationAdmissionExpected.model_validate(expected_raw)
        if expected.model_dump(mode="json") != expected_raw:
            raise ValueError("workflow admission expected bindings are not canonical")

        trusted = config.bundles.get(dispatch.payload.bundle.content_digest)
        if trusted is None or trusted.artifact_sha256 is None:
            raise ValueError("hosted bundle lacks its local artifact digest pin")
        workflow = Workflow.load(trusted.path)
        manifest = workflow.manifest
        project = workflow.qualification
        if manifest is None or project is None:
            raise ValueError("hosted workflow is not sealed and qualified")
        template = manifest.provenance.governed_authorization_template
        if template is None:
            raise ValueError("hosted workflow lacks its governed template")
        profile_path = config.profiles.get(dispatch.payload.deployment_profile_id)
        if profile_path is None:
            raise ValueError("hosted workflow profile is not locally configured")
        deployment_bytes = self._read_private_bytes(
            profile_path,
            maximum_bytes=1024 * 1024,
            label="hosted deployment profile",
        )
        deployment_sha256 = hashlib.sha256(deployment_bytes).hexdigest()
        effect_contract_sha256 = contract_sha256(
            [
                item.model_dump(mode="json")
                for item in template.qualified_effect_requirements
            ]
        )
        public_key = evidence_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        evidence_key_sha256 = evidence_runner_signer_sha256(public_key)
        local_flow = next(
            (item for item in config.local_runtime_release if item.target == "flow"),
            None,
        )
        if local_flow is None:
            raise ValueError("hosted runner lacks its local Flow release binding")
        local_expected = {
            "tenant_id": dispatch.tenant_id,
            "workflow_id": dispatch.workflow_id,
            "workflow_version_id": dispatch.workflow_version_id,
            "bundle_version_id": dispatch.workflow_version_id,
            "bundle_artifact_sha256": trusted.artifact_sha256,
            "bundle_content_digest": manifest.content_digest,
            "environment_digest": project.environment.environment_digest,
            "governed_authorization_template_sha256": template.template_sha256,
            "environment_contract_sha256": (
                template.qualification_environment_contract_sha256
            ),
            "input_policy_sha256": template.parameter_contract_sha256,
            "action_policy_sha256": template.qualification_project_contract_sha256,
            "identity_contract_sha256": template.identity_contract_sha256,
            "effect_contract_sha256": effect_contract_sha256,
            "evidence_runner_signer_sha256": evidence_key_sha256,
            "deployment_manifest_sha256": deployment_sha256,
        }
        mismatches = sorted(
            name
            for name, value in local_expected.items()
            if getattr(expected, name) != value
        )
        runtime = expected.runtime_build_identity
        if (
            runtime.flow_version != local_flow.release_version
            or runtime.flow_wheel_sha256 != local_flow.release_artifact_sha256
        ):
            mismatches.append("runtime_build_identity")
        if mismatches:
            raise ValueError(
                "local workflow admission expectation differs from live state: "
                + ", ".join(sorted(set(mismatches)))
            )
        verify_qualification_admission(
            envelope,
            registry=registry,
            expected=expected,
            revoked_admission_ids=frozenset(revoked),
        )
        return (
            ProductionQualificationAuthority(
                qualification_admission=envelope,
                qualification_admission_sha256=envelope.artifact_sha256(),
                expected=expected,
                qualification_signer_registry=registry,
                qualification_signer_registry_sha256=registry.artifact_sha256(),
                permit_trust_snapshot=None,
                revoked_admission_ids=tuple(sorted(set(revoked))),
            ),
            deployment_bytes,
        )

    @staticmethod
    def _write_private_json(
        path: Path, value: BaseModel | Mapping[str, object]
    ) -> Path:
        payload: object = (
            value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        )
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path

    @staticmethod
    def _write_private_bytes(path: Path, raw: bytes) -> Path:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise OSError("protected file write did not make progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path

    @staticmethod
    def _write_private_bytes_atomic_exclusive(path: Path, raw: bytes) -> Path:
        """Publish complete owner-only bytes once, without an overwrite window."""

        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        try:
            HostedRunnerAdapter._write_private_bytes(temporary, raw)
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise
            try:
                directory = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return path

    @staticmethod
    def _run_store_identity_sha256(run_dir: Path) -> str:
        resolved = Path(run_dir).resolve(strict=True)
        value = resolved.lstat()
        payload = {
            "canonical_run_dir": str(resolved),
            "device": int(value.st_dev),
            "inode": int(value.st_ino),
        }
        return hashlib.sha256(
            _RUN_STORE_IDENTITY_DOMAIN + canonical_json(payload)
        ).hexdigest()

    def _write_child_start_evidence(
        self,
        *,
        dispatch: HostedDispatch,
        run_dir: Path,
        dispatch_binding_sha256: str,
    ) -> ManagedChildStartEvidence:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        marker = ManagedChildStartEvidence.create(
            started_at=now.isoformat().replace("+00:00", "Z"),
            dispatch_id=dispatch.dispatch_id,
            dispatch_session_id=dispatch.dispatch_session_id,
            run_id=dispatch.run_id,
            managed_dispatch_binding_sha256=dispatch_binding_sha256,
            authenticated_runner_id_sha256=hashlib.sha256(
                dispatch.runner_id.encode("utf-8")
            ).hexdigest(),
            authenticated_session_id_sha256=hashlib.sha256(
                dispatch.runner_session_id.encode("utf-8")
            ).hexdigest(),
            execution_authority_id=dispatch.execution_authority_id,
            execution_authority_sha256=dispatch.execution_authority_sha256,
            execution_authority_signer_sha256=(
                dispatch.execution_authority_signer_sha256
            ),
            run_store_identity_sha256=self._run_store_identity_sha256(run_dir),
        )
        self._write_private_bytes_atomic_exclusive(
            run_dir / "managed-child-started.json", canonical_json(marker)
        )
        return marker

    def _read_child_start_evidence(
        self,
        *,
        dispatch: HostedDispatch,
        run_dir: Path,
        dispatch_binding_sha256: str,
    ) -> ManagedChildStartEvidence:
        raw = self._read_private_bytes(
            run_dir / "managed-child-started.json",
            maximum_bytes=64 * 1024,
            label="managed child start evidence",
        )
        try:
            marker = ManagedChildStartEvidence.model_validate_json(raw)
        except ValueError as exc:
            raise ValueError("managed child start evidence is invalid") from exc
        expected = (
            dispatch.dispatch_id,
            dispatch.dispatch_session_id,
            dispatch.run_id,
            dispatch_binding_sha256,
            hashlib.sha256(dispatch.runner_id.encode("utf-8")).hexdigest(),
            hashlib.sha256(dispatch.runner_session_id.encode("utf-8")).hexdigest(),
            dispatch.execution_authority_id,
            dispatch.execution_authority_sha256,
            dispatch.execution_authority_signer_sha256,
            self._run_store_identity_sha256(run_dir),
        )
        actual = (
            marker.dispatch_id,
            marker.dispatch_session_id,
            marker.run_id,
            marker.managed_dispatch_binding_sha256,
            marker.authenticated_runner_id_sha256,
            marker.authenticated_session_id_sha256,
            marker.execution_authority_id,
            marker.execution_authority_sha256,
            marker.execution_authority_signer_sha256,
            marker.run_store_identity_sha256,
        )
        if canonical_json(marker) != raw or actual != expected:
            raise ValueError("managed child start evidence changed")
        return marker

    @staticmethod
    def _managed_result_loss_report(
        *,
        dispatch: HostedDispatch,
        workflow: Workflow,
        manifest: object,
        marker: ManagedChildStartEvidence,
        evidence: ManagedResultLossEvidence,
        verified_params: dict[str, RuntimeParamScalar],
        runtime_substrate: Literal["web", "windows", "macos", "linux", "rdp", "citrix"],
    ) -> RunReport:
        authorization = getattr(manifest, "governed_authorization", None)
        if (
            workflow.manifest is None
            or authorization is None
            or authorization.execution_profile
            not in {
                "standard",
                "regulated",
            }
        ):
            raise ValueError("managed result loss lacks retained authorization")
        qualification_case_id_sha256 = (
            hashlib.sha256(
                authorization.qualification_case_id.encode("utf-8")
            ).hexdigest()
            if authorization.qualification_case_id is not None
            else None
        )
        report = RunReport(
            workflow_name=workflow.name,
            started_at=marker.started_at,
            execution_profile=authorization.execution_profile,
            execution_outcome="HALTED",
            production_eligible=False,
            execution_completed=False,
            transaction_outcome="RECONCILIATION_REQUIRED",
            transaction_billable=False,
            transaction_platform_fault=False,
            managed_result_loss=evidence,
            execution_target_kind=runtime_substrate,
            recorded_surface=workflow.surface,
            bundle_content_digest=workflow.manifest.content_digest,
            workflow_contract_sha256=workflow_contract_sha256(workflow),
            source_recording_sha256=workflow.manifest.provenance.source_recording_sha256,
            parameter_schema_sha256=compute_parameter_schema_digest(workflow),
            governed_authorization_id=authorization.authorization_id,
            governed_approval_source=authorization.approval_source,
            governed_authorization_created_at=authorization.created_at,
            governed_policy_name=authorization.admitted_policy_name,
            governed_policy_contract_sha256=(
                authorization.admitted_policy_contract_sha256
            ),
            governed_minimum_effect_tier=authorization.minimum_effect_tier,
            governed_qualified_effect_requirements=list(
                authorization.qualified_effect_requirements
            ),
            governed_runtime_inputs_digest=authorization.runtime_inputs_digest,
            run_id_sha256=evidence.flow_run_id_sha256,
            governed_qualification_project_id=authorization.qualification_project_id,
            governed_qualification_project_revision=(
                authorization.qualification_project_revision
            ),
            governed_qualification_project_contract_sha256=(
                authorization.qualification_project_contract_sha256
            ),
            governed_qualification_campaign_id_sha256=(
                authorization.qualification_campaign_id_sha256
            ),
            governed_qualification_case_id_sha256=qualification_case_id_sha256,
            governed_qualification_case_input_sha256=(
                authorization.qualification_case_input_sha256
            ),
            governed_qualification_run_id_sha256=(
                authorization.qualification_run_id_sha256
            ),
            governed_qualification_case_kind=authorization.qualification_case_kind,
            governed_qualification_case_action_paths=dict(
                authorization.qualification_case_action_paths
            ),
            governed_qualification_fault_driver_id=(
                authorization.qualification_fault_driver_id
            ),
            governed_qualification_fault_driver_contract_sha256=(
                authorization.qualification_fault_driver_contract_sha256
            ),
            governed_qualification_fault_driver_key_id=(
                authorization.qualification_fault_driver_key_id
            ),
            governed_qualification_fault_step_id_sha256=(
                authorization.qualification_fault_step_id_sha256
            ),
            governed_authorized_effect_contracts={
                approval.step_id: list(approval.effect_contract_hashes)
                for approval in authorization.unverified_write_approvals
            },
            required_identity_step_ids=list(authorization.required_identity_step_ids),
            approved_unverified_effect_step_ids=[
                approval.step_id
                for approval in authorization.unverified_write_approvals
            ],
            params=verified_params,
            results=[
                StepResult(
                    step_id="<managed-result-loss>",
                    intent="retain managed child result loss",
                    ok=False,
                    safety_halt=True,
                    failure_category="safety_halt",
                    delivery_attempted=None,
                )
            ],
            success=False,
            model_calls=int(getattr(manifest, "model_calls", 0)),
            # The exact Cloud authority interaction proves that network I/O
            # occurred even though the child report is unavailable.
            external_network_calls="observed",
        )
        stamp_execution_outcome(
            report,
            workflow,
            authorization.execution_profile,
        )
        if (
            report.execution_outcome != "HALTED"
            or report.transaction_outcome != "RECONCILIATION_REQUIRED"
        ):
            raise ValueError("managed result loss classification changed")
        return RunReport.model_validate_json(report.model_dump_json())

    def _retain_managed_result_loss_closure(
        self,
        *,
        dispatch: HostedDispatch,
        run_dir: Path,
        dispatch_binding_sha256: str,
        closure_authority: HostedRunnerTransport,
        loss_code: ManagedResultLossCode,
    ) -> ManagedResultLossSnapshot:
        """Fence delivery authority before any fallible terminal proof work."""

        marker = self._read_child_start_evidence(
            dispatch=dispatch,
            run_dir=run_dir,
            dispatch_binding_sha256=dispatch_binding_sha256,
        )

        def snapshot_from_closure(
            *,
            request: ProductionDeliveryResultLossClosureRequest,
            retained_loss_code: ManagedResultLossCode,
            closure_artifact: ProductionDeliveryResultLossClosureArtifact,
            retained_chain: ProductionDeliveryPermitChain,
        ) -> ManagedResultLossSnapshot:
            chain = ProductionDeliveryPermitChain.model_validate(
                retained_chain.model_dump(mode="json")
            )
            if not chain.entries and chain.pending is None:
                raise ValueError("managed result loss closure returned no permit")
            closure_artifact = (
                ProductionDeliveryResultLossClosureArtifact.model_validate(
                    closure_artifact.model_dump(mode="json")
                )
            )
            closure = closure_artifact.payload
            if (
                closure.closure_request_sha256 != request.request_sha256()
                or closure.result_loss_observed_at != request.result_loss_observed_at
                or request.child_start_evidence != marker
            ):
                raise ValueError("managed result loss closure changed its request")
            first = chain.entries[0] if chain.entries else chain.pending
            assert first is not None
            pending = chain.pending
            evidence = ManagedResultLossEvidence.create(
                loss_code=retained_loss_code,
                child_started_at=marker.started_at,
                child_start_evidence_sha256=marker.marker_sha256,
                run_store_identity_sha256=marker.run_store_identity_sha256,
                observed_at=request.result_loss_observed_at,
                run_id=dispatch.run_id,
                flow_run_id_sha256=hashlib.sha256(
                    dispatch.run_id.encode("utf-8")
                ).hexdigest(),
                dispatch_id=dispatch.dispatch_id,
                dispatch_session_id=dispatch.dispatch_session_id,
                managed_dispatch_binding_sha256=dispatch_binding_sha256,
                idempotency_key_sha256=managed_result_loss_idempotency_sha256(
                    dispatch.idempotency_key
                ),
                authenticated_runner_id_sha256=hashlib.sha256(
                    dispatch.runner_id.encode("utf-8")
                ).hexdigest(),
                authenticated_session_id_sha256=hashlib.sha256(
                    dispatch.runner_session_id.encode("utf-8")
                ).hexdigest(),
                execution_authority_id=dispatch.execution_authority_id,
                execution_authority_sha256=dispatch.execution_authority_sha256,
                execution_authority_signer_sha256=(
                    dispatch.execution_authority_signer_sha256
                ),
                delivery_result_loss_closure_artifact_sha256=(
                    closure_artifact.artifact_sha256()
                ),
                pending_permit_artifact_sha256=(
                    pending.permit_artifact_sha256 if pending is not None else None
                ),
                run_request_sha256=first.run_request_sha256,
                pending_action_request_sha256=(
                    pending.action_request_sha256 if pending is not None else None
                ),
            )
            verify_production_delivery_result_loss_closure_binding(
                closure_artifact,
                permit_chain=chain,
                result_loss=evidence,
                tenant_id=dispatch.tenant_id,
                terminal_verified_at=closure.closed_at,
            )
            return ManagedResultLossSnapshot.create(
                evidence=evidence,
                permit_chain=chain,
                closure_artifact=closure_artifact,
            )

        request_path = run_dir / "managed-result-loss-closure-request.json"

        def read_retained_request() -> ProductionDeliveryResultLossClosureRequest:
            raw = self._read_private_bytes(
                request_path,
                maximum_bytes=64 * 1024,
                label="managed result loss closure request",
            )
            try:
                retained = (
                    ProductionDeliveryResultLossClosureRequest.model_validate_json(raw)
                )
            except ValueError as exc:
                raise ValueError(
                    "managed result loss closure request is invalid"
                ) from exc
            if (
                canonical_json(retained) != raw
                or retained.child_start_evidence != marker
            ):
                raise ValueError("managed result loss closure request changed")
            return retained

        try:
            request_path.lstat()
        except FileNotFoundError:
            observed_at = (
                datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            request = ProductionDeliveryResultLossClosureRequest(
                child_start_evidence=marker,
                result_loss_observed_at=observed_at,
            )
            try:
                self._write_private_bytes_atomic_exclusive(
                    request_path,
                    request.canonical_bytes(),
                )
            except FileExistsError:
                request = read_retained_request()
        else:
            request = read_retained_request()

        evidence_path = run_dir / "managed-result-loss.json"

        def read_retained_snapshot() -> ManagedResultLossSnapshot:
            retained_bytes = self._read_private_bytes(
                evidence_path,
                maximum_bytes=_MAX_RESULT_LOSS_SNAPSHOT_BYTES,
                label="managed result loss snapshot",
            )
            try:
                retained = ManagedResultLossSnapshot.model_validate_json(retained_bytes)
            except ValueError as exc:
                raise ValueError("managed result loss snapshot is invalid") from exc
            if canonical_json(retained) != retained_bytes:
                raise ValueError("managed result loss snapshot is not canonical")
            expected_retained = snapshot_from_closure(
                request=request,
                retained_loss_code=retained.evidence.loss_code,
                closure_artifact=retained.closure_artifact,
                retained_chain=retained.permit_chain,
            )
            if retained != expected_retained:
                raise ValueError("managed result loss snapshot changed")
            return retained

        try:
            evidence_path.lstat()
        except FileNotFoundError:
            raw_result = closure_authority.close_result_loss(
                dispatch.run_id,
                dispatch.lease_token,
                request,
            )
            result = ProductionDeliveryResultLossClosureResult.model_validate(
                raw_result
            )
            closure_artifact, permit_chain = result.artifacts()
            snapshot = snapshot_from_closure(
                request=request,
                retained_loss_code=loss_code,
                closure_artifact=closure_artifact,
                retained_chain=permit_chain,
            )
            try:
                self._write_private_bytes_atomic_exclusive(
                    evidence_path, canonical_json(snapshot)
                )
            except FileExistsError:
                snapshot = read_retained_snapshot()
        else:
            snapshot = read_retained_snapshot()
        return snapshot

    def _produce_managed_result_loss(
        self,
        *,
        dispatch: HostedDispatch,
        workflow: Workflow,
        run_dir: Path,
        qualification: ProductionQualificationAuthority,
        private_key: Ed25519PrivateKey,
        verified_params: dict[str, RuntimeParamScalar],
        dispatch_binding_sha256: str,
        loss_code: ManagedResultLossCode,
        closure_authority: HostedRunnerTransport | None = None,
        snapshot: ManagedResultLossSnapshot | None = None,
    ) -> tuple[ProductionTerminalVerificationEnvelope, str, RunReport]:
        """Sign one result-loss terminal after Cloud has fenced delivery."""

        if snapshot is None:
            if closure_authority is None:
                raise ValueError("hosted result-loss closure authority is absent")
            snapshot = self._retain_managed_result_loss_closure(
                dispatch=dispatch,
                run_dir=run_dir,
                dispatch_binding_sha256=dispatch_binding_sha256,
                closure_authority=closure_authority,
                loss_code=loss_code,
            )
        store = CheckpointStore(run_dir)
        manifest = store.read_manifest()
        if manifest is None:
            raise ValueError("managed result loss lacks a retained run manifest")
        marker = self._read_child_start_evidence(
            dispatch=dispatch,
            run_dir=run_dir,
            dispatch_binding_sha256=dispatch_binding_sha256,
        )
        evidence = snapshot.evidence
        observed_at = evidence.observed_at
        expected = qualification.expected
        report = self._managed_result_loss_report(
            dispatch=dispatch,
            workflow=workflow,
            manifest=manifest,
            marker=marker,
            evidence=evidence,
            verified_params=verified_params,
            runtime_substrate=expected.runtime_build_identity.substrate,
        )
        proof, digest = self._produce_terminal_verification(
            dispatch=dispatch,
            report=report,
            run_dir=run_dir,
            qualification=qualification,
            private_key=private_key,
            verified_params=verified_params,
            dispatch_binding_sha256=dispatch_binding_sha256,
            result_loss_observed_at=observed_at,
            retained_permit_chain=snapshot.permit_chain,
            retained_result_loss_closure=snapshot.closure_artifact,
        )
        return proof, digest, report

    def _revalidate_terminal_authority(
        self,
        *,
        dispatch: HostedDispatch,
        runner_config: Path,
        configured_origin: str,
        initial_qualification: ProductionQualificationAuthority,
        initial_deployment_bytes: bytes,
    ) -> tuple[Ed25519PrivateKey, ProductionQualificationAuthority]:
        """Revalidate all live signing authority after managed execution."""

        terminal_config = load_runner_config(runner_config, protected=True)
        if self._protected_runner_origin(terminal_config) != configured_origin:
            raise ValueError("protected runner origin changed during execution")
        self._verify_product_release(dispatch, terminal_config)
        terminal_key = self._load_evidence_private_key(terminal_config)
        terminal_qualification, terminal_deployment = self._verify_workflow_admission(
            dispatch,
            terminal_config,
            evidence_private_key=terminal_key,
        )
        if (
            terminal_qualification != initial_qualification
            or terminal_deployment != initial_deployment_bytes
        ):
            raise ValueError("production admission changed during execution")
        return terminal_key, terminal_qualification

    def _resolve_params(
        self,
        dispatch: HostedDispatch,
        config: RunnerConfig,
    ) -> dict[str, RuntimeParamScalar]:
        trusted = config.bundles.get(dispatch.payload.bundle.content_digest)
        if trusted is None:
            raise ValueError("hosted bundle is not locally trusted")
        workflow = Workflow.load(trusted.path)
        if isinstance(dispatch.payload.params, DispatchParamsValues):
            supplied = dict(dispatch.payload.params.values)
            inline = True
        else:
            root = config.params_ref_root
            if root is None:
                raise ValueError("parameter reference has no protected local root")
            ref = dispatch.payload.params.ref
            parsed_url = urlsplit(ref)
            relative = PurePosixPath(ref)
            if (
                parsed_url.scheme
                or parsed_url.netloc
                or parsed_url.query
                or parsed_url.fragment
                or relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or "\\" in ref
            ):
                raise ValueError("parameter reference is not a safe local path")
            root_stat = root.lstat()
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(root_stat.st_mode)
                or (
                    os.name != "nt"
                    and (
                        root_stat.st_uid != os.geteuid()
                        or stat.S_IMODE(root_stat.st_mode) & 0o077
                    )
                )
            ):
                raise ValueError("parameter reference root is not protected")
            current = root
            for component in relative.parts[:-1]:
                current /= component
                component_stat = current.lstat()
                if (
                    not stat.S_ISDIR(component_stat.st_mode)
                    or stat.S_ISLNK(component_stat.st_mode)
                    or (
                        os.name != "nt"
                        and (
                            component_stat.st_uid != os.geteuid()
                            or stat.S_IMODE(component_stat.st_mode) & 0o077
                        )
                    )
                ):
                    raise ValueError("parameter reference traverses an unsafe path")
            raw = self._read_private_bytes(
                root.joinpath(*relative.parts),
                maximum_bytes=256 * 1024,
                label="parameter reference",
            )
            try:
                supplied = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("parameter reference is not valid JSON") from exc
            if not isinstance(supplied, dict):
                raise ValueError("parameter reference has an invalid exact shape")
            inline = False
        for name in supplied:
            if not isinstance(name, str):
                raise ValueError("runtime parameter name is invalid")
            validate_runtime_param_name(name)
        return resolve_admitted_params(workflow, supplied, inline=inline)

    @staticmethod
    def _write_params(path: Path, params: dict[str, RuntimeParamScalar]) -> Path | None:
        if not params:
            return None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            raw = json.dumps(
                params,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path

    def _produce_terminal_verification(
        self,
        *,
        dispatch: HostedDispatch,
        report: RunReport,
        run_dir: Path,
        qualification: ProductionQualificationAuthority,
        private_key: Ed25519PrivateKey,
        verified_params: dict[str, RuntimeParamScalar],
        dispatch_binding_sha256: str,
        result_loss_observed_at: str | None = None,
        retained_permit_chain: ProductionDeliveryPermitChain | None = None,
        retained_result_loss_closure: (
            ProductionDeliveryResultLossClosureArtifact | None
        ) = None,
    ) -> tuple[ProductionTerminalVerificationEnvelope, str]:
        """Build, retain, reread, and verify one exact terminal-v3 proof."""

        store = CheckpointStore(run_dir)
        manifest = store.read_manifest()
        if (
            manifest is None
            or manifest.delivery_authority_kind != "cloud_runner"
            or manifest.remote_delivery_run_id != dispatch.run_id
            or manifest.managed_dispatch_binding_sha256 != dispatch_binding_sha256
            or manifest.params != verified_params
        ):
            raise ValueError("retained managed run manifest differs from the dispatch")
        authorization = manifest.governed_authorization
        admission = qualification.qualification_admission
        evidence_identity = admission.payload.evidence_identity
        expected_production_binding = {
            "production_qualification_admission_id": admission.payload.admission_id,
            "production_qualification_admission_sha256": (
                qualification.qualification_admission_sha256
            ),
            "production_qualification_evidence_identity_sha256": (
                evidence_identity.artifact_sha256()
            ),
            "production_qualification_runtime_validation_id": (
                qualification.expected.runtime_validation_id
            ),
            "production_qualification_signer_registry_sha256": (
                qualification.qualification_signer_registry_sha256
            ),
            "production_qualification_signer_registry_revision": (
                qualification.qualification_signer_registry.revision
            ),
            "production_qualification_signer_registry_expires_at": (
                qualification.qualification_signer_registry.expires_at
            ),
            "production_qualification_authority_sha256": (
                qualification.immutable_binding_sha256()
            ),
        }
        if authorization is None or any(
            getattr(authorization, field) != value
            for field, value in expected_production_binding.items()
        ):
            raise ValueError("retained production authority differs from admission")
        if (
            dispatch_binding_sha256 != dispatch.payload.dispatch_binding_sha256
            or governed_dispatch_binding_sha256(dispatch.run_id, authorization)
            != dispatch.payload.dispatch_binding_sha256
        ):
            raise ValueError("retained governed authorization differs from dispatch")
        qualification_case_id_sha256 = (
            None
            if authorization.qualification_case_id is None
            else hashlib.sha256(
                authorization.qualification_case_id.encode("utf-8")
            ).hexdigest()
        )
        authorized_effect_contracts = {
            approval.step_id: list(approval.effect_contract_hashes)
            for approval in authorization.unverified_write_approvals
        }
        if (
            report.params != verified_params
            or report.governed_authorization_id != authorization.authorization_id
            or report.governed_authorization_created_at != authorization.created_at
            or report.governed_runtime_inputs_digest
            != authorization.runtime_inputs_digest
            or report.governed_policy_name != authorization.admitted_policy_name
            or report.governed_policy_contract_sha256
            != authorization.admitted_policy_contract_sha256
            or report.governed_minimum_effect_tier != authorization.minimum_effect_tier
            or report.governed_approval_source != authorization.approval_source
            or report.execution_profile != authorization.execution_profile
            or tuple(report.governed_qualified_effect_requirements)
            != authorization.qualified_effect_requirements
            or tuple(report.required_identity_step_ids)
            != authorization.required_identity_step_ids
            or report.approved_unverified_effect_step_ids
            != [item.step_id for item in authorization.unverified_write_approvals]
            or report.governed_authorized_effect_contracts
            != authorized_effect_contracts
            or report.governed_qualification_project_id
            != authorization.qualification_project_id
            or report.governed_qualification_project_revision
            != authorization.qualification_project_revision
            or report.governed_qualification_project_contract_sha256
            != authorization.qualification_project_contract_sha256
            or report.governed_qualification_campaign_id_sha256
            != authorization.qualification_campaign_id_sha256
            or report.governed_qualification_case_id_sha256
            != qualification_case_id_sha256
            or report.governed_qualification_case_input_sha256
            != authorization.qualification_case_input_sha256
            or report.governed_qualification_run_id_sha256
            != authorization.qualification_run_id_sha256
            or report.governed_qualification_case_kind
            != authorization.qualification_case_kind
            or report.governed_qualification_case_action_paths
            != authorization.qualification_case_action_paths
            or report.governed_qualification_fault_driver_id
            != authorization.qualification_fault_driver_id
            or report.governed_qualification_fault_driver_contract_sha256
            != authorization.qualification_fault_driver_contract_sha256
            or report.governed_qualification_fault_driver_key_id
            != authorization.qualification_fault_driver_key_id
            or report.governed_qualification_fault_step_id_sha256
            != authorization.qualification_fault_step_id_sha256
        ):
            raise ValueError("run report differs from retained governed inputs")

        prepared = prepare_production_terminal_evidence(report)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if retained_result_loss_closure is not None:
            closure_time = _utc_seconds(
                retained_result_loss_closure.payload.closed_at,
                label="managed result loss authority closure",
            )
            now = max(now, closure_time)
        now_text = now.isoformat().replace("+00:00", "Z")
        chain = (
            ProductionDeliveryPermitChain.model_validate(
                retained_permit_chain.model_dump(mode="json")
            )
            if retained_permit_chain is not None
            else DurableAuthority(run_dir, store).production_delivery_permit_chain(
                allow_empty=(
                    prepared.transaction_outcome
                    is TransactionOutcome.HALTED_BEFORE_EFFECT
                ),
                allow_pending=(
                    prepared.transaction_outcome
                    is TransactionOutcome.RECONCILIATION_REQUIRED
                ),
                receipt_absence_observed_at=(
                    now_text
                    if prepared.transaction_outcome
                    is TransactionOutcome.RECONCILIATION_REQUIRED
                    else None
                ),
            )
        )
        if retained_permit_chain is not None and report.managed_result_loss is None:
            raise ValueError("only managed result loss can use a retained permit chain")
        if (report.managed_result_loss is not None) != all(
            (
                result_loss_observed_at is not None,
                retained_permit_chain is not None,
                retained_result_loss_closure is not None,
            )
        ):
            raise ValueError("managed result loss observation binding is incomplete")
        first = chain.entries[0] if chain.entries else chain.pending
        expected = qualification.expected
        runtime = expected.runtime_build_identity
        flow_run_id_sha256 = hashlib.sha256(dispatch.run_id.encode("utf-8")).hexdigest()
        runner_id_sha256 = hashlib.sha256(
            dispatch.runner_id.encode("utf-8")
        ).hexdigest()
        runner_session_id_sha256 = hashlib.sha256(
            dispatch.runner_session_id.encode("utf-8")
        ).hexdigest()
        if first is not None and (
            first.run_id != dispatch.run_id
            or first.flow_run_id_sha256 != flow_run_id_sha256
            or first.execution_authority_id != dispatch.execution_authority_id
            or first.execution_authority_sha256 != dispatch.execution_authority_sha256
            or first.authority_signer_sha256
            != dispatch.execution_authority_signer_sha256
            or first.admission_artifact_sha256
            != qualification.qualification_admission_sha256
            or first.evidence_identity_sha256
            != admission.payload.evidence_identity.artifact_sha256()
            or first.environment_digest != expected.environment_digest
            or first.qualification_signer_registry_sha256
            != qualification.qualification_signer_registry_sha256
            or first.qualification_signer_registry_revision
            != qualification.qualification_signer_registry.revision
        ):
            raise ValueError("retained delivery chain differs from admitted live state")
        if chain.entries and (
            chain.entries[0].authenticated_runner_id_sha256 != runner_id_sha256
            or chain.entries[0].authenticated_session_id_sha256
            != runner_session_id_sha256
        ):
            raise ValueError("retained delivery chain differs from admitted live state")
        if report.managed_result_loss is not None:
            loss = report.managed_result_loss
            pending = chain.pending
            first_permit = chain.entries[0] if chain.entries else pending
            pending_digest = (
                pending.permit_artifact_sha256 if pending is not None else None
            )
            if (
                first_permit is None
                or loss.observed_at != result_loss_observed_at
                or loss.pending_permit_artifact_sha256 != pending_digest
                or loss.run_request_sha256 != first_permit.run_request_sha256
                or loss.pending_action_request_sha256
                != (pending.action_request_sha256 if pending is not None else None)
                or loss.dispatch_id != dispatch.dispatch_id
                or loss.dispatch_session_id != dispatch.dispatch_session_id
                or loss.managed_dispatch_binding_sha256
                != dispatch.payload.dispatch_binding_sha256
                or loss.idempotency_key_sha256
                != managed_result_loss_idempotency_sha256(dispatch.idempotency_key)
                or loss.authenticated_runner_id_sha256 != runner_id_sha256
                or loss.authenticated_session_id_sha256 != runner_session_id_sha256
                or loss.execution_authority_id != dispatch.execution_authority_id
                or loss.execution_authority_sha256
                != dispatch.execution_authority_sha256
                or loss.execution_authority_signer_sha256
                != dispatch.execution_authority_signer_sha256
            ):
                raise ValueError("managed result loss differs from retained live state")
            assert retained_result_loss_closure is not None
            verify_production_delivery_result_loss_closure_binding(
                retained_result_loss_closure,
                permit_chain=chain,
                result_loss=loss,
                tenant_id=dispatch.tenant_id,
                terminal_verified_at=now_text,
            )
        context = ProductionTerminalVerificationContext(
            run_id=dispatch.run_id,
            tenant_id=dispatch.tenant_id,
            workflow_id=dispatch.workflow_id,
            workflow_version_id=dispatch.workflow_version_id,
            bundle_version_id=dispatch.workflow_version_id,
            bundle_artifact_sha256=expected.bundle_artifact_sha256,
            environment_digest=expected.environment_digest,
            environment_contract_sha256=expected.environment_contract_sha256,
            runtime_environment_sha256=expected.runtime_environment_sha256,
            identity_contract_sha256=expected.identity_contract_sha256,
            effect_contract_sha256=expected.effect_contract_sha256,
            runtime_validation_id=expected.runtime_validation_id,
            runtime_substrate=runtime.substrate,
            admission_id=admission.payload.admission_id,
            admission_artifact_sha256=(qualification.qualification_admission_sha256),
            admission_policy_sha256=evidence_identity.admission_policy_sha256,
            evidence_identity_sha256=evidence_identity.artifact_sha256(),
            admitted_runtime_build_sha256=runtime.artifact_sha256(),
            evidence_runner_signer_sha256=expected.evidence_runner_signer_sha256,
            qualification_signer_registry_sha256=(
                qualification.qualification_signer_registry_sha256
            ),
            qualification_signer_registry_revision=(
                qualification.qualification_signer_registry.revision
            ),
            execution_authority_id=dispatch.execution_authority_id,
            execution_authority_sha256=dispatch.execution_authority_sha256,
            execution_authority_signer_sha256=(
                dispatch.execution_authority_signer_sha256
            ),
            permit_chain=chain,
            delivery_result_loss_closure=retained_result_loss_closure,
            run_report_object_version="sha256:" + prepared.report_sha256,
            verified_at=now_text,
            issued_at=now_text,
        )
        built = build_production_terminal_verification(
            report,
            context=context,
            private_key=private_key,
        )
        if (
            built.report_bytes != prepared.report_bytes
            or built.report_sha256 != prepared.report_sha256
        ):
            raise ValueError("terminal report changed during proof production")
        envelope_bytes = canonical_json(built.envelope)
        if len(envelope_bytes) > _MAX_ARTIFACT_BYTES:
            raise ValueError("terminal verification exceeds the callback size limit")
        payload = built.envelope.payload
        final = chain.entries[-1] if chain.entries else None
        final_authority = chain.pending or final
        live_expected = ProductionTerminalVerificationExpected(
            run_id=dispatch.run_id,
            flow_run_id_sha256=flow_run_id_sha256,
            tenant_id=dispatch.tenant_id,
            workflow_id=dispatch.workflow_id,
            workflow_version_id=dispatch.workflow_version_id,
            bundle_version_id=dispatch.workflow_version_id,
            bundle_artifact_sha256=expected.bundle_artifact_sha256,
            bundle_content_digest=expected.bundle_content_digest,
            environment_digest=expected.environment_digest,
            environment_contract_sha256=expected.environment_contract_sha256,
            runtime_environment_sha256=expected.runtime_environment_sha256,
            identity_contract_sha256=expected.identity_contract_sha256,
            effect_contract_sha256=expected.effect_contract_sha256,
            runtime_validation_id=expected.runtime_validation_id,
            runtime_substrate=runtime.substrate,
            admission_id=admission.payload.admission_id,
            admission_artifact_sha256=(qualification.qualification_admission_sha256),
            admission_policy_sha256=evidence_identity.admission_policy_sha256,
            evidence_identity_sha256=evidence_identity.artifact_sha256(),
            admitted_runtime_build_sha256=runtime.artifact_sha256(),
            evidence_runner_signer_sha256=expected.evidence_runner_signer_sha256,
            qualification_signer_registry_sha256=(
                qualification.qualification_signer_registry_sha256
            ),
            qualification_signer_registry_revision=(
                qualification.qualification_signer_registry.revision
            ),
            execution_authority_id=dispatch.execution_authority_id,
            execution_authority_sha256=dispatch.execution_authority_sha256,
            execution_authority_signer_sha256=(
                dispatch.execution_authority_signer_sha256
            ),
            permit_chain_sha256=chain.permit_chain_sha256,
            permit_count=len(chain.entries) + (1 if chain.pending is not None else 0),
            acknowledged_permit_count=len(chain.entries),
            pending_permit_count=(1 if chain.pending is not None else 0),
            pending_permit_artifact_sha256=(
                chain.pending.permit_artifact_sha256
                if chain.pending is not None
                else None
            ),
            final_authority_sequence=(
                final_authority.authority_sequence if final_authority is not None else 0
            ),
            final_runtime_delivery_sequence=(
                final.runtime_delivery_sequence if final is not None else 0
            ),
            authenticated_runner_id_sha256=runner_id_sha256,
            authenticated_session_id_sha256=runner_session_id_sha256,
            acknowledged_one_use_claim_ids=tuple(
                item.one_use_claim_id for item in chain.entries
            ),
            workflow_contract_sha256=payload.workflow_contract_sha256,
            execution_outcome_sha256=payload.execution_outcome_sha256,
            run_receipt_sha256=payload.run_receipt_sha256,
            run_report_sha256=built.report_sha256,
            run_report_object_version=context.run_report_object_version,
            run_report_object_sha256=built.report_sha256,
            evidence_manifests=payload.evidence_manifests,
            managed_result_loss=payload.managed_result_loss,
            delivery_result_loss_closure=payload.delivery_result_loss_closure,
        )
        artifact_sha256 = verify_production_terminal_verification_from_report(
            built.envelope,
            report_bytes=built.report_bytes,
            expected=live_expected,
            now=now,
        )
        if artifact_sha256 != hashlib.sha256(envelope_bytes).hexdigest():
            raise ValueError("terminal verification artifact digest changed")

        # One exact CAS object owns the terminal state. Once it exists, a late
        # child result or a concurrent terminalizer can only return the same
        # bytes; it can never replace reconciliation with another outcome.
        report_path = run_dir / "production-terminal-report.json"
        envelope_path = run_dir / "production-terminal-verification.json"
        state_path = run_dir / "production-terminal-state.json"
        state_bytes = canonical_json(
            {
                "schema_version": "openadapt.production-terminal-state/v1",
                "report_bytes_base64": b64encode(built.report_bytes).decode("ascii"),
                "report_sha256": built.report_sha256,
                "terminal_verification_bytes_base64": b64encode(envelope_bytes).decode(
                    "ascii"
                ),
                "terminal_verification_sha256": artifact_sha256,
            }
        )
        try:
            self._write_private_bytes_atomic_exclusive(state_path, state_bytes)
        except FileExistsError:
            retained_state = self._read_private_bytes(
                state_path,
                maximum_bytes=_MAX_TERMINAL_STATE_BYTES,
                label="production terminal state",
            )
            if retained_state != state_bytes:
                try:
                    retained_value = json.loads(retained_state.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("retained terminal state is invalid") from exc
                expected_state_keys = {
                    "schema_version",
                    "report_bytes_base64",
                    "report_sha256",
                    "terminal_verification_bytes_base64",
                    "terminal_verification_sha256",
                }
                if (
                    not isinstance(retained_value, dict)
                    or set(retained_value) != expected_state_keys
                    or retained_value["schema_version"]
                    != "openadapt.production-terminal-state/v1"
                    or not all(
                        isinstance(retained_value[field], str)
                        for field in expected_state_keys - {"schema_version"}
                    )
                    or canonical_json(retained_value) != retained_state
                ):
                    raise ValueError("retained terminal state is invalid")
                try:
                    retained_report = b64decode(
                        retained_value["report_bytes_base64"], validate=True
                    )
                    retained_envelope = b64decode(
                        retained_value["terminal_verification_bytes_base64"],
                        validate=True,
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError("retained terminal state is invalid") from exc
                if (
                    len(retained_report) > _MAX_ARTIFACT_BYTES
                    or len(retained_envelope) > _MAX_ARTIFACT_BYTES
                    or retained_report != built.report_bytes
                    or hashlib.sha256(retained_report).hexdigest()
                    != retained_value["report_sha256"]
                    or hashlib.sha256(retained_envelope).hexdigest()
                    != retained_value["terminal_verification_sha256"]
                ):
                    raise ValueError("a different terminal outcome is already sealed")
                try:
                    retained_proof = (
                        ProductionTerminalVerificationEnvelope.model_validate_json(
                            retained_envelope
                        )
                    )
                except ValueError as exc:
                    raise ValueError("retained terminal state is invalid") from exc
                retained_sha256 = verify_production_terminal_verification_from_report(
                    retained_proof,
                    report_bytes=retained_report,
                    expected=live_expected,
                    now=now,
                )
                if (
                    canonical_json(retained_proof) != retained_envelope
                    or retained_sha256 != retained_value["terminal_verification_sha256"]
                ):
                    raise ValueError("retained terminal state is invalid")
                state_bytes = retained_state
                envelope_bytes = retained_envelope
                artifact_sha256 = retained_sha256
        for path, raw, label in (
            (report_path, built.report_bytes, "production terminal report"),
            (envelope_path, envelope_bytes, "production terminal verification"),
        ):
            try:
                self._write_private_bytes_atomic_exclusive(path, raw)
            except FileExistsError:
                retained = self._read_private_bytes(
                    path,
                    maximum_bytes=_MAX_ARTIFACT_BYTES,
                    label=label,
                )
                if retained != raw:
                    raise ValueError("a different terminal artifact is already sealed")
        stored_report = self._read_private_bytes(
            report_path,
            maximum_bytes=_MAX_ARTIFACT_BYTES,
            label="production terminal report",
        )
        stored_envelope = self._read_private_bytes(
            envelope_path,
            maximum_bytes=_MAX_ARTIFACT_BYTES,
            label="production terminal verification",
        )
        if stored_report != built.report_bytes or stored_envelope != envelope_bytes:
            raise ValueError("stored terminal evidence changed after write")
        reread = ProductionTerminalVerificationEnvelope.model_validate_json(
            stored_envelope
        )
        if canonical_json(reread) != stored_envelope:
            raise ValueError("stored terminal verification is not canonical")
        reread_sha256 = verify_production_terminal_verification_from_report(
            reread,
            report_bytes=stored_report,
            expected=live_expected,
            now=now,
        )
        if reread_sha256 != artifact_sha256:
            raise ValueError("stored terminal verification digest changed")
        if (
            self._read_private_bytes(
                state_path,
                maximum_bytes=_MAX_TERMINAL_STATE_BYTES,
                label="production terminal state",
            )
            != state_bytes
        ):
            raise ValueError("stored terminal state changed")
        return reread, built.report_sha256

    @staticmethod
    def _refusal(
        dispatch: HostedDispatchWire | None, code: str, detail: str
    ) -> HostedDispatchRefusal:
        events: tuple[dict[str, Any], ...] = ()
        if dispatch is not None:
            refusal = Refusal(RefusalCode.MALFORMED_DISPATCH, detail[:300])
            events = tuple(
                refusal_events(
                    refusal,
                    run_id=dispatch.run_id,
                    workflow_id=dispatch.workflow_id,
                    bundle_digest=dispatch.payload.bundle.content_digest,
                    authorization_id=dispatch.payload.authorization.authorization_id,
                )
            )
        return HostedDispatchRefusal(
            dispatch_id=dispatch.dispatch_id if dispatch else None,
            run_id=dispatch.run_id if dispatch else None,
            code=code,
            detail=detail[:400],
            evidence_batch=events,
        )

    @staticmethod
    def recovery_binding(dispatch: HostedDispatch) -> HostedRecoveryBinding:
        """Project the exact credential-bearing state needed after a crash."""

        return HostedRecoveryBinding(
            dispatch_id=dispatch.dispatch_id,
            runner_session_id=dispatch.runner_session_id,
            dispatch_session_id=dispatch.dispatch_session_id,
            run_id=dispatch.run_id,
            workflow_id=dispatch.workflow_id,
            idempotency_key=dispatch.idempotency_key,
            lease_token=dispatch.lease_token,
            flow_release_verification_receipt_object_sha256=(
                dispatch.flow_release_verification_receipt.artifact_sha256
            ),
            workflow_admission_sha256=dispatch.workflow_admission.artifact_sha256,
            bundle_content_digest=dispatch.payload.bundle.content_digest,
            authorization_id=dispatch.payload.authorization.authorization_id,
        )

    def reconciliation_required(
        self,
        binding: HostedRecoveryBinding | Mapping[str, object],
        *,
        code: str = "runner_result_lost",
    ) -> HostedRunResult:
        """Refuse an unsigned callback without re-entering the execution path."""

        HostedRecoveryBinding.model_validate(binding)
        if (
            not code
            or len(code) > 64
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in code
            )
        ):
            raise ValueError("reconciliation code is invalid")
        raise RuntimeError("managed run requires signed terminal reconciliation")

    def recover_uncertain_run(
        self,
        dispatch: HostedDispatchWire | Mapping[str, object],
        *,
        runner_config: Path,
        run_dir: Path,
        closure_authority: HostedRunnerTransport,
    ) -> HostedRunResult:
        """Return or create the one signed terminal for a lost managed result.

        This path never invokes the managed child. It accepts only the exact
        original dispatch, protected run store, live admissions, child-start
        marker, and unresolved terminal permit.
        """

        parsed_wire = parse_hosted_dispatch(dispatch)
        if isinstance(parsed_wire, HostedDispatchV1):
            raise ValueError("hosted recovery requires dispatch schema v2")
        parsed = parsed_wire
        run_dir = Path(run_dir)
        run_stat = run_dir.lstat()
        if (
            not stat.S_ISDIR(run_stat.st_mode)
            or stat.S_ISLNK(run_stat.st_mode)
            or (
                os.name != "nt"
                and (
                    run_stat.st_uid != os.geteuid()
                    or stat.S_IMODE(run_stat.st_mode) != 0o700
                )
            )
        ):
            raise ValueError("hosted recovery run directory is not protected")
        self._read_child_start_evidence(
            dispatch=parsed,
            run_dir=run_dir,
            dispatch_binding_sha256=parsed.payload.dispatch_binding_sha256,
        )
        reservation_key = f"{parsed.tenant_id}:{parsed.idempotency_key}"
        reservation = self._ledger.lookup(reservation_key)
        if reservation is None or reservation.get("run_id") != parsed.run_id:
            raise ValueError("hosted recovery lacks the original reservation")
        loss_snapshot = self._retain_managed_result_loss_closure(
            dispatch=parsed,
            run_dir=run_dir,
            dispatch_binding_sha256=parsed.payload.dispatch_binding_sha256,
            closure_authority=closure_authority,
            loss_code="recovered_after_restart",
        )

        config = load_runner_config(runner_config, protected=True)
        self._protected_runner_origin(config)
        self._verify_product_release(parsed, config)
        key = self._load_evidence_private_key(config)
        qualification, deployment_bytes = self._verify_workflow_admission(
            parsed,
            config,
            evidence_private_key=key,
        )
        params = self._resolve_params(parsed, config)
        profile_path = run_dir / "deployment.yaml"
        if (
            self._read_private_bytes(
                profile_path,
                maximum_bytes=1024 * 1024,
                label="staged hosted deployment profile",
            )
            != deployment_bytes
        ):
            raise ValueError("staged hosted deployment profile changed")
        staged_profiles = dict(config.profiles)
        staged_profiles[parsed.payload.deployment_profile_id] = profile_path
        verified = verify_dispatch(
            parsed.payload,
            replace(config, profiles=staged_profiles),
            resolved_params=params,
        )
        if isinstance(verified, Refusal):
            raise ValueError("hosted recovery dispatch no longer verifies")
        if verified.payload.dispatch_binding_sha256 != (
            parsed.payload.dispatch_binding_sha256
        ):
            raise ValueError("hosted recovery dispatch binding changed")

        proof, report_digest, report = self._produce_managed_result_loss(
            dispatch=parsed,
            workflow=verified.workflow,
            run_dir=run_dir,
            qualification=qualification,
            private_key=key,
            verified_params=params,
            dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
            loss_code="recovered_after_restart",
            snapshot=loss_snapshot,
        )
        outcome = TransactionOutcome.RECONCILIATION_REQUIRED
        self._ledger.record_outcome(reservation_key, outcome, run_id=parsed.run_id)
        events = tuple(
            report_events(
                report,
                run_id=parsed.run_id,
                workflow_id=parsed.workflow_id,
                bundle_digest=parsed.payload.bundle.content_digest,
                authorization_id=parsed.payload.authorization.authorization_id,
                consequential_steps=verified.consequential_steps,
                effect_covered_consequential_steps=(
                    verified.effect_covered_consequential_steps
                ),
                terminal_outcome=outcome.value,
            )
        )
        return HostedRunResult(
            dispatch_id=parsed.dispatch_id,
            run_id=parsed.run_id,
            outcome=outcome,
            evidence_batch=events,
            terminal_verification=proof,
            started=True,
            uncertain_delivery=proof.payload.pending_permit_count == 1,
            report_sha256=report_digest,
        )

    def execute(
        self,
        dispatch: HostedDispatchWire | Mapping[str, object],
        *,
        runner_config: Path,
        run_dir: Path,
        authority: DeliveryAuthority,
        closure_authority: HostedRunnerTransport | None = None,
    ) -> Union[HostedRunResult, HostedDispatchRefusal]:
        parsed: HostedDispatchWire | None = None
        try:
            parsed = parse_hosted_dispatch(dispatch)
            assert parsed is not None
            if isinstance(parsed, HostedDispatchV1):
                return self._refusal(
                    parsed,
                    "hosted_protocol_upgrade_required",
                    "dispatch schema v2 is required for managed execution",
                )
            expiry = _utc_seconds(parsed.lease_expires_at, label="hosted lease expiry")
            if datetime.now(timezone.utc) >= expiry:
                raise ValueError("hosted lease expired before execution")
            if (
                authority.url != parsed.managed_delivery_authority_url
                or authority.token != parsed.delivery_authority_token
            ):
                raise ValueError("delivery authority does not match the hosted lease")
            config = load_runner_config(runner_config, protected=True)
            configured_origin = self._protected_runner_origin(config)
            authority_host = urlsplit(authority.url)
            if (
                configured_origin
                != f"{authority_host.scheme}://{authority_host.netloc}"
            ):
                raise ValueError(
                    "delivery authority origin differs from the protected runner host"
                )
            self._verify_product_release(parsed, config)
            evidence_private_key = self._load_evidence_private_key(config)
            qualification, deployment_bytes = self._verify_workflow_admission(
                parsed,
                config,
                evidence_private_key=evidence_private_key,
            )
            params = self._resolve_params(parsed, config)
            run_dir = Path(run_dir)
            run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            if os.name != "nt":
                run_dir.chmod(0o700)
            run_dir_stat = run_dir.lstat()
            if (
                not stat.S_ISDIR(run_dir_stat.st_mode)
                or stat.S_ISLNK(run_dir_stat.st_mode)
                or (
                    os.name != "nt"
                    and (
                        run_dir_stat.st_uid != os.geteuid()
                        or stat.S_IMODE(run_dir_stat.st_mode) != 0o700
                    )
                )
            ):
                raise ValueError("hosted run directory is not protected")
            profile_path = self._write_private_bytes(
                run_dir / "deployment.yaml", deployment_bytes
            )
            staged_deployment_bytes = self._read_private_bytes(
                profile_path,
                maximum_bytes=1024 * 1024,
                label="staged hosted deployment profile",
            )
            if staged_deployment_bytes != deployment_bytes:
                raise ValueError("staged hosted deployment profile changed")
            staged_profiles = dict(config.profiles)
            staged_profiles[parsed.payload.deployment_profile_id] = profile_path
            staged_config = replace(config, profiles=staged_profiles)
            verified = verify_dispatch(
                parsed.payload,
                staged_config,
                resolved_params=params,
            )
            if isinstance(verified, Refusal):
                return self._refusal(parsed, verified.code.value, verified.reason())
        except Exception as exc:
            return self._refusal(
                parsed,
                "hosted_admission_refused",
                f"prestart_{type(exc).__name__}",
            )

        assert parsed is not None
        reservation_key = f"{parsed.tenant_id}:{parsed.idempotency_key}"
        try:
            self._ledger.reserve(reservation_key, run_id=parsed.run_id)
        except DuplicateActuation:
            return self.reconciliation_required(
                self.recovery_binding(parsed), code="dispatch_already_consumed"
            )

        try:
            params_file = self._write_params(run_dir / "params.json", params)
            qualification_authority_file = self._write_private_json(
                run_dir / "qualification-authority.json", qualification
            )
            guard = ProductionQualificationGuard(
                qualification_authority_file,
                remote_permit_revalidation=True,
            )
            production_binding = guard.authorization_binding(verified.workflow)
            local_authorization = verified.payload.authorization.model_copy(
                update=production_binding
            )
            local_payload = verified.payload.model_copy(
                update={"authorization": local_authorization}
            )
            verified = replace(verified, payload=local_payload)
            dispatch_file = write_managed_dispatch_envelope(
                run_dir / "managed-dispatch.json", verified
            )
            argv = build_run_argv(
                verified,
                run_dir,
                params_file,
                managed_dispatch_file=dispatch_file,
                qualification_authority_file=qualification_authority_file,
            )
            child_env = os.environ.copy()
            child_env.pop(REMOTE_AUTHORITY_URL_ENV, None)
            child_env.pop(REMOTE_AUTHORITY_TOKEN_ENV, None)
            child_env.pop(REMOTE_DISPATCH_SESSION_ID_ENV, None)
            child_env.update(authority.child_environment())
            child_env[REMOTE_DISPATCH_SESSION_ID_ENV] = parsed.dispatch_session_id
        except Exception:  # preparation failed before the managed child started
            self._ledger.record_outcome(
                reservation_key,
                TransactionOutcome.FAILED_PLATFORM,
                run_id=parsed.run_id,
            )
            events = tuple(
                failure_events(
                    run_id=parsed.run_id,
                    bundle_digest=parsed.payload.bundle.content_digest,
                    authorization_id=parsed.payload.authorization.authorization_id,
                )
            )
            return HostedRunResult(
                dispatch_id=parsed.dispatch_id,
                run_id=parsed.run_id,
                outcome=TransactionOutcome.FAILED_PLATFORM,
                evidence_batch=events,
                started=False,
                uncertain_delivery=False,
                report_sha256="0" * 64,
            )

        try:
            self._write_child_start_evidence(
                dispatch=parsed,
                run_dir=run_dir,
                dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
            )
        except Exception:
            outcome = TransactionOutcome.FAILED_PLATFORM
            self._ledger.record_outcome(reservation_key, outcome, run_id=parsed.run_id)
            events = tuple(
                failure_events(
                    run_id=parsed.run_id,
                    bundle_digest=parsed.payload.bundle.content_digest,
                    authorization_id=parsed.payload.authorization.authorization_id,
                )
            )
            return HostedRunResult(
                dispatch_id=parsed.dispatch_id,
                run_id=parsed.run_id,
                outcome=outcome,
                evidence_batch=events,
                started=False,
                uncertain_delivery=False,
                report_sha256="0" * 64,
            )

        try:
            # Once this call starts, the child can reach a real input edge. Any
            # lost or malformed result is uncertain and can never be retried.
            execution = self._runner(argv, run_dir, child_env)
        except Exception:
            outcome = TransactionOutcome.RECONCILIATION_REQUIRED
            try:
                if closure_authority is None:
                    raise ValueError("hosted result-loss closure authority is absent")
                loss_snapshot = self._retain_managed_result_loss_closure(
                    dispatch=parsed,
                    run_dir=run_dir,
                    dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
                    closure_authority=closure_authority,
                    loss_code="runner_exception",
                )
                terminal_key, terminal_qualification = (
                    self._revalidate_terminal_authority(
                        dispatch=parsed,
                        runner_config=runner_config,
                        configured_origin=configured_origin,
                        initial_qualification=qualification,
                        initial_deployment_bytes=deployment_bytes,
                    )
                )
                exception_loss_proof, report_digest, exception_loss_report = (
                    self._produce_managed_result_loss(
                        dispatch=parsed,
                        workflow=verified.workflow,
                        run_dir=run_dir,
                        qualification=terminal_qualification,
                        private_key=terminal_key,
                        verified_params=params,
                        dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
                        loss_code="runner_exception",
                        snapshot=loss_snapshot,
                    )
                )
            except Exception as terminal_exc:
                self._ledger.record_outcome(
                    reservation_key, outcome, run_id=parsed.run_id
                )
                raise RuntimeError(
                    "managed run requires signed terminal reconciliation"
                ) from terminal_exc
            self._ledger.record_outcome(reservation_key, outcome, run_id=parsed.run_id)
            events = tuple(
                report_events(
                    exception_loss_report,
                    run_id=parsed.run_id,
                    workflow_id=parsed.workflow_id,
                    bundle_digest=parsed.payload.bundle.content_digest,
                    authorization_id=parsed.payload.authorization.authorization_id,
                    consequential_steps=verified.consequential_steps,
                    effect_covered_consequential_steps=(
                        verified.effect_covered_consequential_steps
                    ),
                    terminal_outcome=outcome.value,
                )
            )
            return HostedRunResult(
                dispatch_id=parsed.dispatch_id,
                run_id=parsed.run_id,
                outcome=outcome,
                evidence_batch=events,
                terminal_verification=exception_loss_proof,
                started=True,
                uncertain_delivery=(
                    exception_loss_proof.payload.pending_permit_count == 1
                ),
                report_sha256=report_digest,
            )

        if execution.report_bytes is None:
            outcome = TransactionOutcome.RECONCILIATION_REQUIRED
            try:
                if closure_authority is None:
                    raise ValueError("hosted result-loss closure authority is absent")
                loss_snapshot = self._retain_managed_result_loss_closure(
                    dispatch=parsed,
                    run_dir=run_dir,
                    dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
                    closure_authority=closure_authority,
                    loss_code="report_missing",
                )
                terminal_key, terminal_qualification = (
                    self._revalidate_terminal_authority(
                        dispatch=parsed,
                        runner_config=runner_config,
                        configured_origin=configured_origin,
                        initial_qualification=qualification,
                        initial_deployment_bytes=deployment_bytes,
                    )
                )
                missing_loss_proof, report_digest, missing_loss_report = (
                    self._produce_managed_result_loss(
                        dispatch=parsed,
                        workflow=verified.workflow,
                        run_dir=run_dir,
                        qualification=terminal_qualification,
                        private_key=terminal_key,
                        verified_params=params,
                        dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
                        loss_code="report_missing",
                        snapshot=loss_snapshot,
                    )
                )
            except Exception as terminal_exc:
                self._ledger.record_outcome(
                    reservation_key, outcome, run_id=parsed.run_id
                )
                raise RuntimeError(
                    "managed run requires signed terminal reconciliation"
                ) from terminal_exc
            self._ledger.record_outcome(reservation_key, outcome, run_id=parsed.run_id)
            events = tuple(
                report_events(
                    missing_loss_report,
                    run_id=parsed.run_id,
                    workflow_id=parsed.workflow_id,
                    bundle_digest=parsed.payload.bundle.content_digest,
                    authorization_id=parsed.payload.authorization.authorization_id,
                    consequential_steps=verified.consequential_steps,
                    effect_covered_consequential_steps=(
                        verified.effect_covered_consequential_steps
                    ),
                    terminal_outcome=outcome.value,
                )
            )
            return HostedRunResult(
                dispatch_id=parsed.dispatch_id,
                run_id=parsed.run_id,
                outcome=outcome,
                evidence_batch=events,
                terminal_verification=missing_loss_proof,
                started=True,
                uncertain_delivery=(
                    missing_loss_proof.payload.pending_permit_count == 1
                ),
                report_sha256=report_digest,
            )
        report: RunReport | None = None
        report_digest = hashlib.sha256(execution.report_bytes).hexdigest()
        terminalization_error: Exception | None
        try:
            report = RunReport.model_validate_json(execution.report_bytes)
            assert report is not None
            outcome = classify_transaction_outcome(report)
            proof: ProductionTerminalVerificationEnvelope | None = None
            if execution.terminal_verification is not None:
                raise ValueError("managed child supplied an untrusted terminal proof")
            if outcome in {
                TransactionOutcome.VERIFIED,
                TransactionOutcome.HALTED_BEFORE_EFFECT,
                TransactionOutcome.RECONCILIATION_REQUIRED,
            }:
                if outcome is TransactionOutcome.VERIFIED and execution.returncode != 0:
                    raise ValueError("managed child exited unsuccessfully")
                terminal_key, terminal_qualification = (
                    self._revalidate_terminal_authority(
                        dispatch=parsed,
                        runner_config=runner_config,
                        configured_origin=configured_origin,
                        initial_qualification=qualification,
                        initial_deployment_bytes=deployment_bytes,
                    )
                )
                proof, report_digest = self._produce_terminal_verification(
                    dispatch=parsed,
                    report=report,
                    run_dir=run_dir,
                    qualification=terminal_qualification,
                    private_key=terminal_key,
                    verified_params=params,
                    dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
                )
                if proof.payload.run_receipt.transaction_outcome != outcome.value:
                    raise ValueError("terminal proof outcome differs from the report")
        except Exception as exc:  # noqa: BLE001 - terminalization fails closed
            outcome = TransactionOutcome.RECONCILIATION_REQUIRED
            proof = None
            terminalization_error = exc
            try:
                if closure_authority is None:
                    raise ValueError("hosted result-loss closure authority is absent")
                loss_snapshot = self._retain_managed_result_loss_closure(
                    dispatch=parsed,
                    run_dir=run_dir,
                    dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
                    closure_authority=closure_authority,
                    loss_code="report_invalid",
                )
                terminal_key, terminal_qualification = (
                    self._revalidate_terminal_authority(
                        dispatch=parsed,
                        runner_config=runner_config,
                        configured_origin=configured_origin,
                        initial_qualification=qualification,
                        initial_deployment_bytes=deployment_bytes,
                    )
                )
                proof, report_digest, report = self._produce_managed_result_loss(
                    dispatch=parsed,
                    workflow=verified.workflow,
                    run_dir=run_dir,
                    qualification=terminal_qualification,
                    private_key=terminal_key,
                    verified_params=params,
                    dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
                    loss_code="report_invalid",
                    snapshot=loss_snapshot,
                )
            except Exception as loss_exc:  # noqa: BLE001 - remains fenced/reconciling
                terminalization_error = loss_exc
            else:
                terminalization_error = None
        else:
            terminalization_error = None
        if proof is None and outcome in {
            TransactionOutcome.VERIFIED,
            TransactionOutcome.HALTED_BEFORE_EFFECT,
            TransactionOutcome.RECONCILIATION_REQUIRED,
        }:
            outcome = TransactionOutcome.RECONCILIATION_REQUIRED
        self._ledger.record_outcome(reservation_key, outcome, run_id=parsed.run_id)
        if outcome is TransactionOutcome.RECONCILIATION_REQUIRED and proof is None:
            raise RuntimeError(
                "managed run requires signed terminal reconciliation"
            ) from terminalization_error
        if report is None:
            events = tuple(
                failure_events(
                    run_id=parsed.run_id,
                    bundle_digest=parsed.payload.bundle.content_digest,
                    authorization_id=parsed.payload.authorization.authorization_id,
                )
            )
        else:
            events = tuple(
                report_events(
                    report,
                    run_id=parsed.run_id,
                    workflow_id=parsed.workflow_id,
                    bundle_digest=parsed.payload.bundle.content_digest,
                    authorization_id=parsed.payload.authorization.authorization_id,
                    consequential_steps=verified.consequential_steps,
                    effect_covered_consequential_steps=(
                        verified.effect_covered_consequential_steps
                    ),
                    terminal_outcome=outcome.value,
                )
            )
        uncertain = bool(proof is not None and proof.payload.pending_permit_count == 1)
        return HostedRunResult(
            dispatch_id=parsed.dispatch_id,
            run_id=parsed.run_id,
            outcome=outcome,
            evidence_batch=events,
            terminal_verification=proof,
            started=True,
            uncertain_delivery=uncertain,
            report_sha256=report_digest,
        )

    def callback_request(
        self,
        dispatch: (
            HostedDispatchWire | HostedRecoveryBindingWire | Mapping[str, object]
        ),
        result: HostedRunResult | HostedDispatchRefusal,
    ) -> CallbackRequestWire:
        """Build one closing callback, or refuse a proofless v2 closure."""

        if isinstance(dispatch, (HostedDispatchV1, HostedDispatchV2)):
            binding: HostedDispatchWire | HostedRecoveryBindingWire = dispatch
        elif isinstance(dispatch, (HostedRecoveryBindingV1, HostedRecoveryBindingV2)):
            binding = dispatch
        else:
            schema = dispatch.get("schema_version")
            if schema == "openadapt.hosted-runner-recovery/v1":
                binding = HostedRecoveryBindingV1.model_validate(dispatch)
            elif schema == "openadapt.hosted-runner-recovery/v2":
                binding = HostedRecoveryBindingV2.model_validate(dispatch)
            else:
                binding = parse_hosted_dispatch(dispatch)
        if result.dispatch_id != binding.dispatch_id or result.run_id != binding.run_id:
            raise ValueError("hosted result does not bind the callback lease")
        if isinstance(
            binding, (HostedDispatchV2, HostedRecoveryBindingV2)
        ) and isinstance(result, HostedDispatchRefusal):
            raise ValueError(
                "callback v2 cannot close a proofless pre-actuation refusal"
            )
        events = list(result.evidence_batch)
        proof_base64 = None
        proof_digest = None
        if isinstance(result, HostedRunResult):
            if result.terminal_verification is not None:
                proof_bytes = canonical_json(result.terminal_verification)
                proof_digest = result.terminal_verification.artifact_sha256()
                if hashlib.sha256(proof_bytes).hexdigest() != proof_digest:
                    raise ValueError("terminal proof digest differs from exact bytes")
                proof_base64 = b64encode(proof_bytes).decode("ascii")
        terminal_type = (
            HostedTerminalEventV1
            if isinstance(binding, (HostedDispatchV1, HostedRecoveryBindingV1))
            else HostedTerminalEventV2
        )
        terminal = terminal_type(
            run_id=binding.run_id,
            outcome=(
                result.outcome.value
                if isinstance(result.outcome, TransactionOutcome)
                else result.outcome
            ),
            report_sha256=result.report_sha256,
            started=result.started,
            uncertain_delivery=result.uncertain_delivery,
            terminal_verification_artifact_bytes_base64=proof_base64,
            terminal_verification_artifact_sha256=proof_digest,
        )
        events.append(terminal.model_dump(mode="json"))
        workflow_admission_sha256 = (
            binding.workflow_admission.artifact_sha256
            if isinstance(binding, (HostedDispatchV1, HostedDispatchV2))
            else binding.workflow_admission_sha256
        )
        if isinstance(binding, (HostedDispatchV1, HostedRecoveryBindingV1)):
            return CallbackRequestV1(
                dispatch_id=binding.dispatch_id,
                runner_session_id=binding.runner_session_id,
                idempotency_key=binding.idempotency_key,
                lease_token=binding.lease_token,
                workflow_admission_sha256=workflow_admission_sha256,
                events=tuple(events),
                product_release_admission_sha256=(
                    binding.product_release_admission.artifact_sha256
                    if isinstance(binding, HostedDispatchV1)
                    else binding.product_release_admission_sha256
                ),
            )
        return CallbackRequestV2(
            dispatch_id=binding.dispatch_id,
            runner_session_id=binding.runner_session_id,
            idempotency_key=binding.idempotency_key,
            lease_token=binding.lease_token,
            workflow_admission_sha256=workflow_admission_sha256,
            events=tuple(events),
            flow_release_verification_receipt_object_sha256=(
                binding.flow_release_verification_receipt.artifact_sha256
                if isinstance(binding, HostedDispatchV2)
                else binding.flow_release_verification_receipt_object_sha256
            ),
        )
