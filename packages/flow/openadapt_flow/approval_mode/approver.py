"""Who may approve a save, and delegated approvers.

The contracts here are field-exact mirrors of
``openadapt_types.approval`` (``ApproverIdentityV1``,
``ApproverPolicyV1``, ``StepClass``; also importable as
``openadapt_types.approval``), which is the single schema the receipt and the
other packages use. The mirror predates the fold of openadapt-types into the
core; ``tests/test_approval_delegated.py`` checks the two stay identical.

What this adds to per-item approval:

* every answer can name its approver: a person, a rule set in advance, an
  agent or another system, with an opaque id and how it was identified;
* an :class:`ApproverPolicyV1` the *run* holds (never the answer) decides
  which kinds of approver may answer for each class of step. The default is
  a person for every class, which is exactly the behavior before this
  module: a non-person answer is refused with ``approver_not_allowed``;
* a ``clinical_write`` step accepts a non-person approver only when the
  policy pins the customer's signed opt-in;
* :class:`Approver` is the pluggable interface for a delegated approver.
  :class:`WebhookApprover` asks the customer's own endpoint over HTTPS with
  HMAC-signed, PHI-free messages. The local CLI path is
  ``openadapt-flow item-approval approve --approver-kind agent ...``.

Only a person's decision is ever a training label
(:func:`is_training_label`). Rule, agent and system decisions are recorded in
the run report and kept out of every label log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from enum import Enum, IntEnum
from typing import Literal, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

APPROVER_IDENTITY_SCHEMA: Literal["openadapt.approver-identity/v1"] = (
    "openadapt.approver-identity/v1"
)
APPROVER_POLICY_SCHEMA: Literal["openadapt.approver-policy/v1"] = (
    "openadapt.approver-policy/v1"
)
_OPAQUE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$"
_SHA256 = r"^sha256:[0-9a-f]{64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApproverKind(str, Enum):
    PERSON = "person"
    RULE = "rule"
    AGENT = "agent"
    SYSTEM = "system"


class ApproverAuth(str, Enum):
    CLOUD_USER = "cloud_user"
    UNAUTHENTICATED = "unauthenticated"
    POLICY = "policy"
    LOCAL_CLI = "local_cli"
    SIGNED_WEBHOOK = "signed_webhook"
    MCP_SESSION = "mcp_session"


ApproverAuthMethod = Literal["cloud_aal2", "cloud_aal1", "none"]

_AUTH_BY_KIND: Mapping[ApproverKind, frozenset[ApproverAuth]] = {
    ApproverKind.PERSON: frozenset(
        {ApproverAuth.CLOUD_USER, ApproverAuth.UNAUTHENTICATED, ApproverAuth.LOCAL_CLI}
    ),
    ApproverKind.RULE: frozenset({ApproverAuth.POLICY}),
    ApproverKind.AGENT: frozenset(
        {ApproverAuth.MCP_SESSION, ApproverAuth.SIGNED_WEBHOOK, ApproverAuth.LOCAL_CLI}
    ),
    ApproverKind.SYSTEM: frozenset(
        {ApproverAuth.SIGNED_WEBHOOK, ApproverAuth.LOCAL_CLI}
    ),
}


class ApproverIdentityV1(_Strict):
    """Mirror of ``openadapt_types.ApproverIdentityV1``."""

    schema_version: Literal["openadapt.approver-identity/v1"] = APPROVER_IDENTITY_SCHEMA
    kind: ApproverKind
    auth: ApproverAuth
    id: Optional[StrictStr] = Field(default=None, pattern=_OPAQUE_ID)
    auth_method: ApproverAuthMethod = "none"
    #: ``sha256:<hex>`` of the policy or rule that allowed this approver
    #: (RFC-0002 ``policy_ref``). Required for a rule.
    policy_ref: Optional[StrictStr] = Field(default=None, pattern=_SHA256)
    on_behalf_of: Optional[StrictStr] = Field(default=None, pattern=_OPAQUE_ID)

    @model_validator(mode="after")
    def _consistent(self) -> "ApproverIdentityV1":
        if self.auth not in _AUTH_BY_KIND[self.kind]:
            raise ValueError(
                f"a {self.kind.value} approver cannot use {self.auth.value} auth"
            )
        if self.auth is ApproverAuth.UNAUTHENTICATED:
            if self.id is not None or self.auth_method != "none":
                raise ValueError("an unauthenticated approval names no approver")
        elif self.id is None:
            raise ValueError(f"a {self.kind.value} approver needs an id")
        if (self.auth is ApproverAuth.CLOUD_USER) != (self.auth_method != "none"):
            raise ValueError("only a Cloud user has a Cloud sign-in method")
        if self.kind is ApproverKind.RULE and self.policy_ref is None:
            raise ValueError("a rule approver pins its rule set in policy_ref")
        if self.on_behalf_of is not None and self.kind is ApproverKind.PERSON:
            raise ValueError("a person approves for themselves")
        return self

    @property
    def is_person(self) -> bool:
        return self.kind is ApproverKind.PERSON


def person_identity(
    approver_id: Optional[str], auth_method: str = "none"
) -> ApproverIdentityV1:
    """The identity behind the older ``approver_kind`` / ``approver_id`` pair."""

    if approver_id is None:
        return ApproverIdentityV1(
            kind=ApproverKind.PERSON, auth=ApproverAuth.UNAUTHENTICATED
        )
    return ApproverIdentityV1(
        kind=ApproverKind.PERSON,
        auth=ApproverAuth.CLOUD_USER,
        id=approver_id,
        auth_method=auth_method,  # type: ignore[arg-type]
    )


def is_training_label(approver: ApproverIdentityV1) -> bool:
    """True only for a person's decision."""

    return approver.kind is ApproverKind.PERSON


