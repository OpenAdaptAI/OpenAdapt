"""Privacy-safe, signed contracts for attended human decisions.

Two models close one round trip:

* :class:`HumanDecisionTaskV1` and :class:`HumanDecisionTaskV2` — portable
  projections of an existing runtime pause. V2 additionally binds a safe
  qualification-approved entity label to its exact qualified step. Neither is
  an execution capability: a consumer must still present the runtime's
  separately issued capability, satisfy its authentication policy, and pass
  fresh revalidation before actuation.
* :class:`HumanDecisionReceiptV1` — the answer's terminal outcome. The only
  decision result that may cross to a phone, a tray, or an authenticated
  remote relay.

Free-form text, screenshots, identifiers, and observed values deliberately do
not belong in either contract.

Cloud-safe by construction
--------------------------
There are two versioned task models and one receipt model, and all are
Cloud-safe; there is no "local extension" variant that a producer could
accidentally relay. Every string-typed field is a ``Literal``, an ``Enum``
member, or carries an explicit ``pattern``, and every model forbids unknown
fields. Raw values, OCR text, screenshots, and operator prose therefore have no
field to travel in, and a new field cannot quietly open one:
``tests/test_human_decision.py`` walks both exported JSON Schemas and fails on
any string that is not closed by a pattern, ``const``, or ``enum``.

The receipt matters most here. A runtime's own decision record is an
append-only audit artifact that carries operator prose and an operator
principal on purpose (Flow's ``AttendedDecision`` has both). A producer must
therefore *rebuild* a receipt rather than redact its audit record field by
field: strip-on-send stays correct only until someone adds a field, whereas a
closed target type has no such failure mode. ``reason_code`` is a closed enum
precisely so a consumer renders deterministic copy instead of relaying a
runtime's free-text message.

Signature canonicalization (normative)
--------------------------------------
An HMAC over either contract must be reproducible byte-for-byte in any
language, so the encoding is fixed rather than left to a JSON library's
defaults. The rules below are written for the task; the receipt uses the same
rules verbatim, with its own domain separator:

1. Start from ``unsigned_payload()``: the model serialized in JSON mode with
   ``signature`` removed. Every other field is always present, including those
   whose value is ``null``; optional fields are never omitted.
2. Serialize with keys sorted ascending by Unicode code point, at every nesting
   level. All keys in this contract are ASCII by construction, so code-point
   order and UTF-16 code-unit order agree and a JavaScript implementation may
   use ``Object.keys().sort()``.
3. Use no insignificant whitespace: ``,`` between items and ``:`` between a key
   and its value, with no spaces.
4. Escape all non-ASCII characters as ``\\uXXXX`` (Python's
   ``ensure_ascii=True``). The canonical form is therefore pure ASCII, and a
   consumer never has to agree on a Unicode normalization form.
5. Encode the result as UTF-8.
6. Prepend the version-matched domain separator (for example,
   ``b"openadapt.human-decision-task/v1\\x00"`` or
   ``b"openadapt.human-decision-task/v2\\x00"``) before computing the HMAC,
   so a signature over this contract can never be replayed as a signature over
   a different OpenAdapt payload or task version. The receipt's separator is
   ``b"openadapt.human-decision-receipt/v1\\x00"``, so a task signature can
   never be replayed as a receipt signature either. The digests in
   :attr:`HumanDecisionTaskV1.digest`, :attr:`HumanDecisionTaskV2.digest`, and
   :attr:`HumanDecisionReceiptV1.digest` are taken over the canonical bytes
   *without* the domain separator.

Only integers and booleans appear as non-string scalars, and every integer is
range-bounded. No float, decimal, or timestamp object is ever serialized, which
removes the usual cross-language number-formatting hazard. Timestamps travel as
pattern-checked RFC 3339 strings and are compared, never reformatted: a signer
must not normalize ``Z`` to ``+00:00`` or vice versa, because that would change
the signed bytes.

``tests/test_human_decision.py`` pins the exact canonical bytes and the exact
signature hex of one fixed task and one fixed receipt. Any change to field
names, field order handling, escaping, or a domain separator will fail those
vectors, which is the intended signal to cut a new schema version rather than
to silently re-sign.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

HUMAN_DECISION_TASK_SCHEMA = "openadapt.human-decision-task/v1"
HUMAN_DECISION_TASK_V2_SCHEMA = "openadapt.human-decision-task/v2"
HUMAN_DECISION_RECEIPT_SCHEMA = "openadapt.human-decision-receipt/v1"
_SIGNING_DOMAIN = b"openadapt.human-decision-task/v1\x00"
_V2_SIGNING_DOMAIN = b"openadapt.human-decision-task/v2\x00"
_RECEIPT_SIGNING_DOMAIN = b"openadapt.human-decision-receipt/v1\x00"
_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^hmac-sha256:[0-9a-f]{64}$"
#: RFC 3339 instant with a required offset, permitting both ``Z`` and a numeric
#: offset because producers differ. Bounding the shape here, not only in the
#: Python validator, is what keeps a non-Python consumer that validates against
#: the exported JSON Schema from accepting free text in a timestamp field.
_TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
)
# Python's datetime parser preserves at most microseconds.  V2 does not accept
# precision it cannot compare faithfully.  V1 retains its established wire
# contract and pattern unchanged.
_V2_TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)
# A qualified label is presentation metadata selected when a workflow is
# qualified.  It is not an observation, an identifier, or a runtime-derived
# value.  Keep the alphabet deliberately small so the shared contract cannot
# become a general-purpose text channel.
_QUALIFIED_ENTITY_LABEL_PATTERN = r"^[a-z][a-z0-9]*(?:[ _-][a-z0-9]+){0,3}$"


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HumanDecisionTaskKind(str, Enum):
    IDENTITY = "identity"
    EFFECT = "effect"
    AMBIGUITY = "ambiguity"
    HUMAN_STEP = "human_step"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    HALT = "halt"
    OPERATOR_REVIEW = "operator_review"


class HumanDecisionQuestionTemplate(str, Enum):
    CONFIRM_IDENTITY = "confirm_identity"
    CONFIRM_PERSISTED_EFFECT = "confirm_persisted_effect"
    RESOLVE_AMBIGUITY = "resolve_ambiguity"
    COMPLETE_HUMAN_STEP = "complete_human_step"
    REVIEW_UNCERTAIN_DELIVERY = "review_uncertain_delivery"
    REVIEW_HALT = "review_halt"


class HumanDecisionAction(str, Enum):
    """Portable action names; the runtime remains authoritative.

    ``verify_and_resume`` maps to Flow's attended ``continue`` operation. It
    means that the operator prepared the live state for fresh revalidation; it
    never authorizes blind repetition of a prior action.

    ``reject`` and ``escalate`` are deliberately separate members rather than
    two labels on one action, because they do opposite things to the run.
    ``escalate`` *parks* it: the durable pause stays intact and a qualified
    colleague can still continue it. ``reject`` *terminates* it: the operator
    looked at the live application and is asserting that this run must not
    proceed at all. Collapsing the two would leave the recorded answer
    distribution unable to distinguish "I don't know" from "stop", which is the
    only reason a disagreement action is worth its cost.

    ``reject`` is scoped to one run. It is not a claim that the saved workflow
    is wrong; that assertion changes future runs and belongs to ``teach``,
    which carries the demonstration and requalification gate such authority
    requires.

    ``reconcile`` asks the runner to re-establish the business effect after an
    uncertain delivery. It is intentionally distinct from
    ``verify_and_resume``: reconciliation may prove that the original action
    already succeeded, and therefore must never imply permission to dispatch
    that action again.
    """

    VERIFY_AND_RESUME = "verify_and_resume"
    SKIP = "skip"
    REJECT = "reject"
    TEACH = "teach"
    ESCALATE = "escalate"
    RECONCILE = "reconcile"


class HumanDecisionDeliveryState(str, Enum):
    NOT_DELIVERED = "not_delivered"
    DELIVERED = "delivered"
    UNKNOWN = "unknown"


class HumanDecisionRiskClass(str, Enum):
    READ_ONLY = "read_only"
    STATE_CHANGING = "state_changing"
    CONSEQUENTIAL = "consequential"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


class HumanDecisionSubstrate(str, Enum):
    BROWSER = "browser"
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    RDP = "rdp"
    CITRIX = "citrix"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class HumanDecisionRequiredAuthn(str, Enum):
    LOCAL_SESSION = "local_session"
    AAL2 = "aal2"
    WEBAUTHN = "webauthn"


class HumanDecisionEntityFallback(str, Enum):
    """Domain-neutral text used when a consumer cannot render the label."""

    RECORD = "record"
    ITEM = "item"


class HumanDecisionQualifiedEntityV1(_StrictContract):
    """A label approved by the bound qualification contract.

    The producer must read this value from the exact qualified contract.  This
    type intentionally has no observation, screenshot, OCR, parameter, or
    model-input field, so those sources cannot cross the decision boundary.
    """

    label: StrictStr = Field(
        min_length=1,
        max_length=63,
        pattern=_QUALIFIED_ENTITY_LABEL_PATTERN,
    )
    fallback: HumanDecisionEntityFallback


class HumanDecisionSafeSlotsV1(_StrictContract):
    """Bounded numeric context safe to relay outside the evidence boundary."""

    candidate_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    required_signal_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    confirmed_signal_count: StrictInt | None = Field(default=None, ge=0, le=1000)

    @model_validator(mode="after")
    def _validate_counts(self) -> "HumanDecisionSafeSlotsV1":
        if (
            self.required_signal_count is not None
            and self.confirmed_signal_count is not None
            and self.confirmed_signal_count > self.required_signal_count
        ):
            raise ValueError("confirmed_signal_count cannot exceed required_signal_count")
        return self


class HumanDecisionQuestionV1(_StrictContract):
    template: HumanDecisionQuestionTemplate
    safe_slots: HumanDecisionSafeSlotsV1 = Field(
        default_factory=HumanDecisionSafeSlotsV1
    )


class HumanDecisionEvidenceSummaryV1(_StrictContract):
    identity_required_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    identity_confirmed_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    effect_required_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    effect_confirmed_count: StrictInt | None = Field(default=None, ge=0, le=1000)
    minimum_effect_tier: StrictInt | None = Field(default=None, ge=1, le=4)
    observed_effect_tier: StrictInt | None = Field(default=None, ge=1, le=4)
    frame_available_locally: StrictBool = False
    sensitive_evidence_local_only: Literal[True] = True

    @model_validator(mode="after")
    def _validate_coverage(self) -> "HumanDecisionEvidenceSummaryV1":
        pairs = (
            (self.identity_required_count, self.identity_confirmed_count),
            (self.effect_required_count, self.effect_confirmed_count),
        )
        if any(
            required is not None
            and confirmed is not None
            and confirmed > required
            for required, confirmed in pairs
        ):
            raise ValueError("confirmed evidence cannot exceed required evidence")
        return self


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Encode one payload under the normative rules in the module docstring.

    Each argument is load-bearing and none may be relaxed: ``sort_keys`` fixes
    key order at every level, ``separators`` removes insignificant whitespace,
    and ``ensure_ascii`` makes the output pure ASCII so no consumer has to agree
    on a Unicode normalization form. Callers that need the signed form must go
    through :meth:`HumanDecisionTaskV1.unsigned_payload` rather than dumping the
    model themselves, so that ``signature`` exclusion stays part of the rule.
    """

    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class HumanDecisionTaskV1(_StrictContract):
    """A signed, PHI-free projection of one exact attended runtime pause."""

    schema_version: Literal["openadapt.human-decision-task/v1"] = (
        HUMAN_DECISION_TASK_SCHEMA
    )
    task_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    task_revision: StrictInt = Field(default=1, ge=1)
    tenant_id: StrictStr | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    runner_id: StrictStr | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    run_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    pause_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    capability_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    bundle_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    task_kind: HumanDecisionTaskKind
    delivery_state: HumanDecisionDeliveryState
    risk_class: HumanDecisionRiskClass = HumanDecisionRiskClass.UNKNOWN
    substrate: HumanDecisionSubstrate = HumanDecisionSubstrate.UNKNOWN
    question: HumanDecisionQuestionV1
    evidence: HumanDecisionEvidenceSummaryV1
    #: Upper bound is the size of the closed vocabulary, not a policy: a pause
    #: can legitimately offer every member at once. Widening it is what a new
    #: member costs, and it is why a consumer must re-validate against the
    #: current schema rather than a cached copy.
    allowed_actions: tuple[HumanDecisionAction, ...] = Field(min_length=1, max_length=6)
    required_authn: HumanDecisionRequiredAuthn
    created_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_TIMESTAMP_PATTERN
    )
    expires_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_TIMESTAMP_PATTERN
    )
    nonce: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    signature_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    signature: StrictStr = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _validate_envelope(self) -> "HumanDecisionTaskV1":
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("allowed_actions must be unique")
        if (
            HumanDecisionAction.RECONCILE in self.allowed_actions
            and self.delivery_state is HumanDecisionDeliveryState.NOT_DELIVERED
        ):
            raise ValueError(
                "reconcile requires a delivered or delivery-uncertain action"
            )
        created_at = _parse_timestamp(self.created_at, "created_at")
        expires_at = _parse_timestamp(self.expires_at, "expires_at")
        if expires_at <= created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    def unsigned_payload(self) -> dict[str, Any]:
        """Return the deterministic language-agnostic signed payload."""

        return self.model_dump(mode="json", exclude={"signature"})

    def canonical_unsigned_bytes(self) -> bytes:
        return _canonical_json(self.unsigned_payload())

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_unsigned_bytes()).hexdigest()

    def verify_hmac(self, key: bytes) -> bool:
        """Verify the task signature without granting execution authority."""

        _validate_hmac_key(key)
        expected = _hmac_signature(key, self.unsigned_payload(), _SIGNING_DOMAIN)
        return hmac.compare_digest(expected, self.signature)


