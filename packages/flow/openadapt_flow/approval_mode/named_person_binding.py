"""Bind a regulated approval to a named person (WebAuthn, OIDC or SAML).

Phone pairing and the local approver-roster Ed25519 keys identify a *device*
or a *registered key*, not an authenticated individual. For regulated audit
trails (21 CFR Part 11, HIPAA, SOC 2) each approval must also carry a
:class:`NamedPersonBindingV1`: a WebAuthn passkey assertion over the exact
approval payload hash, or an OIDC / SAML assertion from the organisation's
identity provider. The binding (person id + evidence digests) goes into the
Seal; raw assertion material never does.

Fail closed under the ``regulated`` profile: missing binding, payload
mismatch, unknown credential, failed assertion, or an unavailable verifier
are all refusals. ``demo`` never requires a binding. ``standard`` accepts a
verified binding when present but does not yet require one (later slices).

No production IdP credentials live here. Unit tests use
:class:`DeterministicPasskeyDouble` and recorded fixture shapes, clearly
labeled as test doubles — not a live WebAuthn ceremony.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    Literal,
    Optional,
    Protocol,
    Sequence,
    Union,
)

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

logger = logging.getLogger(__name__)

NAMED_PERSON_BINDING_SCHEMA: Literal["openadapt.named-person-binding/v1"] = (
    "openadapt.named-person-binding/v1"
)
SEAL_EXTENSION_KEY = "openadapt.named-person-binding/v1"

_OPAQUE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$"
_SHA256 = r"^sha256:[0-9a-f]{64}$"
_RP_ID = r"^[a-z0-9][a-z0-9.-]{0,253}$"
_ISO_Z = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"

ExecutionProfile = Literal["demo", "standard", "regulated"]


class BindingMethod(str, Enum):
    """How the named person was authenticated for this approval."""

    WEBAUTHN_PASSKEY = "webauthn_passkey"
    OIDC = "oidc"
    SAML = "saml"


class NamedPersonRefusal(str, Enum):
    """Closed reasons a regulated named-person check can return."""

    MISSING_BINDING = "named_person_binding_missing"
    MISSING_ASSERTION = "named_person_assertion_missing"
    PAYLOAD_MISMATCH = "named_person_payload_mismatch"
    PERSON_MISMATCH = "named_person_id_mismatch"
    CREDENTIAL_UNKNOWN = "named_person_credential_unknown"
    ASSERTION_INVALID = "named_person_assertion_invalid"
    METHOD_UNSUPPORTED = "named_person_method_unsupported"
    VERIFIER_UNAVAILABLE = "named_person_verifier_unavailable"
    REGISTRY_UNAVAILABLE = "named_person_registry_unavailable"
    EVIDENCE_DIGEST_MISMATCH = "named_person_evidence_digest_mismatch"
    ISSUER_UNTRUSTED = "named_person_issuer_untrusted"
    SUBJECT_MISMATCH = "named_person_subject_mismatch"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def approval_payload_digest(payload: Any) -> str:
    """Canonical sha256 of the exact approval payload the person asserted over.

    Matches the compact sorted-key JSON used elsewhere in Flow approvals.
    """

    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    raw = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return _sha256_bytes(raw)


class WebAuthnEvidenceV1(_Strict):
    """Public, PHI-free digests of one WebAuthn assertion (Seal-safe).

    Raw authenticatorData / clientDataJSON / signature bytes are *not* stored
    here and must never be logged. Verification sees them only in memory via
    :class:`WebAuthnAssertionMaterial`.
    """

    credential_id_sha256: StrictStr = Field(pattern=_SHA256)
    rp_id: StrictStr = Field(pattern=_RP_ID)
    user_handle_sha256: Optional[StrictStr] = Field(default=None, pattern=_SHA256)
    authenticator_data_sha256: StrictStr = Field(pattern=_SHA256)
    client_data_sha256: StrictStr = Field(pattern=_SHA256)
    signature_sha256: StrictStr = Field(pattern=_SHA256)
    #: Challenge the authenticator signed; must equal ``approval_payload_sha256``.
    challenge_sha256: StrictStr = Field(pattern=_SHA256)


class OidcEvidenceV1(_Strict):
    """Public digests of one OIDC ID-token claim set (Seal-safe)."""

    issuer: StrictStr = Field(min_length=1, max_length=512)
    subject_sha256: StrictStr = Field(pattern=_SHA256)
    audience_sha256: StrictStr = Field(pattern=_SHA256)
    token_sha256: StrictStr = Field(pattern=_SHA256)
    #: Nonce bound to the approval payload digest.
    nonce_sha256: StrictStr = Field(pattern=_SHA256)


class SamlEvidenceV1(_Strict):
    """Public digests of one SAML Response assertion (Seal-safe)."""

    issuer: StrictStr = Field(min_length=1, max_length=512)
    subject_sha256: StrictStr = Field(pattern=_SHA256)
    audience_sha256: StrictStr = Field(pattern=_SHA256)
    assertion_sha256: StrictStr = Field(pattern=_SHA256)
    #: InResponseTo / request id bound to the approval payload digest.
    in_response_to_sha256: StrictStr = Field(pattern=_SHA256)


class NamedPersonBindingV1(_Strict):
    """Who was authenticated for one approval, for the audit trail and Seal.

    PHI-free: opaque person ids and digests only. Never carries names, emails
    or raw assertion bytes.
    """

    schema_version: Literal["openadapt.named-person-binding/v1"] = (
        NAMED_PERSON_BINDING_SCHEMA
    )
    person_id: StrictStr = Field(pattern=_OPAQUE_ID)
    method: BindingMethod
    approval_payload_sha256: StrictStr = Field(pattern=_SHA256)
    evidence: Union[WebAuthnEvidenceV1, OidcEvidenceV1, SamlEvidenceV1]
    verified_at: StrictStr = Field(pattern=_ISO_Z)

    @model_validator(mode="after")
    def _method_matches_evidence(self) -> "NamedPersonBindingV1":
        if self.method is BindingMethod.WEBAUTHN_PASSKEY:
            if not isinstance(self.evidence, WebAuthnEvidenceV1):
                raise ValueError("webauthn_passkey needs WebAuthnEvidenceV1")
            if self.evidence.challenge_sha256 != self.approval_payload_sha256:
                raise ValueError("WebAuthn challenge must equal approval payload hash")
        elif self.method is BindingMethod.OIDC:
            if not isinstance(self.evidence, OidcEvidenceV1):
                raise ValueError("oidc needs OidcEvidenceV1")
            if self.evidence.nonce_sha256 != self.approval_payload_sha256:
                raise ValueError("OIDC nonce must equal approval payload hash")
        elif self.method is BindingMethod.SAML:
            if not isinstance(self.evidence, SamlEvidenceV1):
                raise ValueError("saml needs SamlEvidenceV1")
            if self.evidence.in_response_to_sha256 != self.approval_payload_sha256:
                raise ValueError("SAML InResponseTo must equal approval payload hash")
        return self


@dataclass(frozen=True)
class WebAuthnAssertionMaterial:
    """Raw WebAuthn assertion bytes held only in memory for verification.

    ``repr`` and ``str`` redact every field so logs and tracebacks never print
    assertion material.
    """

    credential_id: bytes
    authenticator_data: bytes
    client_data_json: bytes
    signature: bytes
    user_handle: bytes | None = None

    def __repr__(self) -> str:  # noqa: D105
        return (
            "WebAuthnAssertionMaterial(credential_id=<redacted>, "
            "authenticator_data=<redacted>, client_data_json=<redacted>, "
            "signature=<redacted>, user_handle=<redacted>)"
        )

    __str__ = __repr__

    def evidence_digests(self, *, rp_id: str) -> WebAuthnEvidenceV1:
        challenge = _client_data_challenge(self.client_data_json)
        if challenge is None:
            raise NamedPersonBindingError(
                NamedPersonRefusal.ASSERTION_INVALID,
                "WebAuthn clientDataJSON has no challenge",
            )
        return WebAuthnEvidenceV1(
            credential_id_sha256=_sha256_bytes(self.credential_id),
            rp_id=rp_id,
            user_handle_sha256=(
                _sha256_bytes(self.user_handle) if self.user_handle else None
            ),
            authenticator_data_sha256=_sha256_bytes(self.authenticator_data),
            client_data_sha256=_sha256_bytes(self.client_data_json),
            signature_sha256=_sha256_bytes(self.signature),
            challenge_sha256="sha256:" + challenge,
        )


@dataclass(frozen=True)
class FederatedAssertionMaterial:
    """Raw OIDC token or SAML response held only in memory for verification."""

    method: Literal["oidc", "saml"]
    issuer: str
    subject: str
    audience: str
    raw: bytes
    binding_value: str  # nonce or InResponseTo

    def __repr__(self) -> str:  # noqa: D105
        return (
            f"FederatedAssertionMaterial(method={self.method!r}, "
            "issuer=<redacted>, subject=<redacted>, audience=<redacted>, "
            "raw=<redacted>, binding_value=<redacted>)"
        )

    __str__ = __repr__

    def evidence_digests(
        self, *, approval_payload_sha256: str
    ) -> Union[OidcEvidenceV1, SamlEvidenceV1]:
        """Recompute Seal-safe digests from the in-memory assertion."""

        if self.method == "oidc":
            return OidcEvidenceV1(
                issuer=self.issuer,
                subject_sha256=_sha256_text(self.subject),
                audience_sha256=_sha256_text(self.audience),
                token_sha256=_sha256_bytes(self.raw),
                nonce_sha256=approval_payload_sha256,
            )
        return SamlEvidenceV1(
            issuer=self.issuer,
            subject_sha256=_sha256_text(self.subject),
            audience_sha256=_sha256_text(self.audience),
            assertion_sha256=_sha256_bytes(self.raw),
            in_response_to_sha256=approval_payload_sha256,
        )


AssertionMaterial = Union[WebAuthnAssertionMaterial, FederatedAssertionMaterial]


@dataclass(frozen=True)
class RegisteredPasskey:
    """One person's enrolled passkey in the relying-party registry."""

    person_id: str
    credential_id: bytes
    #: COSE / PEM / test-double public key material. Never a private key.
    public_key: bytes
    rp_id: str


