"""A held permit must outlive every local wait before the real input edge.

The actual coordinator owns an external authority record, a local lease and a
protected pending v3 permit. Planted clock advances model a blocked state lock,
a slow pending read and a slow final pause-digest check. The shared Replayer
input boundary must refuse expiry before it can call the counting backend.
"""

from __future__ import annotations

import json
from base64 import b64encode
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import openadapt_flow.runtime.durable.authority as authority_module
import openadapt_flow.runtime.durable.continuation as continuation_module
from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import StepResult
from openadapt_flow.runtime.durable.authority import (
    AUTHORITY_DB_ENV,
    REMOTE_AUTHORITY_TOKEN_ENV,
    REMOTE_AUTHORITY_URL_ENV,
    REMOTE_DISPATCH_SESSION_ID_ENV,
    DurableAuthority,
)
from openadapt_flow.runtime.durable.continuation import (
    ContinuationCoordinator,
    ContinuationGuard,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.terminal_verification_v2 import (
    ProductionDeliveryReceiptPayload,
    sign_production_delivery_receipt,
)
from tests.test_delivery_permit_receiver_freshness import (
    BASE,
    SESSION_ID,
    _CountingBackend,
    _response,
    _utc,
)
from tests.test_durable_authority_v13 import _fresh_run, _pause


@pytest.mark.parametrize("delay_at", ["state_lock", "pending_read", "pause_digest"])
@pytest.mark.parametrize("bound", ["signed_deadline", "local_lease"])
@pytest.mark.parametrize("elapsed_seconds", [9, 10])
def test_continuation_checks_final_receiver_clock_after_all_local_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delay_at: str,
    bound: str,
    elapsed_seconds: int,
) -> None:
    clock = [BASE]
    monkeypatch.setattr(authority_module, "_permit_receiver_now", lambda: clock[0])
    monkeypatch.setattr(continuation_module, "_now", lambda: clock[0])
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(tmp_path / "authority" / "db.sqlite"))
    monkeypatch.setenv(
        REMOTE_AUTHORITY_URL_ENV,
        "https://issuer.example/api/internal/managed-delivery-permit",
    )
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "synthetic-clock-token")
    monkeypatch.setenv(REMOTE_DISPATCH_SESSION_ID_ENV, SESSION_ID)
    run_dir, store, manifest, authority = _fresh_run(
        tmp_path, execution_profile="standard"
    )
    _pause(store, manifest, authority)
    coordinator = ContinuationCoordinator(run_dir)
    deadline = BASE + timedelta(seconds=10 if bound == "signed_deadline" else 60)
    state: dict[str, Any] = {"permits": 0, "receipts": 0}

    def transport(url: str, _headers: dict[str, str], body: bytes) -> bytes:
        request = json.loads(body)
        if url.endswith("managed-delivery-permit"):
            response, retained = _response(
                request,
                checked=BASE,
                issued=BASE,
                expires=BASE + timedelta(seconds=120),
                deadline=deadline,
            )
            state.update(permit=retained.permit_artifact)
            state["permits"] += 1
            return json.dumps(response).encode()
        assert url.endswith("managed-delivery-acknowledgment")
        permit = state["permit"]
        receipt = sign_production_delivery_receipt(
            ProductionDeliveryReceiptPayload(
                execution_authority_id=permit.payload.execution_authority_id,
                permit_id=permit.payload.permit_id,
                permit_artifact_sha256=permit.artifact_sha256(),
                authenticated_runner_id_sha256="8" * 64,
                authenticated_session_id_sha256="9" * 64,
                one_use_claim_id=request["one_use_claim_id"],
                runtime_delivery_sequence=request["runtime_delivery_sequence"],
                delivered_at=_utc(clock[0]),
            ),
            Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
        )
        state["receipts"] += 1
        return json.dumps(
            {
                "schema_version": "openadapt.production-delivery-acknowledgment-result/v3",
                "status": "acknowledged",
                "receipt_artifact_bytes_base64": b64encode(
                    receipt.canonical_bytes()
                ).decode(),
                "receipt_artifact_sha256": receipt.artifact_sha256(),
                "runtime_delivery_sequence": request["runtime_delivery_sequence"],
                "delivered_at": _utc(clock[0]),
            }
        ).encode()

    coordinator.authority = DurableAuthority(
        run_dir, coordinator.store, remote_transport=transport
    )
    ttl = 10 if bound == "local_lease" else 90
    backend = _CountingBackend()
    with coordinator.lease(operation="continue", ttl_s=ttl, now=BASE) as token:
        coordinator.authority.bind_approval(
            manifest,
            attempt_id=token.attempt_id,
            owner_nonce_sha256=coordinator._nonce_digest(token.owner_nonce),
            approval_digest="sha256:" + "b" * 64,
        )
        guard = ContinuationGuard(coordinator, token)
        guard.before_delivery()
        assert guard._pending_remote_permit is not None
        result = StepResult(
            step_id="write-case",
            intent="write the case",
            ok=False,
            delivery_attempted=False,
        )
        replayer = object.__new__(Replayer)
        replayer.managed_dispatch_binding = None
        replayer._active_delivery_acknowledgers = (guard,)

        def advance() -> None:
            clock[0] = BASE + timedelta(seconds=elapsed_seconds)

        with monkeypatch.context() as planted:
            if delay_at == "state_lock":
                original_lock = coordinator.store.state_lock

                @contextmanager
                def delayed_lock() -> Iterator[None]:
                    with original_lock():
                        advance()
                        yield

                planted.setattr(coordinator.store, "state_lock", delayed_lock)
            elif delay_at == "pending_read":
                original_read = coordinator.store.read_pending

                def delayed_read() -> Any:
                    value = original_read()
                    advance()
                    return value

                planted.setattr(coordinator.store, "read_pending", delayed_read)
            else:
                original_digest = continuation_module.approval_pause_digest

                def delayed_digest(pending: Any) -> str:
                    value = original_digest(pending)
                    advance()
                    return value

                planted.setattr(
                    continuation_module, "approval_pause_digest", delayed_digest
                )

            if elapsed_seconds == 10:
                with pytest.raises(
                    StructuralResolutionRefused, match="no longer fresh"
                ):
                    replayer._deliver_backend_call(result, backend.press)
                assert backend.inputs == state["receipts"] == 0
                assert result.delivery_attempted is False
                assert guard._pending_remote_permit is not None
                assert coordinator.authority.validate(manifest).delivery_sequence == 0
            else:
                replayer._deliver_backend_call(result, backend.press)
                assert backend.inputs == state["receipts"] == 1
                assert result.delivery_attempted is True
                assert guard._pending_remote_permit is None
                assert coordinator.authority.validate(manifest).delivery_sequence == 1
        assert state["permits"] == 1