class HumanDecisionTaskV2(HumanDecisionTaskV1):
    """V2 task with qualification-bound, safe entity presentation metadata.

    V2 is a separate signed wire format.  It does not alter V1 canonical
    bytes, validation, schema ID, or signing domain.
    """

    schema_version: Literal["openadapt.human-decision-task/v2"] = (
        HUMAN_DECISION_TASK_V2_SCHEMA
    )
    # Cloud's JavaScript reader accepts signed revisions only through this
    # maximum.  V2 must not sign a value that an existing consumer cannot
    # round-trip; V1 remains unchanged for byte compatibility.
    task_revision: StrictInt = Field(default=1, ge=1, le=2_147_483_647)
    created_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_V2_TIMESTAMP_PATTERN
    )
    expires_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_V2_TIMESTAMP_PATTERN
    )
    qualification_project_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    qualification_revision_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    qualification_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    qualification_step_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    entity: HumanDecisionQualifiedEntityV1

    def verify_hmac(self, key: bytes) -> bool:
        """Verify V2 only under its distinct domain separator."""

        _validate_hmac_key(key)
        expected = _hmac_signature(key, self.unsigned_payload(), _V2_SIGNING_DOMAIN)
        return hmac.compare_digest(expected, self.signature)


def _validate_hmac_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("human decision HMAC key must contain at least 32 bytes")


