from __future__ import annotations

import hashlib
import json
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from openadapt_flow.qualification_admission_v2 import canonical_json
from openadapt_flow.runner.flow_release_receipt import (
    FlowReleaseVerificationReceipt,
    FlowReleaseVerificationReceiptArtifactBytes,
    HostedFlowReleaseIdentity,
    assert_hosted_flow_release,
)

FIXTURE = Path("tests/fixtures/remote-safe-synthetic-flow-release-verification.json")
OBJECT_SHA256 = (
    "sha256:bf170018cd1d3519f40ebd2788e158dd34a789c7bfe1e85e30e3a445e5e40af8"
)
NOW = datetime(2026, 8, 28, tzinfo=timezone.utc)
VERIFICATION_ID_DOMAIN = b"OpenAdapt qualification release verification receipt v1\0"


def _artifact(raw: bytes | None = None) -> FlowReleaseVerificationReceiptArtifactBytes:
    exact = raw if raw is not None else FIXTURE.read_bytes()
    return FlowReleaseVerificationReceiptArtifactBytes(
        artifact_bytes_base64=b64encode(exact).decode("ascii"),
        artifact_sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
    )


def _with_verification_id(payload: dict[str, object]) -> dict[str, object]:
    projection = dict(payload)
    projection.pop("verification_id_sha256", None)
    payload["verification_id_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            VERIFICATION_ID_DOMAIN + canonical_json(projection)
        ).hexdigest()
    )
    return payload


def test_flow_release_fixture_has_exact_identity_and_object_digest() -> None:
    raw = FIXTURE.read_bytes()
    artifact = _artifact(raw)
    receipt = artifact.decode(now=NOW)
    identity = artifact.identity(now=NOW)

    assert "sha256:" + hashlib.sha256(raw).hexdigest() == OBJECT_SHA256
    assert artifact.artifact_sha256 == OBJECT_SHA256
    assert identity == HostedFlowReleaseIdentity(
        verification_receipt_object_sha256=OBJECT_SHA256,
        release_sha256=receipt.release_sha256,
        source_commit=receipt.source_commit,
        version="1.35.0",
    )
    assert receipt.tag == "v1.35.0"
    assert assert_hosted_flow_release(identity, artifact, now=NOW) == receipt


def test_flow_release_receipt_refuses_object_and_identity_drift() -> None:
    raw = FIXTURE.read_bytes()
    with pytest.raises(ValidationError, match="bytes or digest differ"):
        FlowReleaseVerificationReceiptArtifactBytes(
            artifact_bytes_base64=b64encode(raw).decode("ascii"),
            artifact_sha256="sha256:" + "0" * 64,
        )
    with pytest.raises(ValidationError, match="object bytes are invalid"):
        FlowReleaseVerificationReceiptArtifactBytes(
            artifact_bytes_base64=b64encode(raw).decode("ascii") + "!",
            artifact_sha256=OBJECT_SHA256,
        )

    artifact = _artifact(raw)
    identity = artifact.identity(now=NOW)
    drifted = identity.model_copy(update={"release_sha256": "sha256:" + "0" * 64})
    with pytest.raises(ValueError, match="differs from the verified Flow release"):
        assert_hosted_flow_release(drifted, artifact, now=NOW)


def test_flow_release_receipt_refuses_self_binding_tag_and_time_drift() -> None:
    original = json.loads(FIXTURE.read_text(encoding="utf-8"))
    changed = dict(original)
    changed["source_commit"] = "0" * 40
    with pytest.raises(ValidationError, match="verification digest is invalid"):
        FlowReleaseVerificationReceipt.model_validate(changed)

    invalid_tag = _with_verification_id(dict(original) | {"tag": "1.35.0"})
    with pytest.raises(ValidationError, match="tag differs"):
        FlowReleaseVerificationReceipt.model_validate(invalid_tag)

    expired = _with_verification_id(
        dict(original) | {"expires_at": "2026-08-28T00:00:00Z"}
    )
    expired_raw = json.dumps(expired, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="expired"):
        _artifact(expired_raw).decode(now=NOW)

    future = _with_verification_id(
        dict(original) | {"verified_at": "2026-08-29T00:00:00Z"}
    )
    future_raw = json.dumps(future, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="in the future"):
        _artifact(future_raw).decode(now=NOW)


@pytest.mark.parametrize("field", ["registry_revision", "release_identity"])
def test_flow_release_receipt_refuses_integers_above_wire_safe_range(
    field: str,
) -> None:
    changed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if field == "release_identity":
        release_identity = dict(changed[field])
        release_identity["sequence"] = 1 << 53
        changed[field] = release_identity
    else:
        changed[field] = 1 << 53
    changed = _with_verification_id(changed)

    with pytest.raises(ValidationError, match="less than or equal to"):
        FlowReleaseVerificationReceipt.model_validate(changed)
