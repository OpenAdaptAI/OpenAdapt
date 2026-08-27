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

from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.private_file import (
    PrivateFileAclError,
    windows_descriptor_has_private_acl,
)
from openadapt_flow.production_qualification import (
    ProductionQualificationAuthority,
    ProductionQualificationGuard,
    _read_private_json,
)
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
    ProductionTerminalVerificationContext,
    ProductionTerminalVerificationEnvelope,
    ProductionTerminalVerificationExpected,
    build_production_terminal_verification,
    evidence_runner_signer_sha256,
    prepare_production_terminal_evidence,
    verify_production_terminal_verification_from_report,
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


class RegisterRequest(_Closed):
    schema_version: Literal["openadapt.hosted-runner-registration/v1"] = (
        "openadapt.hosted-runner-registration/v1"
    )
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
    def _exact_local_targets(self) -> "RegisterRequest":
        if set(self.local_runtime_release) != {"flow", "desktop", "capture"} or any(
            key != item.target for key, item in self.local_runtime_release.items()
        ):
            raise ValueError(
                "local runtime release targets must be flow, desktop, capture"
            )
        return self


class RegisterResponse(_Closed):
    schema_version: Literal["openadapt.hosted-runner-registration-result/v1"]
    runner_id: str = Field(pattern=_UUID)
    tenant_id: str = Field(pattern=_UUID)
    runner_session_id: str = Field(pattern=_UUID)
    runner_token: str = Field(pattern=_RUNNER_TOKEN, repr=False)
    token_expires_at: str

    @model_validator(mode="after")
    def _canonical_expiry(self) -> "RegisterResponse":
        _utc_seconds(self.token_expires_at, label="runner token expiry")
        return self


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


class HostedDispatch(_Closed):
    schema_version: Literal["openadapt.hosted-runner/v1"]
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
    product_release_admission: AdmissionArtifactBytes
    workflow_admission: AdmissionArtifactBytes
    managed_delivery_authority_url: str = Field(min_length=1, max_length=2048)
    delivery_authority_token: str = Field(pattern=_HEX64, repr=False)
    payload: RunnerDispatchPayload

    @model_validator(mode="after")
    def _exact_run_binding(self) -> "HostedDispatch":
        _utc_seconds(self.lease_expires_at, label="hosted lease expiry")
        if (
            self.payload.run_id != self.run_id
            or self.payload.workflow_id != self.workflow_id
        ):
            raise ValueError("hosted lease identity does not match its payload")
        if self.payload.bundle.version_id != self.workflow_version_id:
            raise ValueError("hosted lease workflow version does not match its bundle")
        return self


class HostedRecoveryBinding(_Closed):
    """Callback state without params or the delivery-authority credential.

    This projection remains credential-bearing because it retains the lease
    token required for the exact terminal callback.
    """

    schema_version: Literal["openadapt.hosted-runner-recovery/v1"] = (
        "openadapt.hosted-runner-recovery/v1"
    )
    dispatch_id: str = Field(pattern=_UUID)
    runner_session_id: str = Field(pattern=_UUID)
    dispatch_session_id: str = Field(pattern=_UUID)
    run_id: str = Field(pattern=_UUID)
    workflow_id: str = Field(pattern=_UUID)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY)
    lease_token: str = Field(pattern=_LEASE_TOKEN, repr=False)
    product_release_admission_sha256: str = Field(pattern=_HEX64)
    workflow_admission_sha256: str = Field(pattern=_HEX64)
    bundle_content_digest: str = Field(pattern=_HEX64)
    authorization_id: str = Field(min_length=1, max_length=128)