def _hmac_signature(key: bytes, payload: Mapping[str, Any], domain: bytes) -> str:
    """Sign one canonical payload under an explicit, contract-specific domain.

    ``domain`` is a required positional argument rather than a default so that
    adding a third contract cannot silently reuse the task's separator and make
    one contract's signature replayable as another's.
    """

    digest = hmac.new(key, domain + _canonical_json(payload), hashlib.sha256)
    return "hmac-sha256:" + digest.hexdigest()


def sign_human_decision_task_hmac(
    *, key: bytes, fields: Mapping[str, Any]
) -> HumanDecisionTaskV1:
    """Validate and sign a task using the local-v1 HMAC profile."""

    _validate_hmac_key(key)
    if "signature" in fields:
        raise ValueError("fields must not contain a signature")
    unsigned = dict(fields)
    unsigned.setdefault("schema_version", HUMAN_DECISION_TASK_SCHEMA)
    unsigned.setdefault("signature_algorithm", "hmac-sha256")
    validated = HumanDecisionTaskV1.model_validate(
        {**unsigned, "signature": "hmac-sha256:" + "0" * 64}
    )
    canonical_unsigned = validated.unsigned_payload()
    signature = _hmac_signature(key, canonical_unsigned, _SIGNING_DOMAIN)
    return validated.model_copy(update={"signature": signature})


