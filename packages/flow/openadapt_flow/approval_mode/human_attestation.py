"""Proof that a person, not an agent, answered a save.

A person's approval used to be a claim: ``--decided-by human`` with
``--unauthenticated`` or a named ``--auth-method`` was enough, and any process
that could run the CLI or write the run directory could approve its own save.
Now a person's answer counts only when it carries a :class:`HumanAttestation`:
an Ed25519 signature over the answer, by a key on the signed approver roster
(``approvers.json`` in the key directory) that the run's pinned root vouches
for (:mod:`.approver_roster`).

* Desktop's Run it signs with the Desktop approver key (``desktop_key``).
* The decision portal signs, after the person answered on their phone, with
  the portal's approver key (``portal_key``).
* ``openadapt-flow approve`` signs a durable resume approval with the
  person's own approver key (``person_key``); ``resume`` refuses a stored
  approval without one.

The roster starts from a root approver key; every later approver is added
with an approver's signature (``jit approver add``, or Desktop's setup behind
Touch ID). A run pins the root (the deployment config only, for a governed or
agent-started run) and the roster head it saw. An approver key can't hold
another role, and an approver id that names an agent or model is refused.
Keep the key directory, the deployment config and the approver keys away from
the agents they govern.

Every check fails closed: no pin, no roster, a roster that doesn't extend
the recorded head, an unlisted signer, a wrong key, a changed answer, or a
route signed with the other route's method is a refusal. A claimed Cloud sign-in (``cloud_aal1``/``cloud_aal2``) is refused
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


def approvers(
    key_dir: Path | str,
    root: Optional[str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    at_least: Any = None,
) -> dict[str, str]:
    """The verified approvers for the given pinned root.

    ``root`` is the caller's pin (a run's, from its deployment config or an
    explicit flag). Nothing here reads the environment: without a root,
    nobody is an approver (``approver_root_unpinned``). Raises
    :class:`AttestationRefused` with the roster's code. ``env`` is unused and
    kept for callers that pass it.
    """

    from openadapt_flow.approval_mode.approver_roster import (
        RosterError,
        verified_approvers,
    )

    del env
    try:
        return verified_approvers(key_dir, root, at_least=at_least)
    except RosterError as exc:
        raise AttestationRefused(exc.code, str(exc)) from exc


def load_signer(
    key_path: Path | str,
    key_dir: Path,
    *,
    method: AttestationMethod,
    env: Optional[Mapping[str, str]] = None,
    root: Optional[str] = None,
) -> ApproverSigner:
    """Open an encrypted approver key and name it from the signed roster.

    The principal is the name the key holds on the roster the run's pinned
    root vouches for, never a name the caller passes. Raises
    :class:`AttestationRefused`.
    """

    from openadapt_flow.jit.authority import (
        AuthorityError,
        load_encrypted_key,
        public_hex,
    )

    try:
        key = load_encrypted_key(key_path, env=env)
    except (AuthorityError, OSError, ValueError) as exc:
        raise AttestationRefused("approver_key_unusable", str(exc)) from exc
    public = public_hex(key)
    found = next(
        (
            pid
            for pid, value in approvers(key_dir, root, env=env).items()
            if value == public
        ),
        None,
    )
    if found is None:
        raise AttestationRefused(
            "human_approver_unregistered",
            "this key isn't on the approver roster; an approver adds it with "
            "openadapt-flow jit approver add",
        )
    return ApproverSigner(principal_id=found, method=method, signing=key)


def refusal(
    attestation: Optional[HumanAttestation],
    *,
    domain: bytes,
    payload: Any,
    key_dir: Optional[Path],
    method: Optional[AttestationMethod] = None,
    root: Optional[str] = None,
    at_least: Any = None,
) -> Optional[str]:
    """A closed reason this attestation proves nothing, or ``None``.

    The signer must be on the approver roster that the run's pinned root
    (``root``, else the environment's pin) vouches for.
    """

    if attestation is None:
        return "human_answer_unsigned"
    if method is not None and attestation.method != method:
        return "attestation_method_mismatch"
    if key_dir is None:
        return "human_answer_unverifiable"
    from openadapt_flow.jit.authority import _verify

    try:
        known = approvers(key_dir, root, at_least=at_least)
    except AttestationRefused as exc:
        return exc.code
    public = known.get(attestation.signer)
    if public is None:
        return "human_approver_unregistered"
    if not _verify(public, domain, payload, attestation.signature):
        return "human_answer_signature_invalid"
    return None


def portal_signer(
    key_dir: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    root: Optional[str] = None,
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
            "a key on the approver roster (openadapt-flow jit approver add)",
        )
    return load_signer(path, key_dir, method="portal_key", env=env, root=root)
