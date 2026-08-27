"""Exercise the product release admission gate itself.

The hosted runtime tests replace ``HostedRunnerAdapter._verify_product_release``
with a double, so the release-admission gate that guards managed execution is
never driven by them.  These tests drive both halves of the gate for real: the
pure ``verify_product_release_admission`` verifier and the adapter method that
binds it to the leased artifact bytes, the local runtime inventory, and the
monotonic sequence ledger.

Every fixture builds its validity window around the current time so the suite
does not expire on a fixed date.
"""

from __future__ import annotations

import hashlib
import json
import os
from base64 import b64encode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.runner.config import (
    AdmissionTrustFiles,
    LocalRuntimeRelease,
    RunnerConfig,
)
from openadapt_flow.runner.hosted_adapter import (
    AdmissionArtifactBytes,
    HostedRunnerAdapter,
)
from openadapt_flow.runner.product_release import (
    DOMAIN,
    TARGETS,
    ProductReleaseAdmissionArtifact,
    ProductReleaseAdmissionError,
    ProductReleaseAdmissionPayload,
    ProductReleaseSignerTrust,
    verify_product_release_admission,
)

SEQUENCE = 7
SET_ID = "00000000-0000-4000-8000-000000000099"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _release_payload(**overrides: object) -> dict[str, object]:
    opened = _stamp(_now() - timedelta(days=1))
    closes = _stamp(_now() + timedelta(days=30))
    targets = []
    for index, target in enumerate(TARGETS, start=1):
        targets.append(
            {
                "target": target,
                "admission_id": f"00000000-0000-4000-8000-{index:012d}",
                "admission_sha256": f"{index:x}" * 64,
                "release_id": "1.34.0",
                "release_artifact_sha256": f"{index + 7:x}" * 64,
                "admission_issued_at": opened,
                "admission_expires_at": closes,
                "revoked_at": None,
                "artifact_authority_sha256": f"{index + 8:x}" * 64,
                "artifact_authority_state": "active",
                "artifact_authority_checked_at": opened,
                "artifact_authority_expires_at": closes,
            }
        )
    payload: dict[str, object] = {
        "schema_version": "openadapt.product-release-admission-payload/v1",
        "set_id": SET_ID,
        "sequence": SEQUENCE,
        "policy_sha256": "a" * 64,
        "issued_at": opened,
        "expires_at": closes,
        "targets": tuple(targets),
    }
    payload.update(overrides)
    return payload


def _sign(payload_raw: dict[str, object], *, key: Ed25519PrivateKey | None = None):
    """Return a signed artifact plus the matching active signer trust."""

    private_key = key or Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    payload = ProductReleaseAdmissionPayload.model_validate(payload_raw)
    signature = private_key.sign(DOMAIN + payload.canonical_bytes())
    public_b64 = b64encode(public_key).decode("ascii")
    artifact = ProductReleaseAdmissionArtifact.model_validate(
        {
            "schema_version": "openadapt.product-release-admission-artifact/v1",
            "payload": payload,
            "payload_sha256": payload.payload_sha256_value(),
            "signer": {
                "algorithm": "ed25519",
                "key_id": (
                    "release-admission-ed25519-"
                    + hashlib.sha256(public_key).hexdigest()[:16]
                ),
                "public_key": public_b64,
            },
            "signature": urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        }
    )
    trust = ProductReleaseSignerTrust(
        public_key=public_b64, status="active", revoked_at=None
    )
    return artifact, trust


def _verify(artifact, trust, **kwargs):
    params: dict[str, object] = {
        "trusted_signers": {artifact.signer.key_id: trust},
        "newest_sequence": SEQUENCE,
        "now": _now(),
    }
    params.update(kwargs)
    return verify_product_release_admission(artifact, **params)


# --------------------------------------------------------------------------
# Positive control.  Without this, every refusal test below would still pass
# against a verifier that rejected unconditionally.
# --------------------------------------------------------------------------


def test_valid_product_release_admission_verifies() -> None:
    artifact, trust = _sign(_release_payload())
    payload = _verify(artifact, trust)
    assert payload.set_id == SET_ID
    assert payload.sequence == SEQUENCE
    assert tuple(item.target for item in payload.targets) == TARGETS


