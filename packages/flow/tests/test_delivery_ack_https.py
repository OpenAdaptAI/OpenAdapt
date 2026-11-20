"""Real TLS ACK paths preserve nonauthorizing evidence and historical receipts."""

from __future__ import annotations

import json
import ssl
import threading
import time
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import HTTPSHandler, ProxyHandler

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import openadapt_flow.runtime.durable.authority as authority_module
from openadapt_flow.backends.win_agent import generate_self_signed_cert
from openadapt_flow.runtime.durable.authority import (
    AUTHORITY_DB_ENV,
    REMOTE_AUTHORITY_TOKEN_ENV,
    REMOTE_AUTHORITY_URL_ENV,
    REMOTE_DISPATCH_SESSION_ID_ENV,
    DurableAuthority,
    DurableAuthorityBusy,
)
from openadapt_flow.runtime.durable.delivery_observation import SCHEMA
from openadapt_flow.terminal_verification_v2 import (
    ProductionDeliveryPermitArtifact,
    ProductionDeliveryReceiptPayload,
    sign_production_delivery_receipt,
)
from tests.test_delivery_permit_receiver_freshness import (
    SESSION_ID,
    _CountingBackend,
    _response,
    _utc,
)
from tests.test_durable_authority_v13 import _fresh_run


def _wait_until(instant: datetime) -> None:
    while datetime.now(timezone.utc) < instant:
        time.sleep(0.01)


@pytest.mark.parametrize("mode", ["uncertainty_409", "timely_receipt_delayed_response"])
def test_real_https_ack_distinguishes_uncertainty_from_late_historical_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    cert = generate_self_signed_cert(dirpath=str(tmp_path / "tls"))
    state: dict[str, Any] = {"permit_requests": 0}
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))

    class Issuer(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            pass

        def _send(self, status: int, raw: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            assert self.headers["Authorization"] == "Bearer synthetic-ack-token"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("managed-delivery-permit"):
                state["permit_requests"] += 1
                if state["permit_requests"] > 1:
                    self._send(403, b'{"refused":"session_expired"}')
                    return
                issued = datetime.now(timezone.utc).replace(microsecond=0)
                deadline = issued + timedelta(seconds=3)
                response, retained = _response(
                    request,
                    checked=issued,
                    issued=issued,
                    expires=issued + timedelta(seconds=120),
                    deadline=deadline,
                )
                state.update(deadline=deadline, permit=retained.permit_artifact)
                self._send(200, json.dumps(response).encode())
                return
            assert self.path.endswith("managed-delivery-acknowledgment")
            permit = ProductionDeliveryPermitArtifact.model_validate_json(
                b64decode(request["permit_artifact_bytes_base64"])
            )
            assert permit.canonical_bytes() == state["permit"].canonical_bytes()
            if mode == "uncertainty_409":
                # The caller completed after the start grant. No signed
                # authorizing receipt is created for this observation.
                controller = datetime.now(timezone.utc).isoformat()
                received = datetime.now(timezone.utc).isoformat()
                value = {
                    "schema_version": SCHEMA,
                    "status": "uncertain",
                    "observation_id": "10300000-0000-4000-8000-000000000006",
                    "run_id": request["run_id"],
                    "dispatch_session_id": request["dispatch_session_id"],
                    "permit_id": permit.payload.permit_id,
                    "one_use_claim_id": request["one_use_claim_id"],
                    "runtime_delivery_sequence": request["runtime_delivery_sequence"],
                    "controller_delivered_at": controller,
                    "received_at": received,
                }
                raw = json.dumps(value, indent=1).encode()
                state["observation_bytes"] = raw
                self._send(409, raw)
                return
            # The server commits the valid timely receipt first. Only its
            # already-committed response is delayed by the network fixture.
            delivered_at = _utc(datetime.now(timezone.utc))
            assert delivered_at < _utc(state["deadline"])
            receipt = sign_production_delivery_receipt(
                ProductionDeliveryReceiptPayload(
                    execution_authority_id=permit.payload.execution_authority_id,
                    permit_id=permit.payload.permit_id,
                    permit_artifact_sha256=permit.artifact_sha256(),
                    authenticated_runner_id_sha256="8" * 64,
                    authenticated_session_id_sha256="9" * 64,
                    one_use_claim_id=request["one_use_claim_id"],
                    runtime_delivery_sequence=request["runtime_delivery_sequence"],
                    delivered_at=delivered_at,
                ),
                key,
            )
            receipt_bytes = receipt.canonical_bytes()
            value = {
                "schema_version": "openadapt.production-delivery-acknowledgment-result/v3",
                "status": "acknowledged",
                "receipt_artifact_bytes_base64": b64encode(receipt_bytes).decode(),
                "receipt_artifact_sha256": receipt.artifact_sha256(),
                "runtime_delivery_sequence": request["runtime_delivery_sequence"],
                "delivered_at": delivered_at,
            }
            state["committed_at"] = datetime.now(timezone.utc)
            _wait_until(state["deadline"] + timedelta(milliseconds=10))
            state["response_sent_at"] = datetime.now(timezone.utc)
            self._send(200, json.dumps(value).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Issuer)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(tmp_path / "authority" / "db.sqlite"))
    monkeypatch.setenv(
        REMOTE_AUTHORITY_URL_ENV,
        f"https://127.0.0.1:{server.server_port}/api/internal/managed-delivery-permit",
    )
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "synthetic-ack-token")
    monkeypatch.setenv(REMOTE_DISPATCH_SESSION_ID_ENV, SESSION_ID)
    _run_dir, _store, manifest, authority = _fresh_run(
        tmp_path, execution_profile="standard"
    )
    backend = _CountingBackend()
    try:
        permit = authority.before_initial_delivery(manifest)
        assert permit is not None
        authority.require_remote_delivery_fresh(permit)
        backend.press()
        if mode == "uncertainty_409":
            _wait_until(state["deadline"] + timedelta(milliseconds=10))
            with pytest.raises(DurableAuthorityBusy, match="non-authorizing.*retained"):
                authority.acknowledge_remote_delivery(manifest, permit)
            assert authority.production_delivery_uncertainty_observations() == (
                state["observation_bytes"],
            )
            assert authority.validate(manifest).delivery_sequence == 0
            request_count = state["permit_requests"]
            reopened = DurableAuthority(authority.run_dir, authority.store)
            with pytest.raises(
                DurableAuthorityBusy, match="non-authorizing delivery uncertainty"
            ):
                reopened.before_initial_delivery(manifest)
            assert state["permit_requests"] == request_count
        else:
            entry = authority.acknowledge_remote_delivery(manifest, permit)
            assert state["committed_at"] < state["deadline"] < state["response_sent_at"]
            assert entry.delivery_receipt_artifact.payload.delivered_at < _utc(
                state["deadline"]
            )
            assert authority.production_delivery_uncertainty_observations() == ()
            assert authority.validate(manifest).delivery_sequence == 1
            assert len(authority.production_delivery_permit_chain().entries) == 1
            # A historical receipt cannot stand in for the NEXT input's fresh
            # permit. The expired session is refused by the issuer.
            with pytest.raises(DurableAuthorityBusy, match="unavailable or refused"):
                authority.before_initial_delivery(manifest)
            assert state["permit_requests"] == 2
        assert backend.inputs == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
