"""Non-authorizing ACK observations survive without permitting a second input."""

from __future__ import annotations

import json
from base64 import b64decode
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import openadapt_flow.runtime.durable.authority as authority_module
from openadapt_flow.runtime.durable.authority import (
    DurableAuthority,
    DurableAuthorityBusy,
)
from openadapt_flow.runtime.durable.delivery_observation import (
    SCHEMA,
    DeliveryUncertaintyObservation,
)
from tests.test_durable_authority_v13 import (
    _issued_permit_transport,
    _remote_initial_authority,
    _remote_ready_authority,
)

OBSERVATION_ID = "10300000-0000-4000-8000-000000000006"


def _observation(request: dict[str, Any]) -> dict[str, Any]:
    artifact = DurableAuthority._parse_permit_artifact_bytes(
        b64decode(request["permit_artifact_bytes_base64"])
    )
    return {
        "schema_version": SCHEMA,
        "status": "uncertain",
        "observation_id": OBSERVATION_ID,
        "run_id": request["run_id"],
        "dispatch_session_id": request["dispatch_session_id"],
        "permit_id": artifact.payload.permit_id,
        "one_use_claim_id": request["one_use_claim_id"],
        "runtime_delivery_sequence": request["runtime_delivery_sequence"],
        "controller_delivered_at": "2026-08-20T01:00:00.123Z",
        "received_at": "2026-08-20T01:00:00.654321+00:00",
    }


@pytest.fixture(autouse=True)
def _fixture_permit_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Match the existing signed synthetic issuer's chronology only. The
    # separate real-HTTPS tests exercise the actual receiver wall clock.
    monkeypatch.setattr(
        authority_module,
        "_permit_receiver_now",
        lambda: datetime(2026, 8, 20, 0, 1, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize("entrypoint", ["initial", "continuation"])
def test_uncertainty_is_retained_verbatim_and_fences_every_later_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    issuer = _issued_permit_transport("observation", "a" * 64)
    requests: list[str] = []
    raw: list[bytes] = []

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        requests.append(url)
        if url.endswith("managed-delivery-acknowledgment"):
            if not raw:
                # Preserve this timestamp spelling and precision on retries.
                raw.append(
                    json.dumps(_observation(json.loads(body)), indent=1).encode()
                )
            raise HTTPError(url, 409, "Conflict", {}, BytesIO(raw[0]))
        return issuer(url, headers, body)

    if entrypoint == "initial":
        manifest, authority = _remote_initial_authority(
            tmp_path, monkeypatch, transport
        )
        kwargs: dict[str, Any] = {}
    else:
        manifest, authority, owner = _remote_ready_authority(
            tmp_path, monkeypatch, transport
        )
        kwargs = {"attempt_id": "a" * 32, "owner_nonce_sha256": owner}

    def get_permit(a: DurableAuthority) -> Any:
        if entrypoint == "initial":
            return a.before_initial_delivery(manifest)
        return a.before_delivery(manifest, **kwargs)

    permit = get_permit(authority)
    assert permit is not None
    with pytest.raises(DurableAuthorityBusy, match="non-authorizing.*retained"):
        authority.acknowledge_remote_delivery(manifest, permit, **kwargs)
    assert authority.production_delivery_uncertainty_observations() == (raw[0],)
    assert authority.validate(manifest).delivery_sequence == 0
    with pytest.raises(DurableAuthorityBusy, match="uncertain without a receipt"):
        authority.production_delivery_permit_chain()
    # Exact lost-response retry returns original raw bytes and adds no row.
    with pytest.raises(DurableAuthorityBusy, match="non-authorizing.*retained"):
        authority.acknowledge_remote_delivery(manifest, permit, **kwargs)
    assert authority.production_delivery_uncertainty_observations() == (raw[0],)
    request_count = len(requests)
    for next_authority in (
        authority,
        DurableAuthority(
            authority.run_dir, authority.store, remote_transport=transport
        ),
    ):
        with pytest.raises(
            DurableAuthorityBusy, match="non-authorizing delivery uncertainty"
        ):
            get_permit(next_authority)
    assert len(requests) == request_count  # refusal precedes any HTTP request


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "dispatch_session_id",
        "permit_id",
        "one_use_claim_id",
        "runtime_delivery_sequence",
    ],
)
def test_observation_for_another_edge_cannot_be_attached_to_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    issuer = _issued_permit_transport("binding", "b" * 64)

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        if url.endswith("managed-delivery-acknowledgment"):
            value = _observation(json.loads(body))
            value[field] = 1 if field == "runtime_delivery_sequence" else OBSERVATION_ID
            raise HTTPError(
                url, 409, "Conflict", {}, BytesIO(json.dumps(value).encode())
            )
        return issuer(url, headers, body)

    manifest, authority = _remote_initial_authority(tmp_path, monkeypatch, transport)
    permit = authority.before_initial_delivery(manifest)
    assert permit is not None
    with pytest.raises(DurableAuthorityBusy, match="does not bind"):
        authority.acknowledge_remote_delivery(manifest, permit)
    assert authority.production_delivery_uncertainty_observations() == ()
    assert authority.validate(manifest).delivery_sequence == 0


@pytest.mark.parametrize("fault", ["ack_on_409", "observation_on_200", "oversize_409"])
def test_error_status_never_turns_into_a_valid_acknowledgment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    issuer = _issued_permit_transport("wrong-status", "c" * 64)

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        if url.endswith("managed-delivery-acknowledgment"):
            if fault == "observation_on_200":
                return json.dumps(_observation(json.loads(body))).encode()
            if fault == "ack_on_409":
                raw = issuer(url, headers, body)
            else:
                raw = b"x" * (authority_module.MAX_REMOTE_AUTHORITY_RESPONSE_BYTES + 1)
            raise HTTPError(url, 409, "Conflict", {}, BytesIO(raw))
        return issuer(url, headers, body)

    manifest, authority = _remote_initial_authority(tmp_path, monkeypatch, transport)
    permit = authority.before_initial_delivery(manifest)
    assert permit is not None
    with pytest.raises(
        DurableAuthorityBusy, match="HTTP 409|requires HTTP 409|too large"
    ):
        authority.acknowledge_remote_delivery(manifest, permit)
    assert authority.production_delivery_uncertainty_observations() == ()
    assert authority.validate(manifest).delivery_sequence == 0


@pytest.mark.parametrize(
    "fault",
    ["receipt_field", "acknowledged", "naive_time", "non_utc", "boolean_sequence"],
)
def test_raw_observation_cannot_carry_permission_fields_or_ambiguous_time(
    fault: str,
) -> None:
    value = {
        "schema_version": SCHEMA,
        "status": "uncertain",
        "observation_id": OBSERVATION_ID,
        "run_id": OBSERVATION_ID,
        "dispatch_session_id": OBSERVATION_ID,
        "permit_id": OBSERVATION_ID,
        "one_use_claim_id": OBSERVATION_ID,
        "runtime_delivery_sequence": 0,
        "controller_delivered_at": "2026-08-20T01:00:00.123Z",
        "received_at": "2026-08-20T01:00:00.654321+00:00",
    }
    if fault == "receipt_field":
        value["receipt_artifact_sha256"] = "a" * 64
    elif fault == "acknowledged":
        value["status"] = "acknowledged"
    elif fault == "naive_time":
        value["received_at"] = "2026-08-20T01:00:00"
    elif fault == "non_utc":
        value["received_at"] = "2026-08-20T01:00:00+01:00"
    else:
        value["runtime_delivery_sequence"] = True
    with pytest.raises(ValueError):
        DeliveryUncertaintyObservation.model_validate(value)
