"""The key hierarchy and signature chains behind fleet patches.

Three levels, each Ed25519 over a domain-separated canonical JSON payload
(the same construction the qualification permits use):

1. The **fleet root** is an offline key. It signs only
   :class:`IntermediateCertificate` objects, and only in a custody ceremony
   where at least M of N named custodians approve
   (:class:`OfflineRootCustody`). Receivers pin its public key.
2. An **intermediate** key signs short-lived :class:`SignerCertificate`
   objects. A signer certificate is valid for at most
   :data:`MAX_SIGNER_VALIDITY` (7 days), so a lost signer key stops working
   within a week even if no one revokes it.
3. A **signer** key signs one kind of document, named by its purpose:

   * ``contributor``: a tenant's patch contribution and its fingerprint
     registrations. These go to the fleet hub only and are never
     distributed;
   * ``distribution``: a patch the hub re-signed for receivers;
   * ``status``: revocation lists and kill switches
     (:mod:`openadapt_flow.fleet.status`).

Key material is held behind :class:`KeyCustody`. :class:`SoftwareKeyCustody`
keeps an Ed25519 key in process, for tests, the simulator, and short-lived
signer keys. A production root and intermediate live in an HSM behind the
same interface; that adapter is not part of this package. The custody
procedure is in ``docs/FLEET_DRIFT_PATCHES.md``.

A receiving tenant pins the root public keys it trusts in a
:class:`FleetTrust`. It never trusts a key because a patch or a feed carries
it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from typing import Final, Literal, Optional, Protocol, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.fleet.fingerprint import APP_FAMILY_PATTERN
from openadapt_flow.qualification_admission_v2 import canonical_json

INTERMEDIATE_DOMAIN: Final[bytes] = b"openadapt.fleet.intermediate-certificate/v1\x00"
CERTIFICATE_DOMAIN: Final[bytes] = b"openadapt.fleet.signer-certificate/v2\x00"

#: Longest validity of a signer certificate.
MAX_SIGNER_VALIDITY: Final[timedelta] = timedelta(days=7)
#: Longest validity of an intermediate certificate.
MAX_INTERMEDIATE_VALIDITY: Final[timedelta] = timedelta(days=400)
#: Clock skew tolerated on ``not_before``.
CLOCK_SKEW: Final[timedelta] = timedelta(minutes=5)

Purpose = Literal["contributor", "distribution", "status"]
PURPOSES: Final[tuple[str, ...]] = ("contributor", "distribution", "status")
SUBJECT_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9-]{2,63}$"
_KEY_ID = r"^[a-f0-9]{32}$"
_CUSTODIAN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


class PatchSignatureError(ValueError):
    """A certificate or signature failed. The document is refused."""


class CustodyCeremonyError(PatchSignatureError):
    """The root custody quorum was not met. Nothing was signed."""


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


def public_key_b64(
    key: Ed25519PrivateKey | Ed25519PublicKey,  # gitleaks:allow (type annotation)
) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return b64encode(raw).decode("ascii")


def key_id(public_b64: str) -> str:
    return hashlib.sha256(b64decode(public_b64, validate=True)).hexdigest()[:32]


def _load_public(value: str) -> Ed25519PublicKey:
    try:
        raw = b64decode(value, validate=True)
    except ValueError as exc:
        raise PatchSignatureError("public key is not base64") from exc
    if len(raw) != 32:
        raise PatchSignatureError("public key is not an Ed25519 key")
    return Ed25519PublicKey.from_public_bytes(raw)


class KeyCustody(Protocol):
    """Somewhere a private key lives. Callers see only sign and the public key."""

    def public_key_b64(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


class SoftwareKeyCustody:
    """An Ed25519 key held in this process.

    For tests, the simulator, and short-lived signer keys. A fleet root or
    intermediate must not be held this way in production.
    """

    def __init__(self, key: Optional[Ed25519PrivateKey] = None) -> None:
        self._key = key or Ed25519PrivateKey.generate()

    @classmethod
    def generate(cls) -> "SoftwareKeyCustody":
        return cls(Ed25519PrivateKey.generate())

    def public_key_b64(self) -> str:
        return public_key_b64(self._key)

    @property
    def key_id(self) -> str:
        return key_id(self.public_key_b64())

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)


def _custody_key_id(custody: KeyCustody) -> str:
    return key_id(custody.public_key_b64())


def sign_document(custody: KeyCustody, domain: bytes, payload: BaseModel) -> str:
    return b64encode(custody.sign(domain + canonical_json(payload))).decode("ascii")


def _signature(value: str) -> bytes:
    try:
        raw = b64decode(value, validate=True)
    except ValueError as exc:
        raise PatchSignatureError("signature is not base64") from exc
    if len(raw) != 64:
        raise PatchSignatureError("signature has the wrong length")
    return raw


def verify_document(
    public_b64: str, domain: bytes, payload: BaseModel, signature: str, what: str
) -> None:
    try:
        _load_public(public_b64).verify(
            _signature(signature), domain + canonical_json(payload)
        )
    except InvalidSignature as exc:
        raise PatchSignatureError(f"{what} signature is invalid") from exc


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PatchSignatureError("certificate time is not ISO 8601") from exc
    if parsed.tzinfo is None:
        raise PatchSignatureError("certificate time has no timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _families_ok(value: tuple[str, ...]) -> tuple[str, ...]:
    if list(value) != sorted(set(value)):
        raise ValueError("app families must be sorted and unique")
    for family in value:
        if not re.match(APP_FAMILY_PATTERN, family):
            raise ValueError(f"invalid app family {family!r}")
    return value


# --------------------------------------------------------------------------- #
# Certificates
# --------------------------------------------------------------------------- #


class IntermediateCertificatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.intermediate-certificate/v1"] = (
        "openadapt.fleet.intermediate-certificate/v1"
    )
    intermediate_public_key: str = Field(min_length=44, max_length=44)
    purposes: tuple[Purpose, ...] = Field(min_length=1, max_length=3)
    not_before: str = Field(min_length=20, max_length=64)
    not_after: str = Field(min_length=20, max_length=64)
    root_key_id: str = Field(pattern=_KEY_ID)
    #: The custody ceremony that produced this signature, and who approved.
    ceremony_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    threshold: int = Field(ge=2, le=16)
    approvals: tuple[str, ...] = Field(min_length=2, max_length=16)

    @field_validator("purposes")
    @classmethod
    def _purposes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if list(value) != sorted(set(value)):
            raise ValueError("purposes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _quorum(self) -> "IntermediateCertificatePayload":
        if list(self.approvals) != sorted(set(self.approvals)):
            raise ValueError("approvals must be sorted and unique")
        if len(self.approvals) < self.threshold:
            raise ValueError("fewer approvals than the custody threshold")
        return self


class IntermediateCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: IntermediateCertificatePayload
    signature: str = Field(min_length=1, max_length=128)

    @property
    def intermediate_key_id(self) -> str:
        return key_id(self.payload.intermediate_public_key)


class SignerCertificatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.signer-certificate/v2"] = (
        "openadapt.fleet.signer-certificate/v2"
    )
    serial: str = Field(pattern=_KEY_ID)
    purpose: Purpose
    #: Who holds the key: a tenant id for ``contributor`` (seen by the hub
    #: only), the hub's own id for ``distribution`` and ``status``.
    subject: str = Field(pattern=SUBJECT_PATTERN)
    signer_public_key: str = Field(min_length=44, max_length=44)
    app_families: tuple[str, ...] = Field(default=(), max_length=64)
    not_before: str = Field(min_length=20, max_length=64)
    not_after: str = Field(min_length=20, max_length=64)
    intermediate_key_id: str = Field(pattern=_KEY_ID)

    @field_validator("app_families")
    @classmethod
    def _families(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _families_ok(value)

    @model_validator(mode="after")
    def _shape(self) -> "SignerCertificatePayload":
        if self.purpose != "status" and not self.app_families:
            raise ValueError("a patch signer certificate names its app families")
        start, end = _utc(self.not_before), _utc(self.not_after)
        if end <= start:
            raise ValueError("certificate ends before it starts")
        if end - start > MAX_SIGNER_VALIDITY:
            raise ValueError("a signer certificate is valid for at most 7 days")
        return self


class SignerCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: SignerCertificatePayload
    signature: str = Field(min_length=1, max_length=128)

    @property
    def signer_key_id(self) -> str:
        return key_id(self.payload.signer_public_key)


class CertificateChain(BaseModel):
    """Intermediate certificate plus signer certificate. The root is pinned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intermediate: IntermediateCertificate
    signer: SignerCertificate

    @property
    def root_key_id(self) -> str:
        return self.intermediate.payload.root_key_id

    @property
    def intermediate_key_id(self) -> str:
        return self.intermediate.intermediate_key_id

    @property
    def signer_key_id(self) -> str:
        return self.signer.signer_key_id

    @property
    def serial(self) -> str:
        return self.signer.payload.serial