class StepClass(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    CONSEQUENTIAL = "consequential"
    IRREVERSIBLE = "irreversible"
    CLINICAL_WRITE = "clinical_write"


_PERSON_ONLY: tuple[ApproverKind, ...] = (ApproverKind.PERSON,)


def _default_allowed() -> dict[StepClass, tuple[ApproverKind, ...]]:
    return {step_class: _PERSON_ONLY for step_class in StepClass}


class ApproverPolicyRefusal(str, Enum):
    KIND_NOT_ALLOWED = "approver_kind_not_allowed"
    CLINICAL_WRITE_NEEDS_OPT_IN = "clinical_write_needs_opt_in"


class ApproverPolicyV1(_Strict):
    """Mirror of ``openadapt_types.ApproverPolicyV1``. Person-only by default."""

    schema_version: Literal["openadapt.approver-policy/v1"] = APPROVER_POLICY_SCHEMA
    allowed: dict[StepClass, tuple[ApproverKind, ...]] = Field(
        default_factory=_default_allowed
    )
    clinical_write_opt_in_sha256: Optional[StrictStr] = Field(
        default=None, pattern=_SHA256
    )

    @model_validator(mode="after")
    def _complete(self) -> "ApproverPolicyV1":
        missing = [c.value for c in StepClass if c not in self.allowed]
        if missing:
            raise ValueError(f"policy must name every step class: {missing}")
        for step_class, kinds in self.allowed.items():
            if not kinds or len(set(kinds)) != len(kinds):
                raise ValueError(f"{step_class.value} needs distinct approver kinds")
        clinical = self.allowed[StepClass.CLINICAL_WRITE]
        if clinical != _PERSON_ONLY and self.clinical_write_opt_in_sha256 is None:
            raise ValueError("clinical_write allows a non-person only with an opt-in")
        return self

    def refusal(
        self, step_class: StepClass, approver: ApproverIdentityV1
    ) -> Optional[ApproverPolicyRefusal]:
        if approver.kind not in self.allowed[step_class]:
            if (
                step_class is StepClass.CLINICAL_WRITE
                and self.clinical_write_opt_in_sha256 is None
            ):
                return ApproverPolicyRefusal.CLINICAL_WRITE_NEEDS_OPT_IN
            return ApproverPolicyRefusal.KIND_NOT_ALLOWED
        return None

    def permits(self, step_class: StepClass, approver: ApproverIdentityV1) -> bool:
        return self.refusal(step_class, approver) is None

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()


DEFAULT_APPROVER_POLICY = ApproverPolicyV1()


def step_class_for(step: object, *, policy_name: Optional[str] = None) -> StepClass:
    """The class of a save step, from the run's policy and its effects.

    Under the ``clinical-write`` and ``regulated`` policies every save is
    ``clinical_write``. Otherwise a step with an irreversible effect is
    ``irreversible`` and any other save is ``consequential``. Unknown shapes
    are ``consequential``, never weaker.
    """

    if policy_name in CLINICAL_POLICIES:
        return StepClass.CLINICAL_WRITE
    effects = getattr(step, "effects", None) or []
    if any(getattr(e, "risk", "") == "irreversible" for e in effects):
        return StepClass.IRREVERSIBLE
    return StepClass.CONSEQUENTIAL


#: Policy names whose saves are all clinical writes.
CLINICAL_POLICIES = frozenset({"clinical-write", "regulated"})


class ReadBackTier(IntEnum):
    """The verification ladder (mirrors ``openadapt_types.ReadBackTier``).

    Names and labels are RFC-0002's ``verification_tier`` values. Only a
    read of the system of record that skips the UI can make a save clean.
    """

    INDEPENDENT_READBACK = 1
    SCREEN_READBACK = 2
    UNCHECKED = 3

    @property
    def label(self) -> str:
        """The RFC-0002 ``verification_tier`` wire value."""

        return self.name.lower()


def read_back_tier_for(flow_tier: Optional[int]) -> ReadBackTier:
    """Map Flow's ``VerificationTier`` (1 to 4) onto the ladder (RFC-0002).

    Only tier 1, a read of the system of record that doesn't go through the
    UI (REST, SQL, FHIR, GraphQL or a file export), is independent. Tiers 2
    to 4 read through the UI, including a separate session, and are screen
    read-backs. Anything else, or no tier, is unchecked.
    """

    if flow_tier == 1:
        return ReadBackTier.INDEPENDENT_READBACK
    if flow_tier in (2, 3, 4):
        return ReadBackTier.SCREEN_READBACK
    return ReadBackTier.UNCHECKED


#: Routes a non-person answer may arrive by.
DELEGATED_ROUTES = frozenset({"local_cli", "webhook", "mcp"})


# -- delegated approvers --------------------------------------------------------


class DelegatedDecision(_Strict):
    """What a delegated approver decided for one request."""

    decision: Literal["approve", "decline"]
    decline_reason: Optional[
        Literal["wrong_values", "wrong_record", "screen_problem"]
    ] = None


@runtime_checkable
class Approver(Protocol):
    """A delegated approver: the customer's agent or system.

    ``decide`` sees only PHI-free request facts (digests, ids, the step class
    and mode). Return ``None`` when it has no answer yet; a person can still
    answer the same request in Desktop.
    """

    identity: ApproverIdentityV1
    route: str

    def decide(self, facts: Mapping[str, str]) -> Optional[DelegatedDecision]: ...


class WebhookRefused(RuntimeError):
    """The approver endpoint's reply can't be used."""


def _sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


class WebhookApprover:
    """Ask the customer's approver endpoint over HTTPS.

    Request body: the PHI-free facts as canonical JSON, signed in the
    ``X-OpenAdapt-Signature`` header with HMAC-SHA256 over the body. The
    reply must echo ``request_sha256``, carry ``decision`` (and
    ``decline_reason`` for a decline), and be signed the same way. An
    unsigned, mis-signed or mismatched reply is refused, and the run keeps
    waiting for a person.
    """

    route = "webhook"

    def __init__(
        self,
        url: str,
        secret: bytes,
        identity: ApproverIdentityV1,
        *,
        timeout_s: float = 10.0,
        opener: Optional[object] = None,
    ) -> None:
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            raise ValueError("the approver endpoint must use https")
        if len(secret) < 32:
            raise ValueError("the webhook secret must be at least 32 bytes")
        if identity.is_person:
            raise ValueError("a webhook approver is an agent or a system")
        if identity.auth is not ApproverAuth.SIGNED_WEBHOOK:
            raise ValueError("a webhook approver authenticates with a signed webhook")
        self.url = url
        self._secret = secret
        self.identity = identity
        self._timeout_s = timeout_s
        self._opener = opener or urllib.request.build_opener()

    def decide(self, facts: Mapping[str, str]) -> Optional[DelegatedDecision]:
        body = json.dumps(dict(facts), sort_keys=True, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-OpenAdapt-Signature": _sign(self._secret, body),
            },
        )
        with self._opener.open(request, timeout=self._timeout_s) as response:  # type: ignore[attr-defined]
            raw = response.read(65536)
            signature = response.headers.get("X-OpenAdapt-Signature", "")
        if not hmac.compare_digest(signature, _sign(self._secret, raw)):
            raise WebhookRefused("reply_signature_invalid")
        try:
            reply = json.loads(raw)
        except ValueError as exc:
            raise WebhookRefused("reply_unreadable") from exc
        if not isinstance(reply, dict):
            raise WebhookRefused("reply_unreadable")
        if reply.get("request_sha256") != facts.get("request_sha256"):
            raise WebhookRefused("reply_for_another_request")
        if reply.get("decision") in (None, "pending"):
            return None
        try:
            return DelegatedDecision.model_validate(
                {
                    "decision": reply.get("decision"),
                    "decline_reason": reply.get("decline_reason"),
                }
            )
        except ValueError as exc:
            raise WebhookRefused("reply_fields_invalid") from exc


__all__ = [
    "APPROVER_IDENTITY_SCHEMA",
    "APPROVER_POLICY_SCHEMA",
    "DEFAULT_APPROVER_POLICY",
    "DELEGATED_ROUTES",
    "Approver",
    "ApproverAuth",
    "ApproverIdentityV1",
    "ApproverKind",
    "ApproverPolicyRefusal",
    "ApproverPolicyV1",
    "DelegatedDecision",
    "StepClass",
    "WebhookApprover",
    "WebhookRefused",
    "is_training_label",
    "person_identity",
    "step_class_for",
]