# --------------------------------------------------------------------------
# Signature and signer authority.
# --------------------------------------------------------------------------


def _artifact_json(artifact, **overrides) -> str:
    """Serialize an artifact to JSON, overriding top-level fields."""

    raw = json.loads(_canonical_artifact_bytes(artifact))
    raw.update(overrides)
    return json.dumps(raw)


def test_refuses_artifact_whose_payload_was_altered_after_signing() -> None:
    """Re-digesting a tampered payload must not launder a stale signature."""

    artifact, _ = _sign(_release_payload())
    tampered = artifact.payload.model_copy(update={"policy_sha256": "b" * 64})
    raw = json.loads(_canonical_artifact_bytes(artifact))
    raw["payload"] = json.loads(
        tampered.model_dump_json()
    )  # keep the artifact self-consistent
    raw["payload_sha256"] = tampered.payload_sha256_value()
    with pytest.raises(ValueError, match="signature is invalid"):
        ProductReleaseAdmissionArtifact.model_validate_json(json.dumps(raw))


def test_refuses_a_payload_digest_that_does_not_cover_the_payload() -> None:
    artifact, _ = _sign(_release_payload())
    with pytest.raises(ValueError, match="payload digest is invalid"):
        ProductReleaseAdmissionArtifact.model_validate_json(
            _artifact_json(artifact, payload_sha256="b" * 64)
        )


def test_refuses_signature_from_another_key() -> None:
    other = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
    artifact, _ = _sign(_release_payload())
    foreign, _ = _sign(_release_payload(), key=other)
    assert foreign.signature != artifact.signature
    with pytest.raises(ValueError, match="signature is invalid"):
        ProductReleaseAdmissionArtifact.model_validate_json(
            _artifact_json(artifact, signature=foreign.signature)
        )


def test_refuses_untrusted_signer_key_id() -> None:
    artifact, trust = _sign(_release_payload())
    with pytest.raises(ProductReleaseAdmissionError, match="not trusted"):
        _verify(artifact, trust, trusted_signers={})