def sign_human_decision_task_v2_hmac(
    *, key: bytes, fields: Mapping[str, Any]
) -> HumanDecisionTaskV2:
    """Validate and sign a qualification-bound task using the V2 profile."""

    _validate_hmac_key(key)
    if "signature" in fields:
        raise ValueError("fields must not contain a signature")
    unsigned = dict(fields)
    unsigned.setdefault("schema_version", HUMAN_DECISION_TASK_V2_SCHEMA)
    unsigned.setdefault("signature_algorithm", "hmac-sha256")
    validated = HumanDecisionTaskV2.model_validate(
        {**unsigned, "signature": "hmac-sha256:" + "0" * 64}
    )
    signature = _hmac_signature(key, validated.unsigned_payload(), _V2_SIGNING_DOMAIN)
    return validated.model_copy(update={"signature": signature})


class HumanDecisionReceiptState(str, Enum):
    """Terminal outcome of one attended decision, as the runtime reports it.

    ``ACCEPTED_PENDING_RUNNER`` and ``DELIVERY_UNCERTAIN`` are the two
    non-success outcomes a phone must be able to show. ``DELIVERY_UNCERTAIN``
    is the "may have been sent" state: it exists so that losing the network
    after a tap can be reported honestly instead of collapsing into either a
    success or a refusal.

    ``DEMONSTRATION_REQUESTED`` and ``ESCALATED`` are separate terminal states
    rather than variants of ``ACCEPTED_PENDING_RUNNER``: a ``teach`` or
    ``escalate`` decision is durably recorded and returns immediately without
    any runner continuation pending, so folding them into "accepted, pending"
    would tell an operator to wait for something that is never coming.

    ``REJECTED`` is the outcome of a ``reject`` decision and is distinct from
    both of those and from ``HALTED``. ``ESCALATED`` and
    ``DEMONSTRATION_REQUESTED`` leave the run paused and resumable;
    ``REJECTED`` ends it. ``HALTED`` is the engine's own verdict after it acted
    on an answer, whereas ``REJECTED`` records that a human ended the run
    without the engine attempting anything further. A consumer that collapsed
    them would tell the operator the machine stopped when in fact they did.
    """

    ACCEPTED_PENDING_RUNNER = "accepted_pending_runner"
    COMPLETED = "completed"
    REFUSED = "refused"
    HALTED = "halted"
    EXPIRED = "expired"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    DEMONSTRATION_REQUESTED = "demonstration_requested"
    ESCALATED = "escalated"
    REJECTED = "rejected"


