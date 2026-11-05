"""Replay admission keeps independent labels, coverage and dependencies intact."""

import pytest
from pydantic import ValidationError

from openadapt_flow.generate.effect_model_admission import (
    ReplayKind,
    ReplayLabel,
    ReplayPlan,
    ReplayResult,
    summarize_replays,
)
from openadapt_flow.runtime.effects.effect import Verdict
from openadapt_flow.runtime.effects.effect_model import (
    CandidateResult,
    EvaluationIdentity,
)


def identity(**changes):
    return EvaluationIdentity(
        **(
            {
                "model_sha256": "a" * 64,
                "language_sha256": "b" * 64,
                "language_version": "synthetic/v1",
                "operator_profile": "synthetic_effect",
                "schema_sha256": "c" * 64,
                "reader_sha256": "d" * 64,
                "source_sha256": "e" * 64,
            }
            | changes
        )
    )


def plan():
    return ReplayPlan(
        required_kinds=(ReplayKind(kind="edit", identity=identity()),),
        labels=(
            ReplayLabel(
                case_id="clean", kind="edit", label="clean", authority_ref="truth:1"
            ),
            ReplayLabel(
                case_id="fault", kind="edit", label="fault", authority_ref="truth:2"
            ),
        ),
    )


def result(case_id, verdict, **identity_changes):
    reasons = {
        Verdict.CONFIRMED: "satisfied",
        Verdict.REFUTED: "contradiction",
        Verdict.INDETERMINATE: "evidence_unavailable",
    }
    return ReplayResult(
        case_id=case_id,
        result=CandidateResult(
            identity=identity(**identity_changes),
            verdict=verdict,
            judged=verdict != Verdict.INDETERMINATE,
            reason=reasons[verdict],
        ),
    )


def correct_results():
    return (result("clean", Verdict.CONFIRMED), result("fault", Verdict.REFUTED))


def test_complete_history_is_admitted_without_mutation():
    frozen_plan, results = plan(), correct_results()
    before = frozen_plan.model_dump_json(), tuple(r.model_dump_json() for r in results)
    summary = summarize_replays(frozen_plan, results)
    assert summary.admitted
    assert (summary.clean_cases, summary.fault_cases) == (1, 1)
    assert before == (
        frozen_plan.model_dump_json(),
        tuple(r.model_dump_json() for r in results),
    )


@pytest.mark.parametrize(
    "case_id,verdict",
    (
        ("clean", Verdict.INDETERMINATE),
        ("clean", Verdict.REFUTED),
        ("fault", Verdict.CONFIRMED),
        ("fault", Verdict.INDETERMINATE),
    ),
)
def test_wrong_or_unavailable_case_prevents_admission(case_id, verdict):
    results = tuple(
        result(r.case_id, verdict) if r.case_id == case_id else r
        for r in correct_results()
    )
    assert not summarize_replays(plan(), results).admitted


def test_missing_fault_stays_in_denominator():
    summary = summarize_replays(plan(), correct_results()[:1])
    assert not summary.admitted
    assert summary.fault_cases == 1
    assert "missing_result" in summary.reasons


@pytest.mark.parametrize(
    "field,value",
    (
        ("model_sha256", "e" * 64),
        ("language_sha256", "e" * 64),
        ("schema_sha256", "e" * 64),
        ("reader_sha256", "e" * 64),
        ("language_version", "synthetic/v2"),
        ("operator_profile", "other"),
    ),
)
def test_changed_dependency_requires_full_replay(field, value):
    results = (correct_results()[0], result("fault", Verdict.REFUTED, **{field: value}))
    assert "identity_mismatch" in summarize_replays(plan(), results).reasons


def test_duplicate_and_unexpected_results_cannot_replace_a_missing_case():
    clean = correct_results()[0]
    summary = summarize_replays(
        plan(), (clean, clean, result("extra", Verdict.REFUTED))
    )
    assert not summary.admitted
    expected = {"duplicate_result", "missing_result", "unexpected_result"}
    assert expected <= set(summary.reasons)


def test_new_kind_requires_its_own_clean_and_fault_history():
    original = plan()
    extended = ReplayPlan(
        required_kinds=original.required_kinds
        + (ReplayKind(kind="create", identity=identity(model_sha256="e" * 64)),),
        labels=original.labels,
    )
    summary = summarize_replays(extended, correct_results())
    assert "missing_clean_or_fault_kind" in summary.reasons


def test_conflicting_labels_are_rejected_before_scoring():
    original = plan()
    with pytest.raises(ValidationError, match="conflicting"):
        ReplayPlan(
            required_kinds=original.required_kinds,
            labels=original.labels
            + (
                ReplayLabel(
                    case_id="clean", kind="edit", label="fault", authority_ref="truth:3"
                ),
            ),
        )


def test_candidate_cannot_supply_its_own_label():
    with pytest.raises(ValidationError):
        ReplayResult(case_id="clean", result=correct_results()[0].result, label="clean")
