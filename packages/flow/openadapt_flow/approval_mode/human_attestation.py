"""Proof that a person, not an agent, answered a save.

A person's approval used to be a claim: ``--decided-by human`` with
``--unauthenticated`` or a named ``--auth-method`` was enough, and any process
that could run the CLI or write the run directory could approve its own save.
Now a person's answer counts only when it carries a :class:`HumanAttestation`:
an Ed25519 signature over the answer, by a key registered under ``approvers``
in the key directory's ``principals.json``
(:class:`~openadapt_flow.jit.authority.PrincipalRegistry`).

* Desktop's Run it signs with the Desktop approver key (``desktop_key``).
* The decision portal signs, after the person answered on their phone, with
  the portal's approver key (``portal_key``).
* ``openadapt-flow approve`` signs a durable resume approval with the
  person's own approver key (``person_key``); ``resume`` refuses a stored
  approval without one.

The registry and the keys follow the trust root of program admission
(:mod:`openadapt_flow.jit.authority`): a person registers each key at a
terminal (``jit register --role approver``), an approver key can't hold any
other role, and an approver id that names an agent or model is refused. Keep
the key directory and the approver keys away from the agents they govern.

Every check fails closed: no key directory, no registered approver, a wrong
key, a changed answer, or a route signed with the other route's method is a
refusal. A claimed Cloud sign-in (``cloud_aal1``/``cloud_aal2``) is refused
too, because nothing here can verify it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.approval_mode.model import (
    DECISION_PORTAL_ROUTE,
    ITEM_APPROVAL_ROUTE,
)

#: The decision portal's encrypted approver key. Its passphrase comes from
#: ``OPENADAPT_JIT_KEY_PASSPHRASE``. Unset: the portal can't record an answer.
PORTAL_KEY_ENV = "OPENADAPT_PORTAL_APPROVER_KEY"
ANSWER_DOMAIN = b"openadapt-flow.approval.human-answer/v1\x00"
RESUME_DOMAIN = b"openadapt-flow.durable.resume-approval/v1\x00"
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"

#: ``desktop_key``: Desktop's Run it. ``portal_key``: the decision portal.
#: ``person_key``: a person at a terminal (``openadapt-flow approve``).
AttestationMethod = Literal["desktop_key", "portal_key", "person_key"]

#: The signing method each person route must use.
ROUTE_METHOD: dict[str, AttestationMethod] = {
    ITEM_APPROVAL_ROUTE: "desktop_key",
    DECISION_PORTAL_ROUTE: "portal_key",
}


class HumanAttestation(BaseModel):
    """A registered approver key's signature over one answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signer: str = Field(pattern=_ID)
    method: AttestationMethod
    signature: str = Field(min_length=1, max_length=256)


@dataclass(frozen=True)
class ApproverSigner:
    """An approver key ready to sign, and who the registry says it is."""

    principal_id: str
    method: AttestationMethod
    signing: Ed25519PrivateKey

    def attest(self, domain: bytes, payload: Any) -> HumanAttestation:
        from openadapt_flow.jit.authority import _sign

        return HumanAttestation(
            signer=self.principal_id,
            method=self.method,
            signature=_sign(self.signing, domain, payload),
        )


class AttestationRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_signer(
    key_path: Path | str,
    key_dir: Path,
    *,
    method: AttestationMethod,
    env: Optional[Mapping[str, str]] = None,
) -> ApproverSigner:
    """Open an encrypted approver key and name it from the registry.

    The principal is the name the key is registered under, never a name the
    caller passes. Raises :class:`AttestationRefused`.
    """

    from openadapt_flow.jit.authority import (
        AuthorityError,
        PrincipalRegistry,
        load_encrypted_key,
        public_hex,
    )

    try:
        key = load_encrypted_key(key_path, env=env)
        registry = PrincipalRegistry.load(key_dir)
    except (AuthorityError, OSError, ValueError) as exc:
        raise AttestationRefused("approver_key_unusable", str(exc)) from exc
    found = registry.lookup(public_hex(key))
    if found is None or found[0] != "approver":
        raise AttestationRefused(
            "human_approver_unregistered",
            "this key isn't registered as an approver; a person registers it "
            "with jit register --role approver",
        )
    return ApproverSigner(principal_id=found[1], method=method, signing=key)


def refusal(
    attestation: Optional[HumanAttestation],
    *,
    domain: bytes,
    payload: Any,
    key_dir: Optional[Path],
    method: Optional[AttestationMethod] = None,
) -> Optional[str]:
    """A closed reason this attestation proves nothing, or ``None``."""

    if attestation is None:
        return "human_answer_unsigned"
    if method is not None and attestation.method != method:
        return "attestation_method_mismatch"
    if key_dir is None:
        return "human_answer_unverifiable"
    from openadapt_flow.jit.authority import PrincipalRegistry, _verify

    try:
        registry = PrincipalRegistry.load(key_dir)
    except (OSError, ValueError):
        return "human_answer_unverifiable"
    public = registry.approvers.get(attestation.signer)
    if public is None:
        return "human_approver_unregistered"
    if not _verify(public, domain, payload, attestation.signature):
        return "human_answer_signature_invalid"
    return None


def portal_signer(
    key_dir: Path, *, env: Optional[Mapping[str, str]] = None
) -> ApproverSigner:
    """The decision portal's approver key, from :data:`PORTAL_KEY_ENV`.

    Raises :class:`AttestationRefused` when it isn't configured, so a portal
    without a key records no answer rather than an unsigned one.
    """

    import os

    source = os.environ if env is None else env
    path = source.get(PORTAL_KEY_ENV)
    if not path:
        raise AttestationRefused(
            "human_answer_unverifiable",
            f"the decision portal has no approver key; set {PORTAL_KEY_ENV} to "
            "a key registered with jit register --role approver",
        )
    return load_signer(path, key_dir, method="portal_key", env=env)
