"""Signed authority must still be fresh when the receiver may send input.

The network cases use Flow's real urllib HTTPS reader against a local TLS
issuer with a test CA. The issuer signs a valid permit before withholding its
response until the registry has expired or its check has become stale. No GUI
or production service is involved; a counting backend records the only input
that the caller can attempt after permission returns.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
from base64 import b64encode
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import HTTPSHandler, ProxyHandler

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import openadapt_flow.runtime.durable.authority as authority_module
from openadapt_flow.backends.win_agent import generate_self_signed_cert
from openadapt_flow.runtime.durable.authority import (
    REMOTE_AUTHORITY_TOKEN_ENV,
    REMOTE_AUTHORITY_URL_ENV,
    DurableAuthority,
    DurableAuthorityBusy,
    _RemoteDeliveryPermit,
)
from openadapt_flow.terminal_verification_v2 import (
    ProductionDeliveryPermitV3Payload,
    sign_production_delivery_permit_v3,
)

RUN_ID = "10300000-0000-4000-8000-000000000001"
AUTHORITY_ID = "10300000-0000-4000-8000-000000000002"
PERMIT_ID = "10300000-0000-4000-8000-000000000003"
SESSION_ID = "10300000-0000-4000-8000-000000000004"
CLAIM_ID = "10300000-0000-4000-8000-000000000005"
BOUND = "sha256:" + "b" * 64
BASE = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _request() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "namespace_id": "a" * 32,
        "path_key": "c" * 64,
        "execution_profile": "standard",
        "pause_binding_sha256": BOUND,
        "progress_digest": "sha256:" + "d" * 64,
        "approval_digest": BOUND,
        "attempt_id": "a" * 32,
        "operation": "initial",
        "remote_delivery_sequence": 0,
        "permit_cursor": None,
        "delivery_sequence": 0,
    }


def _response(
    request: dict[str, Any],
    *,
    checked: datetime,
    issued: datetime,
    expires: datetime,
    deadline: datetime | None = None,
) -> tuple[dict[str, Any], _RemoteDeliveryPermit]:
    artifact = sign_production_delivery_permit_v3(
        ProductionDeliveryPermitV3Payload(
            execution_authority_id=AUTHORITY_ID,
            execution_authority_sha256="1" * 64,
            permit_id=PERMIT_ID,
            run_id=request["run_id"],
            flow_run_id_sha256=hashlib.sha256(request["run_id"].encode()).hexdigest(),
            run_request_sha256="2" * 64,
            action_request_sha256=DurableAuthority._remote_request_digest(request),
            admission_artifact_sha256="3" * 64,
            evidence_identity_sha256="4" * 64,
            environment_digest="5" * 64,
            qualification_signer_registry_sha256="6" * 64,
            qualification_signer_registry_revision=1,
            qualification_signer_registry_checked_at=_utc(checked),
            qualification_signer_registry_expires_at=_utc(expires),
            input_edge_sequence=1,
            authority_sequence=0,
            issued_at=_utc(issued),
            delivery_not_after=_utc(
                deadline or min(expires, checked + timedelta(seconds=60))
            ),
        ),
        Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
    )
    raw = artifact.canonical_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    response = {
        "schema_version": "openadapt.production-delivery-permit-issue/v3",
        "status": "issued",
        "execution_authority_id": AUTHORITY_ID,
        "permit_id": PERMIT_ID,
        "dispatch_session_id": SESSION_ID,
        "one_use_claim_id": CLAIM_ID,
        "permit_artifact_bytes_base64": b64encode(raw).decode(),
        "permit_artifact_sha256": digest,
        "next_permit_cursor": "7" * 64,
        "input_edge_sequence": 1,
        "authority_sequence": 0,
        "runtime_delivery_sequence": 0,
    }
    retained = _RemoteDeliveryPermit(
        authority_id=AUTHORITY_ID,
        authority_origin="https://issuer.example",
        next_sequence=1,
        cursor_secret="7" * 64,
        permit_digest="sha256:" + digest,
        authority_signer_sha256=artifact.signer.signer_sha256(),
        permit_id=PERMIT_ID,
        dispatch_session_id=SESSION_ID,
        one_use_claim_id=CLAIM_ID,
        input_edge_sequence=1,
        authority_sequence=0,
        runtime_delivery_sequence=0,
        permit_artifact_bytes=raw,
        permit_artifact=artifact,
    )
    return response, retained


class _CountingBackend:
    def __init__(self) -> None:
        self.inputs = 0

    def press(self) -> None:
        self.inputs += 1


def _input_after_permission(
    authority: DurableAuthority, backend: _CountingBackend
) -> None:
    manifest = SimpleNamespace(
        governed_authorization=SimpleNamespace(execution_profile="standard"),
        delivery_authority_kind="cloud_runner",
        remote_delivery_run_id=RUN_ID,
        namespace_id="a" * 32,
        managed_dispatch_binding_sha256=BOUND,
    )
    record = SimpleNamespace(
        path_key="c" * 64,
        progress_digest="sha256:" + "d" * 64,
        operation="initial",
        delivery_sequence=0,
    )
    permit = authority._require_remote_delivery_permit(
        manifest,
        record,
        remote_authority_id=None,
        remote_delivery_sequence=0,
        permit_cursor=None,
    )
    assert permit is not None
    authority.require_remote_delivery_fresh(permit)
    backend.press()


@pytest.mark.parametrize(
    "fault", ["none", "expired_in_transit", "stale_in_transit", "deadline_in_transit"]
)
def test_real_https_response_must_be_fresh_before_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    cert = generate_self_signed_cert(dirpath=str(tmp_path / "tls"))
    observed: dict[str, Any] = {}

    class Issuer(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            pass

        def do_POST(self) -> None:
            assert self.headers["Authorization"] == "Bearer synthetic-test-run-token"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request == _request()
            issued = datetime.now(timezone.utc).replace(microsecond=0)
            checked = issued
            expires = issued + timedelta(seconds=120)
            release_at = issued
            deadline = expires
            if fault == "deadline_in_transit":
                deadline = issued + timedelta(seconds=2)
                release_at = deadline + timedelta(milliseconds=10)
            if fault == "expired_in_transit":
                expires = issued + timedelta(seconds=2)
                release_at = expires + timedelta(milliseconds=10)
            elif fault == "stale_in_transit":
                checked = issued - timedelta(seconds=59)
                release_at = checked + timedelta(seconds=60, milliseconds=10)
            deadline = min(deadline, expires, checked + timedelta(seconds=60))
            response, _permit = _response(
                request,
                checked=checked,
                issued=issued,
                expires=expires,
                deadline=deadline,
            )
            observed.update(
                checked=checked, expires=expires, deadline=deadline, signed_at=issued
            )
            while datetime.now(timezone.utc) < release_at:
                time.sleep(0.01)
            observed["sent_at"] = datetime.now(timezone.utc)
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Issuer)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert.certfile, cert.keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    real_opener = authority_module.build_opener
    trusted_test_ca = ssl.create_default_context(cafile=cert.certfile)
    monkeypatch.setattr(
        authority_module,
        "build_opener",
        lambda *handlers: real_opener(
            *handlers, HTTPSHandler(context=trusted_test_ca), ProxyHandler({})
        ),
    )
    monkeypatch.setenv(
        REMOTE_AUTHORITY_URL_ENV,
        f"https://127.0.0.1:{server.server_port}/api/internal/managed-delivery-permit",
    )
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "synthetic-test-run-token")
    authority = object.__new__(DurableAuthority)
    authority._remote_transport = (
        None  # the production HTTPS reader, not a fake transport
    )
    backend = _CountingBackend()
    try:
        if fault == "none":
            _input_after_permission(authority, backend)
            assert backend.inputs == 1
            assert observed["sent_at"] < observed["expires"]
        else:
            with pytest.raises(DurableAuthorityBusy, match="expired|stale"):
                _input_after_permission(authority, backend)
            assert backend.inputs == 0
            if fault == "expired_in_transit":
                assert observed["sent_at"] >= observed["expires"]
            elif fault == "deadline_in_transit":
                assert observed["deadline"] <= observed["sent_at"] < observed["expires"]
            else:
                assert observed["sent_at"] - observed["checked"] > timedelta(seconds=60)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("checked", "issued", "expires", "received", "allowed"),
    [
        (BASE, BASE, BASE + timedelta(seconds=120), BASE, True),
        (
            BASE,
            BASE,
            BASE + timedelta(seconds=120),
            BASE + timedelta(seconds=60) - timedelta(microseconds=1),
            True,
        ),
        (
            BASE,
            BASE,
            BASE + timedelta(seconds=120),
            BASE + timedelta(seconds=60),
            False,
        ),
        (
            BASE,
            BASE,
            BASE + timedelta(seconds=120),
            BASE + timedelta(seconds=60, microseconds=1),
            False,
        ),
        (BASE, BASE, BASE + timedelta(seconds=10), BASE + timedelta(seconds=10), False),
        (
            BASE,
            BASE,
            BASE + timedelta(seconds=10),
            BASE + timedelta(seconds=10) - timedelta(microseconds=1),
            True,
        ),
        (
            BASE,
            BASE,
            BASE + timedelta(seconds=120),
            BASE - timedelta(microseconds=1),
            False,
        ),
    ],
)
def test_receiver_clock_boundaries_are_conservative(
    checked: datetime,
    issued: datetime,
    expires: datetime,
    received: datetime,
    allowed: bool,
) -> None:
    _body, permit = _response(
        _request(), checked=checked, issued=issued, expires=expires
    )
    backend = _CountingBackend()
    if allowed:
        DurableAuthority.require_remote_delivery_fresh(permit, now=received)
        backend.press()
        assert backend.inputs == 1
    else:
        with pytest.raises(DurableAuthorityBusy, match="expired|stale|future"):
            DurableAuthority.require_remote_delivery_fresh(permit, now=received)
        assert backend.inputs == 0


def test_retained_permission_expires_before_the_later_input() -> None:
    _body, permit = _response(
        _request(), checked=BASE, issued=BASE, expires=BASE + timedelta(seconds=1)
    )
    DurableAuthority.require_remote_delivery_fresh(permit, now=BASE)
    backend = _CountingBackend()
    with pytest.raises(DurableAuthorityBusy, match="expired"):
        DurableAuthority.require_remote_delivery_fresh(
            permit, now=BASE + timedelta(seconds=1)
        )
    assert backend.inputs == 0


def test_receiver_rechecks_exact_signed_bytes_and_rejects_naive_clock() -> None:
    _body, permit = _response(
        _request(), checked=BASE, issued=BASE, expires=BASE + timedelta(seconds=120)
    )
    changed = replace(
        permit,
        permit_artifact=permit.permit_artifact.model_copy(
            update={"payload_sha256": "f" * 64}
        ),
    )
    with pytest.raises(DurableAuthorityBusy, match="changed"):
        DurableAuthority.require_remote_delivery_fresh(changed, now=BASE)
    with pytest.raises(DurableAuthorityBusy, match="aware"):
        DurableAuthority.require_remote_delivery_fresh(
            permit, now=BASE.replace(tzinfo=None)
        )


@pytest.mark.parametrize("response_schema", ["v2", "v3"])
def test_legacy_v2_is_auditable_but_never_grants_live_input(
    monkeypatch: pytest.MonkeyPatch, response_schema: str
) -> None:
    from openadapt_flow.terminal_verification_v2 import (
        ProductionDeliveryPermitArtifact,
        ProductionDeliveryPermitPayload,
        sign_production_delivery_permit,
    )

    current = datetime.now(timezone.utc).replace(microsecond=0)
    response, _retained = _response(
        _request(),
        checked=current,
        issued=current,
        expires=current + timedelta(seconds=120),
    )
    body = _retained.permit_artifact.payload.model_dump(mode="json")
    body.pop("delivery_not_after")
    body["schema_version"] = "openadapt.production-delivery-permit-payload/v2"
    legacy = sign_production_delivery_permit(
        ProductionDeliveryPermitPayload.model_validate(body),
        Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
    )
    raw = legacy.canonical_bytes()
    assert (
        ProductionDeliveryPermitArtifact.model_validate_json(raw).canonical_bytes()
        == raw
    )
    response.update(
        schema_version=f"openadapt.production-delivery-permit-issue/{response_schema}",
        permit_artifact_bytes_base64=b64encode(raw).decode(),
        permit_artifact_sha256=hashlib.sha256(raw).hexdigest(),
    )
    authority = object.__new__(DurableAuthority)
    authority._remote_transport = lambda _u, _h, _b: json.dumps(response).encode()
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "https://issuer.example/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "synthetic-test-run-token")
    backend = _CountingBackend()
    with pytest.raises(DurableAuthorityBusy, match="does not match|v3 signed"):
        _input_after_permission(authority, backend)
    assert backend.inputs == 0


def test_start_deadline_is_exclusive_even_while_registry_remains_fresh() -> None:
    _body, permit = _response(
        _request(),
        checked=BASE,
        issued=BASE,
        expires=BASE + timedelta(seconds=120),
        deadline=BASE + timedelta(seconds=10),
    )
    DurableAuthority.require_remote_delivery_fresh(
        permit, now=BASE + timedelta(seconds=10) - timedelta(microseconds=1)
    )
    with pytest.raises(DurableAuthorityBusy, match="expired"):
        DurableAuthority.require_remote_delivery_fresh(
            permit, now=BASE + timedelta(seconds=10)
        )