class HostedTerminalEvent(_Closed):
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
    def _verified_requires_exact_proof(self) -> "HostedTerminalEvent":
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
                proof = ProductionTerminalVerificationEnvelope.model_validate_json(raw)
            except (ValueError, TypeError) as exc:
                raise ValueError("terminal verification artifact is invalid") from exc
            if (
                len(raw) > _MAX_ARTIFACT_BYTES
                or b64encode(raw).decode("ascii")
                != self.terminal_verification_artifact_bytes_base64
                or canonical_json(proof) != raw
                or proof.artifact_sha256() != self.terminal_verification_artifact_sha256
            ):
                raise ValueError("terminal verification artifact binding is invalid")
            if (
                proof.payload.run_id != self.run_id
                or proof.payload.run_report_sha256 != self.report_sha256
                or proof.payload.run_report_object_sha256 != self.report_sha256
            ):
                raise ValueError("terminal verification names a different run report")
        return self


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
        if (self.outcome is TransactionOutcome.VERIFIED) != (
            self.terminal_verification is not None
        ):
            raise ValueError("only a terminally verified result can be VERIFIED")
        if self.uncertain_delivery and self.outcome not in {
            TransactionOutcome.RECONCILIATION_REQUIRED,
            TransactionOutcome.VERIFIED,
        }:
            raise ValueError("uncertain delivery has an invalid terminal outcome")
        if self.terminal_verification is not None and (
            self.terminal_verification.payload.run_id != self.run_id
            or self.terminal_verification.payload.run_report_sha256
            != self.report_sha256
            or self.terminal_verification.payload.run_report_object_sha256
            != self.report_sha256
        ):
            raise ValueError("terminal verification names a different run report")
        return self


class HostedDispatchRefusal(_Closed):
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


class CallbackRequest(_Closed):
    schema_version: Literal["openadapt.hosted-runner-callback/v1"] = (
        "openadapt.hosted-runner-callback/v1"
    )
    dispatch_id: str = Field(pattern=_UUID)
    runner_session_id: str = Field(pattern=_UUID)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY)
    lease_token: str = Field(pattern=_LEASE_TOKEN, repr=False)
    product_release_admission_sha256: str = Field(pattern=_HEX64)
    workflow_admission_sha256: str = Field(pattern=_HEX64)
    events: tuple[dict[str, Any], ...] = Field(min_length=1, max_length=10_001)


class CallbackResponse(_Closed):
    schema_version: Literal["openadapt.hosted-runner-callback-result/v1"]
    status: Literal["accepted", "duplicate"]
    run_id: str = Field(pattern=_UUID)
    outcome: TransactionOutcome
    dispatch_state: Literal["closed"]
    accepted_events: int = Field(ge=0, le=10_001)


