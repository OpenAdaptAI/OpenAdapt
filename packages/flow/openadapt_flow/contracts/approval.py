"""Who approved a consequential step, when approval is needed, and how a save
was checked.

This module is the single contract for three things that Flow, the agent
tools, Desktop, Cloud and the Seal receipt all have to agree on:

* :class:`ApproverIdentityV1` names who or what made an approval decision: a
  person, a rule set in advance, an agent, or another system. Every decision
  carries one, so the audit trail and the receipt always say who decided.
* :class:`ApprovalModeV1` says when a step waits for a decision, and
  :class:`ApproverPolicyV1` says which kinds of approver may decide for each
  class of step. The default policy lets only a person decide, and a
  ``clinical_write`` step accepts a non-person approver only after an explicit
  opt-in.
* :class:`ReadBackTier` and :class:`ReadBackOutcome` are the coarse,
  customer-facing verification ladder. Only an independent system read-back
  that confirmed the save counts as clean. A screen read-back or no check is
  always a reason for per-item review.

Only a person's decision is a training label (:func:`is_training_label`).
Rule, agent and system decisions are recorded, never learned from.

Nothing here approves anything. Callers decide; these types record and check.

Legacy fields map onto :class:`ApproverIdentityV1` without loss:

==========================  =================================================
legacy                      ApproverIdentityV1
==========================  =================================================
``decided_by="human"``,     ``kind=person``, ``auth=cloud_user``, ``id`` set,
``approver_kind=            ``auth_method`` from the record
"cloud_user"``
``decided_by="human"``,     ``kind=person``, ``auth=unauthenticated``, no id
``approver_kind=
"unauthenticated"``
``decided_by="automation"`` refused unless the caller names the system's id
``decided_by="unknown"``    refused
==========================  =================================================
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Literal, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

APPROVER_IDENTITY_SCHEMA: Literal["openadapt.approver-identity/v1"] = (
    "openadapt.approver-identity/v1"
)
APPROVAL_RECORD_SCHEMA: Literal["openadapt.approval-record/v1"] = (
    "openadapt.approval-record/v1"
)
APPROVER_POLICY_SCHEMA: Literal["openadapt.approver-policy/v1"] = (
    "openadapt.approver-policy/v1"
)
AUTONOMY_ELIGIBILITY_SCHEMA: Literal["openadapt.autonomy-eligibility/v1"] = (
    "openadapt.autonomy-eligibility/v1"
)

_OPAQUE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$"
_CODE = r"^[a-z][a-z0-9_]{0,63}$"
_SHA256 = r"^sha256:[0-9a-f]{64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------
# Approver identity
# --------------------------------------------------------------------------


class ApproverKind(str, Enum):
    """Who or what made an approval decision."""

    #: A person. The only kind whose decisions are training labels.
    PERSON = "person"
    #: A rule set in advance (field limits, allowlists), named by its digest.
    RULE = "rule"
    #: An agent acting for the customer, such as an MCP client or approver bot.
    AGENT = "agent"
    #: Another program, such as the customer's workflow tool behind a webhook.
    SYSTEM = "system"


class ApproverAuth(str, Enum):
    """How the approver was identified."""

    #: A person signed in to OpenAdapt Cloud. ``id`` is the Cloud user id.
    CLOUD_USER = "cloud_user"
    #: A person who approved in Desktop while signed out. No id.
    UNAUTHENTICATED = "unauthenticated"
    #: A rule evaluated by the runtime. ``policy_ref`` pins the rule set.
    POLICY = "policy"
    #: A local command on the runner, run by the named principal.
    LOCAL_CLI = "local_cli"
    #: A signed HTTP callback from the customer's approver endpoint.
    SIGNED_WEBHOOK = "signed_webhook"
    #: An authenticated MCP session of a named agent.
    MCP_SESSION = "mcp_session"


#: How a Cloud sign-in was made. ``none`` for every non-Cloud approver.
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
    """Who or what approved or declined one step. PHI-free by construction.

    ``id`` is an opaque identifier (a Cloud user id, a rule id, an agent id,
    a system id), never a name or free text.
    """

    schema_version: Literal["openadapt.approver-identity/v1"] = APPROVER_IDENTITY_SCHEMA
    kind: ApproverKind
    auth: ApproverAuth
    id: Optional[StrictStr] = Field(default=None, pattern=_OPAQUE_ID)
    auth_method: ApproverAuthMethod = "none"
    #: ``sha256:<hex>`` of the policy or rule that allowed this approver to
    #: decide (RFC-0002 ``authorization.approver.policy_ref``). Required for
    #: a ``rule`` approver, whose rule set it pins; optional for the others.
    policy_ref: Optional[StrictStr] = Field(default=None, pattern=_SHA256)
    #: The person or organisation an agent or system acts for, when known.
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

    def legacy_decided_by(self) -> Literal["human", "automation"]:
        """The value older ``decided_by`` fields carry for this approver."""

        return "human" if self.is_person else "automation"

    def legacy_approver_kind(self) -> Literal["cloud_user", "unauthenticated"]:
        """The value older ``approver_kind`` fields carry. Person only."""

        if self.auth is ApproverAuth.CLOUD_USER:
            return "cloud_user"
        if self.auth is ApproverAuth.UNAUTHENTICATED:
            return "unauthenticated"
        raise ValueError("older approver_kind fields describe a person in Desktop")


class LegacyApproverRefused(ValueError):
    """A legacy record does not say enough to name its approver."""


def approver_from_legacy(
    *,
    decided_by: str,
    approver_kind: Optional[str] = None,
    approver_id: Optional[str] = None,
    auth_method: str = "none",
    system_id: Optional[str] = None,
) -> ApproverIdentityV1:
    """Map ``decided_by`` / ``approver_kind`` fields onto the identity.

    ``human`` with ``cloud_user`` or ``unauthenticated`` maps exactly.
    ``automation`` maps to a ``system`` approver only when the caller names
    the system (``system_id``); a record that says only "automation" cannot
    say who decided. ``unknown`` and anything else are refused.
    """

    if decided_by == "human":
        if approver_kind == "cloud_user":
            return ApproverIdentityV1(
                kind=ApproverKind.PERSON,
                auth=ApproverAuth.CLOUD_USER,
                id=approver_id,
                auth_method=auth_method,  # type: ignore[arg-type]
            )
        if approver_kind in (None, "unauthenticated"):
            return ApproverIdentityV1(
                kind=ApproverKind.PERSON, auth=ApproverAuth.UNAUTHENTICATED
            )
        raise LegacyApproverRefused(f"unknown approver_kind {approver_kind!r}")
    if decided_by == "automation":
        if system_id is None:
            raise LegacyApproverRefused("an automated decision must name its system")
        return ApproverIdentityV1(
            kind=ApproverKind.SYSTEM, auth=ApproverAuth.LOCAL_CLI, id=system_id
        )
    raise LegacyApproverRefused(f"decided_by {decided_by!r} names no approver")


def is_training_label(approver: ApproverIdentityV1) -> bool:
    """True only for a person's decision.

    Rule, agent and system decisions are logged with the run and kept out of
    every training or calibration set, so a model never learns from its own
    or another program's approvals.
    """

    return approver.kind is ApproverKind.PERSON


# --------------------------------------------------------------------------
# Approval mode and approver policy
# --------------------------------------------------------------------------


class ApprovalModeV1(str, Enum):
    """When a consequential step waits for a decision.

    The values ``each_item``, ``problems_only`` and ``not_gated`` are the
    ones Flow already stores; they keep their meaning.
    """

    #: No pause. For read-only or reversible steps, set explicitly and recorded.
    NOT_GATED = "not_gated"
    #: Every item waits for a decision before its first save. The default.
    EACH_ITEM = "each_item"
    #: The items of one list are decided together, bound to a batch digest,
    #: and any item can be pulled back for its own decision.
    BATCH = "batch"
    #: Exceptions only: an item proceeds when every check passes and waits
    #: for a decision only when something is off. Needs eligibility.
    PROBLEMS_ONLY = "problems_only"

    #: The plain name for ``problems_only``.
    EXCEPTIONS_ONLY = "problems_only"


def resolve_approval_mode_v1(value: object) -> ApprovalModeV1:
    """A missing, malformed or unknown mode means ``each_item``."""

    if isinstance(value, ApprovalModeV1):
        return value
    if isinstance(value, str):
        for mode in ApprovalModeV1:
            if value == mode.value:
                return mode
    return ApprovalModeV1.EACH_ITEM


class StepClass(str, Enum):
    """What kind of step a decision is about."""

    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    CONSEQUENTIAL = "consequential"
    IRREVERSIBLE = "irreversible"
    #: An irreversible write to a clinical record.
    CLINICAL_WRITE = "clinical_write"


_PERSON_ONLY: tuple[ApproverKind, ...] = (ApproverKind.PERSON,)


def _default_allowed() -> dict[StepClass, tuple[ApproverKind, ...]]:
    return {step_class: _PERSON_ONLY for step_class in StepClass}


class ApproverPolicyRefusal(str, Enum):
    """Closed reasons a policy refuses an approver."""

    KIND_NOT_ALLOWED = "approver_kind_not_allowed"
    CLINICAL_WRITE_NEEDS_OPT_IN = "clinical_write_needs_opt_in"


class ApproverPolicyV1(_Strict):
    """Which kinds of approver may decide each class of step.

    The default allows only a person for every class. A deployment may
    widen a class to rules, agents or systems. ``clinical_write`` accepts a
    non-person approver only when ``clinical_write_opt_in_sha256`` pins the
    customer's signed opt-in.
    """

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
        """``None`` when ``approver`` may decide a ``step_class`` step."""

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


# --------------------------------------------------------------------------
# Verification ladder
# --------------------------------------------------------------------------


class ReadBackTier(IntEnum):
    """How a save was checked, coarse and customer-facing. Lower is stronger.

    The names and :attr:`label` values are RFC-0002's ``verification_tier``
    (``independent_readback``, ``screen_readback``, ``unchecked``). When a
    save has several checks, the weakest wins.

    * ``INDEPENDENT_READBACK``: a read of the system of record that doesn't
      go through the UI: REST, SQL, FHIR, GraphQL or a file export (Flow
      tier 1).
    * ``SCREEN_READBACK``: any read through the UI, in a separate session
      (Flow tier 2), the acting session (tier 3) or the acting screen
      (tier 4).
    * ``UNCHECKED``: nothing read the saved result back, the store was
      unreachable or stale, or no tier was declared.

    Only a confirmed ``INDEPENDENT_READBACK`` can make a save clean.
    """

    INDEPENDENT_READBACK = 1
    SCREEN_READBACK = 2
    UNCHECKED = 3

    @property
    def label(self) -> VerificationTierLabel:
        """The RFC-0002 wire value, such as ``independent_readback``."""

        return _TIER_LABELS[self]

    @classmethod
    def from_label(cls, label: str) -> "ReadBackTier":
        """Parse an RFC-0002 ``verification_tier``. Unknown means unchecked."""

        for tier, text in _TIER_LABELS.items():
            if text == label:
                return tier
        return cls.UNCHECKED

    def satisfies(self, minimum: "ReadBackTier") -> bool:
        return int(self) <= int(minimum)


#: RFC-0002 ``verification_tier`` wire values.
VerificationTierLabel = Literal["independent_readback", "screen_readback", "unchecked"]

_TIER_LABELS: Mapping[ReadBackTier, VerificationTierLabel] = {
    ReadBackTier.INDEPENDENT_READBACK: "independent_readback",
    ReadBackTier.SCREEN_READBACK: "screen_readback",
    ReadBackTier.UNCHECKED: "unchecked",
}


class ReadBackOutcome(str, Enum):
    """What the read-back found."""

    CONFIRMED = "confirmed"
    #: The record does not match what was entered.
    REFUTED = "refuted"
    #: The read-back ran but could not decide.
    INDETERMINATE = "indeterminate"
    #: No read-back ran.
    NONE = "none"

    @classmethod
    def from_effect_verdict(cls, verdict: Optional[str]) -> "ReadBackOutcome":
        """Parse RFC-0002 ``effect_verdict`` (``CONFIRMED``...) or a Flow
        verdict (``confirmed``...). Anything else is ``NONE``."""

        text = (verdict or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        return cls.NONE


#: Fine-grained strength names (``EffectStrengthV1``, ``RunnerCapability``,
#: Flow's ``VerificationTier`` names) onto the coarse ladder.
_TIER_BY_STRENGTH: Mapping[str, ReadBackTier] = {
    # RFC-0002 labels.
    "independent_readback": ReadBackTier.INDEPENDENT_READBACK,
    "screen_readback": ReadBackTier.SCREEN_READBACK,
    "independent_system_of_record": ReadBackTier.INDEPENDENT_READBACK,
    "independent_system": ReadBackTier.INDEPENDENT_READBACK,
    # A UI read in a separate session is still a screen read.
    "independent_session": ReadBackTier.SCREEN_READBACK,
    "persisted_state_reacquisition": ReadBackTier.SCREEN_READBACK,
    "immediate_screen_confirmation": ReadBackTier.SCREEN_READBACK,
    "immediate_screen": ReadBackTier.SCREEN_READBACK,
    # ``OracleTier`` names (the Seal ladder).
    "system_of_record": ReadBackTier.INDEPENDENT_READBACK,
    "counterparty": ReadBackTier.INDEPENDENT_READBACK,
    "visual": ReadBackTier.SCREEN_READBACK,
}


def _strength_names(strength: object) -> list[str]:
    names: list[str] = []
    if isinstance(strength, Enum):
        names.append(str(strength.name).lower())
        if isinstance(strength.value, str):
            names.append(strength.value)
    elif isinstance(strength, str):
        names.append(strength)
    return names


def read_back_tier(strength: object) -> ReadBackTier:
    """Map a fine-grained strength onto the ladder. Unknown means unchecked.

    Accepts an enum member (by ``value`` or, for ``IntEnum`` tiers, by
    ``name``) or a string. ``EffectVerificationTier`` values 1 to 4 map by
    name. Anything not recognised is ``UNCHECKED``, never stronger.
    """

    if strength is None:
        return ReadBackTier.UNCHECKED
    if isinstance(strength, ReadBackTier):
        return strength
    for name in _strength_names(strength):
        if name == "independent_system_interface":
            return ReadBackTier.INDEPENDENT_READBACK
        if name in _TIER_BY_STRENGTH:
            return _TIER_BY_STRENGTH[name]
    return ReadBackTier.UNCHECKED


#: Closed reasons a save needs its own review.
ReviewReason = Literal[
    "read_back_refuted",
    "read_back_indeterminate",
    "screen_readback_only",
    "not_checked",
    "below_minimum_tier",
]


def per_item_review_reason(
    tier: ReadBackTier,
    outcome: ReadBackOutcome,
    *,
    minimum: Optional[ReadBackTier] = None,
) -> Optional[ReviewReason]:
    """Why a save must be reviewed on its own, or ``None`` when it is clean.

    Only an independent read-back (not through the UI) that confirmed the
    save is clean. A refuted or indeterminate read-back, a screen-only read-back and no
    check at all each return a reason, whatever the policy minimum is.
    ``below_minimum_tier`` means the check was weaker than the policy's
    ``minimum``, which callers treat as a stop, not only a review.
    """

    if outcome is ReadBackOutcome.REFUTED:
        return "read_back_refuted"
    if outcome is ReadBackOutcome.INDETERMINATE:
        return "read_back_indeterminate"
    if tier is ReadBackTier.UNCHECKED or outcome is ReadBackOutcome.NONE:
        return "not_checked"
    if minimum is not None and not tier.satisfies(minimum):
        return "below_minimum_tier"
    if tier is ReadBackTier.SCREEN_READBACK:
        return "screen_readback_only"
    return None


def counts_as_clean(tier: ReadBackTier, outcome: ReadBackOutcome) -> bool:
    """True only for a confirmed independent read-back (not through the UI)."""

    return per_item_review_reason(tier, outcome) is None


# --------------------------------------------------------------------------
# Approval record
# --------------------------------------------------------------------------

#: Where a decision was made.
ApprovalRoute = Literal[
    "desktop_run_it",
    "review_item",
    "review_batch",
    "local_cli",
    "webhook",
    "mcp",
    "rule",
]


class ApprovalRecordV1(_Strict):
    """One approval decision, as it goes into the audit trail and receipt.

    PHI-free: digests, codes and opaque ids only.
    """

    schema_version: Literal["openadapt.approval-record/v1"] = APPROVAL_RECORD_SCHEMA
    workflow_id: StrictStr = Field(pattern=_OPAQUE_ID)
    step_id: StrictStr = Field(min_length=1, max_length=256)
    step_class: StepClass
    mode: ApprovalModeV1
    decision: Literal["approved", "declined"]
    decline_reason: Optional[StrictStr] = Field(default=None, pattern=_CODE)
    approver: ApproverIdentityV1
    route: ApprovalRoute
    #: The exact request the approver saw.
    request_sha256: StrictStr = Field(pattern=_SHA256)
    #: For ``batch`` decisions, the digest of the batch the approver saw.
    batch_sha256: Optional[StrictStr] = Field(default=None, pattern=_SHA256)
    #: The policy that allowed this approver.
    policy_sha256: Optional[StrictStr] = Field(default=None, pattern=_SHA256)
    #: RFC 3339 time of the decision.
    decided_at: StrictStr = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
    )

    @model_validator(mode="after")
    def _consistent(self) -> "ApprovalRecordV1":
        if (self.decision == "declined") != (self.decline_reason is not None):
            raise ValueError("a declined step needs exactly one closed reason")
        if self.mode is ApprovalModeV1.NOT_GATED:
            raise ValueError("a not_gated step records no approval")
        if (self.route == "review_batch") != (self.batch_sha256 is not None):
            raise ValueError("a batch decision, and only one, names its batch")
        if (self.route == "rule") != (self.approver.kind is ApproverKind.RULE):
            raise ValueError("the rule route is for rule approvers only")
        return self

    @property
    def training_label(self) -> bool:
        """True only when a person decided. See :func:`is_training_label`."""

        return is_training_label(self.approver)


# --------------------------------------------------------------------------
# Graduated autonomy: eligibility for exceptions only
# --------------------------------------------------------------------------


class AutonomyEligibilityV1(_Strict):
    """Evidence that one workflow may run in ``problems_only`` mode.

    Produced by the decision-triage engine from its calibration artifact and
    its audit sample of clean saves that a person reviewed anyway.
    """

    schema_version: Literal["openadapt.autonomy-eligibility/v1"] = (
        AUTONOMY_ELIGIBILITY_SCHEMA
    )
    workflow_id: StrictStr = Field(pattern=_OPAQUE_ID)
    calibration_artifact_sha256: StrictStr = Field(pattern=_SHA256)
    #: Verified runs on record for this workflow version.
    verified_runs: int = Field(ge=0)
    #: Clean saves a person reviewed in the audit sample.
    audited_items: int = Field(ge=0)
    #: Audited saves the person declined.
    audit_misses: int = Field(ge=0)
    #: A one-sided upper confidence bound on the miss rate.
    audit_miss_rate_upper_bound: float = Field(ge=0.0, le=1.0)
    #: The rate the bound must stay under.
    target_miss_rate: float = Field(gt=0.0, le=1.0)
    #: The share of clean saves still sent for audit, at least 1%.
    audit_fraction: float = Field(ge=0.01, le=1.0)
    evaluated_at: StrictStr = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
    )

    @model_validator(mode="after")
    def _counts(self) -> "AutonomyEligibilityV1":
        if self.audit_misses > self.audited_items:
            raise ValueError("audit_misses cannot exceed audited_items")
        return self

    @property
    def eligible(self) -> bool:
        return (
            self.audited_items > 0
            and self.audit_miss_rate_upper_bound < self.target_miss_rate
        )


class EligibilityUnavailable(LookupError):
    """No eligibility evidence exists yet for a workflow."""


@runtime_checkable
class AutonomyEligibilityProvider(Protocol):
    """Supplies :class:`AutonomyEligibilityV1` for a workflow.

    Raise :class:`EligibilityUnavailable` when there is no evidence. A caller
    must then keep the workflow at ``each_item`` or ``batch``.
    """

    def eligibility(self, workflow_id: str) -> AutonomyEligibilityV1: ...


__all__ = [
    "APPROVAL_RECORD_SCHEMA",
    "APPROVER_IDENTITY_SCHEMA",
    "APPROVER_POLICY_SCHEMA",
    "AUTONOMY_ELIGIBILITY_SCHEMA",
    "ApprovalModeV1",
    "ApprovalRecordV1",
    "ApprovalRoute",
    "ApproverAuth",
    "ApproverAuthMethod",
    "ApproverIdentityV1",
    "ApproverKind",
    "ApproverPolicyRefusal",
    "ApproverPolicyV1",
    "AutonomyEligibilityProvider",
    "AutonomyEligibilityV1",
    "EligibilityUnavailable",
    "LegacyApproverRefused",
    "ReadBackOutcome",
    "ReadBackTier",
    "VerificationTierLabel",
    "ReviewReason",
    "StepClass",
    "approver_from_legacy",
    "counts_as_clean",
    "is_training_label",
    "per_item_review_reason",
    "read_back_tier",
    "resolve_approval_mode_v1",
]
