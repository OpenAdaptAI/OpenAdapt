"""Signed, durable finite business decisions for ProgramGraph execution.

A business decision is control authority only.  It binds one authenticated
human answer to one qualified branch.  It cannot prove which entity is open,
that an action succeeded, or that a business effect persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.ir import (
    BusinessDecisionEvidence,
    BusinessDecisionOption,
    BusinessDecisionSpec,
    ProgramExecutionScopeFrame,
    Workflow,
)
from openadapt_flow.runtime.authorization import RuntimeParamScalar
from openadapt_flow.runtime.durable.approval import (
    ApprovalRecord,
    ApprovalRequired,
    approval_pause_digest,
    issue_resume_approval,
)
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunManifest,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    GraphFrame,
    bound_params_sha256,
    bundle_version,
    control_frames_hash,
)

ROOT_DIRNAME = ".business_decisions"
KEY_FILENAME = ".business_decision.key"
ACTIVE_FILENAME = "active.json"
LOCK_FILENAME = ".business_decision.lock"
REQUEST_DOMAIN = b"openadapt:business-decision-request-v1\0"
RECEIPT_DOMAIN = b"openadapt:business-decision-receipt-v1\0"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical(payload: Any) -> bytes:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_value(value: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise BusinessDecisionRefused("the business decision SHA-256 is invalid")
    return value


def _digest_value(value: str) -> str:
    if not value.startswith("sha256:"):
        raise BusinessDecisionRefused("the business decision digest is invalid")
    return _sha256_value(value.removeprefix("sha256:"))


def _digest(payload: Any) -> str:
    return "sha256:" + _sha256(_canonical(payload))


class BusinessDecisionRefused(ApprovalRequired):
    """A business answer failed before resumed workflow actuation."""


class BusinessDecisionPrincipal(BaseModel):
    """Identity and roles supplied by an authenticated operator route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operator_ref: str = Field(min_length=1, max_length=256)
    roles: tuple[str, ...] = Field(min_length=1)
    authenticated_by: str = Field(min_length=1, max_length=128)
    authentication_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _roles_are_closed(self) -> "BusinessDecisionPrincipal":
        stripped = tuple(role.strip() for role in self.roles)
        if (
            not self.operator_ref.strip()
            or self.operator_ref.strip() != self.operator_ref
            or not self.authenticated_by.strip()
            or self.authenticated_by.strip() != self.authenticated_by
            or any(not role for role in stripped)
            or stripped != self.roles
            or len(set(self.roles)) != len(self.roles)
        ):
            raise ValueError(
                "principal attribution and roles must be trimmed, unique, and non-empty"
            )
        return self


class BusinessDecisionRequest(BaseModel):
    """Engine-issued signed request for one exact durable program pause."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.business-decision-request/v1"] = (
        "openadapt.business-decision-request/v1"
    )
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    run_id: str = Field(min_length=1)
    workflow_name: str = Field(min_length=1)
    workflow_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_version: str = Field(min_length=1)
    governed_runtime_inputs_digest: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    pause_binding_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    graph_id: str = Field(min_length=1, max_length=128)
    state_id: str = Field(min_length=1, max_length=128)
    program_scope: tuple[ProgramExecutionScopeFrame, ...] = Field(min_length=1)
    control_frames_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bound_params_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: BusinessDecisionSpec
    supersedes_request_sha256: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    supersedes_request_digest: Optional[str] = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    issued_at: str
    expires_at: str
    signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")

    def unsigned(self) -> dict[str, Any]:
        return self.model_dump(exclude={"signature"}, mode="json")

    @model_validator(mode="after")
    def _expiry_matches_contract(self) -> "BusinessDecisionRequest":
        issued = _parse(self.issued_at)
        expires = _parse(self.expires_at)
        if expires != issued + timedelta(seconds=self.decision.expires_after_s):
            raise ValueError("business decision request expiry differs from contract")
        if (self.supersedes_request_sha256 is None) != (
            self.supersedes_request_digest is None
        ):
            raise ValueError(
                "business decision renewal requires both predecessor bindings"
            )
        return self

    @property
    def digest(self) -> str:
        return _digest(self.unsigned())


class BusinessDecisionSubmission(BaseModel):
    """One finite answer and the local evidence digests required by it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_key: str = Field(
        min_length=16,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    option_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    evidence_artifact_sha256s: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _digests_are_exact(self) -> "BusinessDecisionSubmission":
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in self.evidence_artifact_sha256s.values()
        ):
            raise ValueError("business decision evidence digests must be SHA-256")
        return self


