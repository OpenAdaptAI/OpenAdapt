"""Per-run accounting rows that carry no record values.

One :class:`RunLedgerRowV1` describes one run of one task, whatever did it:
an OpenAdapt agent, a compiled program, a third-party agent or a person. It
answers the questions an operations dashboard asks: did the case finish, was
the save checked, how long did it take, what did the model calls cost, and
how many minutes did a person spend on it.

:class:`AgentRunSummaryV1` is the same row without ``sources`` (local paths),
the shape a runner sends to OpenAdapt Cloud.

No values, by construction
--------------------------
Every string field is a ``Literal``, an ``Enum`` member, or carries an
explicit ``pattern``, and every model forbids unknown fields. A field value,
an OCR string, a screenshot or operator prose has no field to travel in, and a
new field can't quietly open one: ``tests/test_run_ledger.py`` walks both JSON
Schemas and fails on any string that isn't closed. Values the person needs to
see (what was meant to be saved, what was saved, the difference) live only in
a local private sidecar the producer writes beside its store, never here.

The outcome
-----------
``outcome`` is one of five buckets and ``outcome_reason`` is a closed code
that belongs to exactly one bucket (:data:`RUN_LEDGER_REASON_OUTCOME`); a row
whose reason names another bucket is refused. ``UNKNOWN`` is a bucket of its
own and is never folded into another. When a producer's sources disagree, the
stronger finding wins in this order (:data:`RUN_LEDGER_OUTCOME_PRECEDENCE`):
``CAUGHT_ERROR`` > ``UNKNOWN`` > ``ESCALATED`` > ``FAILED`` > ``VERIFIED``.

Unknown is never zero
---------------------
Model calls, tokens, cost and intervention time are ``null`` when a source
doesn't say, never ``0``. ``human.intervention_s`` is ``null`` whenever any
interval has no ``decided_at``, and with no intervals unless a source shows no
person was asked.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Mapping, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

RUN_LEDGER_SCHEMA = "openadapt.run-ledger/v1"
AGENT_RUN_SUMMARY_SCHEMA = "openadapt.agent-run-summary/v1"

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: An opaque task id, as the guard and the run store already use.
_TASK_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
#: An operator label: lower-case words, no spaces, no record identity.
_LABEL_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
#: A field name as a form or an API names it: an identifier, not a value.
_FIELD_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_HEX64_PATTERN = r"^[0-9a-f]{64}$"
#: A path relative to the run directory: no leading slash, and no part that
#: starts with a dot (so no ``..``).
_SOURCE_PATTERN = r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$"
_TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)


class RunLedgerOutcome(str, Enum):
    """The five accounting buckets (``docs/RUN_LEDGER.md`` in openadapt-flow)."""

    #: An independent read-back verified the save and no hold is pending.
    VERIFIED = "VERIFIED"
    #: The run stopped for a person, a person declined it, or a person did
    #: the step (a shadow run: the person's own save is the real one).
    ESCALATED = "ESCALATED"
    #: A check caught a wrong save: refuted read-back, blocked save, or a
    #: shadow-mode field mismatch or wrong record. A person declining an
    #: approval is not a catch on its own.
    CAUGHT_ERROR = "CAUGHT_ERROR"
    #: Crashed, timed out or gave up, with no evidence of a write.
    FAILED = "FAILED"
    #: Nothing settles it. Never folded into another bucket.
    UNKNOWN = "UNKNOWN"


#: Strongest first. When sources disagree the earliest bucket wins.
RUN_LEDGER_OUTCOME_PRECEDENCE: tuple[RunLedgerOutcome, ...] = (
    RunLedgerOutcome.CAUGHT_ERROR,
    RunLedgerOutcome.UNKNOWN,
    RunLedgerOutcome.ESCALATED,
    RunLedgerOutcome.FAILED,
    RunLedgerOutcome.VERIFIED,
)

RunLedgerOutcomeReason = Literal[
    # VERIFIED
    "readback_verified",
    # ESCALATED
    "approval_held",
    "person_declined",
    "approval_declined",
    "question_asked",
    "exception_card",
    "second_factor",
    "halted_before_effect",
    "gate_held",
    "shadow_matched",
    # CAUGHT_ERROR
    "readback_refuted",
    "save_blocked_wrong_record",
    "save_blocked_other_origin",
    "save_blocked_double_save",
    "save_blocked_out_of_scope",
    "gate_refused",
    "shadow_wrong_record",
    "shadow_field_mismatch",
    # FAILED
    "crashed_no_write",
    "timed_out_no_write",
    "agent_gave_up",
    "platform_failed",
    "canceled_no_write",
    "policy_rejected_no_write",
    # UNKNOWN
    "shadow_write_landed",
    "completed_unverified",
    "shadow_not_compared",
    "shadow_extra_target",
    "egress_refused",
    "reconciliation_required",
    "screen_readback_only",
    "lost_reply",
    "write_unconfirmed",
    "saved_awaiting_check",
    "rolled_back",
    "self_approved",
    "observe_only_would_block",
    "schema_unrecognised",
    "source_unreadable",
    "task_id_conflict",
    "unmapped_outcome",
    "no_sources",
]

_V = RunLedgerOutcome.VERIFIED
_E = RunLedgerOutcome.ESCALATED
_C = RunLedgerOutcome.CAUGHT_ERROR
_F = RunLedgerOutcome.FAILED
_U = RunLedgerOutcome.UNKNOWN

#: The one bucket each reason belongs to.
RUN_LEDGER_REASON_OUTCOME: Mapping[str, RunLedgerOutcome] = {
    "readback_verified": _V,
    "approval_held": _E,
    "person_declined": _E,
    "approval_declined": _E,
    "question_asked": _E,
    "exception_card": _E,
    "second_factor": _E,
    "halted_before_effect": _E,
    "gate_held": _E,
    "shadow_matched": _E,
    "readback_refuted": _C,
    "save_blocked_wrong_record": _C,
    "save_blocked_other_origin": _C,
    "save_blocked_double_save": _C,
    "save_blocked_out_of_scope": _C,
    "gate_refused": _C,
    "shadow_wrong_record": _C,
    "shadow_field_mismatch": _C,
    "crashed_no_write": _F,
    "timed_out_no_write": _F,
    "agent_gave_up": _F,
    "platform_failed": _F,
    "canceled_no_write": _F,
    "policy_rejected_no_write": _F,
    "shadow_write_landed": _U,
    "completed_unverified": _U,
    "shadow_not_compared": _U,
    "shadow_extra_target": _U,
    "egress_refused": _U,
    "reconciliation_required": _U,
    "screen_readback_only": _U,
    "lost_reply": _U,
    "write_unconfirmed": _U,
    "saved_awaiting_check": _U,
    "rolled_back": _U,
    "self_approved": _U,
    "observe_only_would_block": _U,
    "schema_unrecognised": _U,
    "source_unreadable": _U,
    "task_id_conflict": _U,
    "unmapped_outcome": _U,
    "no_sources": _U,
}

RunLedgerExecutorKind = Literal["agent", "program", "human", "unknown"]
RunLedgerCostSource = Literal["metered", "reported", "unknown"]
RunLedgerInterventionKind = Literal[
    "approval", "question", "exception", "second_factor"
]
RunLedgerFieldCheck = Literal["matched", "differs", "not_checked"]
RunLedgerReadbackTier = Literal["independent_readback", "screen_readback", "unchecked"]

Count = StrictInt
_Source = Annotated[StrictStr, Field(pattern=_SOURCE_PATTERN, max_length=256)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class RunLedgerModelUsageV1(_Strict):
    """What the run's model calls cost. ``null`` means the source didn't say."""

    calls: Optional[Count] = Field(default=None, ge=0)
    input_tokens: Optional[Count] = Field(default=None, ge=0)
    output_tokens: Optional[Count] = Field(default=None, ge=0)
    cost_usd: Optional[StrictFloat] = Field(default=None, ge=0.0, le=1_000_000.0)
    cost_source: RunLedgerCostSource = "unknown"

    @model_validator(mode="after")
    def _source_matches_cost(self) -> "RunLedgerModelUsageV1":
        if self.cost_usd is None and self.cost_source != "unknown":
            raise ValueError("a null cost_usd has cost_source unknown")
        if self.cost_usd is not None and self.cost_source == "unknown":
            raise ValueError("a known cost_usd names its cost_source")
        return self


class RunLedgerIntervalV1(_Strict):
    """One stretch a person spent on the run: opened, then decided."""

    kind: RunLedgerInterventionKind
    opened_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN, max_length=40)
    decided_at: Optional[StrictStr] = Field(
        default=None, pattern=_TIMESTAMP_PATTERN, max_length=40
    )

    @model_validator(mode="after")
    def _ordered(self) -> "RunLedgerIntervalV1":
        if self.decided_at is not None and _parse(self.decided_at) < _parse(
            self.opened_at
        ):
            raise ValueError("decided_at is before opened_at")
        return self

    def seconds(self) -> Optional[float]:
        if self.decided_at is None:
            return None
        return (_parse(self.decided_at) - _parse(self.opened_at)).total_seconds()


class RunLedgerHumanV1(_Strict):
    """A person's time on the run. ``intervention_s`` is the sum of the
    intervals, and ``null`` when any interval is still open (never ``0``).
    With no intervals it is ``0`` only when a source shows no person was
    asked, else ``null`` (unknown)."""

    intervention_s: Optional[StrictFloat] = Field(default=None, ge=0.0)
    intervals: tuple[RunLedgerIntervalV1, ...] = ()

    @model_validator(mode="after")
    def _sum(self) -> "RunLedgerHumanV1":
        if not self.intervals:
            if self.intervention_s not in (None, 0.0):
                raise ValueError("intervention_s is the sum of the intervals")
            return self
        spans = [interval.seconds() for interval in self.intervals]
        if any(span is None for span in spans):
            if self.intervention_s is not None:
                raise ValueError("an open interval makes intervention_s null")
            return self
        total = round(sum(span for span in spans if span is not None), 3)
        if self.intervention_s is None or abs(self.intervention_s - total) > 0.002:
            raise ValueError("intervention_s is the sum of the intervals")
        return self


class RunLedgerFieldV1(_Strict):
    """A field the run wrote: its name and how its check came out. No value."""

    name: StrictStr = Field(pattern=_FIELD_NAME_PATTERN)
    check: RunLedgerFieldCheck


class _RowBody(_Strict):
    row_id: StrictStr = Field(pattern=_UUID_PATTERN)
    task_id: StrictStr = Field(pattern=_TASK_ID_PATTERN)
    task_type: StrictStr = Field(pattern=_LABEL_PATTERN)
    executor_kind: RunLedgerExecutorKind
    executor_label: StrictStr = Field(pattern=_LABEL_PATTERN)
    #: ``sha256:`` of the salted inputs; the salt is in the private sidecar.
    #: ``null`` when no source names the run's inputs.
    inputs_commitment: Optional[StrictStr] = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    started_at: Optional[StrictStr] = Field(
        default=None, pattern=_TIMESTAMP_PATTERN, max_length=40
    )
    ended_at: Optional[StrictStr] = Field(
        default=None, pattern=_TIMESTAMP_PATTERN, max_length=40
    )
    duration_s: Optional[StrictFloat] = Field(default=None, ge=0.0)
    model: RunLedgerModelUsageV1 = Field(default_factory=RunLedgerModelUsageV1)
    human: RunLedgerHumanV1 = Field(default_factory=RunLedgerHumanV1)
    outcome: RunLedgerOutcome
    outcome_reason: RunLedgerOutcomeReason
    fields_written: tuple[RunLedgerFieldV1, ...] = ()
    fields_differing: Count = Field(default=0, ge=0)
    readback_tier: Optional[RunLedgerReadbackTier] = None
    receipt_digest: Optional[StrictStr] = Field(default=None, pattern=_HEX64_PATTERN)

    @model_validator(mode="after")
    def _consistent(self) -> "_RowBody":
        if RUN_LEDGER_REASON_OUTCOME[self.outcome_reason] is not self.outcome:
            raise ValueError(
                f"outcome_reason {self.outcome_reason} is not a {self.outcome.value} reason"
            )
        if self.outcome is RunLedgerOutcome.VERIFIED and (
            self.readback_tier != "independent_readback"
        ):
            raise ValueError("VERIFIED needs an independent read-back")
        if self.started_at is not None and self.ended_at is not None:
            if _parse(self.ended_at) < _parse(self.started_at):
                raise ValueError("ended_at is before started_at")
        differing = sum(1 for f in self.fields_written if f.check == "differs")
        if self.fields_differing < differing:
            raise ValueError("fields_differing counts every field that differs")
        return self


class RunLedgerRowV1(_RowBody):
    """One run, value-free. Local: ``sources`` names the files it came from."""

    schema_: Literal["openadapt.run-ledger/v1"] = Field(
        default=RUN_LEDGER_SCHEMA, alias="schema"
    )
    sources: tuple[_Source, ...] = Field(default=(), max_length=256)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def to_json(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)

    def summary(self) -> "AgentRunSummaryV1":
        """The Cloud shape: this row without ``sources``."""

        body = self.model_dump(mode="json", by_alias=True, exclude={"sources"})
        body["schema"] = AGENT_RUN_SUMMARY_SCHEMA
        return AgentRunSummaryV1.model_validate(body)


class AgentRunSummaryV1(_RowBody):
    """What ``openadapt-flow ledger push`` sends: the row minus ``sources``."""

    schema_: Literal["openadapt.agent-run-summary/v1"] = Field(
        default=AGENT_RUN_SUMMARY_SCHEMA, alias="schema"
    )

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def to_json(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)


__all__ = [
    "AGENT_RUN_SUMMARY_SCHEMA",
    "RUN_LEDGER_OUTCOME_PRECEDENCE",
    "RUN_LEDGER_REASON_OUTCOME",
    "RUN_LEDGER_SCHEMA",
    "AgentRunSummaryV1",
    "RunLedgerCostSource",
    "RunLedgerExecutorKind",
    "RunLedgerFieldCheck",
    "RunLedgerFieldV1",
    "RunLedgerHumanV1",
    "RunLedgerIntervalV1",
    "RunLedgerInterventionKind",
    "RunLedgerModelUsageV1",
    "RunLedgerOutcome",
    "RunLedgerOutcomeReason",
    "RunLedgerReadbackTier",
    "RunLedgerRowV1",
]