@dataclass(frozen=True)
class RegisteredFederatedSubject:
    """One person's IdP subject for OIDC or SAML."""

    person_id: str
    method: Literal["oidc", "saml"]
    issuer: str
    subject: str
    audience: str


class PersonCredentialRegistry(Protocol):
    def passkey(self, credential_id: bytes) -> RegisteredPasskey | None: ...

    def federated(
        self, *, method: Literal["oidc", "saml"], issuer: str, subject: str
    ) -> RegisteredFederatedSubject | None: ...


class NamedPersonVerifier(Protocol):
    """Verifies raw assertion material against a registered credential."""

    def verify_webauthn(
        self,
        *,
        challenge_sha256: str,
        assertion: WebAuthnAssertionMaterial,
        registered: RegisteredPasskey,
    ) -> None:
        """Raise :class:`NamedPersonBindingError` on failure."""

    def verify_federated(
        self,
        *,
        challenge_sha256: str,
        assertion: FederatedAssertionMaterial,
        registered: RegisteredFederatedSubject,
    ) -> None:
        """Raise :class:`NamedPersonBindingError` on failure."""


class NamedPersonBindingError(Exception):
    def __init__(self, code: NamedPersonRefusal, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MemoryPersonRegistry:
    """In-memory registry for tests and offline runners."""

    passkeys: Sequence[RegisteredPasskey] = ()
    federated_subjects: Sequence[RegisteredFederatedSubject] = ()

    def passkey(self, credential_id: bytes) -> RegisteredPasskey | None:
        for entry in self.passkeys:
            if hmac.compare_digest(entry.credential_id, credential_id):
                return entry
        return None

    def federated(
        self, *, method: Literal["oidc", "saml"], issuer: str, subject: str
    ) -> RegisteredFederatedSubject | None:
        for entry in self.federated_subjects:
            if (
                entry.method == method
                and entry.issuer == issuer
                and entry.subject == subject
            ):
                return entry
        return None


def _normalize_challenge_digest(challenge: str) -> str | None:
    """Return the 64-char lowercase hex of a 32-byte WebAuthn challenge.

    Browsers put the challenge in ``clientDataJSON`` as base64url of the raw
    bytes (WebAuthn). Our deterministic test double also accepts the hex form
    of those same 32 bytes. Anything else is refused.
    """

    if not challenge:
        return None
    lowered = challenge.lower()
    if len(lowered) == 64 and all(c in "0123456789abcdef" for c in lowered):
        return lowered
    # WebAuthn CollectedClientData.challenge is base64url without padding.
    try:
        padded = challenge + ("=" * ((4 - len(challenge) % 4) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return None
    if len(raw) != 32:
        return None
    return raw.hex()


def _client_data_challenge(client_data_json: bytes) -> str | None:
    try:
        data = json.loads(client_data_json.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    challenge = data.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        return None
    return _normalize_challenge_digest(challenge)


def _challenge_hex(challenge_sha256: str) -> str:
    return challenge_sha256.removeprefix("sha256:")


class DeterministicPasskeyDouble:
    """Test-double WebAuthn / federated verifier (NOT a live ceremony).

    Uses HMAC-SHA256 over a domain-separated message so unit tests are
    deterministic without ``py_webauthn``, a virtual authenticator, or an IdP.
    Production slices replace this with a ``py_webauthn``-backed verifier.
    Fixture assertions built by :meth:`sign_webauthn` / :meth:`sign_federated`
    are labeled test doubles.
    """

    DOMAIN_WEBAUTHN = b"openadapt.named-person.webauthn-double/v1\x00"
    DOMAIN_FEDERATED = b"openadapt.named-person.federated-double/v1\x00"

    def sign_webauthn(
        self,
        *,
        challenge_sha256: str,
        registered: RegisteredPasskey,
        credential_id: bytes | None = None,
        user_handle: bytes | None = None,
    ) -> WebAuthnAssertionMaterial:
        """Build a fixture assertion the double will accept."""

        cred = credential_id if credential_id is not None else registered.credential_id
        challenge_hex = _challenge_hex(challenge_sha256)
        client_data = json.dumps(
            {
                "type": "webauthn.get",
                "origin": f"https://{registered.rp_id}",
                "challenge": challenge_hex,
                "fixture": "openadapt.named-person.webauthn-double/v1",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        # Minimal authenticator data: rpIdHash(32) + flags(1) + counter(4).
        rp_hash = hashlib.sha256(registered.rp_id.encode("utf-8")).digest()
        authenticator_data = rp_hash + bytes([0x01, 0x00, 0x00, 0x00, 0x01])
        signature = hmac.new(
            registered.public_key,
            self.DOMAIN_WEBAUTHN
            + challenge_hex.encode("ascii")
            + authenticator_data
            + hashlib.sha256(client_data).digest(),
            hashlib.sha256,
        ).digest()
        return WebAuthnAssertionMaterial(
            credential_id=cred,
            authenticator_data=authenticator_data,
            client_data_json=client_data,
            signature=signature,
            user_handle=user_handle,
        )

    def sign_federated(
        self,
        *,
        challenge_sha256: str,
        registered: RegisteredFederatedSubject,
    ) -> FederatedAssertionMaterial:
        """Build a fixture OIDC/SAML assertion the double will accept.

        The MAC key is the registered audience string. That is a test-double
        convention only — never a production IdP secret.
        """

        binding = _challenge_hex(challenge_sha256)
        body = {
            "iss": registered.issuer,
            "sub": registered.subject,
            "aud": registered.audience,
            ("nonce" if registered.method == "oidc" else "in_response_to"): binding,
            "fixture": "openadapt.named-person.federated-double/v1",
        }
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        mac = hmac.new(
            registered.audience.encode("utf-8"),
            self.DOMAIN_FEDERATED + raw,
            hashlib.sha256,
        ).hexdigest()
        return FederatedAssertionMaterial(
            method=registered.method,
            issuer=registered.issuer,
            subject=registered.subject,
            audience=registered.audience,
            raw=raw + b"." + mac.encode("ascii"),
            binding_value=binding,
        )

    def verify_webauthn(
        self,
        *,
        challenge_sha256: str,
        assertion: WebAuthnAssertionMaterial,
        registered: RegisteredPasskey,
    ) -> None:
        if not hmac.compare_digest(assertion.credential_id, registered.credential_id):
            raise NamedPersonBindingError(
                NamedPersonRefusal.CREDENTIAL_UNKNOWN,
                "assertion credential is not the registered passkey",
            )
        challenge = _client_data_challenge(assertion.client_data_json)
        if challenge != _challenge_hex(challenge_sha256):
            raise NamedPersonBindingError(
                NamedPersonRefusal.ASSERTION_INVALID,
                "WebAuthn clientData challenge does not match approval payload",
            )
        expected = hmac.new(
            registered.public_key,
            self.DOMAIN_WEBAUTHN
            + _challenge_hex(challenge_sha256).encode("ascii")
            + assertion.authenticator_data
            + hashlib.sha256(assertion.client_data_json).digest(),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(assertion.signature, expected):
            raise NamedPersonBindingError(
                NamedPersonRefusal.ASSERTION_INVALID,
                "WebAuthn assertion signature failed (test double)",
            )

    def verify_federated(
        self,
        *,
        challenge_sha256: str,
        assertion: FederatedAssertionMaterial,
        registered: RegisteredFederatedSubject,
    ) -> None:
        if assertion.method != registered.method:
            raise NamedPersonBindingError(
                NamedPersonRefusal.METHOD_UNSUPPORTED,
                "federated method does not match the registered subject",
            )
        if assertion.issuer != registered.issuer:
            raise NamedPersonBindingError(
                NamedPersonRefusal.ISSUER_UNTRUSTED,
                "federated issuer is not trusted for this person",
            )
        if assertion.subject != registered.subject:
            raise NamedPersonBindingError(
                NamedPersonRefusal.SUBJECT_MISMATCH,
                "federated subject does not match the registered person",
            )
        if assertion.audience != registered.audience:
            raise NamedPersonBindingError(
                NamedPersonRefusal.ASSERTION_INVALID,
                "federated audience mismatch",
            )
        if assertion.binding_value != _challenge_hex(challenge_sha256):
            raise NamedPersonBindingError(
                NamedPersonRefusal.PAYLOAD_MISMATCH,
                "federated nonce/InResponseTo does not match approval payload",
            )
        try:
            raw, mac = assertion.raw.rsplit(b".", 1)
        except ValueError as exc:
            raise NamedPersonBindingError(
                NamedPersonRefusal.ASSERTION_INVALID,
                "federated assertion fixture is malformed",
            ) from exc
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise NamedPersonBindingError(
                NamedPersonRefusal.ASSERTION_INVALID,
                "federated assertion body is not JSON",
            ) from exc
        key_field = "nonce" if registered.method == "oidc" else "in_response_to"
        if body.get(key_field) != _challenge_hex(challenge_sha256):
            raise NamedPersonBindingError(
                NamedPersonRefusal.PAYLOAD_MISMATCH,
                "federated body binding field mismatch",
            )
        expected = (
            hmac.new(
                registered.audience.encode("utf-8"),
                self.DOMAIN_FEDERATED + raw,
                hashlib.sha256,
            )
            .hexdigest()
            .encode("ascii")
        )
        if not hmac.compare_digest(mac, expected):
            raise NamedPersonBindingError(
                NamedPersonRefusal.ASSERTION_INVALID,
                "federated assertion MAC failed (test double)",
            )


def build_binding(
    *,
    person_id: str,
    method: BindingMethod,
    approval_payload_sha256: str,
    assertion: AssertionMaterial,
    rp_id: str | None = None,
    verified_at: str,
) -> NamedPersonBindingV1:
    """Build a Seal-safe binding from in-memory assertion material."""

    evidence: Union[WebAuthnEvidenceV1, OidcEvidenceV1, SamlEvidenceV1]
    if isinstance(assertion, WebAuthnAssertionMaterial):
        if method is not BindingMethod.WEBAUTHN_PASSKEY:
            raise NamedPersonBindingError(
                NamedPersonRefusal.METHOD_UNSUPPORTED,
                "WebAuthn material needs method=webauthn_passkey",
            )
        if not rp_id:
            raise NamedPersonBindingError(
                NamedPersonRefusal.ASSERTION_INVALID,
                "WebAuthn binding needs an rp_id",
            )
        evidence = assertion.evidence_digests(rp_id=rp_id)
        if evidence.challenge_sha256 != approval_payload_sha256:
            raise NamedPersonBindingError(
                NamedPersonRefusal.PAYLOAD_MISMATCH,
                "WebAuthn challenge does not match approval payload hash",
            )
    else:
        if method not in {BindingMethod.OIDC, BindingMethod.SAML}:
            raise NamedPersonBindingError(
                NamedPersonRefusal.METHOD_UNSUPPORTED,
                "federated material needs method=oidc or saml",
            )
        if assertion.method != ("oidc" if method is BindingMethod.OIDC else "saml"):
            raise NamedPersonBindingError(
                NamedPersonRefusal.METHOD_UNSUPPORTED,
                "federated material method does not match binding method",
            )
        evidence = assertion.evidence_digests(
            approval_payload_sha256=approval_payload_sha256,
        )
    return NamedPersonBindingV1(
        person_id=person_id,
        method=method,
        approval_payload_sha256=approval_payload_sha256,
        evidence=evidence,
        verified_at=verified_at,
    )


def seal_extension(binding: NamedPersonBindingV1) -> dict[str, Any]:
    """Seal ``extensions`` entry: identity + evidence digests, never raw bytes."""

    return {
        "schema": SEAL_EXTENSION_KEY,
        "binding": binding.model_dump(mode="json"),
    }


def require_named_person_binding(
    *,
    profile: ExecutionProfile | None,
    approval_payload_sha256: str,
    expected_person_id: str | None,
    binding: NamedPersonBindingV1 | None,
    assertion: AssertionMaterial | None,
    registry: PersonCredentialRegistry | None,
    verifier: NamedPersonVerifier | None,
) -> Optional[str]:
    """Return a closed refusal code, or ``None`` when the check passes.

    Under ``regulated``, a missing or unverifiable named person always
    refuses. Under ``demo``, the check is skipped. Under ``standard``, a
    present binding is verified fail-closed, but absence is still allowed
    in this first slice.
    """

    if profile is None or profile == "demo":
        return None
    required = profile == "regulated"
    if binding is None:
        return NamedPersonRefusal.MISSING_BINDING.value if required else None
    if assertion is None:
        return NamedPersonRefusal.MISSING_ASSERTION.value
    if registry is None:
        return NamedPersonRefusal.REGISTRY_UNAVAILABLE.value
    if verifier is None:
        return NamedPersonRefusal.VERIFIER_UNAVAILABLE.value
    if binding.approval_payload_sha256 != approval_payload_sha256:
        return NamedPersonRefusal.PAYLOAD_MISMATCH.value
    if expected_person_id is not None and binding.person_id != expected_person_id:
        return NamedPersonRefusal.PERSON_MISMATCH.value

    try:
        if binding.method is BindingMethod.WEBAUTHN_PASSKEY:
            if not isinstance(assertion, WebAuthnAssertionMaterial):
                return NamedPersonRefusal.METHOD_UNSUPPORTED.value
            if not isinstance(binding.evidence, WebAuthnEvidenceV1):
                return NamedPersonRefusal.METHOD_UNSUPPORTED.value
            expected_evidence = assertion.evidence_digests(
                rp_id=binding.evidence.rp_id,
            )
            if expected_evidence != binding.evidence:
                return NamedPersonRefusal.EVIDENCE_DIGEST_MISMATCH.value
            registered = registry.passkey(assertion.credential_id)
            if registered is None:
                return NamedPersonRefusal.CREDENTIAL_UNKNOWN.value
            if registered.person_id != binding.person_id:
                return NamedPersonRefusal.PERSON_MISMATCH.value
            verifier.verify_webauthn(
                challenge_sha256=approval_payload_sha256,
                assertion=assertion,
                registered=registered,
            )
        elif binding.method in {BindingMethod.OIDC, BindingMethod.SAML}:
            if not isinstance(assertion, FederatedAssertionMaterial):
                return NamedPersonRefusal.METHOD_UNSUPPORTED.value
            method: Literal["oidc", "saml"] = (
                "oidc" if binding.method is BindingMethod.OIDC else "saml"
            )
            expected_type = OidcEvidenceV1 if method == "oidc" else SamlEvidenceV1
            if not isinstance(binding.evidence, expected_type):
                return NamedPersonRefusal.METHOD_UNSUPPORTED.value
            if assertion.method != method:
                return NamedPersonRefusal.METHOD_UNSUPPORTED.value
            recomputed = assertion.evidence_digests(
                approval_payload_sha256=approval_payload_sha256,
            )
            if recomputed != binding.evidence:
                return NamedPersonRefusal.EVIDENCE_DIGEST_MISMATCH.value
            registered_f = registry.federated(
                method=method, issuer=assertion.issuer, subject=assertion.subject
            )
            if registered_f is None:
                return NamedPersonRefusal.CREDENTIAL_UNKNOWN.value
            if registered_f.person_id != binding.person_id:
                return NamedPersonRefusal.PERSON_MISMATCH.value
            verifier.verify_federated(
                challenge_sha256=approval_payload_sha256,
                assertion=assertion,
                registered=registered_f,
            )
        else:
            return NamedPersonRefusal.METHOD_UNSUPPORTED.value
    except NamedPersonBindingError as exc:
        return exc.code.value
    except Exception as exc:
        # Never turn an unexpected verifier crash into a pass, and never copy
        # assertion-bearing exception text into logs (material repr is redacted;
        # str(exc) is not).
        logger.error(
            "named-person verifier failed closed; type=%s",
            type(exc).__name__,
        )
        return NamedPersonRefusal.ASSERTION_INVALID.value
    return None


__all__ = [
    "NAMED_PERSON_BINDING_SCHEMA",
    "SEAL_EXTENSION_KEY",
    "AssertionMaterial",
    "BindingMethod",
    "DeterministicPasskeyDouble",
    "ExecutionProfile",
    "FederatedAssertionMaterial",
    "MemoryPersonRegistry",
    "NamedPersonBindingError",
    "NamedPersonBindingV1",
    "NamedPersonRefusal",
    "NamedPersonVerifier",
    "OidcEvidenceV1",
    "PersonCredentialRegistry",
    "RegisteredFederatedSubject",
    "RegisteredPasskey",
    "SamlEvidenceV1",
    "WebAuthnAssertionMaterial",
    "WebAuthnEvidenceV1",
    "approval_payload_digest",
    "build_binding",
    "require_named_person_binding",
    "seal_extension",
]
