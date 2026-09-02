"""Durable local Execute store: idempotency, dispatch, self-signed receipts."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, cast
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.execute import (
    ExecuteAcceptedV1,
    ExecuteEvidenceContractV1,
    ExecuteEvidenceReceiptV1,
    ExecuteLifecycleStateV1,
    ExecuteRequestV1,
    ExecuteStatusV1,
    ExecuteTerminalOutcomeV1,
)
from openadapt_types.oracle import oracle_tier_from_effect_strength
from pydantic import ValidationError

from openadapt_flow.execute.dispatch import (
    DispatchResult,
    Runner,
    default_runner,
)
from openadapt_flow.execute.keys import (
    fingerprint_of,
    load_or_create_private_key,
    load_or_create_token,
    sign_bytes,
)
from openadapt_flow.execute.models import (
    SelfSignedSealV1,
    assert_no_forbidden_keys,
)
from openadapt_flow.execute.registry import (
    AdmissionError,
    lookup_admission,
    seed_mockmed_admissions,
)


class ExecuteServiceError(Exception):
    """Typed failure with an HTTP status for the reference server."""

    def __init__(self, status_code: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error = error
        self.detail = detail

    def body(self) -> dict[str, str]:
        return {"error": self.error, "detail": self.detail}


class ExecuteService:
    """One-operator Execute store on the local filesystem."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        token: str | None = None,
        runner: Runner | None = None,
        process_inline: bool = True,
        seed_mockmed: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._key: Ed25519PrivateKey = load_or_create_private_key(self.data_dir)
        self.token = load_or_create_token(self.data_dir, token)
        self.fingerprint = fingerprint_of(self._key.public_key())
        self.runner = runner or default_runner
        self.process_inline = process_inline
        if seed_mockmed:
            seed_mockmed_admissions(self.data_dir)
        (self.data_dir / "executions").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "idempotency").mkdir(parents=True, exist_ok=True)

    def create_execution(self, payload: dict[str, Any]) -> ExecuteAcceptedV1:
        try:
            request = ExecuteRequestV1.model_validate(payload)
        except ValidationError as exc:
            raise ExecuteServiceError(422, "invalid_request", str(exc)) from exc
        canonical = _canonical_request(request)
        digest = _sha256_hex(canonical)
        with self._lock:
            existing = self._read_idempotency(request.idempotency_key)
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise ExecuteServiceError(
                        409,
                        "idempotency_conflict",
                        "idempotency key already bound to a different request",
                    )
                return ExecuteAcceptedV1(execution_id=existing["execution_id"])
            try:
                lookup_admission(
                    self.data_dir,
                    qualification_id=request.qualification_id,
                    workflow_version=request.workflow_version,
                    workflow_digest=request.workflow_digest,
                    environment_id=request.environment_id,
                    minimum_effect_strength=request.minimum_effect_strength.value,
                )
            except AdmissionError as exc:
                raise ExecuteServiceError(
                    422, "qualification_mismatch", str(exc)
                ) from exc
            execution_id = _new_id("execution")
            now = _now()
            self._write_json(
                self._execution_path(execution_id) / "request.json",
                json.loads(canonical),
            )
            status = ExecuteStatusV1(
                execution_id=execution_id,
                state=ExecuteLifecycleStateV1.QUEUED,
                updated_at=now,
            )
            self._write_status(status)
            self._write_json(
                self.data_dir / "idempotency" / f"{request.idempotency_key}.json",
                {"execution_id": execution_id, "request_digest": digest},
            )
        self._start(execution_id, request)
        return ExecuteAcceptedV1(execution_id=execution_id)

    def get_status(self, execution_id: str) -> ExecuteStatusV1:
        path = self._execution_path(execution_id) / "status.json"
        if not path.is_file():
            raise ExecuteServiceError(404, "not_found", "no such execution")
        return ExecuteStatusV1.model_validate(json.loads(path.read_text("utf-8")))

    def get_receipt(self, execution_id: str) -> ExecuteEvidenceReceiptV1:
        status = self.get_status(execution_id)
        if status.state is not ExecuteLifecycleStateV1.TERMINAL:
            raise ExecuteServiceError(
                409,
                "receipt_not_ready",
                "execution is not terminal",
            )
        path = self._execution_path(execution_id) / "receipt.json"
        if not path.is_file():
            raise ExecuteServiceError(
                409,
                "receipt_not_ready",
                "terminal run still waits for trusted evidence",
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert_no_forbidden_keys(payload)
        return ExecuteEvidenceReceiptV1.model_validate(payload)

    def get_seal(self, seal_id: str) -> SelfSignedSealV1:
        execution_id = self._execution_id_for_seal(seal_id)
        path = self._execution_path(execution_id) / "seal.json"
        if not path.is_file():
            raise ExecuteServiceError(404, "not_found", "no such seal")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert_no_forbidden_keys(payload.get("receipt") or {})
        return SelfSignedSealV1.model_validate(payload)

    def _start(self, execution_id: str, request: ExecuteRequestV1) -> None:
        if self.process_inline:
            self._run(execution_id, request)
            return
        thread = threading.Thread(
            target=self._run,
            args=(execution_id, request),
            name=f"execute-ref-{execution_id}",
            daemon=True,
        )
        thread.start()

    def _run(self, execution_id: str, request: ExecuteRequestV1) -> None:
        try:
            self._write_status(
                ExecuteStatusV1(
                    execution_id=execution_id,
                    state=ExecuteLifecycleStateV1.RUNNING,
                    updated_at=_now(),
                )
            )
            admission = lookup_admission(
                self.data_dir,
                qualification_id=request.qualification_id,
                workflow_version=request.workflow_version,
                workflow_digest=request.workflow_digest,
                environment_id=request.environment_id,
                minimum_effect_strength=request.minimum_effect_strength.value,
            )
            run_dir = self._execution_path(execution_id) / "run"
            result = self.runner(admission, request, run_dir)
            self._finalize(execution_id, request, result)
        except Exception as exc:
            failed = _failed_platform_result(request, str(exc))
            try:
                self._finalize(execution_id, request, failed)
            except Exception:
                self._write_status(
                    ExecuteStatusV1(
                        execution_id=execution_id,
                        state=ExecuteLifecycleStateV1.RUNNING,
                        updated_at=_now(),
                    )
                )

    def _finalize(
        self,
        execution_id: str,
        request: ExecuteRequestV1,
        result: DispatchResult,
    ) -> None:
        receipt_id = _new_id("receipt")
        issued_at = _now()
        contracts = ExecuteEvidenceContractV1(
            authorization_passed=result.authorization_passed,
            identity_passed=result.identity_passed,
            postcondition_passed=result.postcondition_passed,
            effect_passed=result.effect_passed,
            minimum_effect_strength=result.minimum_effect_strength,
            observed_effect_strength=result.observed_effect_strength,
            model_used=result.model_used,
            external_network_used=result.external_network_used,
        )
        receipt = ExecuteEvidenceReceiptV1(
            receipt_id=receipt_id,
            execution_id=execution_id,
            workflow_digest=result.workflow_digest,
            workflow_version=request.workflow_version,
            qualification_id=request.qualification_id,
            environment_id=request.environment_id,
            runner_id=_new_id("runner"),
            nonce=_new_id("nonce"),
            oracle_tier=_oracle_tier(result.observed_effect_strength),
            outcome=result.outcome,
            contracts=contracts,
            delivery_uncertain=result.delivery_uncertain,
            compensation_effect_verified=result.compensation_effect_verified,
            evidence_digest=result.evidence_digest,
            issued_at=issued_at,
        )
        payload = receipt.model_dump(mode="json")
        assert_no_forbidden_keys(payload)
        canonical = _canonical_json(payload)
        signature = sign_bytes(self._key, canonical)
        seal = SelfSignedSealV1(
            issuer_key_fingerprint=self.fingerprint,
            signature=signature,
            production_seal=False,
            meter_usd=0.0,
            receipt=payload,
        )
        directory = self._execution_path(execution_id)
        self._write_json(directory / "receipt.json", payload)
        self._write_json(directory / "seal.json", seal.model_dump(mode="json"))
        self._write_json(
            directory / "seal-index.json",
            {"receipt_id": receipt_id, "execution_id": execution_id},
        )
        self._write_status(
            ExecuteStatusV1(
                execution_id=execution_id,
                state=ExecuteLifecycleStateV1.TERMINAL,
                terminal_outcome=result.outcome,
                evidence_receipt_id=receipt_id,
                updated_at=issued_at,
            )
        )

    def _execution_id_for_seal(self, seal_id: str) -> str:
        direct = self.data_dir / "executions" / seal_id / "seal.json"
        if direct.is_file():
            return seal_id
        for status_path in (self.data_dir / "executions").glob("*/status.json"):
            try:
                status = ExecuteStatusV1.model_validate(
                    json.loads(status_path.read_text(encoding="utf-8"))
                )
            except (OSError, ValidationError, json.JSONDecodeError):
                continue
            if status.evidence_receipt_id == seal_id:
                return status.execution_id
        raise ExecuteServiceError(404, "not_found", "no such seal")

    def _execution_path(self, execution_id: str) -> Path:
        return self.data_dir / "executions" / execution_id

    def _read_idempotency(self, key: str) -> Optional[dict[str, str]]:
        path = self.data_dir / "idempotency" / f"{key}.json"
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return {
            "execution_id": str(payload["execution_id"]),
            "request_digest": str(payload["request_digest"]),
        }

    def _write_status(self, status: ExecuteStatusV1) -> None:
        self._write_json(
            self._execution_path(status.execution_id) / "status.json",
            status.model_dump(mode="json"),
        )

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)


def _oracle_tier(observed: object | None) -> Literal[0, 1, 2, 3]:
    return cast(
        Literal[0, 1, 2, 3],
        int(oracle_tier_from_effect_strength(observed)),
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_request(request: ExecuteRequestV1) -> bytes:
    return _canonical_json(request.model_dump(mode="json"))


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _failed_platform_result(request: ExecuteRequestV1, tag: str) -> DispatchResult:
    from openadapt_flow.execute.dispatch import _result

    return _result(
        outcome=ExecuteTerminalOutcomeV1.FAILED_PLATFORM,
        authorization_passed=False,
        identity_passed=False,
        postcondition_passed=False,
        effect_passed=False,
        minimum_effect_strength=request.minimum_effect_strength,
        observed_effect_strength=None,
        workflow_digest=request.workflow_digest,
        evidence_tag=f"platform-fault:{tag[:64]}",
    )


def default_data_dir() -> Path:
    return Path.home() / ".openadapt" / "execute-ref"
