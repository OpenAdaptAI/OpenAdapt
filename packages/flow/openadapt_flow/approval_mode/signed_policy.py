"""A person-signed grant that lets named agents or systems answer saves.

Without a grant, only a person answers a save. An ``ApproverPolicyV1`` file on
its own is unsigned JSON, so anyone who could write the run directory could
widen it. Flow therefore accepts a non-person approver only under an
:class:`ApproverGrant` that a person signed, using the same signing as program
admission (:mod:`openadapt_flow.jit.authority`, monorepo #1207):

* the person's Ed25519 key is registered as a ``policy_signer`` in
  ``principals.json`` in the key directory (``$OPENADAPT_JIT_KEY_DIR`` or
  ``~/.openadapt/jit``); an agent key can't also be a signer, and a signer
  name that names an agent or model is refused;
* the grant names its delegates (agent or system ids), the approver policy
  they act under, an expiry, and optionally the workflows it covers;
* the signature is domain-separated Ed25519 over canonical JSON. Its digest
  (``policy_ref``) is recorded on every answer a delegate gives.

An unsigned, tampered, expired or unregistered grant gives no authority: the
run falls back to person-only and records why. Flow re-verifies the grant every
time it reads an answer, so a file swapped mid-run is refused too.

Nothing here lets an agent widen its own authority. There is no API to sign a
grant with an agent key, and the delegate that started a run can never answer
that run's saves (see ``ItemApprovalStore.write_initiator``).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openadapt_flow.approval_mode.approver import (
    DEFAULT_APPROVER_POLICY,
    ApproverKind,
    ApproverPolicyV1,
)

GRANT_SCHEMA: Literal["openadapt-flow/approver-grant/v1"] = (
    "openadapt-flow/approver-grant/v1"
)
GRANT_DOMAIN = b"openadapt-flow.approval.approver-grant/v1\x00"
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$"
_SIGNER_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"
#: Longest a grant may last. A person re-signs to extend it.
MAX_GRANT_DAYS = 366

GrantRefusal = Literal[
    "approver_policy_unsigned",
    "approver_grant_unreadable",
    "approver_grant_signer_unregistered",
    "approver_grant_signer_key_mismatch",
    "approver_grant_signature_invalid",
    "approver_grant_expired",
    "approver_grant_not_yet_valid",
    "approver_grant_policy_mismatch",
]


def _canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} has no time zone; use UTC (…Z)")
    return parsed.astimezone(timezone.utc)


def allows_non_person(policy: ApproverPolicyV1) -> bool:
    """True when ``policy`` lets anything other than a person answer."""

    return any(
        kind is not ApproverKind.PERSON
        for kinds in policy.allowed.values()
        for kind in kinds
    )


class ApproverGrant(BaseModel):
    """What one person lets named delegates approve, and until when."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["openadapt-flow/approver-grant/v1"] = Field(
        default=GRANT_SCHEMA, alias="schema"
    )
    grant_id: str = Field(pattern=_ID)
    #: The person who signs, as registered under ``policy_signers``.
    signer: str = Field(pattern=_SIGNER_ID)
    signed_at: str = Field(min_length=10, max_length=64)
    expires_at: str = Field(min_length=10, max_length=64)
    #: Which kinds may answer each class of save under this grant.
    approver_policy: ApproverPolicyV1
    #: The agent or system ids that may answer. Any other id is refused.
    delegates: tuple[str, ...] = Field(min_length=1)
    #: Workflow names the grant covers. Empty means every workflow.
    workflows: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "ApproverGrant":
        signed = _utc(self.signed_at)
        expires = _utc(self.expires_at)
        if expires <= signed:
            raise ValueError("the grant expires before it was signed")
        if (expires - signed).days > MAX_GRANT_DAYS:
            raise ValueError(f"a grant lasts at most {MAX_GRANT_DAYS} days")
        for delegate in self.delegates:
            if not re.match(_ID, delegate):
                raise ValueError(f"invalid delegate id {delegate!r}")
        if len({d.lower() for d in self.delegates}) != len(self.delegates):
            raise ValueError("each delegate is named once")
        if self.signer.lower() in {d.lower() for d in self.delegates}:
            raise ValueError(
                "the person who signs a grant can't be one of its delegates"
            )
        if not allows_non_person(self.approver_policy):
            raise ValueError("a grant that lets only a person answer grants nothing")
        return self

    def digest(self) -> str:
        """``sha256:<hex>`` of the grant: the ``policy_ref`` answers record."""

        payload = self.model_dump(mode="json", by_alias=True)
        return (
            "sha256:" + hashlib.sha256(GRANT_DOMAIN + _canonical(payload)).hexdigest()
        )


class SignedApproverGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant: ApproverGrant
    signer_public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(min_length=1, max_length=200)

    def save(self, path: Path | str) -> Path:
        out = Path(path)
        out.write_text(
            self.model_dump_json(indent=2, by_alias=True) + "\n", encoding="utf-8"
        )
        return out