class HumanDecisionReceiptReason(str, Enum):
    """Closed cause for a terminal state; the consumer renders copy from it.

    This is the field that would otherwise be a free-text ``message``. It is a
    closed enum, never an arbitrary string, so a runtime cannot relay operator
    prose, an exception message, an observed value, or a record identifier
    through the receipt's explanation.
    """

    PENDING_RUNNER = "pending_runner"
    VERIFIED_AND_RESUMED = "verified_and_resumed"
    SKIPPED_AND_RESUMED = "skipped_and_resumed"
    RECONCILED_AND_RESUMED = "reconciled_and_resumed"
    CONTINUATION_HALTED = "continuation_halted"
    REVALIDATION_REFUSED = "revalidation_refused"
    EXPIRED = "expired"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    DEMONSTRATION_REQUESTED = "demonstration_requested"
    ESCALATION_RECORDED = "escalation_recorded"
    #: The only cause of ``REJECTED``. It is a single closed member on purpose:
    #: a *reason* taxonomy for disagreement would be more informative, but
    #: there is no evidence yet for what its members should be -- the reject
    #: rate is the data needed to design them -- and a wrong taxonomy is worse
    #: than none. Adding members later is additive here and does not reopen a
    #: free-text hole, which is why the cause is an enum from the start rather
    #: than a string that a later change could widen.
    REJECTED_BY_OPERATOR = "rejected_by_operator"


