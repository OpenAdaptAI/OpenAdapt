"""Named-person binding for regulated approvals (Part of #1888).

Phone pairing / roster Ed25519 keys identify a device or key, not a person.
Under the regulated profile an approval without a verified WebAuthn / OIDC /
SAML binding to a named person must refuse. These tests use
DeterministicPasskeyDouble fixtures — not a live WebAuthn ceremony.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pytest

from openadapt_flow.approval_mode.item_approval import (
    ApproverContext,
    ItemApprovalAnswer,
    ItemApprovalStore,
    answer_refusal,
)
from openadapt_flow.approval_mode.model import ApprovalMode
from openadapt_flow.approval_mode.named_person_binding import (
    BindingMethod,
    DeterministicPasskeyDouble,
    MemoryPersonRegistry,
    NamedPersonBindingError,
    NamedPersonBindingV1,
    NamedPersonRefusal,
    OidcEvidenceV1,
    RegisteredFederatedSubject,
    RegisteredPasskey,
    SamlEvidenceV1,
    WebAuthnAssertionMaterial,
    approval_payload_digest,
    build_binding,
    require_named_person_binding,
    seal_extension,
)

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "named_person_binding"
    / "webauthn_passkey_double.json"
)

FRAME = b"\x89PNG\r\n\x1a\nmade-up filled form"
PERSON = "person:alice"
RP_ID = "approve.openadapt.test"


@pytest.fixture
def passkey() -> RegisteredPasskey:
    return RegisteredPasskey(
        person_id=PERSON,
        credential_id=b"cred-alice-1",
        public_key=b"P" * 32,
        rp_id=RP_ID,
    )


@pytest.fixture
def double() -> DeterministicPasskeyDouble:
    return DeterministicPasskeyDouble()


@pytest.fixture
def registry(passkey: RegisteredPasskey) -> MemoryPersonRegistry:
    return MemoryPersonRegistry(passkeys=[passkey])


def _payload() -> dict:
    return {
        "request_id": "req-1",
        "workflow": "referral",
        "decision": "approve",
        "fields": ["status"],
    }


def test_regulated_refuses_when_binding_is_missing(passkey, double, registry) -> None:
    digest = approval_payload_digest(_payload())
    code = require_named_person_binding(
        profile="regulated",
        approval_payload_sha256=digest,
        expected_person_id=PERSON,
        binding=None,
        assertion=None,
        registry=registry,
        verifier=double,
    )
    assert code == NamedPersonRefusal.MISSING_BINDING.value


def test_demo_does_not_require_binding() -> None:
    assert (
        require_named_person_binding(
            profile="demo",
            approval_payload_sha256="sha256:" + "0" * 64,
            expected_person_id=None,
            binding=None,
            assertion=None,
            registry=None,
            verifier=None,
        )
        is None
    )


def test_standard_allows_absent_binding_in_this_slice() -> None:
    assert (
        require_named_person_binding(
            profile="standard",
            approval_payload_sha256="sha256:" + "0" * 64,
            expected_person_id=None,
            binding=None,
            assertion=None,
            registry=None,
            verifier=None,
        )
        is None
    )


def test_webauthn_passkey_binding_verifies(passkey, double, registry) -> None:
    digest = approval_payload_digest(_payload())
    assertion = double.sign_webauthn(challenge_sha256=digest, registered=passkey)
    binding = build_binding(
        person_id=PERSON,
        method=BindingMethod.WEBAUTHN_PASSKEY,
        approval_payload_sha256=digest,
        assertion=assertion,
        rp_id=RP_ID,
        verified_at="2026-11-05T17:00:00Z",
    )
    assert (
        require_named_person_binding(
            profile="regulated",
            approval_payload_sha256=digest,
            expected_person_id=PERSON,
            binding=binding,
            assertion=assertion,
            registry=registry,
            verifier=double,
        )
        is None
    )
    ext = seal_extension(binding)
    assert ext["schema"] == "openadapt.named-person-binding/v1"
    dumped = json.dumps(ext)
    # Seal may name digest fields like signature_sha256; never the raw bytes.
    assert assertion.signature.hex() not in dumped
    assert "signature_sha256" in dumped


def test_webauthn_wrong_payload_challenge_refuses(passkey, double, registry) -> None:
    digest = approval_payload_digest(_payload())
    other = approval_payload_digest({"other": True})
    assertion = double.sign_webauthn(challenge_sha256=other, registered=passkey)
    with pytest.raises(NamedPersonBindingError) as excinfo:
        build_binding(
            person_id=PERSON,
            method=BindingMethod.WEBAUTHN_PASSKEY,
            approval_payload_sha256=digest,
            assertion=assertion,
            rp_id=RP_ID,
            verified_at="2026-11-05T17:00:00Z",
        )
    assert excinfo.value.code == NamedPersonRefusal.PAYLOAD_MISMATCH


def test_webauthn_unknown_credential_refuses(passkey, double) -> None:
    digest = approval_payload_digest(_payload())
    assertion = double.sign_webauthn(challenge_sha256=digest, registered=passkey)
    binding = build_binding(
        person_id=PERSON,
        method=BindingMethod.WEBAUTHN_PASSKEY,
        approval_payload_sha256=digest,
        assertion=assertion,
        rp_id=RP_ID,
        verified_at="2026-11-05T17:00:00Z",
    )
    empty = MemoryPersonRegistry(passkeys=[])
    code = require_named_person_binding(
        profile="regulated",
        approval_payload_sha256=digest,
        expected_person_id=PERSON,
        binding=binding,
        assertion=assertion,
        registry=empty,
        verifier=double,
    )
    assert code == NamedPersonRefusal.CREDENTIAL_UNKNOWN.value


def test_webauthn_tampered_signature_refuses(passkey, double, registry) -> None:
    digest = approval_payload_digest(_payload())
    assertion = double.sign_webauthn(challenge_sha256=digest, registered=passkey)
    # Rebuild binding evidence to match the tampered assertion so the digest
    # gate does not fire first — the verifier must still refuse.
    tampered = WebAuthnAssertionMaterial(
        credential_id=assertion.credential_id,
        authenticator_data=assertion.authenticator_data,
        client_data_json=assertion.client_data_json,
        signature=b"\x00" * 32,
        user_handle=assertion.user_handle,
    )
    binding = build_binding(
        person_id=PERSON,
        method=BindingMethod.WEBAUTHN_PASSKEY,
        approval_payload_sha256=digest,
        assertion=tampered,
        rp_id=RP_ID,
        verified_at="2026-11-05T17:00:00Z",
    )
    code = require_named_person_binding(
        profile="regulated",
        approval_payload_sha256=digest,
        expected_person_id=PERSON,
        binding=binding,
        assertion=tampered,
        registry=registry,
        verifier=double,
    )
    assert code == NamedPersonRefusal.ASSERTION_INVALID.value


def test_person_id_mismatch_refuses(passkey, double, registry) -> None:
    digest = approval_payload_digest(_payload())
    assertion = double.sign_webauthn(challenge_sha256=digest, registered=passkey)
    binding = build_binding(
        person_id=PERSON,
        method=BindingMethod.WEBAUTHN_PASSKEY,
        approval_payload_sha256=digest,
        assertion=assertion,
        rp_id=RP_ID,
        verified_at="2026-11-05T17:00:00Z",
    )
    code = require_named_person_binding(
        profile="regulated",
        approval_payload_sha256=digest,
        expected_person_id="person:other",
        binding=binding,
        assertion=assertion,
        registry=registry,
        verifier=double,
    )
    assert code == NamedPersonRefusal.PERSON_MISMATCH.value


def test_oidc_and_saml_bindings_verify(double) -> None:
    digest = approval_payload_digest(_payload())
    for method, enum in (("oidc", BindingMethod.OIDC), ("saml", BindingMethod.SAML)):
        fed = RegisteredFederatedSubject(
            person_id="person:bob",
            method=method,  # type: ignore[arg-type]
            issuer="https://idp.example.test",
            subject="sub-bob",
            audience="openadapt-regulated",
        )
        registry = MemoryPersonRegistry(federated_subjects=[fed])
        assertion = double.sign_federated(challenge_sha256=digest, registered=fed)
        binding = build_binding(
            person_id="person:bob",
            method=enum,
            approval_payload_sha256=digest,
            assertion=assertion,
            verified_at="2026-11-05T17:00:00Z",
        )
        assert (
            require_named_person_binding(
                profile="regulated",
                approval_payload_sha256=digest,
                expected_person_id="person:bob",
                binding=binding,
                assertion=assertion,
                registry=registry,
                verifier=double,
            )
            is None
        )


def test_assertion_repr_redacts_secrets(passkey, double) -> None:
    digest = approval_payload_digest(_payload())
    assertion = double.sign_webauthn(challenge_sha256=digest, registered=passkey)
    text = repr(assertion)
    assert "redacted" in text
    assert assertion.signature.hex() not in text
    assert assertion.credential_id.hex() not in text


def test_recorded_webauthn_fixture_round_trips(double) -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "Synthetic" in data["note"] or "NOT a live" in data["note"]
    reg = RegisteredPasskey(
        person_id=data["registered_passkey"]["person_id"],
        credential_id=bytes.fromhex(data["registered_passkey"]["credential_id_hex"]),
        public_key=bytes.fromhex(data["registered_passkey"]["public_key_hex"]),
        rp_id=data["registered_passkey"]["rp_id"],
    )
    assertion = WebAuthnAssertionMaterial(
        credential_id=bytes.fromhex(data["assertion_hex"]["credential_id"]),
        authenticator_data=bytes.fromhex(data["assertion_hex"]["authenticator_data"]),
        client_data_json=data["assertion_hex"]["client_data_json"].encode("utf-8"),
        signature=bytes.fromhex(data["assertion_hex"]["signature"]),
    )
    binding = NamedPersonBindingV1.model_validate(data["binding"])
    registry = MemoryPersonRegistry(passkeys=[reg])
    assert (
        require_named_person_binding(
            profile="regulated",
            approval_payload_sha256=data["approval_payload_sha256"],
            expected_person_id=reg.person_id,
            binding=binding,
            assertion=assertion,
            registry=registry,
            verifier=double,
        )
        is None
    )


def _request(store: ItemApprovalStore):
    return store.open_request(
        run_id="run-0001",
        workflow_name="referral",
        step_id="save",
        step_index=3,
        step_intent="Click 'Save'",
        mode=ApprovalMode.EACH_ITEM,
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        frame_png=FRAME,
        form_state=("dom", "sha256:" + "1" * 64),
    )


def test_answer_refusal_regulated_missing_binding_fail_closed(
    tmp_path, monkeypatch
) -> None:
    """Regulated profile refuses a person answer with no named-person binding."""

    from openadapt_flow.approval_mode import item_approval as mod

    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)
    answer = ItemApprovalAnswer(
        request_id=request.request_id,
        request_sha256=request.digest(),
        decision="approve",
        decided_by="human",
        route="desktop_run_it",
        approver_kind="unauthenticated",
        approver_id=None,
        auth_method="none",
        answered_at=request.created_at,
        attestation=None,
    )

    monkeypatch.setattr(mod, "attestation_refusal", lambda *a, **k: None)

    ctx = ApproverContext(
        execution_profile="regulated",
        key_dir=tmp_path / "keys",
    )
    code = answer_refusal(request, answer, policy=ctx)
    assert code == NamedPersonRefusal.MISSING_BINDING.value


def test_answer_refusal_regulated_accepts_verified_webauthn(
    tmp_path, monkeypatch, passkey, double, registry
) -> None:
    from openadapt_flow.approval_mode import item_approval as mod

    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)
    digest = request.digest()
    assertion = double.sign_webauthn(challenge_sha256=digest, registered=passkey)
    binding = build_binding(
        person_id=PERSON,
        method=BindingMethod.WEBAUTHN_PASSKEY,
        approval_payload_sha256=digest,
        assertion=assertion,
        rp_id=RP_ID,
        verified_at="2026-11-05T17:00:00Z",
    )
    answer = ItemApprovalAnswer(
        request_id=request.request_id,
        request_sha256=request.digest(),
        decision="approve",
        decided_by="human",
        route="desktop_run_it",
        approver_kind="unauthenticated",
        # Leave approver_id unset so stubbed attestation (signer="") does not
        # trip answer_fields_inconsistent; the binding still names the person.
        approver_id=None,
        auth_method="none",
        answered_at=request.created_at,
        attestation=None,
    )
    monkeypatch.setattr(mod, "attestation_refusal", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_person_refusal", lambda *a, **k: None)
    ctx = ApproverContext(
        execution_profile="regulated",
        key_dir=tmp_path / "keys",
        named_person_binding=binding,
        named_person_assertion=assertion,
        named_person_registry=registry,
        named_person_verifier=double,
    )
    assert answer_refusal(request, answer, policy=ctx) is None


def test_answer_refusal_demo_skips_named_person(tmp_path, monkeypatch) -> None:
    from openadapt_flow.approval_mode import item_approval as mod

    store = ItemApprovalStore(tmp_path / "run")
    request = _request(store)
    answer = ItemApprovalAnswer(
        request_id=request.request_id,
        request_sha256=request.digest(),
        decision="approve",
        decided_by="human",
        route="desktop_run_it",
        approver_kind="unauthenticated",
        approver_id=None,
        auth_method="none",
        answered_at=request.created_at,
        attestation=None,
    )
    monkeypatch.setattr(mod, "attestation_refusal", lambda *a, **k: None)
    ctx = ApproverContext(execution_profile="demo", key_dir=tmp_path / "keys")
    assert answer_refusal(request, answer, policy=ctx) is None


def _federated_pair(double: DeterministicPasskeyDouble, method: str):
    digest = approval_payload_digest(_payload())
    fed = RegisteredFederatedSubject(
        person_id="person:bob",
        method=method,  # type: ignore[arg-type]
        issuer="https://idp.example.test",
        subject="sub-bob",
        audience="openadapt-regulated",
    )
    registry = MemoryPersonRegistry(federated_subjects=[fed])
    assertion = double.sign_federated(challenge_sha256=digest, registered=fed)
    enum = BindingMethod.OIDC if method == "oidc" else BindingMethod.SAML
    binding = build_binding(
        person_id="person:bob",
        method=enum,
        approval_payload_sha256=digest,
        assertion=assertion,
        verified_at="2026-11-05T17:00:00Z",
    )
    return digest, fed, registry, assertion, binding, enum


@pytest.mark.parametrize("method", ["oidc", "saml"])
@pytest.mark.parametrize(
    "field",
    ["issuer", "subject_sha256", "token_or_assertion_sha256"],
)
def test_federated_binding_evidence_mismatch_refuses(
    double: DeterministicPasskeyDouble, method: str, field: str
) -> None:
    """Seal evidence must match the assertion just verified (Codex BLOCK #1981).

    A normally validated OIDC/SAML binding whose issuer, subject digest or
    raw-token/assertion digest was rewritten used to pass while the original
    assertion stayed valid. Recompute and compare every evidence field.
    """

    digest, _fed, registry, assertion, binding, enum = _federated_pair(double, method)
    assert (
        require_named_person_binding(
            profile="regulated",
            approval_payload_sha256=digest,
            expected_person_id="person:bob",
            binding=binding,
            assertion=assertion,
            registry=registry,
            verifier=double,
        )
        is None
    )

    evidence = binding.evidence
    if field == "issuer":
        mutated = evidence.model_copy(update={"issuer": "https://evil.example.test"})
    elif field == "subject_sha256":
        mutated = evidence.model_copy(update={"subject_sha256": "sha256:" + "a" * 64})
    else:
        digest_field = "token_sha256" if method == "oidc" else "assertion_sha256"
        mutated = evidence.model_copy(update={digest_field: "sha256:" + "b" * 64})

    tampered = binding.model_copy(update={"evidence": mutated})
    # Model validator must still accept the method/evidence pairing and the
    # nonce/InResponseTo == approval payload constraint.
    assert isinstance(
        tampered.evidence, OidcEvidenceV1 if method == "oidc" else SamlEvidenceV1
    )
    code = require_named_person_binding(
        profile="regulated",
        approval_payload_sha256=digest,
        expected_person_id="person:bob",
        binding=tampered,
        assertion=assertion,
        registry=registry,
        verifier=double,
    )
    assert code == NamedPersonRefusal.EVIDENCE_DIGEST_MISMATCH.value


def test_webauthn_browser_base64url_challenge_builds_binding(
    passkey: RegisteredPasskey, double: DeterministicPasskeyDouble, registry
) -> None:
    """Chromium encodes the 32-byte challenge as base64url (Codex BLOCK #1981).

    Prefixing the browser string with ``sha256:`` without decoding rejects a
    real virtual-authenticator assertion against the hex-digest schema.
    """

    digest = approval_payload_digest(_payload())
    hex_digest = digest.removeprefix("sha256:")
    raw_challenge = bytes.fromhex(hex_digest)
    b64url = base64.urlsafe_b64encode(raw_challenge).decode("ascii").rstrip("=")

    # Start from a double-signed assertion, then rewrite clientDataJSON to the
    # browser wire format and re-sign so the verifier still accepts it.
    base = double.sign_webauthn(challenge_sha256=digest, registered=passkey)
    client_data = json.dumps(
        {
            "type": "webauthn.get",
            "origin": f"https://{passkey.rp_id}",
            "challenge": b64url,
            "fixture": "openadapt.named-person.webauthn-browser-format/v1",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    import hashlib
    import hmac

    signature = hmac.new(
        passkey.public_key,
        double.DOMAIN_WEBAUTHN
        + hex_digest.encode("ascii")
        + base.authenticator_data
        + hashlib.sha256(client_data).digest(),
        hashlib.sha256,
    ).digest()
    assertion = WebAuthnAssertionMaterial(
        credential_id=base.credential_id,
        authenticator_data=base.authenticator_data,
        client_data_json=client_data,
        signature=signature,
        user_handle=base.user_handle,
    )
    binding = build_binding(
        person_id=PERSON,
        method=BindingMethod.WEBAUTHN_PASSKEY,
        approval_payload_sha256=digest,
        assertion=assertion,
        rp_id=RP_ID,
        verified_at="2026-11-05T17:00:00Z",
    )
    assert binding.evidence.challenge_sha256 == digest
    assert (
        require_named_person_binding(
            profile="regulated",
            approval_payload_sha256=digest,
            expected_person_id=PERSON,
            binding=binding,
            assertion=assertion,
            registry=registry,
            verifier=double,
        )
        is None
    )


def test_unexpected_verifier_exception_log_omits_assertion_text(
    passkey, registry, caplog
) -> None:
    """Unexpected verifier errors must not copy assertion-bearing text into logs."""

    class BoomVerifier:
        def verify_webauthn(self, **kwargs):  # noqa: ANN003
            # Synthetic control: exception text carries assertion material.
            raise RuntimeError(
                "provider blew up with assertion=" + kwargs["assertion"].signature.hex()
            )

        def verify_federated(self, **kwargs):  # noqa: ANN003
            raise AssertionError("federated path unused")

    digest = approval_payload_digest(_payload())
    double = DeterministicPasskeyDouble()
    assertion = double.sign_webauthn(challenge_sha256=digest, registered=passkey)
    binding = build_binding(
        person_id=PERSON,
        method=BindingMethod.WEBAUTHN_PASSKEY,
        approval_payload_sha256=digest,
        assertion=assertion,
        rp_id=RP_ID,
        verified_at="2026-11-05T17:00:00Z",
    )
    secret_hex = assertion.signature.hex()
    with caplog.at_level(
        logging.ERROR, logger="openadapt_flow.approval_mode.named_person_binding"
    ):
        code = require_named_person_binding(
            profile="regulated",
            approval_payload_sha256=digest,
            expected_person_id=PERSON,
            binding=binding,
            assertion=assertion,
            registry=registry,
            verifier=BoomVerifier(),  # type: ignore[arg-type]
        )
    assert code == NamedPersonRefusal.ASSERTION_INVALID.value
    messages = [r.getMessage() for r in caplog.records]
    joined = "\n".join(messages)
    assert any("type=RuntimeError" in m for m in messages)
    # caplog.text includes traceback text when logger.exception is used; that
    # path must not survive either.
    full = caplog.text
    for hay in (joined, full):
        assert secret_hex not in hay
        assert "provider blew up" not in hay
    for record in caplog.records:
        assert secret_hex not in record.getMessage()
        assert secret_hex not in str(record.msg)
        if record.args:
            assert secret_hex not in str(record.args)
        if record.exc_text:
            assert secret_hex not in record.exc_text
            assert "provider blew up" not in record.exc_text