@pytest.mark.parametrize("elapsed_seconds", [9, 10])
def test_customer_local_lease_is_still_fresh_after_final_pause_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, elapsed_seconds: int
) -> None:
    """The same final clock model covers local continuations without a permit."""
    clock = [BASE]
    monkeypatch.setattr(authority_module, "_permit_receiver_now", lambda: clock[0])
    monkeypatch.setattr(continuation_module, "_now", lambda: clock[0])
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(tmp_path / "authority" / "db.sqlite"))
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    assert manifest.delivery_authority_kind == "customer_local"
    _pause(store, manifest, authority)
    coordinator = ContinuationCoordinator(run_dir)
    backend = _CountingBackend()
    with coordinator.lease(operation="continue", ttl_s=10, now=BASE) as token:
        guard = ContinuationGuard(coordinator, token)
        guard.before_delivery()
        assert guard._pending_remote_permit is None
        result = StepResult(
            step_id="write-case",
            intent="write the case",
            ok=False,
            delivery_attempted=False,
        )
        replayer = object.__new__(Replayer)
        replayer.managed_dispatch_binding = None
        replayer._active_delivery_acknowledgers = (guard,)
        original_digest = continuation_module.approval_pause_digest

        def delayed_digest(pending: Any) -> str:
            value = original_digest(pending)
            clock[0] = BASE + timedelta(seconds=elapsed_seconds)
            return value

        with monkeypatch.context() as planted:
            planted.setattr(
                continuation_module, "approval_pause_digest", delayed_digest
            )
            if elapsed_seconds == 10:
                with pytest.raises(
                    StructuralResolutionRefused, match="no longer fresh"
                ):
                    replayer._deliver_backend_call(result, backend.press)
                assert backend.inputs == 0
                assert result.delivery_attempted is False
            else:
                replayer._deliver_backend_call(result, backend.press)
                assert backend.inputs == 1
                assert result.delivery_attempted is True