class OfflineRootCustody:
    """The fleet root key, usable only with an M-of-N custodian quorum.

    In production ``key`` is an offline HSM. The ceremony (who holds which
    share, how the HSM is brought online, what is logged) is a written
    procedure in ``docs/FLEET_DRIFT_PATCHES.md``. This class enforces the part
    code can enforce: the root signs nothing but intermediate certificates,
    and only when at least ``threshold`` distinct, named custodians from the
    roster approve. The approvals are recorded in the certificate.
    """

    def __init__(
        self, key: KeyCustody, *, custodians: Sequence[str], threshold: int
    ) -> None:
        roster = tuple(sorted(set(custodians)))
        if len(roster) != len(custodians):
            raise ValueError("custodian names must be unique")
        if any(not _CUSTODIAN.match(name) for name in roster):
            raise ValueError("custodian names are lowercase ids")
        if threshold < 2 or threshold > len(roster):
            raise ValueError("the threshold must be at least 2 and at most N")
        self._key = key
        self.custodians = roster
        self.threshold = threshold

    def public_key_b64(self) -> str:
        return self._key.public_key_b64()

    @property
    def key_id(self) -> str:
        return _custody_key_id(self._key)

    def issue_intermediate(
        self,
        intermediate_public_key: str,
        *,
        purposes: Sequence[str],
        not_before: datetime,
        not_after: datetime,
        approvals: Sequence[str],
        ceremony_id: str,
    ) -> IntermediateCertificate:
        approved = tuple(sorted(set(approvals)))
        unknown = [name for name in approved if name not in self.custodians]
        if unknown:
            raise CustodyCeremonyError("an approval is not from a listed custodian")
        if len(approved) < self.threshold:
            raise CustodyCeremonyError(
                f"{len(approved)} of {self.threshold} required custodian "
                "approvals; the root signs nothing"
            )
        if not_after - not_before > MAX_INTERMEDIATE_VALIDITY:
            raise CustodyCeremonyError("intermediate validity is too long")
        payload = IntermediateCertificatePayload(
            intermediate_public_key=intermediate_public_key,
            purposes=tuple(sorted(set(purposes))),  # type: ignore[arg-type]
            not_before=_iso(not_before),
            not_after=_iso(not_after),
            root_key_id=self.key_id,
            ceremony_id=ceremony_id,
            threshold=self.threshold,
            approvals=approved,
        )
        return IntermediateCertificate(
            payload=payload,
            signature=sign_document(self._key, INTERMEDIATE_DOMAIN, payload),
        )


