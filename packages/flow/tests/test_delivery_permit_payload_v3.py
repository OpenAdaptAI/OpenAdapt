"""The versioned wire grammar preserves old signatures and closes v3 deadlines."""

from __future__ import annotations

import json
from base64 import b64decode, urlsafe_b64decode
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from openadapt_flow.delivery_permit_payload import (
    PERMIT_V3_PAYLOAD_DOMAIN,
    ProductionDeliveryPermitPayload,
    ProductionDeliveryPermitV3Payload,
    parse_delivery_permit_payload,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "filename",
    [
        "terminal_verification_v2_delivery_vector.json",
        "terminal_verification_v2_delivery_consumer_vector.json",
    ],
)
def test_historical_v2_payloads_preserve_exact_bytes_hash_and_signature(
    filename: str,
) -> None:
    vector = json.loads((FIXTURES / filename).read_text())
    moved = ProductionDeliveryPermitPayload.model_validate(vector["permit_payload"])
    assert (
        moved.canonical_bytes()
        == json.dumps(
            vector["permit_payload"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    assert moved.payload_sha256() == vector["permit_payload_sha256"]
    public = Ed25519PublicKey.from_public_bytes(b64decode(vector["public_key_base64"]))
    signature = urlsafe_b64decode(vector["permit_signature"] + "==")
    public.verify(signature, moved.signing_bytes())


def _v3_body() -> dict:
    vector = json.loads(
        (FIXTURES / "terminal_verification_v2_delivery_vector.json").read_text()
    )
    return {
        **vector["permit_payload"],
        "schema_version": "openadapt.production-delivery-permit-payload/v3",
        "delivery_not_after": "2026-08-18T12:00:20Z",
    }


def test_v3_adds_only_the_signed_exclusive_start_deadline_and_separates_domain() -> (
    None
):
    body = _v3_body()
    payload = ProductionDeliveryPermitV3Payload.model_validate(body)
    assert payload.model_dump(mode="json") == body
    assert payload.signing_bytes() == (
        b"openadapt.production-delivery-permit-payload.v3\0" + payload.canonical_bytes()
    )
    assert PERMIT_V3_PAYLOAD_DOMAIN.endswith(b".v3\0")
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    signature = key.sign(payload.signing_bytes())
    key.public_key().verify(signature, payload.signing_bytes())
    with pytest.raises(InvalidSignature):
        key.public_key().verify(
            signature,
            b"openadapt.production-delivery-permit-payload.v2\0"
            + payload.canonical_bytes(),
        )
    assert isinstance(
        parse_delivery_permit_payload(body), ProductionDeliveryPermitV3Payload
    )


@pytest.mark.parametrize(
    "deadline",
    [None, "issued", "before_issued", "after_registry", "noncanonical"],
)
def test_v3_refuses_missing_nonexclusive_or_unbounded_deadline(
    deadline: str | None,
) -> None:
    body = _v3_body()
    if deadline is None:
        body.pop("delivery_not_after")
    else:
        issued = datetime.fromisoformat(body["issued_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(
            body["qualification_signer_registry_expires_at"].replace("Z", "+00:00")
        )
        invalid = {
            "issued": body["issued_at"],
            "before_issued": (issued - timedelta(seconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "after_registry": (expires + timedelta(seconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "noncanonical": "2026-08-18T12:00:20+00:00",
        }
        body["delivery_not_after"] = invalid[deadline]
    with pytest.raises(ValueError):
        parse_delivery_permit_payload(body)


def test_v2_and_v3_do_not_guess_each_others_grammar() -> None:
    body = _v3_body()
    with pytest.raises(ValueError):
        ProductionDeliveryPermitPayload.model_validate(body)
    body["schema_version"] = "openadapt.production-delivery-permit-payload/v2"
    with pytest.raises(ValueError):
        parse_delivery_permit_payload(body)
    body["schema_version"] = "openadapt.production-delivery-permit-payload/v4"
    with pytest.raises(ValueError):
        parse_delivery_permit_payload(body)


def test_v3_artifact_audit_parser_requires_paired_versions_and_exact_signature() -> (
    None
):
    from base64 import urlsafe_b64encode

    from openadapt_flow.terminal_verification_v2 import (
        ProductionDeliveryPermitArtifact,
        sign_production_delivery_permit_v3,
    )

    payload = ProductionDeliveryPermitV3Payload.model_validate(_v3_body())
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    artifact = sign_production_delivery_permit_v3(payload, key)
    raw = artifact.canonical_bytes()
    # Historical/audit parsing is clock-free, even though this permit's
    # absolute start deadline has passed. This is not live permission.
    audited = ProductionDeliveryPermitArtifact.model_validate_json(raw)
    assert audited.canonical_bytes() == raw
    assert audited.schema_version == "openadapt.production-delivery-permit-artifact/v3"
    assert (
        audited.payload.schema_version
        == "openadapt.production-delivery-permit-payload/v3"
    )
    wrong_outer = artifact.model_dump(mode="json")
    wrong_outer["schema_version"] = "openadapt.production-delivery-permit-artifact/v2"
    with pytest.raises(ValueError, match="versions differ"):
        ProductionDeliveryPermitArtifact.model_validate(wrong_outer)
    wrong_domain = artifact.model_dump(mode="json")
    wrong_domain["signature"] = (
        urlsafe_b64encode(
            key.sign(
                b"openadapt.production-delivery-permit-payload.v2\0"
                + payload.canonical_bytes()
            )
        )
        .decode()
        .rstrip("=")
    )
    with pytest.raises(ValueError, match="signature"):
        ProductionDeliveryPermitArtifact.model_validate(wrong_domain)
    changed_deadline = artifact.model_dump(mode="json")
    changed_deadline["payload"]["delivery_not_after"] = "2026-08-18T12:00:21Z"
    with pytest.raises(ValueError, match="digest"):
        ProductionDeliveryPermitArtifact.model_validate(changed_deadline)


@pytest.mark.parametrize(
    ("delivered_at", "accepted"),
    [
        ("2026-08-18T12:00:19Z", True),
        ("2026-08-18T12:00:20Z", False),
        ("2026-08-18T12:00:21Z", False),
        ("2026-08-18T11:59:59Z", False),
        ("2026-08-20T11:00:00Z", False),
    ],
)
def test_v3_receipt_grants_only_completion_inside_exact_signed_window(
    delivered_at: str, accepted: bool
) -> None:
    from openadapt_flow.terminal_verification_v2 import (
        ProductionDeliveryPermit,
        ProductionDeliveryReceiptPayload,
        sign_production_delivery_permit_v3,
        sign_production_delivery_receipt,
    )

    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    permit = sign_production_delivery_permit_v3(
        ProductionDeliveryPermitV3Payload.model_validate(_v3_body()), key
    )
    receipt = sign_production_delivery_receipt(
        ProductionDeliveryReceiptPayload(
            execution_authority_id=permit.payload.execution_authority_id,
            permit_id=permit.payload.permit_id,
            permit_artifact_sha256=permit.artifact_sha256(),
            authenticated_runner_id_sha256="8" * 64,
            authenticated_session_id_sha256="9" * 64,
            one_use_claim_id="10300000-0000-4000-8000-000000000005",
            runtime_delivery_sequence=0,
            delivered_at=delivered_at,
        ),
        key,
    )
    if accepted:
        entry = ProductionDeliveryPermit.build(permit, receipt)
        assert entry.permit_artifact.artifact_sha256() == permit.artifact_sha256()
    else:
        with pytest.raises(ValueError, match="chronology|signed input deadline"):
            ProductionDeliveryPermit.build(permit, receipt)