class BusinessDecisionReceipt(BaseModel):
    """Signed, attributed, one-use answer retained on the customer runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.business-decision-receipt/v1"] = (
        "openadapt.business-decision-receipt/v1"
    )
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    workflow_name: str = Field(min_length=1)
    workflow_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_version: str = Field(min_length=1)
    governed_runtime_inputs_digest: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    pause_binding_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    graph_id: str = Field(min_length=1, max_length=128)
    state_id: str = Field(min_length=1, max_length=128)
    program_scope: tuple[ProgramExecutionScopeFrame, ...] = Field(min_length=1)
    decision_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    option_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    output_param: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
    output_value: str = Field(min_length=1, max_length=512)
    target_state_id: str = Field(min_length=1, max_length=128)
    evidence_artifact_sha256s: dict[str, str] = Field(default_factory=dict)
    operator_ref: str = Field(min_length=1, max_length=256)
    authorized_role: str = Field(min_length=1, max_length=128)
    authenticated_by: str = Field(min_length=1, max_length=128)
    authentication_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_by: Literal["human"] = "human"
    decided_at: str
    signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")

    def unsigned(self) -> dict[str, Any]:
        return self.model_dump(exclude={"signature"}, mode="json")

    @model_validator(mode="after")
    def _decided_at_is_valid(self) -> "BusinessDecisionReceipt":
        _parse(self.decided_at)
        return self

    @property
    def digest(self) -> str:
        return _digest(self.unsigned())


class _ActiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class _AnswerPointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _IdempotencyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BusinessDecisionStore:
    """HMAC-authenticated request, receipt, and idempotency persistence."""

    def __init__(self, run_dir: Path | str, *, checkpoint_key: str | None = None):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / ROOT_DIRNAME
        self.requests_dir = self.root / "requests"
        self.receipts_dir = self.root / "receipts"
        self.answers_dir = self.root / "answers"
        self.evidence_dir = self.root / "evidence"
        self.idempotency_dir = self.root / "idempotency"
        self.active_path = self.root / ACTIVE_FILENAME
        self.lock_path = self.root / LOCK_FILENAME
        self.key_path = self.run_dir / KEY_FILENAME
        self.checkpoint_key = checkpoint_key

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _assert_managed_path(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.run_dir)
        except ValueError as exc:
            raise BusinessDecisionRefused(
                "the business decision artifact left the run directory"
            ) from exc
        cursor = self.run_dir
        for component in relative.parts:
            cursor = cursor / component
            if cursor.is_symlink():
                raise BusinessDecisionRefused(
                    "the business decision artifact path must not contain a symlink"
                )

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        self._assert_managed_path(path)
        if path.parent.is_symlink() or path.is_symlink():
            raise BusinessDecisionRefused(
                "the business decision artifact path must not be a symlink"
            )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_parent(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _key(self, *, create: bool) -> bytes:
        self._assert_managed_path(self.key_path)
        try:
            key = self.key_path.read_bytes()
        except FileNotFoundError:
            if not create:
                raise BusinessDecisionRefused(
                    "the business decision signing key is missing"
                ) from None
            self.run_dir.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            try:
                descriptor = os.open(
                    self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                return self._key(create=False)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_parent(self.key_path)
        if len(key) != 32 or (os.name != "nt" and self.key_path.stat().st_mode & 0o077):
            raise BusinessDecisionRefused(
                "the business decision signing key is invalid or too broadly readable"
            )
        return key

    def _signature(self, domain: bytes, payload: dict[str, Any]) -> str:
        return (
            "hmac-sha256:"
            + hmac.new(
                self._key(create=False), domain + _canonical(payload), hashlib.sha256
            ).hexdigest()
        )

    @contextmanager
    def _lock(self, timeout_s: float = 5.0) -> Iterator[None]:
        """Serialize submissions with a crash-released operating-system lock."""

        self._assert_managed_path(self.lock_path)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise BusinessDecisionRefused(
                "the business decision lock cannot be opened safely"
            ) from exc
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            elif os.fstat(descriptor).st_size == 0:
                # Windows locks a byte range. The byte has no authority or
                # sensitive content. It only provides a stable lock range.
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
        except Exception:
            os.close(descriptor)
            raise

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(  # type: ignore[attr-defined]
                        descriptor,
                        msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise BusinessDecisionRefused(
                        "another business decision submission is active"
                    ) from None
                time.sleep(0.01)
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(  # type: ignore[attr-defined]
                        descriptor,
                        msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _request_path(self, sha256: str) -> Path:
        return self.requests_dir / f"{_sha256_value(sha256)}.json"

    def _receipt_path(self, sha256: str) -> Path:
        return self.receipts_dir / f"{_sha256_value(sha256)}.json"

    def request_inventory_ref(self, sha256: str) -> str:
        return f"{ROOT_DIRNAME}/requests/{_sha256_value(sha256)}.json"

    def receipt_inventory_ref(self, sha256: str) -> str:
        return f"{ROOT_DIRNAME}/receipts/{_sha256_value(sha256)}.json"

    def _answer_path(self, request_digest: str) -> Path:
        return self.answers_dir / f"{_digest_value(request_digest)}.json"

    def _idempotency_path(self, key: str) -> Path:
        return self.idempotency_dir / f"{_sha256(key.encode('utf-8'))}.json"

    def _idempotency_sha_path(self, sha256: str) -> Path:
        return self.idempotency_dir / f"{_sha256_value(sha256)}.json"

    def _evidence_path(self, sha256: str) -> Path:
        return self.evidence_dir / f"{_sha256_value(sha256)}.bin"

    def evidence_inventory_ref(self, sha256: str) -> str:
        """Return the fixed local inventory reference for retained evidence."""

        return f"{ROOT_DIRNAME}/evidence/{_sha256_value(sha256)}.bin"

    def retain_evidence(self, payload: bytes) -> str:
        """Retain one local content-addressed evidence artifact.

        The artifact can support a human branch decision. It never becomes
        entity-identity, postcondition, or business-effect proof.
        """

        sha256 = _sha256(payload)
        path = self._evidence_path(sha256)
        if path.is_file():
            if path.is_symlink() or path.read_bytes() != payload:
                raise BusinessDecisionRefused(
                    "the retained business decision evidence differs"
                )
            return sha256
        self._atomic_write(path, payload)
        return sha256

    def _authenticate_evidence_artifacts(self, artifacts: dict[str, str]) -> None:
        for sha256 in artifacts.values():
            path = self._evidence_path(sha256)
            if path.is_symlink():
                raise BusinessDecisionRefused(
                    "the business decision evidence path must not be a symlink"
                )
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise BusinessDecisionRefused(
                    "required business decision evidence is unavailable locally"
                ) from exc
            if _sha256(payload) != sha256:
                raise BusinessDecisionRefused(
                    "required business decision evidence content hash differs"
                )

    def _authenticate_idempotency_binding(
        self,
        *,
        request: BusinessDecisionRequest,
        receipt: BusinessDecisionReceipt,
        receipt_sha256: str,
    ) -> None:
        retained = self._receipts_for_request(request.digest)
        if len(retained) != 1 or retained[0][1] != receipt_sha256:
            raise BusinessDecisionRefused(
                "the business decision does not have one exact signed receipt"
            )
        binding = _IdempotencyBinding.model_validate(
            self._read_model(
                self._idempotency_sha_path(receipt.idempotency_key_sha256),
                _IdempotencyBinding,
            )
        )
        pointer = self._read_answer_pointer(request.digest)
        if (
            binding.request_digest != request.digest
            or binding.receipt_sha256 != receipt_sha256
            or binding.submission_sha256 != pointer.submission_sha256
            or pointer.receipt_sha256 != receipt_sha256
        ):
            raise BusinessDecisionRefused(
                "the business decision idempotency binding differs"
            )

    def _read_model(self, path: Path, model: type[BaseModel]) -> BaseModel:
        self._assert_managed_path(path)
        if path.parent.is_symlink() or path.is_symlink():
            raise BusinessDecisionRefused(
                "the business decision artifact path must not be a symlink"
            )
        try:
            payload = path.read_bytes()
            return model.model_validate_json(payload)
        except (OSError, ValueError) as exc:
            raise BusinessDecisionRefused(
                "the business decision artifact is missing or invalid"
            ) from exc

    def _read_request_sha(self, sha256: str) -> BusinessDecisionRequest:
        request = BusinessDecisionRequest.model_validate(
            self._read_model(self._request_path(sha256), BusinessDecisionRequest)
        )
        if _sha256(self._request_path(sha256).read_bytes()) != sha256:
            raise BusinessDecisionRefused("the decision request content hash differs")
        expected = self._signature(REQUEST_DOMAIN, request.unsigned())
        if not hmac.compare_digest(request.signature, expected):
            raise BusinessDecisionRefused("the decision request signature differs")
        return request

    def authenticate_request(self, sha256: str) -> BusinessDecisionRequest:
        """Authenticate one content-addressed request artifact."""

        request = self._read_request_sha(sha256)
        self._authenticate_renewal(request)
        return request

    def read_active_request(self) -> tuple[BusinessDecisionRequest, str]:
        active = _ActiveRequest.model_validate(
            self._read_model(self.active_path, _ActiveRequest)
        )
        request = self.authenticate_request(active.request_sha256)
        if active.request_digest != request.digest:
            raise BusinessDecisionRefused("the active decision request binding differs")
        return request, active.request_sha256

    def _authenticate_renewal(self, request: BusinessDecisionRequest) -> None:
        """Authenticate the complete retained chain of unanswered requests."""

        current = request
        seen = {request.digest}
        for _ in range(4096):
            predecessor_sha256 = current.supersedes_request_sha256
            predecessor_digest = current.supersedes_request_digest
            if predecessor_sha256 is None or predecessor_digest is None:
                return
            predecessor = self._read_request_sha(predecessor_sha256)
            if predecessor.digest in seen:
                raise BusinessDecisionRefused(
                    "the business decision renewal chain contains a cycle"
                )
            same_pause = (
                predecessor.digest == predecessor_digest
                and predecessor.run_id == current.run_id
                and predecessor.workflow_name == current.workflow_name
                and predecessor.workflow_contract_sha256
                == current.workflow_contract_sha256
                and predecessor.bundle_version == current.bundle_version
                and predecessor.governed_runtime_inputs_digest
                == current.governed_runtime_inputs_digest
                and predecessor.pause_binding_sha256 == current.pause_binding_sha256
                and predecessor.graph_id == current.graph_id
                and predecessor.state_id == current.state_id
                and predecessor.program_scope == current.program_scope
                and predecessor.control_frames_sha256 == current.control_frames_sha256
                and predecessor.bound_params_sha256 == current.bound_params_sha256
                and predecessor.decision.contract_sha256()
                == current.decision.contract_sha256()
            )
            if not same_pause or _parse(predecessor.expires_at) > _parse(
                current.issued_at
            ):
                raise BusinessDecisionRefused(
                    "the business decision renewal predecessor differs"
                )
            if (
                self._receipts_for_request(predecessor.digest)
                or self._answer_path(predecessor.digest).is_file()
            ):
                raise BusinessDecisionRefused(
                    "an answered business decision request cannot be renewed"
                )
            seen.add(predecessor.digest)
            current = predecessor
        raise BusinessDecisionRefused("the business decision renewal chain is too long")

    def _read_receipt_sha(self, sha256: str) -> BusinessDecisionReceipt:
        path = self._receipt_path(sha256)
        receipt = BusinessDecisionReceipt.model_validate(
            self._read_model(path, BusinessDecisionReceipt)
        )
        if _sha256(path.read_bytes()) != sha256:
            raise BusinessDecisionRefused("the decision receipt content hash differs")
        expected = self._signature(RECEIPT_DOMAIN, receipt.unsigned())
        if not hmac.compare_digest(receipt.signature, expected):
            raise BusinessDecisionRefused("the decision receipt signature differs")
        return receipt

    def _receipts_for_request(
        self, request_digest: str
    ) -> list[tuple[BusinessDecisionReceipt, str]]:
        """Authenticate every signed receipt that names one request.

        This scan is the write-ahead recovery boundary. A process can die after
        it writes the signed receipt but before it writes the answer pointer.
        The next submit must recover that receipt or refuse a conflicting
        answer; it must never sign a second authority for the same request.
        """

        self._assert_managed_path(self.receipts_dir)
        if not self.receipts_dir.exists():
            return []
        if self.receipts_dir.is_symlink() or not self.receipts_dir.is_dir():
            raise BusinessDecisionRefused(
                "the business decision receipt inventory is invalid"
            )
        matches: list[tuple[BusinessDecisionReceipt, str]] = []
        for path in sorted(self.receipts_dir.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise BusinessDecisionRefused(
                    "the business decision receipt inventory is invalid"
                )
            try:
                receipt_sha256 = _sha256_value(path.stem)
            except BusinessDecisionRefused as exc:
                raise BusinessDecisionRefused(
                    "the business decision receipt inventory is invalid"
                ) from exc
            receipt = self._read_receipt_sha(receipt_sha256)
            if receipt.request_digest == request_digest:
                matches.append((receipt, receipt_sha256))
        return matches

    @staticmethod
    def _receipt_matches_submission(
        *,
        receipt: BusinessDecisionReceipt,
        request: BusinessDecisionRequest,
        option: BusinessDecisionOption,
        submission: BusinessDecisionSubmission,
        principal: BusinessDecisionPrincipal,
        authorized_role: str,
        idempotency_key_sha256: str,
    ) -> bool:
        """Whether an orphaned signed receipt is this exact answer attempt."""

        return (
            receipt.request_digest == request.digest
            and receipt.run_id == request.run_id
            and receipt.workflow_name == request.workflow_name
            and receipt.workflow_contract_sha256 == request.workflow_contract_sha256
            and receipt.bundle_version == request.bundle_version
            and receipt.governed_runtime_inputs_digest
            == request.governed_runtime_inputs_digest
            and receipt.pause_binding_sha256 == request.pause_binding_sha256
            and receipt.graph_id == request.graph_id
            and receipt.state_id == request.state_id
            and receipt.program_scope == request.program_scope
            and receipt.decision_contract_sha256 == request.decision.contract_sha256()
            and receipt.option_id == option.id
            and receipt.output_param == request.decision.output_param
            and receipt.output_value == option.value
            and receipt.target_state_id == option.target
            and receipt.evidence_artifact_sha256s
            == submission.evidence_artifact_sha256s
            and receipt.operator_ref == principal.operator_ref
            and receipt.authorized_role == authorized_role
            and receipt.authenticated_by == principal.authenticated_by
            and receipt.authentication_context_sha256
            == principal.authentication_context_sha256
            and receipt.idempotency_key_sha256 == idempotency_key_sha256
            and receipt.decided_by == "human"
        )

    def read_receipt(
        self, request_digest: str
    ) -> tuple[BusinessDecisionReceipt, str] | None:
        path = self._answer_path(request_digest)
        retained = self._receipts_for_request(request_digest)
        if not path.is_file():
            if retained:
                raise BusinessDecisionRefused(
                    "a signed business decision receipt is awaiting recovery"
                )
            return None
        if len(retained) != 1:
            raise BusinessDecisionRefused(
                "the business decision does not have one exact signed receipt"
            )
        pointer = self._read_answer_pointer(request_digest)
        receipt = self._read_receipt_sha(pointer.receipt_sha256)
        if (
            retained[0][1] != pointer.receipt_sha256
            or receipt.digest != pointer.receipt_digest
        ):
            raise BusinessDecisionRefused("the decision receipt digest differs")
        return receipt, pointer.receipt_sha256

    def _read_answer_pointer(self, request_digest: str) -> _AnswerPointer:
        path = self._answer_path(request_digest)
        pointer = _AnswerPointer.model_validate(self._read_model(path, _AnswerPointer))
        if pointer.request_digest != request_digest:
            raise BusinessDecisionRefused("the decision answer pointer differs")
        return pointer

    def _approval_for_receipt(
        self,
        *,
        pending: PendingEscalation,
        receipt: BusinessDecisionReceipt,
    ) -> ApprovalRecord:
        """Reconstruct the exact resume authority bound into one receipt."""

        return issue_resume_approval(
            pending,
            approver=receipt.operator_ref,
            resolution=f"business_decision:{receipt.option_id}",
            bundle_version=receipt.bundle_version,
            run_id=receipt.run_id,
            workflow_name=receipt.workflow_name,
            run_dir=self.run_dir,
        ).model_copy(update={"approved_at": receipt.decided_at})

    def _commit_receipt_approval(
        self,
        *,
        checkpoint_store: CheckpointStore,
        pending: PendingEscalation,
        receipt: BusinessDecisionReceipt,
    ) -> None:
        """Commit or recover the approval transition for a retained receipt."""

        approval = self._approval_for_receipt(pending=pending, receipt=receipt)
        existing = checkpoint_store.read_approval()
        if existing is not None and existing != approval:
            same_pause = existing.pause_binding_sha256 == approval.pause_binding_sha256
            if same_pause or pending.status != "pending":
                raise BusinessDecisionRefused(
                    "another approval already owns this durable pause"
                )
        checkpoint_store.commit_approval_transition(
            expected_pending=pending,
            approval=approval,
            target_status="approved",
        )

    def issue(
        self,
        *,
        pending: PendingEscalation,
        manifest: RunManifest,
        workflow: Workflow,
        graph_id: str,
        state_id: str,
        frames: list[GraphFrame],
        params: dict[str, RuntimeParamScalar],
        spec: BusinessDecisionSpec,
        governed_runtime_inputs_digest: str | None,
        now: datetime | None = None,
    ) -> tuple[BusinessDecisionRequest, str]:
        """Issue one signed request for the exact current durable pause."""

        from openadapt_flow.qualification import workflow_contract_sha256

        if (
            not pending.program
            or pending.state_id != state_id
            or not frames
            or frames[-1].graph_id != graph_id
            or frames[-1].state_id != state_id
            or frames[-1].params != params
            or manifest.run_id != pending.run_id
            or manifest.workflow_name != workflow.name
        ):
            raise BusinessDecisionRefused(
                "the business decision request does not match the durable pause"
            )
        now = now or _now()
        self._key(create=True)
        expected_contract = workflow_contract_sha256(workflow)
        with self._lock():
            supersedes_request_sha256: str | None = None
            supersedes_request_digest: str | None = None
            if self.active_path.is_file():
                existing, sha256 = self.read_active_request()
                same_pause = (
                    existing.pause_binding_sha256 == approval_pause_digest(pending)
                    and existing.graph_id == graph_id
                    and existing.state_id == state_id
                    and existing.decision.contract_sha256() == spec.contract_sha256()
                )
                retained = self.read_receipt(existing.digest)
                if same_pause:
                    if retained is not None or now < _parse(existing.expires_at):
                        return existing, sha256
                    supersedes_request_sha256 = sha256
                    supersedes_request_digest = existing.digest
                elif retained is None:
                    raise BusinessDecisionRefused(
                        "another unanswered business decision request already "
                        "owns this run"
                    )
            unsigned = BusinessDecisionRequest.model_construct(
                request_id=secrets.token_hex(16),
                run_id=manifest.run_id,
                workflow_name=workflow.name,
                workflow_contract_sha256=expected_contract,
                bundle_version=bundle_version(manifest.bundle_dir),
                governed_runtime_inputs_digest=governed_runtime_inputs_digest,
                pause_binding_sha256=approval_pause_digest(pending),
                graph_id=graph_id,
                state_id=state_id,
                program_scope=tuple(
                    ProgramExecutionScopeFrame(
                        graph_id=frame.graph_id,
                        loop_state_id=(
                            frame.loop.loop_state_id if frame.loop is not None else None
                        ),
                        relation=(
                            frame.loop.relation if frame.loop is not None else None
                        ),
                        row_index=(
                            frame.loop.row_index if frame.loop is not None else None
                        ),
                    )
                    for frame in frames
                ),
                control_frames_sha256=control_frames_hash(frames).removeprefix(
                    "sha256:"
                ),
                bound_params_sha256=bound_params_sha256(params),
                decision=spec,
                supersedes_request_sha256=supersedes_request_sha256,
                supersedes_request_digest=supersedes_request_digest,
                issued_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=spec.expires_after_s)),
                signature="hmac-sha256:" + ("0" * 64),
            )
            request = BusinessDecisionRequest.model_validate(
                unsigned.model_copy(
                    update={
                        "signature": self._signature(
                            REQUEST_DOMAIN, unsigned.unsigned()
                        )
                    }
                )
            )
            payload = request.model_dump_json(indent=2).encode("utf-8")
            request_sha256 = _sha256(payload)
            self._atomic_write(self._request_path(request_sha256), payload)
            self._atomic_write(
                self.active_path,
                _ActiveRequest(
                    request_sha256=request_sha256,
                    request_digest=request.digest,
                )
                .model_dump_json(indent=2)
                .encode("utf-8"),
            )
            return request, request_sha256

    @staticmethod
    def _option(
        request: BusinessDecisionRequest, option_id: str
    ) -> BusinessDecisionOption:
        matches = [
            option for option in request.decision.options if option.id == option_id
        ]
        if len(matches) != 1:
            raise BusinessDecisionRefused("the answer is not a declared option")
        return matches[0]

    def _validate_live_binding(
        self,
        request: BusinessDecisionRequest,
        *,
        pending: PendingEscalation,
        manifest: RunManifest,
        workflow: Workflow,
        graph_id: str,
        state_id: str,
        frames: list[GraphFrame],
        params: dict[str, RuntimeParamScalar],
        spec: BusinessDecisionSpec,
        governed_runtime_inputs_digest: str | None,
    ) -> None:
        from openadapt_flow.qualification import workflow_contract_sha256

        scope = tuple(
            ProgramExecutionScopeFrame(
                graph_id=frame.graph_id,
                loop_state_id=(
                    frame.loop.loop_state_id if frame.loop is not None else None
                ),
                relation=frame.loop.relation if frame.loop is not None else None,
                row_index=frame.loop.row_index if frame.loop is not None else None,
            )
            for frame in frames
        )
        if (
            request.run_id != manifest.run_id
            or request.workflow_name != workflow.name
            or request.workflow_contract_sha256 != workflow_contract_sha256(workflow)
            or request.bundle_version != bundle_version(manifest.bundle_dir)
            or request.governed_runtime_inputs_digest != governed_runtime_inputs_digest
            or request.pause_binding_sha256 != approval_pause_digest(pending)
            or request.graph_id != graph_id
            or request.state_id != state_id
            or request.program_scope != scope
            or request.control_frames_sha256
            != control_frames_hash(frames).removeprefix("sha256:")
            or request.bound_params_sha256 != bound_params_sha256(params)
            or request.decision.contract_sha256() != spec.contract_sha256()
        ):
            raise BusinessDecisionRefused(
                "the signed business decision no longer matches the live run"
            )

    def submit(
        self,
        submission: BusinessDecisionSubmission,
        *,
        principal: BusinessDecisionPrincipal,
        now: datetime | None = None,
    ) -> BusinessDecisionReceipt:
        """Admit one answer and create exact resume authority."""

        request, _request_sha256 = self.read_active_request()
        if submission.request_digest != request.digest:
            raise BusinessDecisionRefused("the answer names another decision request")
        now = now or _now()
        if now >= _parse(request.expires_at):
            raise BusinessDecisionRefused("the business decision request expired")
        option = self._option(request, submission.option_id)
        roles = [
            role
            for role in request.decision.authorized_roles
            if role in principal.roles
        ]
        if not roles:
            raise BusinessDecisionRefused(
                "the authenticated principal has no authorized decision role"
            )
        expected_evidence = set(option.required_evidence)
        if set(submission.evidence_artifact_sha256s) != expected_evidence:
            raise BusinessDecisionRefused(
                "the answer does not carry the exact required evidence set"
            )
        self._authenticate_evidence_artifacts(submission.evidence_artifact_sha256s)
        checkpoint_store = CheckpointStore(self.run_dir, key=self.checkpoint_key)
        pending = checkpoint_store.read_pending()
        manifest = checkpoint_store.read_manifest()
        if pending is None or manifest is None:
            raise BusinessDecisionRefused(
                "the durable run has no active business decision pause"
            )
        if (
            request.pause_binding_sha256 != approval_pause_digest(pending)
            or request.run_id != manifest.run_id
            or request.workflow_name != manifest.workflow_name
        ):
            raise BusinessDecisionRefused("the durable decision pause changed")
        submission_sha256 = _sha256(_canonical(submission))
        idempotency_sha256 = _sha256(submission.idempotency_key.encode("utf-8"))
        with self._lock():
            current_request, _current_request_sha256 = self.read_active_request()
            if current_request.digest != request.digest:
                raise BusinessDecisionRefused(
                    "the active business decision request changed before answer"
                )
            if now >= _parse(current_request.expires_at):
                raise BusinessDecisionRefused("the business decision request expired")
            binding_path = self._idempotency_path(submission.idempotency_key)
            binding: _IdempotencyBinding | None = None
            if binding_path.is_file():
                binding = _IdempotencyBinding.model_validate(
                    self._read_model(binding_path, _IdempotencyBinding)
                )
                if (
                    binding.request_digest != request.digest
                    or binding.submission_sha256 != submission_sha256
                ):
                    raise BusinessDecisionRefused(
                        "the idempotency key was used for another answer"
                    )
            retained = self._receipts_for_request(request.digest)
            if len(retained) > 1:
                raise BusinessDecisionRefused(
                    "this business decision has multiple signed answers; "
                    "manual audit is required"
                )
            if retained:
                prior_receipt, prior_receipt_sha256 = retained[0]
                if not self._receipt_matches_submission(
                    receipt=prior_receipt,
                    request=request,
                    option=option,
                    submission=submission,
                    principal=principal,
                    authorized_role=roles[0],
                    idempotency_key_sha256=idempotency_sha256,
                ):
                    raise BusinessDecisionRefused(
                        "this business decision already has a different answer"
                    )
                answer_path = self._answer_path(request.digest)
                pointer = _AnswerPointer(
                    request_digest=request.digest,
                    receipt_sha256=prior_receipt_sha256,
                    receipt_digest=prior_receipt.digest,
                    submission_sha256=submission_sha256,
                )
                if answer_path.is_file():
                    if self._read_answer_pointer(request.digest) != pointer:
                        raise BusinessDecisionRefused(
                            "the decision answer pointer differs from the "
                            "signed receipt"
                        )
                else:
                    self._atomic_write(
                        answer_path,
                        pointer.model_dump_json(indent=2).encode("utf-8"),
                    )
                recovered_binding = _IdempotencyBinding(
                    request_digest=request.digest,
                    submission_sha256=submission_sha256,
                    receipt_sha256=prior_receipt_sha256,
                )
                if binding is not None and binding != recovered_binding:
                    raise BusinessDecisionRefused(
                        "the decision idempotency binding differs from the "
                        "signed receipt"
                    )
                self._atomic_write(
                    binding_path,
                    recovered_binding.model_dump_json(indent=2).encode("utf-8"),
                )
                self._commit_receipt_approval(
                    checkpoint_store=checkpoint_store,
                    pending=pending,
                    receipt=prior_receipt,
                )
                return prior_receipt
            if binding is not None or self._answer_path(request.digest).is_file():
                raise BusinessDecisionRefused(
                    "the business decision answer metadata has no signed receipt"
                )
            unsigned = BusinessDecisionReceipt.model_construct(
                request_digest=request.digest,
                run_id=request.run_id,
                workflow_name=request.workflow_name,
                workflow_contract_sha256=request.workflow_contract_sha256,
                bundle_version=request.bundle_version,
                governed_runtime_inputs_digest=(request.governed_runtime_inputs_digest),
                pause_binding_sha256=request.pause_binding_sha256,
                graph_id=request.graph_id,
                state_id=request.state_id,
                program_scope=request.program_scope,
                decision_contract_sha256=request.decision.contract_sha256(),
                option_id=option.id,
                output_param=request.decision.output_param,
                output_value=option.value,
                target_state_id=option.target,
                evidence_artifact_sha256s=(submission.evidence_artifact_sha256s),
                operator_ref=principal.operator_ref,
                authorized_role=roles[0],
                authenticated_by=principal.authenticated_by,
                authentication_context_sha256=(principal.authentication_context_sha256),
                idempotency_key_sha256=idempotency_sha256,
                decided_by="human",
                decided_at=_iso(now),
                signature="hmac-sha256:" + ("0" * 64),
            )
            receipt = BusinessDecisionReceipt.model_validate(
                unsigned.model_copy(
                    update={
                        "signature": self._signature(
                            RECEIPT_DOMAIN, unsigned.unsigned()
                        )
                    }
                )
            )
            payload = receipt.model_dump_json(indent=2).encode("utf-8")
            receipt_sha256 = _sha256(payload)
            approval = self._approval_for_receipt(pending=pending, receipt=receipt)
            existing_approval = checkpoint_store.read_approval()
            if existing_approval is not None and existing_approval != approval:
                same_pause = (
                    existing_approval.pause_binding_sha256
                    == approval.pause_binding_sha256
                )
                if same_pause or pending.status != "pending":
                    raise BusinessDecisionRefused(
                        "another approval already owns this durable pause"
                    )
            self._atomic_write(self._receipt_path(receipt_sha256), payload)
            pointer = _AnswerPointer(
                request_digest=request.digest,
                receipt_sha256=receipt_sha256,
                receipt_digest=receipt.digest,
                submission_sha256=submission_sha256,
            )
            self._atomic_write(
                self._answer_path(request.digest),
                pointer.model_dump_json(indent=2).encode("utf-8"),
            )
            self._atomic_write(
                binding_path,
                _IdempotencyBinding(
                    request_digest=request.digest,
                    submission_sha256=submission_sha256,
                    receipt_sha256=receipt_sha256,
                )
                .model_dump_json(indent=2)
                .encode("utf-8"),
            )
            self._commit_receipt_approval(
                checkpoint_store=checkpoint_store,
                pending=pending,
                receipt=receipt,
            )
            return receipt

    def consume(
        self,
        *,
        pending: PendingEscalation,
        manifest: RunManifest,
        workflow: Workflow,
        graph_id: str,
        state_id: str,
        frames: list[GraphFrame],
        params: dict[str, RuntimeParamScalar],
        spec: BusinessDecisionSpec,
        governed_runtime_inputs_digest: str | None,
        now: datetime | None = None,
    ) -> tuple[BusinessDecisionRequest, str, BusinessDecisionReceipt, str] | None:
        """Authenticate the answer for the exact resumed interpreter cursor."""

        request, request_sha256 = self.read_active_request()
        self._validate_live_binding(
            request,
            pending=pending,
            manifest=manifest,
            workflow=workflow,
            graph_id=graph_id,
            state_id=state_id,
            frames=frames,
            params=params,
            spec=spec,
            governed_runtime_inputs_digest=governed_runtime_inputs_digest,
        )
        retained = self.read_receipt(request.digest)
        if retained is None:
            return None
        receipt, receipt_sha256 = retained
        current_time = now or _now()
        decided_at = _parse(receipt.decided_at)
        if (
            current_time >= _parse(request.expires_at)
            or decided_at < _parse(request.issued_at)
            or decided_at >= _parse(request.expires_at)
        ):
            raise BusinessDecisionRefused(
                "the signed business decision expired before resume"
            )
        option = self._option(request, receipt.option_id)
        if (
            receipt.request_digest != request.digest
            or receipt.run_id != request.run_id
            or receipt.workflow_name != request.workflow_name
            or receipt.workflow_contract_sha256 != request.workflow_contract_sha256
            or receipt.bundle_version != request.bundle_version
            or receipt.governed_runtime_inputs_digest
            != request.governed_runtime_inputs_digest
            or receipt.pause_binding_sha256 != request.pause_binding_sha256
            or receipt.graph_id != graph_id
            or receipt.state_id != state_id
            or receipt.program_scope != request.program_scope
            or receipt.decision_contract_sha256 != spec.contract_sha256()
            or receipt.output_param != spec.output_param
            or receipt.output_value != option.value
            or receipt.target_state_id != option.target
            or receipt.authorized_role not in spec.authorized_roles
            or receipt.decided_by != "human"
            or set(receipt.evidence_artifact_sha256s) != set(option.required_evidence)
        ):
            raise BusinessDecisionRefused(
                "the signed business decision receipt binding differs"
            )
        self._authenticate_evidence_artifacts(receipt.evidence_artifact_sha256s)
        self._authenticate_idempotency_binding(
            request=request,
            receipt=receipt,
            receipt_sha256=receipt_sha256,
        )
        return request, request_sha256, receipt, receipt_sha256

    def authenticate_evidence(
        self,
        evidence: BusinessDecisionEvidence,
        *,
        workflow: Workflow,
        run_id: str,
        expected_bundle_version: str,
        governed_runtime_inputs_digest: str | None,
    ) -> BusinessDecisionReceipt:
        """Verify a retained decision delta before it changes resumed params."""

        from openadapt_flow.qualification import workflow_contract_sha256

        request = self.authenticate_request(evidence.request_sha256)
        receipt = self._read_receipt_sha(evidence.receipt_sha256)
        if (
            request.run_id != run_id
            or request.workflow_name != workflow.name
            or request.workflow_contract_sha256 != workflow_contract_sha256(workflow)
            or request.bundle_version != expected_bundle_version
            or request.governed_runtime_inputs_digest != governed_runtime_inputs_digest
            or request.digest != evidence.request_digest
            or receipt.digest != evidence.receipt_digest
            or receipt.request_digest != request.digest
            or request.graph_id != evidence.graph_id
            or request.state_id != evidence.state_id
            or list(request.program_scope) != evidence.program_scope
            or request.decision.contract_sha256() != evidence.decision_contract_sha256
            or receipt.option_id != evidence.option_id
            or receipt.output_param != evidence.output_param
            or receipt.output_value != evidence.output_value
            or receipt.target_state_id != evidence.target_state_id
            or receipt.operator_ref != evidence.operator_ref
            or receipt.authorized_role != evidence.authorized_role
            or receipt.authentication_context_sha256
            != evidence.authentication_context_sha256
            or receipt.evidence_artifact_sha256s != evidence.evidence_artifact_sha256s
            or receipt.idempotency_key_sha256 != evidence.idempotency_key_sha256
            or receipt.decided_at != evidence.decided_at
            or receipt.governed_runtime_inputs_digest
            != evidence.governed_runtime_inputs_digest
        ):
            raise BusinessDecisionRefused(
                "the retained business decision evidence binding differs"
            )
        self._authenticate_evidence_artifacts(receipt.evidence_artifact_sha256s)
        self._authenticate_idempotency_binding(
            request=request,
            receipt=receipt,
            receipt_sha256=evidence.receipt_sha256,
        )
        return receipt


def submit_business_decision(
    run_dir: Path | str,
    submission: BusinessDecisionSubmission,
    *,
    principal: BusinessDecisionPrincipal,
    checkpoint_key: str | None = None,
) -> BusinessDecisionReceipt:
    """Public library entry point for one authenticated finite answer."""

    return BusinessDecisionStore(run_dir, checkpoint_key=checkpoint_key).submit(
        submission, principal=principal
    )


__all__ = [
    "BusinessDecisionPrincipal",
    "BusinessDecisionReceipt",
    "BusinessDecisionRefused",
    "BusinessDecisionRequest",
    "BusinessDecisionStore",
    "BusinessDecisionSubmission",
    "submit_business_decision",
]
