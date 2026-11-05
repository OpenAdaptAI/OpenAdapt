"""Pure replay admission for shadow models, without write or activation authority.

Like zero_touch.Outcome.ok, a fault must have been judged: an unavailable
read cannot count as fault detection. Labels come through a separate caller-
owned plan, never from the candidate result. This module does not authenticate
label issuers; the caller must obtain that plan from its independent authority.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from openadapt_flow.runtime.effects.effect import Verdict
from openadapt_flow.runtime.effects.effect_model import (
    CandidateResult,
    EvaluationIdentity,
    Identifier,
    Name,
    StrictModel,
)


class ReplayLabel(StrictModel):
    case_id: Identifier
    kind: Name
    label: Literal["clean", "fault"]
    authority_ref: Identifier


class ReplayKind(StrictModel):
    kind: Name
    identity: EvaluationIdentity


class ReplayPlan(StrictModel):
    """The complete expected labelled history, fixed before candidate scoring."""

    required_kinds: Annotated[
        tuple[ReplayKind, ...], Field(min_length=1, max_length=256)
    ]
    labels: Annotated[tuple[ReplayLabel, ...], Field(min_length=1, max_length=10000)]

    @model_validator(mode="after")
    def _unique_cases_and_kinds(self) -> ReplayPlan:
        cases = [label.case_id for label in self.labels]
        if len(cases) != len(set(cases)):
            raise ValueError("duplicate or conflicting case labels")
        kinds = [entry.kind for entry in self.required_kinds]
        if len(kinds) != len(set(kinds)):
            raise ValueError("duplicate required kind")
        if any(label.kind not in kinds for label in self.labels):
            raise ValueError("label kind is outside the required plan")
        return self


class ReplayResult(StrictModel):
    case_id: Identifier
    result: CandidateResult


class AdmissionSummary(StrictModel):
    """A finite-history check. Admission does not activate production use."""

    admitted: bool
    clean_cases: int
    fault_cases: int
    reasons: tuple[str, ...]


def summarize_replays(
    plan: ReplayPlan, results: tuple[ReplayResult, ...]
) -> AdmissionSummary:
    """Require exact case coverage, matching dependencies and every case correct.

    All expected cases stay in the denominator, including missing results.
    A changed model/language/reader/schema requires a replay of the whole plan.
    """

    reasons: list[str] = []
    if len(results) > 10000:
        raise ValueError("too many replay results")
    expected = {label.case_id: label for label in plan.labels}
    identities = {entry.kind: entry.identity for entry in plan.required_kinds}
    actual: dict[str, CandidateResult] = {}
    for replay in results:
        if replay.case_id in actual:
            reasons.append("duplicate_result")
        else:
            actual[replay.case_id] = replay.result
        label = expected.get(replay.case_id)
        if label is not None and replay.result.identity != identities[label.kind]:
            reasons.append("identity_mismatch")
    if expected.keys() - actual.keys():
        reasons.append("missing_result")
    if actual.keys() - expected.keys():
        reasons.append("unexpected_result")
    for kind in identities:
        labels = {item.label for item in plan.labels if item.kind == kind}
        if labels != {"clean", "fault"}:
            reasons.append("missing_clean_or_fault_kind")
    for case_id, label in expected.items():
        result = actual.get(case_id)
        if result is None:
            continue
        if label.label == "clean":
            if result.verdict != Verdict.CONFIRMED or not result.judged:
                reasons.append("clean_not_confirmed")
        elif result.verdict != Verdict.REFUTED or not result.judged:
            reasons.append("fault_not_judged_refuted")
    return AdmissionSummary(
        admitted=not reasons,
        clean_cases=sum(label.label == "clean" for label in plan.labels),
        fault_cases=sum(label.label == "fault" for label in plan.labels),
        reasons=tuple(sorted(set(reasons))),
    )