def test_refuses_registry_entry_with_a_different_public_key() -> None:
    artifact, trust = _sign(_release_payload())
    other = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
    other_b64 = b64encode(
        other.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode("ascii")
    swapped = trust.model_copy(update={"public_key": other_b64})
    with pytest.raises(ProductReleaseAdmissionError, match="not trusted"):
        _verify(artifact, swapped)


def test_refuses_revoked_signer() -> None:
    artifact, trust = _sign(_release_payload())
    revoked = trust.model_copy(
        update={"status": "revoked", "revoked_at": _stamp(_now())}
    )
    with pytest.raises(ProductReleaseAdmissionError, match="signer is revoked"):
        _verify(artifact, revoked)


# --------------------------------------------------------------------------
# Set revocation, sequence, and validity window.
# --------------------------------------------------------------------------


def test_refuses_revoked_set_id() -> None:
    artifact, trust = _sign(_release_payload())
    with pytest.raises(ProductReleaseAdmissionError, match="admission is revoked"):
        _verify(artifact, trust, revoked_set_ids=frozenset({SET_ID}))


@pytest.mark.parametrize("newest", [SEQUENCE - 1, SEQUENCE + 1])
def test_refuses_any_sequence_other_than_the_newest(newest: int) -> None:
    artifact, trust = _sign(_release_payload())
    with pytest.raises(ProductReleaseAdmissionError, match="superseded"):
        _verify(artifact, trust, newest_sequence=newest)


def test_refuses_admission_before_it_is_issued() -> None:
    artifact, trust = _sign(_release_payload())
    with pytest.raises(ProductReleaseAdmissionError, match="is not active"):
        _verify(artifact, trust, now=_now() - timedelta(days=2))


def test_refuses_expired_admission() -> None:
    artifact, trust = _sign(_release_payload())
    with pytest.raises(ProductReleaseAdmissionError, match="is not active"):
        _verify(artifact, trust, now=_now() + timedelta(days=31))


# --------------------------------------------------------------------------
# Per-target state.  Each of the seven targets must independently be live.
# --------------------------------------------------------------------------


def _with_target(index: int, **changes: object) -> dict[str, object]:
    raw = _release_payload()
    targets = [dict(item) for item in raw["targets"]]  # type: ignore[arg-type]
    targets[index].update(changes)
    raw["targets"] = tuple(targets)
    return raw


@pytest.mark.parametrize("index", range(len(TARGETS)))
def test_refuses_a_revoked_target(index: int) -> None:
    artifact, trust = _sign(_with_target(index, revoked_at=_stamp(_now())))
    with pytest.raises(
        ProductReleaseAdmissionError, match=f"target {TARGETS[index]} is revoked"
    ):
        _verify(artifact, trust)


@pytest.mark.parametrize("state", ["revoked", "expired", "unavailable"])
def test_refuses_a_target_whose_artifact_authority_is_not_active(state: str) -> None:
    artifact, trust = _sign(_with_target(5, artifact_authority_state=state))
    with pytest.raises(ProductReleaseAdmissionError, match="authority is not active"):
        _verify(artifact, trust)


def test_refuses_a_target_whose_admission_window_closed() -> None:
    closed = _stamp(_now() - timedelta(hours=1))
    artifact, trust = _sign(_with_target(5, admission_expires_at=closed))
    with pytest.raises(ProductReleaseAdmissionError, match="admission is not active"):
        _verify(artifact, trust)


def test_refuses_a_target_whose_authority_check_is_stale() -> None:
    stale = _stamp(_now() - timedelta(hours=1))
    artifact, trust = _sign(_with_target(5, artifact_authority_expires_at=stale))
    with pytest.raises(ProductReleaseAdmissionError, match="authority is stale"):
        _verify(artifact, trust)


def test_refuses_an_incomplete_or_reordered_target_set() -> None:
    raw = _release_payload()
    targets = list(raw["targets"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProductReleaseAdmissionPayload.model_validate({**raw, "targets": targets[:6]})
    reordered = [targets[1], targets[0], *targets[2:]]
    with pytest.raises(ValueError, match="not exact and ordered"):
        ProductReleaseAdmissionPayload.model_validate(
            {**raw, "targets": tuple(reordered)}
        )


# --------------------------------------------------------------------------
# The adapter method: artifact-byte binding, local inventory, sequence ledger.
# --------------------------------------------------------------------------


def _private_file(path: Path, raw: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return path


def _canonical_artifact_bytes(artifact: ProductReleaseAdmissionArtifact) -> bytes:
    return json.dumps(
        artifact.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _gate_fixture(tmp_path: Path, *, payload_raw=None, sequence: int = SEQUENCE):
    """Build a real adapter, config, and dispatch carrier for the gate."""

    artifact, trust = _sign(payload_raw or _release_payload())
    raw = _canonical_artifact_bytes(artifact)
    bytes_model = AdmissionArtifactBytes(
        artifact_bytes_base64=b64encode(raw).decode("ascii"),
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
    )
    registry = _private_file(
        tmp_path / "trust" / "signers.json",
        json.dumps({artifact.signer.key_id: trust.model_dump(mode="json")}).encode(),
    )
    state = _private_file(
        tmp_path / "trust" / "state.json",
        json.dumps({"newest_sequence": sequence, "revoked_set_ids": []}).encode(),
    )
    admitted = {item.target: item for item in artifact.payload.targets}
    config = RunnerConfig(
        name="gate",
        product_release_admission=AdmissionTrustFiles(
            signer_registry=registry, state=state
        ),
        local_runtime_release=tuple(
            LocalRuntimeRelease(
                target=target,
                admission_id=admitted[target].admission_id,
                admission_sha256=admitted[target].admission_sha256,
                release_version=admitted[target].release_id,
                release_artifact_sha256=admitted[target].release_artifact_sha256,
            )
            for target in ("flow", "desktop", "capture")
        ),
    )
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    dispatch = SimpleNamespace(product_release_admission=bytes_model)
    return adapter, dispatch, config, artifact


def test_adapter_gate_accepts_an_exactly_admitted_local_runtime(tmp_path) -> None:
    adapter, dispatch, config, artifact = _gate_fixture(tmp_path)
    payload = adapter._verify_product_release(dispatch, config)
    assert payload.sequence == SEQUENCE
    ledger = json.loads(adapter._release_state_path.read_bytes())
    assert ledger == {
        "sequence": SEQUENCE,
        "artifact_sha256": dispatch.product_release_admission.artifact_sha256,
    }


def test_adapter_gate_refuses_when_no_trust_is_configured(tmp_path) -> None:
    adapter, dispatch, config, _ = _gate_fixture(tmp_path)
    from dataclasses import replace

    with pytest.raises(ValueError, match="no product release admission trust"):
        adapter._verify_product_release(
            dispatch, replace(config, product_release_admission=None)
        )


@pytest.mark.parametrize(
    "field",
    [
        "admission_id",
        "admission_sha256",
        "release_version",
        "release_artifact_sha256",
    ],
)
def test_adapter_gate_refuses_a_local_release_that_is_not_admitted(
    tmp_path, field: str
) -> None:
    from dataclasses import replace

    adapter, dispatch, config, _ = _gate_fixture(tmp_path)
    installed = config.local_runtime_release[0]
    # The fixture uses digests "1".."7" and "8".."e", so "f" cannot collide
    # with the value legitimately admitted for any of the seven targets.
    substitute = {
        "admission_id": "00000000-0000-4000-8000-000000009999",
        "admission_sha256": "f" * 64,
        "release_version": "1.33.0",
        "release_artifact_sha256": "f" * 64,
    }[field]
    assert getattr(installed, field) != substitute
    mutated = replace(installed, **{field: substitute})
    config = replace(
        config,
        local_runtime_release=(mutated, *config.local_runtime_release[1:]),
    )
    with pytest.raises(ValueError, match="is not exactly admitted"):
        adapter._verify_product_release(dispatch, config)


def test_adapter_gate_refuses_an_incomplete_local_inventory(tmp_path) -> None:
    from dataclasses import replace

    adapter, dispatch, config, _ = _gate_fixture(tmp_path)
    config = replace(config, local_runtime_release=config.local_runtime_release[:2])
    with pytest.raises(ValueError, match="local release inventory is incomplete"):
        adapter._verify_product_release(dispatch, config)


def test_adapter_gate_refuses_noncanonical_artifact_bytes(tmp_path) -> None:
    """The leased bytes must be the canonical serialization, not merely valid."""

    adapter, dispatch, config, artifact = _gate_fixture(tmp_path)
    padded = json.dumps(
        artifact.model_dump(mode="json"), sort_keys=True, indent=1
    ).encode("utf-8")
    dispatch.product_release_admission = AdmissionArtifactBytes(
        artifact_bytes_base64=b64encode(padded).decode("ascii"),
        artifact_sha256=hashlib.sha256(padded).hexdigest(),
    )
    with pytest.raises(ValueError, match="canonical digest changed"):
        adapter._verify_product_release(dispatch, config)


def test_adapter_gate_refuses_a_sequence_rollback(tmp_path) -> None:
    adapter, dispatch, config, _ = _gate_fixture(tmp_path)
    adapter._verify_product_release(dispatch, config)

    older = _release_payload(sequence=SEQUENCE - 1)
    adapter2, dispatch2, config2, _ = _gate_fixture(
        tmp_path / "second", payload_raw=older, sequence=SEQUENCE - 1
    )
    # Point the older dispatch at the ledger the newer sequence already wrote.
    adapter2._release_state_path = adapter._release_state_path
    with pytest.raises(ValueError, match="sequence is stale"):
        adapter2._verify_product_release(dispatch2, config2)


def test_adapter_gate_refuses_a_changed_artifact_at_one_sequence(tmp_path) -> None:
    adapter, dispatch, config, _ = _gate_fixture(tmp_path)
    adapter._verify_product_release(dispatch, config)

    altered = _release_payload(policy_sha256="e" * 64)
    adapter2, dispatch2, config2, _ = _gate_fixture(
        tmp_path / "third", payload_raw=altered
    )
    adapter2._release_state_path = adapter._release_state_path
    with pytest.raises(ValueError, match="changed at one sequence"):
        adapter2._verify_product_release(dispatch2, config2)


def test_adapter_gate_refuses_a_malformed_authority_state_file(tmp_path) -> None:
    adapter, dispatch, config, _ = _gate_fixture(tmp_path)
    _private_file(
        config.product_release_admission.state,
        json.dumps({"newest_sequence": SEQUENCE}).encode(),
    )
    with pytest.raises(ValueError, match="authority state is invalid"):
        adapter._verify_product_release(dispatch, config)