class HostedRunnerTransport(Protocol):
    """Desktop-owned HTTP surface. Credentials stay in its transport state."""

    def register(self, request: RegisterRequest) -> RegisterResponse: ...

    def poll(self, request: PollRequest) -> HostedDispatch | None: ...

    def callback(self, run_id: str, request: CallbackRequest) -> CallbackResponse: ...


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
    ) -> tuple[ProductionTerminalVerificationEnvelope, str]:
        """Build, retain, reread, and verify one exact terminal-v2 proof."""

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

        chain = DurableAuthority(run_dir, store).production_delivery_permit_chain()
        first = chain.entries[0]
        expected = qualification.expected
        runtime = expected.runtime_build_identity
        flow_run_id_sha256 = hashlib.sha256(dispatch.run_id.encode("utf-8")).hexdigest()
        runner_id_sha256 = hashlib.sha256(
            dispatch.runner_id.encode("utf-8")
        ).hexdigest()
        runner_session_id_sha256 = hashlib.sha256(
            dispatch.runner_session_id.encode("utf-8")
        ).hexdigest()
        if (
            first.run_id != dispatch.run_id
            or first.flow_run_id_sha256 != flow_run_id_sha256
            or first.admission_artifact_sha256
            != qualification.qualification_admission_sha256
            or first.evidence_identity_sha256
            != admission.payload.evidence_identity.artifact_sha256()
            or first.environment_digest != expected.environment_digest
            or first.qualification_signer_registry_sha256
            != qualification.qualification_signer_registry_sha256
            or first.qualification_signer_registry_revision
            != qualification.qualification_signer_registry.revision
            or first.authenticated_runner_id_sha256 != runner_id_sha256
            or first.authenticated_session_id_sha256 != runner_session_id_sha256
        ):
            raise ValueError("retained delivery chain differs from admitted live state")

        prepared = prepare_production_terminal_evidence(report)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        now_text = now.isoformat().replace("+00:00", "Z")
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
            execution_authority_id=first.execution_authority_id,
            execution_authority_sha256=first.execution_authority_sha256,
            execution_authority_signer_sha256=first.authority_signer_sha256,
            permit_chain=chain,
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
        payload = built.envelope.payload
        final = chain.entries[-1]
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
            execution_authority_id=first.execution_authority_id,
            execution_authority_sha256=first.execution_authority_sha256,
            execution_authority_signer_sha256=first.authority_signer_sha256,
            permit_chain_sha256=chain.permit_chain_sha256,
            permit_count=len(chain.entries),
            final_authority_sequence=final.authority_sequence,
            final_runtime_delivery_sequence=final.runtime_delivery_sequence,
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
        )
        artifact_sha256 = verify_production_terminal_verification_from_report(
            built.envelope,
            report_bytes=built.report_bytes,
            expected=live_expected,
            now=now,
        )
        if artifact_sha256 != hashlib.sha256(envelope_bytes).hexdigest():
            raise ValueError("terminal verification artifact digest changed")

        # Final-named evidence exists only after the complete in-memory proof
        # passes. If storage or the required reread fails, remove only files
        # created by this call so no failed terminalization leaves success
        # artifacts behind.
        report_path = run_dir / "production-terminal-report.json"
        envelope_path = run_dir / "production-terminal-verification.json"
        written: list[Path] = []
        try:
            self._write_private_bytes(report_path, built.report_bytes)
            written.append(report_path)
            self._write_private_bytes(envelope_path, envelope_bytes)
            written.append(envelope_path)
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
        except Exception:
            for path in reversed(written):
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return reread, built.report_sha256

    @staticmethod
    def _refusal(
        dispatch: HostedDispatch | None, code: str, detail: str
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
            product_release_admission_sha256=(
                dispatch.product_release_admission.artifact_sha256
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
        """Close a crash window without re-entering the execution path."""

        parsed = HostedRecoveryBinding.model_validate(binding)
        if (
            not code
            or len(code) > 64
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in code
            )
        ):
            raise ValueError("reconciliation code is invalid")
        return HostedRunResult(
            dispatch_id=parsed.dispatch_id,
            run_id=parsed.run_id,
            outcome=TransactionOutcome.RECONCILIATION_REQUIRED,
            evidence_batch=tuple(
                failure_events(
                    run_id=parsed.run_id,
                    bundle_digest=parsed.bundle_content_digest,
                    authorization_id=parsed.authorization_id,
                )
            ),
            started=True,
            uncertain_delivery=True,
            report_sha256="0" * 64,
        )

    def execute(
        self,
        dispatch: HostedDispatch | Mapping[str, object],
        *,
        runner_config: Path,
        run_dir: Path,
        authority: DeliveryAuthority,
    ) -> Union[HostedRunResult, HostedDispatchRefusal]:
        parsed: HostedDispatch | None = None
        try:
            parsed = HostedDispatch.model_validate(dispatch)
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
            # Once this call starts, the child can reach a real input edge. Any
            # lost or malformed result is uncertain and can never be retried.
            execution = self._runner(argv, run_dir, child_env)
        except Exception:
            outcome = TransactionOutcome.RECONCILIATION_REQUIRED
            self._ledger.record_outcome(reservation_key, outcome, run_id=parsed.run_id)
            return HostedRunResult(
                dispatch_id=parsed.dispatch_id,
                run_id=parsed.run_id,
                outcome=outcome,
                evidence_batch=tuple(
                    failure_events(
                        run_id=parsed.run_id,
                        bundle_digest=parsed.payload.bundle.content_digest,
                        authorization_id=parsed.payload.authorization.authorization_id,
                    )
                ),
                started=True,
                uncertain_delivery=True,
                report_sha256="0" * 64,
            )

        if execution.report_bytes is None:
            outcome = TransactionOutcome.RECONCILIATION_REQUIRED
            self._ledger.record_outcome(reservation_key, outcome, run_id=parsed.run_id)
            return HostedRunResult(
                dispatch_id=parsed.dispatch_id,
                run_id=parsed.run_id,
                outcome=outcome,
                evidence_batch=tuple(
                    failure_events(
                        run_id=parsed.run_id,
                        bundle_digest=parsed.payload.bundle.content_digest,
                        authorization_id=parsed.payload.authorization.authorization_id,
                    )
                ),
                started=True,
                uncertain_delivery=True,
                report_sha256="0" * 64,
            )
        report: RunReport | None = None
        report_digest = hashlib.sha256(execution.report_bytes).hexdigest()
        try:
            report = RunReport.model_validate_json(execution.report_bytes)
            outcome = classify_transaction_outcome(report)
            proof: ProductionTerminalVerificationEnvelope | None = None
            if execution.terminal_verification is not None:
                raise ValueError("managed child supplied an untrusted terminal proof")
            if outcome is TransactionOutcome.VERIFIED:
                if execution.returncode != 0:
                    raise ValueError("managed child exited unsuccessfully")
                terminal_config = load_runner_config(runner_config, protected=True)
                if self._protected_runner_origin(terminal_config) != configured_origin:
                    raise ValueError("protected runner origin changed during execution")
                self._verify_product_release(parsed, terminal_config)
                terminal_key = self._load_evidence_private_key(terminal_config)
                terminal_qualification, terminal_deployment = (
                    self._verify_workflow_admission(
                        parsed,
                        terminal_config,
                        evidence_private_key=terminal_key,
                    )
                )
                if (
                    terminal_qualification != qualification
                    or terminal_deployment != deployment_bytes
                ):
                    raise ValueError("production admission changed during execution")
                proof, report_digest = self._produce_terminal_verification(
                    dispatch=parsed,
                    report=report,
                    run_dir=run_dir,
                    qualification=qualification,
                    private_key=terminal_key,
                    verified_params=params,
                    dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
                )
        except Exception:  # noqa: BLE001 - post-delivery terminalization fails closed
            outcome = TransactionOutcome.RECONCILIATION_REQUIRED
            proof = None
        if outcome is not TransactionOutcome.VERIFIED:
            proof = None
        self._ledger.record_outcome(reservation_key, outcome, run_id=parsed.run_id)
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
                )
            )
        uncertain = outcome is TransactionOutcome.RECONCILIATION_REQUIRED or any(
            result.delivery_uncertainty is not None
            for result in (report.results if report is not None else ())
        )
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
        dispatch: HostedDispatch | HostedRecoveryBinding | Mapping[str, object],
        result: HostedRunResult | HostedDispatchRefusal,
    ) -> CallbackRequest:
        if isinstance(dispatch, HostedDispatch):
            binding: HostedDispatch | HostedRecoveryBinding = dispatch
        elif isinstance(dispatch, HostedRecoveryBinding):
            binding = dispatch
        else:
            schema = dispatch.get("schema_version")
            if schema == "openadapt.hosted-runner-recovery/v1":
                binding = HostedRecoveryBinding.model_validate(dispatch)
            else:
                binding = HostedDispatch.model_validate(dispatch)
        if result.dispatch_id != binding.dispatch_id or result.run_id != binding.run_id:
            raise ValueError("hosted result does not bind the callback lease")
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
        terminal = HostedTerminalEvent(
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
        return CallbackRequest(
            dispatch_id=binding.dispatch_id,
            runner_session_id=binding.runner_session_id,
            idempotency_key=binding.idempotency_key,
            lease_token=binding.lease_token,
            product_release_admission_sha256=(
                binding.product_release_admission.artifact_sha256
                if isinstance(binding, HostedDispatch)
                else binding.product_release_admission_sha256
            ),
            workflow_admission_sha256=(
                binding.workflow_admission.artifact_sha256
                if isinstance(binding, HostedDispatch)
                else binding.workflow_admission_sha256
            ),
            events=tuple(events),
        )