@dataclass(frozen=True)
class VerifiedGrant:
    """A grant whose signature, signer and dates Flow checked just now."""

    grant: ApproverGrant
    policy_ref: str
    signer: str

    @property
    def policy(self) -> ApproverPolicyV1:
        return self.grant.approver_policy

    def names(self, delegate_id: Optional[str]) -> bool:
        return delegate_id is not None and delegate_id in self.grant.delegates

    def covers(self, workflow_name: str) -> bool:
        return not self.grant.workflows or workflow_name in self.grant.workflows


class GrantRefused(ValueError):
    def __init__(self, code: GrantRefusal, message: str) -> None:
        super().__init__(message)
        self.code: GrantRefusal = code


def sign_grant(grant: ApproverGrant, key: Any) -> SignedApproverGrant:
    """A person signs ``grant`` with their registered policy-signer key."""

    from openadapt_flow.jit.authority import _sign, public_hex

    return SignedApproverGrant(
        grant=grant,
        signer_public_key=public_hex(key),
        signature=_sign(
            key, GRANT_DOMAIN, grant.model_dump(mode="json", by_alias=True)
        ),
    )


def verify_grant(
    signed: SignedApproverGrant,
    key_dir: Path | str,
    *,
    now: Optional[datetime] = None,
) -> VerifiedGrant:
    """Check ``signed`` against the registry in ``key_dir``.

    Raises:
        GrantRefused: the signer isn't a registered person, the key or
            signature doesn't match, or the grant isn't valid now.
    """

    from openadapt_flow.jit.authority import PrincipalRegistry, _verify

    try:
        registry = PrincipalRegistry.load(Path(key_dir))
    except (OSError, ValueError) as exc:
        raise GrantRefused(
            "approver_grant_signer_unregistered", "the signer registry is unreadable"
        ) from exc
    grant = signed.grant
    registered = registry.policy_signers.get(grant.signer)
    if registered is None:
        raise GrantRefused(
            "approver_grant_signer_unregistered",
            f"{grant.signer!r} is not a registered person who signs policy",
        )
    if registered != signed.signer_public_key:
        raise GrantRefused(
            "approver_grant_signer_key_mismatch",
            "the grant was signed with another key",
        )
    payload = grant.model_dump(mode="json", by_alias=True)
    if not _verify(registered, GRANT_DOMAIN, payload, signed.signature):
        raise GrantRefused(
            "approver_grant_signature_invalid", "the grant's signature does not verify"
        )
    at = now or datetime.now(timezone.utc)
    if _utc(grant.expires_at) <= at:
        raise GrantRefused(
            "approver_grant_expired", f"the grant expired at {grant.expires_at}"
        )
    if _utc(grant.signed_at) > at:
        raise GrantRefused(
            "approver_grant_not_yet_valid", "the grant is signed in the future"
        )
    return VerifiedGrant(grant=grant, policy_ref=grant.digest(), signer=grant.signer)


@dataclass(frozen=True)
class LoadedApproverPolicy:
    """What ``--approver-policy`` resolved to for one run."""

    policy: ApproverPolicyV1
    signed: Optional[SignedApproverGrant] = None
    verified: Optional[VerifiedGrant] = None
    #: Why a wider policy was not used, when it wasn't.
    refusal: Optional[GrantRefusal] = None


def load_approver_policy(
    path: Path | str,
    key_dir: Path | str,
    *,
    now: Optional[datetime] = None,
) -> LoadedApproverPolicy:
    """Resolve an ``--approver-policy`` file. Never raises for a bad file.

    * A signed grant that verifies: its policy and delegates apply.
    * A plain ``openadapt.approver-policy/v1`` that lets only a person
      answer: applies as-is (it changes nothing).
    * Anything else (an unsigned policy that admits an agent, a tampered or
      expired grant, an unreadable file): person-only, with the reason.
    """

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LoadedApproverPolicy(
            DEFAULT_APPROVER_POLICY, refusal="approver_grant_unreadable"
        )
    if isinstance(raw, dict) and "grant" in raw:
        try:
            signed = SignedApproverGrant.model_validate(raw)
        except ValidationError:
            return LoadedApproverPolicy(
                DEFAULT_APPROVER_POLICY, refusal="approver_grant_unreadable"
            )
        try:
            verified = verify_grant(signed, key_dir, now=now)
        except GrantRefused as exc:
            return LoadedApproverPolicy(DEFAULT_APPROVER_POLICY, refusal=exc.code)
        return LoadedApproverPolicy(verified.policy, signed, verified)
    try:
        policy = ApproverPolicyV1.model_validate(raw)
    except ValidationError:
        return LoadedApproverPolicy(
            DEFAULT_APPROVER_POLICY, refusal="approver_grant_unreadable"
        )
    if allows_non_person(policy):
        return LoadedApproverPolicy(
            DEFAULT_APPROVER_POLICY, refusal="approver_policy_unsigned"
        )
    return LoadedApproverPolicy(policy)


__all__ = [
    "GRANT_SCHEMA",
    "ApproverGrant",
    "GrantRefused",
    "LoadedApproverPolicy",
    "SignedApproverGrant",
    "VerifiedGrant",
    "allows_non_person",
    "load_approver_policy",
    "sign_grant",
    "verify_grant",
]