#: Which causes each terminal state may carry.
#:
#: ``state`` and ``reason_code`` are separate fields because one state can have
#: more than one cause -- ``completed`` is reached both by resuming after
#: verification and by resuming after a skip, and a consumer must be able to
#: tell those apart without parsing prose. They are not independent, though: an
#: unconstrained pair would let a producer emit ``completed`` with reason
#: ``expired``, so the permitted combinations are pinned here and enforced by a
#: model validator.
HUMAN_DECISION_RECEIPT_REASONS: Mapping[
    HumanDecisionReceiptState, frozenset[HumanDecisionReceiptReason]
] = {
    HumanDecisionReceiptState.ACCEPTED_PENDING_RUNNER: frozenset(
        {HumanDecisionReceiptReason.PENDING_RUNNER}
    ),
    HumanDecisionReceiptState.COMPLETED: frozenset(
        {
            HumanDecisionReceiptReason.VERIFIED_AND_RESUMED,
            HumanDecisionReceiptReason.SKIPPED_AND_RESUMED,
            HumanDecisionReceiptReason.RECONCILED_AND_RESUMED,
        }
    ),
    HumanDecisionReceiptState.REFUSED: frozenset(
        {HumanDecisionReceiptReason.REVALIDATION_REFUSED}
    ),
    HumanDecisionReceiptState.HALTED: frozenset(
        {HumanDecisionReceiptReason.CONTINUATION_HALTED}
    ),
    HumanDecisionReceiptState.EXPIRED: frozenset({HumanDecisionReceiptReason.EXPIRED}),
    HumanDecisionReceiptState.DELIVERY_UNCERTAIN: frozenset(
        {HumanDecisionReceiptReason.DELIVERY_UNCERTAIN}
    ),
    HumanDecisionReceiptState.DEMONSTRATION_REQUESTED: frozenset(
        {HumanDecisionReceiptReason.DEMONSTRATION_REQUESTED}
    ),
    HumanDecisionReceiptState.ESCALATED: frozenset(
        {HumanDecisionReceiptReason.ESCALATION_RECORDED}
    ),
    HumanDecisionReceiptState.REJECTED: frozenset(
        {HumanDecisionReceiptReason.REJECTED_BY_OPERATOR}
    ),
}

#: States that report a *successful* effect on the workflow. Everything else is
#: a non-success outcome, so a consumer that wants "did this work?" reads this
#: set rather than inferring from the absence of an error string.
HUMAN_DECISION_RECEIPT_SUCCESS_STATES: frozenset[HumanDecisionReceiptState] = frozenset(
    {HumanDecisionReceiptState.COMPLETED}
)


