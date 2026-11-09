"""The deterministic hard-invariant layer, separate from field matching.

Readers supply complete scope, business-key history/search and delete
observations. This module neither reads a store nor judges task intent.
Unknown observations cannot establish an invariant. Evidence contains counts
and fixed reason codes, never record ids, business keys or stored values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, TypeGuard

InvariantName = Literal["declared_scope", "at_most_one", "no_undeclared_deletes"]
InvariantStatus = Literal["held", "refuted", "not_checked"]
LAYER_SCHEMA = "openadapt.hard-invariants/v1"


@dataclass(frozen=True)
class InvariantResult:
    name: InvariantName
    status: InvariantStatus
    reason: str
    observed_count: Optional[int] = None

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
        if any(c.status == "refuted" for c in self.checks):
            return "refuted"
        if (
            {c.name for c in self.checks}
            != {"declared_scope", "at_most_one", "no_undeclared_deletes"}
            or len(self.checks) != 3
            or any(c.status != "held" for c in self.checks)
        ):
            return "not_checked"
        return "held"

    def evidence(self) -> dict[str, object]:
        return {
            "schema_version": LAYER_SCHEMA,
            "status": self.status,
            "checks": [c.evidence() for c in self.checks],
            "model_calls": 0,
        }


def _count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _zero(
    name: InvariantName,
    count: Optional[int],
    complete: bool,
    *,
    held: str,
    refuted: str,
    unknown: str,
) -> InvariantResult:
    if not _count(count):
        return InvariantResult(name, "not_checked", "invalid_or_missing_observation")
    assert count is not None
    if count > 0:
        return InvariantResult(name, "refuted", refuted, count)
    return InvariantResult(
        name,
        "held" if complete is True else "not_checked",
        held if complete is True else unknown,
        count,
    )


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
    if maximum is not None and maximum > 1:
        cardinality = InvariantResult(
            "at_most_one", "refuted", "duplicate_business_key", maximum
        )
    elif (
        not all(_count(n) for n in counts)
        or history_complete is not True
        or search_complete is not True
    ):
        cardinality = InvariantResult(
            "at_most_one",
            "not_checked",
            "business_key_history_or_search_incomplete",
            maximum,
        )
    elif _count(history_matches) and maximum is not None and history_matches < maximum:
        cardinality = InvariantResult(
            "at_most_one",
            "not_checked",
            "business_key_observations_inconsistent",
            maximum,
        )
    else:
        cardinality = InvariantResult(
            "at_most_one", "held", "at_most_one_business_record", maximum
        )
    return HardInvariantVerdict((scope, cardinality, deletes))