def issue_signer_certificate(
    intermediate: KeyCustody,
    intermediate_certificate: IntermediateCertificate,
    *,
    purpose: str,
    subject: str,
    signer_public_key: str,
    app_families: Sequence[str] = (),
    not_before: datetime,
    not_after: datetime,
    serial: Optional[str] = None,
) -> CertificateChain:
    """Sign a short-lived signer certificate with an intermediate key."""
    if intermediate.public_key_b64() != (
        intermediate_certificate.payload.intermediate_public_key
    ):
        raise PatchSignatureError("the intermediate key does not match its certificate")
    if purpose not in intermediate_certificate.payload.purposes:
        raise PatchSignatureError(
            f"this intermediate may not issue {purpose} certificates"
        )
    payload = SignerCertificatePayload(
        serial=serial or secrets.token_hex(16),
        purpose=purpose,  # type: ignore[arg-type]
        subject=subject,
        signer_public_key=signer_public_key,
        app_families=tuple(sorted(set(app_families))),
        not_before=_iso(not_before),
        not_after=_iso(not_after),
        intermediate_key_id=intermediate_certificate.intermediate_key_id,
    )
    return CertificateChain(
        intermediate=intermediate_certificate,
        signer=SignerCertificate(
            payload=payload,
            signature=sign_document(intermediate, CERTIFICATE_DOMAIN, payload),
        ),
    )


# --------------------------------------------------------------------------- #
# Trust and verification
# --------------------------------------------------------------------------- #


