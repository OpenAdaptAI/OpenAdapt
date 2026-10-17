"""Signed fleet status: the revocation list and kill switches.

Both are signed by a ``status`` signer certificate (see
:mod:`openadapt_flow.fleet.signing`) and travel in the patch feed.

* A :class:`SignedRevocationList` names revoked intermediate and signer keys
  and signer certificate serials. Each list says when it was issued and when
  the next one is due (``next_update``, at most 24 hours later).
* A :class:`SignedKillSwitch` can turn off auto-promotion for every tenant
  and can name patch ids every tenant must roll back. The entry with the
  highest sequence number decides whether auto-promotion is off; rollback ids
  accumulate across all entries.

:func:`read_fleet_status` reads both and never raises. It fails closed: an
unreachable, missing, unverifiable, or stale revocation list, or a kill-switch
entry that does not verify, each produce a reason, and any reason stops
auto-promotion (:mod:`openadapt_flow.fleet.policy`). A person can still
approve the patch through the ordinary repair lifecycle.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.fleet.signing import (
    CLOCK_SKEW,
    CertificateChain,
    FleetTrust,
    KeyCustody,
    PatchSignatureError,
    chain_revocation_reason,
    sign_document,
    verify_chain,
    verify_document,
)
from openadapt_flow.qualification_admission_v2 import canonical_json

REVOCATION_DOMAIN: Final[bytes] = b"openadapt.fleet.revocation-list/v1\x00"
KILL_SWITCH_DOMAIN: Final[bytes] = b"openadapt.fleet.kill-switch/v1\x00"
#: Longest gap between ``issued_at`` and ``next_update``.
MAX_REVOCATION_LIST_VALIDITY: Final[timedelta] = timedelta(hours=24)
#: A tenant refuses a list older than this, whatever its ``next_update``.
DEFAULT_MAX_STATUS_AGE: Final[timedelta] = timedelta(hours=24)
_SHA256 = r"^[a-f0-9]{64}$"
_KEY_ID = r"^[a-f0-9]{32}$"


class FleetStatusUnavailable(RuntimeError):
    """The feed could not serve a status document."""


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time has no timezone")
    return parsed.astimezone(timezone.utc)


def _sorted_unique(value: tuple[str, ...], what: str) -> tuple[str, ...]:
    if list(value) != sorted(set(value)):
        raise ValueError(f"{what} must be sorted and unique")
    return value


class RevocationListPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.revocation-list/v1"] = (
        "openadapt.fleet.revocation-list/v1"
    )
    sequence: int = Field(ge=1)
    issued_at: str = Field(min_length=20, max_length=64)
    next_update: str = Field(min_length=20, max_length=64)
    revoked_key_ids: tuple[str, ...] = Field(default=(), max_length=10_000)
    revoked_serials: tuple[str, ...] = Field(default=(), max_length=10_000)

    @field_validator("revoked_key_ids", "revoked_serials")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if any(not re.match(_KEY_ID, v) for v in value):
            raise ValueError("revoked ids are 32 hex characters")
        return _sorted_unique(value, "revoked ids")

    @model_validator(mode="after")
    def _window(self) -> "RevocationListPayload":
        start, end = _utc(self.issued_at), _utc(self.next_update)
        if not start < end <= start + MAX_REVOCATION_LIST_VALIDITY:
            raise ValueError("next_update must fall within 24 hours of issued_at")
        return self


class SignedRevocationList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: RevocationListPayload
    signature: str = Field(min_length=1, max_length=128)
    chain: CertificateChain


KillReason = Literal[
    "contributor_revoked",
    "key_compromise",
    "bad_patch",
    "incident",
    "resume",
]


class KillSwitchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.kill-switch/v1"] = (
        "openadapt.fleet.kill-switch/v1"
    )
    sequence: int = Field(ge=1)
    issued_at: str = Field(min_length=20, max_length=64)
    #: True turns auto-promotion off on every tenant until a later entry
    #: (higher sequence) turns it back on.
    disable_auto_promotion: bool
    #: Patch ids every tenant rolls back (canary or active) or rejects.
    rollback_patch_ids: tuple[str, ...] = Field(default=(), max_length=1000)
    reason: KillReason

    @field_validator("rollback_patch_ids")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if any(not re.match(_SHA256, v) for v in value):
            raise ValueError("patch ids are SHA-256 hex")
        return _sorted_unique(value, "patch ids")


class SignedKillSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: KillSwitchPayload
    signature: str = Field(min_length=1, max_length=128)
    chain: CertificateChain

    @property
    def entry_id(self) -> str:
        return hashlib.sha256(
            KILL_SWITCH_DOMAIN + canonical_json(self.payload)
        ).hexdigest()


def sign_revocation_list(
    payload: RevocationListPayload, signer: KeyCustody, chain: CertificateChain
) -> SignedRevocationList:
    _require_status_signer(signer, chain)
    return SignedRevocationList(
        payload=payload,
        signature=sign_document(signer, REVOCATION_DOMAIN, payload),
        chain=chain,
    )


def sign_kill_switch(
    payload: KillSwitchPayload, signer: KeyCustody, chain: CertificateChain
) -> SignedKillSwitch:
    _require_status_signer(signer, chain)
    return SignedKillSwitch(
        payload=payload,
        signature=sign_document(signer, KILL_SWITCH_DOMAIN, payload),
        chain=chain,
    )


def _require_status_signer(signer: KeyCustody, chain: CertificateChain) -> None:
    if signer.public_key_b64() != chain.signer.payload.signer_public_key:
        raise PatchSignatureError("the signing key does not match its certificate")
    if chain.signer.payload.purpose != "status":
        raise PatchSignatureError("status documents need a status certificate")


class FleetStatusSource(Protocol):
    def revocation_list(self) -> SignedRevocationList:
        """The newest list. Raise :class:`FleetStatusUnavailable` if none."""
        ...

    def kill_switches(self) -> list[SignedKillSwitch]: ...


@dataclass
class FleetStatus:
    """What a tenant learned from the feed, and why it may not trust it."""

    checked_at: datetime
    #: Reasons auto-promotion must not proceed. Empty means fresh and verified.
    problems: list[str] = field(default_factory=list)
    revocation_sequence: Optional[int] = None
    revoked_key_ids: frozenset[str] = frozenset()
    revoked_serials: frozenset[str] = frozenset()
    auto_promotion_disabled: bool = False
    rollback_patch_ids: frozenset[str] = frozenset()

    @property
    def fresh(self) -> bool:
        return not self.problems

    def revocation_reason(self, chain: CertificateChain) -> Optional[str]:
        return chain_revocation_reason(
            chain, self.revoked_key_ids, self.revoked_serials
        )


def _verify_status_chain(
    chain: CertificateChain, trust: FleetTrust, now: datetime
) -> None:
    verify_chain(chain, trust, purpose="status", now=now)


def read_fleet_status(
    source: FleetStatusSource,
    trust: FleetTrust,
    *,
    now: Optional[datetime] = None,
    max_age: timedelta = DEFAULT_MAX_STATUS_AGE,
) -> FleetStatus:
    """Read and verify the revocation list and kill switches. Never raises."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    status = FleetStatus(checked_at=current)

    try:
        rl = source.revocation_list()
    except Exception as exc:  # noqa: BLE001 - any failure is "unreachable"
        status.problems.append(
            f"revocation list unreachable ({type(exc).__name__}); failing closed"
        )
        rl = None
    if rl is not None:
        try:
            _verify_status_chain(rl.chain, trust, current)
            verify_document(
                rl.chain.signer.payload.signer_public_key,
                REVOCATION_DOMAIN,
                rl.payload,
                rl.signature,
                "revocation list",
            )
        except PatchSignatureError as exc:
            status.problems.append(f"revocation list does not verify: {exc}")
        else:
            issued, due = _utc(rl.payload.issued_at), _utc(rl.payload.next_update)
            if issued > current + CLOCK_SKEW:
                status.problems.append("revocation list is dated in the future")
            elif current >= due or current - issued > max_age:
                status.problems.append(
                    "revocation list is stale (issued "
                    f"{rl.payload.issued_at}, next update was due "
                    f"{rl.payload.next_update})"
                )
            status.revocation_sequence = rl.payload.sequence
            status.revoked_key_ids = frozenset(rl.payload.revoked_key_ids)
            status.revoked_serials = frozenset(rl.payload.revoked_serials)
            own = status.revocation_reason(rl.chain)
            if own is not None:
                status.problems.append(f"revocation list signer: {own}")

    try:
        switches = source.kill_switches()
    except Exception as exc:  # noqa: BLE001
        status.problems.append(
            f"kill switches unreachable ({type(exc).__name__}); failing closed"
        )
        switches = []
    verified: list[SignedKillSwitch] = []
    for entry in switches:
        try:
            _verify_status_chain(entry.chain, trust, current)
            verify_document(
                entry.chain.signer.payload.signer_public_key,
                KILL_SWITCH_DOMAIN,
                entry.payload,
                entry.signature,
                "kill switch",
            )
        except PatchSignatureError as exc:
            status.problems.append(f"a kill-switch entry does not verify: {exc}")
            continue
        verified.append(entry)
    if verified:
        latest = max(verified, key=lambda e: e.payload.sequence)
        status.auto_promotion_disabled = latest.payload.disable_auto_promotion
        ids: set[str] = set()
        for entry in verified:
            ids.update(entry.payload.rollback_patch_ids)
        status.rollback_patch_ids = frozenset(ids)
    return status


__all__ = [
    "DEFAULT_MAX_STATUS_AGE",
    "FleetStatus",
    "FleetStatusSource",
    "FleetStatusUnavailable",
    "KillSwitchPayload",
    "MAX_REVOCATION_LIST_VALIDITY",
    "RevocationListPayload",
    "SignedKillSwitch",
    "SignedRevocationList",
    "read_fleet_status",
    "sign_kill_switch",
    "sign_revocation_list",
]