class HumanDecisionReceiptV1(_StrictContract):
    """The closed, PHI-free terminal outcome of one attended decision.

    This is the only decision result that may cross to a phone, a tray, or an
    authenticated remote relay. It is a rebuilt value, not a filtered copy of
    a runtime's audit record: there is no free-text field, no operator
    identity, no workflow or step label, no parameter, no path, no observed
    value, and no evidence. Protected content is structurally unrepresentable
    rather than stripped on send, so a later field addition on the producer
    side cannot reopen a hole here.

    ``action`` reuses :class:`HumanDecisionAction`, the same portable
    vocabulary the task advertises in ``allowed_actions``, so a consumer
    compares the receipt against the task it answered without translating an
    engine-internal name.

    The digests are one-way commitments, not content: they let a consumer bind
    this receipt to the exact capability, request, decision record, and
    transition receipt it came from without ever receiving those payloads.
    A completed receipt requires ``report_success=true`` and a transition
    receipt digest. A producer cannot use a success-shaped state without the
    corresponding transition commitment.

    ``signature`` is optional because a runtime that returns a receipt over an
    authenticated loopback connection is already inside its own trust boundary
    and does not sign. A receipt that arrives over a network must be signed,
    and its consumer must verify it: see :meth:`verify_hmac`.
    """

    schema_version: Literal["openadapt.human-decision-receipt/v1"] = (
        HUMAN_DECISION_RECEIPT_SCHEMA
    )
    task_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    task_revision: StrictInt = Field(default=1, ge=1)
    pause_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    capability_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    request_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    decision_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    transition_receipt_digest: StrictStr | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    action: HumanDecisionAction
    state: HumanDecisionReceiptState
    reason_code: HumanDecisionReceiptReason
    report_success: StrictBool | None = None
    decided_at: StrictStr = Field(
        min_length=20, max_length=40, pattern=_TIMESTAMP_PATTERN
    )
    signature_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    signature: StrictStr | None = Field(default=None, pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _validate_outcome(self) -> "HumanDecisionReceiptV1":
        permitted = HUMAN_DECISION_RECEIPT_REASONS[self.state]
        if self.reason_code not in permitted:
            raise ValueError(
                f"reason_code {self.reason_code.value!r} is not a cause of state "
                f"{self.state.value!r}"
            )
        is_success = self.state in HUMAN_DECISION_RECEIPT_SUCCESS_STATES
        if self.report_success and not is_success:
            raise ValueError(
                f"report_success cannot be true for state {self.state.value!r}"
            )
        if is_success and self.report_success is not True:
            raise ValueError("a completed receipt requires report_success=true")
        if is_success and self.transition_receipt_digest is None:
            raise ValueError("a completed receipt requires a transition receipt digest")
        completed_action = {
            HumanDecisionReceiptReason.VERIFIED_AND_RESUMED: (
                HumanDecisionAction.VERIFY_AND_RESUME
            ),
            HumanDecisionReceiptReason.SKIPPED_AND_RESUMED: HumanDecisionAction.SKIP,
            HumanDecisionReceiptReason.RECONCILED_AND_RESUMED: (
                HumanDecisionAction.RECONCILE
            ),
        }
        expected_action = completed_action.get(self.reason_code)
        if expected_action is not None and self.action is not expected_action:
            raise ValueError(
                f"reason_code {self.reason_code.value!r} requires action "
                f"{expected_action.value!r}"
            )
        terminal_action = {
            HumanDecisionReceiptState.DEMONSTRATION_REQUESTED: HumanDecisionAction.TEACH,
            HumanDecisionReceiptState.ESCALATED: HumanDecisionAction.ESCALATE,
            HumanDecisionReceiptState.REJECTED: HumanDecisionAction.REJECT,
        }.get(self.state)
        if terminal_action is not None and self.action is not terminal_action:
            raise ValueError(
                f"state {self.state.value!r} requires action {terminal_action.value!r}"
            )
        _parse_timestamp(self.decided_at, "decided_at")
        return self

    @property
    def succeeded(self) -> bool:
        """Whether this receipt reports a successful workflow continuation."""

        return (
            self.state in HUMAN_DECISION_RECEIPT_SUCCESS_STATES
            and self.report_success is True
        )

    def unsigned_payload(self) -> dict[str, Any]:
        """Return the deterministic language-agnostic signed payload."""

        return self.model_dump(mode="json", exclude={"signature"})

    def canonical_unsigned_bytes(self) -> bytes:
        return _canonical_json(self.unsigned_payload())

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_unsigned_bytes()).hexdigest()

    def verify_hmac(self, key: bytes) -> bool:
        """Verify the receipt signature. An unsigned receipt never verifies."""

        _validate_hmac_key(key)
        if self.signature is None:
            return False
        expected = _hmac_signature(
            key, self.unsigned_payload(), _RECEIPT_SIGNING_DOMAIN
        )
        return hmac.compare_digest(expected, self.signature)


def sign_human_decision_receipt_hmac(
    *, key: bytes, fields: Mapping[str, Any]
) -> HumanDecisionReceiptV1:
    """Validate and sign a receipt using the local-v1 HMAC profile."""

    _validate_hmac_key(key)
    if "signature" in fields:
        raise ValueError("fields must not contain a signature")
    unsigned = dict(fields)
    unsigned.setdefault("schema_version", HUMAN_DECISION_RECEIPT_SCHEMA)
    unsigned.setdefault("signature_algorithm", "hmac-sha256")
    validated = HumanDecisionReceiptV1.model_validate(unsigned)
    signature = _hmac_signature(
        key, validated.unsigned_payload(), _RECEIPT_SIGNING_DOMAIN
    )
    return validated.model_copy(update={"signature": signature})