class FleetTrust(BaseModel):
    """What one tenant trusts. Configured locally; never read from a feed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: key id -> base64 Ed25519 public key of each trusted fleet root.
    roots: dict[str, str] = Field(min_length=1)
    #: Local revocations, in addition to the signed revocation list.
    revoked_key_ids: frozenset[str] = frozenset()
    revoked_serials: frozenset[str] = frozenset()

    @classmethod
    def pin(cls, *roots: KeyCustody | OfflineRootCustody) -> "FleetTrust":
        keys = {key_id(r.public_key_b64()): r.public_key_b64() for r in roots}
        return cls(roots=keys)


def revocation_reason(
    *,
    root_key_id: str,
    intermediate_key_id: str,
    signer_key_id: str,
    serial: str,
    revoked_key_ids: frozenset[str] | set[str],
    revoked_serials: frozenset[str] | set[str],
) -> Optional[str]:
    """Why a chain, given by its key ids and serial, is revoked, or None."""
    if root_key_id in revoked_key_ids:
        return "the fleet root key is revoked"
    if intermediate_key_id in revoked_key_ids:
        return "the intermediate certificate is revoked"
    if signer_key_id in revoked_key_ids or serial in revoked_serials:
        return "the signer certificate is revoked"
    return None


def chain_revocation_reason(
    chain: CertificateChain,
    revoked_key_ids: frozenset[str] | set[str],
    revoked_serials: frozenset[str] | set[str],
) -> Optional[str]:
    return revocation_reason(
        root_key_id=chain.root_key_id,
        intermediate_key_id=chain.intermediate_key_id,
        signer_key_id=chain.signer_key_id,
        serial=chain.serial,
        revoked_key_ids=revoked_key_ids,
        revoked_serials=revoked_serials,
    )


def verify_chain(
    chain: CertificateChain,
    trust: FleetTrust,
    *,
    purpose: str,
    app_family: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """Refuse unless pinned root -> intermediate -> signer all hold for ``purpose``."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    inter = chain.intermediate.payload
    root = trust.roots.get(inter.root_key_id)
    if root is None or key_id(root) != inter.root_key_id:
        raise PatchSignatureError("the chain does not lead to a trusted fleet root")
    verify_document(
        root, INTERMEDIATE_DOMAIN, inter, chain.intermediate.signature, "intermediate"
    )
    if not (_utc(inter.not_before) - CLOCK_SKEW <= current < _utc(inter.not_after)):
        raise PatchSignatureError("the intermediate certificate is not valid now")
    if _utc(inter.not_after) - _utc(inter.not_before) > MAX_INTERMEDIATE_VALIDITY:
        raise PatchSignatureError("the intermediate certificate is valid for too long")
    if purpose not in inter.purposes:
        raise PatchSignatureError(
            f"the intermediate may not issue {purpose} certificates"
        )

    signer = chain.signer.payload
    if signer.intermediate_key_id != chain.intermediate_key_id:
        raise PatchSignatureError("the signer certificate names another intermediate")
    verify_document(
        inter.intermediate_public_key,
        CERTIFICATE_DOMAIN,
        signer,
        chain.signer.signature,
        "signer certificate",
    )
    if not (_utc(signer.not_before) - CLOCK_SKEW <= current < _utc(signer.not_after)):
        raise PatchSignatureError("the signer certificate is not valid now")
    if signer.purpose != purpose:
        raise PatchSignatureError(
            f"a {signer.purpose} certificate cannot sign a {purpose} document"
        )
    if app_family is not None and app_family not in signer.app_families:
        raise PatchSignatureError(
            "the certificate does not cover this application family"
        )
    reason = chain_revocation_reason(
        chain, trust.revoked_key_ids, trust.revoked_serials
    )
    if reason is not None:
        raise PatchSignatureError(reason)


__all__ = [
    "CLOCK_SKEW",
    "CertificateChain",
    "CustodyCeremonyError",
    "FleetTrust",
    "IntermediateCertificate",
    "IntermediateCertificatePayload",
    "KeyCustody",
    "MAX_INTERMEDIATE_VALIDITY",
    "MAX_SIGNER_VALIDITY",
    "OfflineRootCustody",
    "PURPOSES",
    "PatchSignatureError",
    "SignerCertificate",
    "SignerCertificatePayload",
    "SoftwareKeyCustody",
    "chain_revocation_reason",
    "issue_signer_certificate",
    "key_id",
    "public_key_b64",
    "revocation_reason",
    "sign_document",
    "verify_chain",
    "verify_document",
]
