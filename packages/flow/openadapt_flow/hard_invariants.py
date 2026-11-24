"""The deterministic hard-invariant layer, separate from field matching.

Readers supply complete scope, business-key history/search and delete
observations. This module neither reads a store nor judges task intent.
Unknown observations cannot establish an invariant. Evidence contains counts
and fixed reason codes, never record ids, business keys or stored values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, TypeGuard

from openadapt_flow.verdict import (
    Obligations,
    Observed,
    Result,
    Status,
    Subject,
    settle,
)
from openadapt_flow.verdict_sites import verdict_site

InvariantName = Literal["declared_scope", "at_most_one", "no_undeclared_deletes"]
InvariantStatus = Literal["held", "refuted", "not_checked"]
LAYER_SCHEMA = "openadapt.hard-invariants/v1"


NAMES: tuple[InvariantName, ...] = (
    "declared_scope",
    "at_most_one",
    "no_undeclared_deletes",
)
#: The three obligations, fixed before any reader runs.
_OBLIGATIONS = Obligations.fix(
    (Subject(surface="hard_invariants", key=name) for name in NAMES),
    source="hard_invariants",
)


@dataclass(frozen=True)
class InvariantResult:
    name: InvariantName
    status: InvariantStatus
    reason: str
    observed_count: Optional[int] = None
    #: The kernel result this check was judged as. Only a check built by
    #: :func:`judge_hard_invariants` carries one, so a hand-written "held"
    #: can't make the layer hold.
    result: Optional[Result] = field(default=None, compare=False, repr=False)

    def _kernel(self) -> Result:
        subject = Subject(surface="hard_invariants", key=self.name)
        if self.result is not None and self.result.subject == subject:
            return self.result
        if self.status == "refuted":
            # A refutation needs no complete read: one violation is enough.
            return Result.refuted(subject, Observed(None, complete=False), self.reason)
        return Result.not_checked(subject, "not judged by the kernel")

    def evidence(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "observed_count": self.observed_count,
        }


@dataclass(frozen=True)
class HardInvariantVerdict:
    checks: tuple[InvariantResult, ...]

    @property
    def status(self) -> InvariantStatus:
        """Held only when each of the three invariants held, once. One
        refutation refutes; anything missing, repeated or extra is not
        checked."""

        settled = settle(_OBLIGATIONS, (c._kernel() for c in self.checks))
        labels: dict[Status, InvariantStatus] = {
            Status.HELD: "held",
            Status.REFUTED: "refuted",
            Status.NOT_CHECKED: "not_checked",
        }
        return labels[settled.label(precedence=(Status.REFUTED, Status.NOT_CHECKED))]

    def evidence(self) -> dict[str, object]:
        return {
            "schema_version": LAYER_SCHEMA,
            "status": self.status,
            "checks": [c.evidence() for c in self.checks],
            "model_calls": 0,
        }


def _count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _invariant(
    name: InvariantName, result: Result, reason: str, count: Optional[int]
) -> InvariantResult:
    labels: dict[Status, InvariantStatus] = {
        Status.HELD: "held",
        Status.REFUTED: "refuted",
        Status.NOT_CHECKED: "not_checked",
    }
    return InvariantResult(name, labels[result.status], reason, count, result)


def _zero(
    name: InvariantName,
    count: Optional[int],
    complete: bool,
    *,
    held: str,
    refuted: str,
    unknown: str,
) -> InvariantResult:
    """No change of this kind: any one refutes; none holds only on a complete
    read (:meth:`Result.nothing_else`)."""

    subject = Subject(surface="hard_invariants", key=name)
    if not _count(count):
        result = Result.not_checked(subject, "invalid_or_missing_observation")
        return _invariant(name, result, "invalid_or_missing_observation", None)
    result = Result.nothing_else(
        subject,
        Observed(count, complete=complete is True),
        held=held,
        refuted=refuted,
        partial=unknown,
    )
    reason = {Status.HELD: held, Status.REFUTED: refuted}.get(result.status, unknown)
    return _invariant(name, result, reason, count)


@verdict_site("hard_invariants.judge", classes="C2 C5")
def judge_hard_invariants(
    *,
    outside_scope_changes: Optional[int],
    undeclared_deletes: Optional[int],
    before_matches: Optional[int],
    after_matches: Optional[int],
    history_matches: Optional[int],
    scope_complete: bool,
    deletes_complete: bool,
    history_complete: bool,
    search_complete: bool,
) -> HardInvariantVerdict:
    """Judge three fixed predicates over authoritative reader observations.

    ``history_matches`` counts distinct records carrying the declared business
    key across the read window, including records later deleted. A
    current-state count is never a substitute for that history. One observed violation refutes even when
    other reads are incomplete; otherwise incomplete reads remain unknown.
    """

    scope = _zero(
        "declared_scope",
        outside_scope_changes,
        scope_complete,
        held="no_outside_scope_changes",
        refuted="outside_scope_changes",
        unknown="scope_history_incomplete",
    )
    deletes = _zero(
        "no_undeclared_deletes",
        undeclared_deletes,
        deletes_complete,
        held="no_undeclared_deletes",
        refuted="undeclared_deletes",
        unknown="delete_history_incomplete",
    )
    counts = (before_matches, after_matches, history_matches)
    known = [n for n in counts if _count(n)]
    maximum = max(known) if known else None
    subject = Subject(surface="hard_invariants", key="at_most_one")
    # One duplicate refutes, even beside an incomplete read.
    if maximum is not None and maximum > 1:
        result = Result.refuted(
            subject, Observed(maximum, complete=False), "duplicate_business_key"
        )
        reason = "duplicate_business_key"
    elif (
        len(known) != len(counts)
        or history_complete is not True
        or search_complete is not True
    ):
        reason = "business_key_history_or_search_incomplete"
        result = Result.not_checked(subject, reason)
    elif _count(history_matches) and maximum is not None and history_matches < maximum:
        reason = "business_key_observations_inconsistent"
        result = Result.not_checked(subject, reason)
    else:
        reason = "at_most_one_business_record"
        # Every count is known and both reads were complete (checked above).
        complete = history_complete is True and search_complete is True
        result = Result.held(subject, Observed(maximum, complete=complete), reason)
    cardinality = _invariant("at_most_one", result, reason, maximum)
    return HardInvariantVerdict((scope, cardinality, deletes))
