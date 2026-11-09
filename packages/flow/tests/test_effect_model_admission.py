"""Replay admission keeps independent labels, coverage and dependencies intact."""

import pytest
from pydantic import ValidationError

from openadapt_flow.generate.effect_model_admission import (
    ReplayKind,
    ReplayLabel,
    ReplayPlan,
    ReplayResult,
    summarize_predictions,
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


def result(case_id, prediction, **identity_changes):
    verdict = Verdict.REFUTED if prediction == "false" else Verdict.INDETERMINATE
    reason = {
        "true": "model_unqualified",
        "false": "contradiction",
        "unknown": "evidence_unavailable",
    }[prediction]
    return ReplayResult(
        case_id=case_id,
        result=CandidateResult(
            identity=identity(**identity_changes),
            verdict=verdict,
            judged=prediction == "false",
            reason=reason,
            predicate_outcomes=(prediction,),
        ),
    )


def correct_results():
    return (result("clean", "true"), result("fault", "false"))


def test_complete_history_matches_finitely_without_admission_or_mutation():
    frozen_plan, results = plan(), correct_results()
    before = frozen_plan.model_dump_json(), tuple(r.model_dump_json() for r in results)
    summary = summarize_predictions(frozen_plan, results)
    assert summary.matches_supplied_history
    assert summary.scope == "finite_supplied_history_only"
    assert not summarize_replays(frozen_plan, results).admitted
    assert results[0].result.verdict == Verdict.INDETERMINATE
    assert (summary.clean_cases, summary.fault_cases) == (1, 1)
    assert before == (
        frozen_plan.model_dump_json(),
        tuple(r.model_dump_json() for r in results),
    )


@pytest.mark.parametrize(
    "case_id,prediction",
    (
        ("clean", "unknown"),
        ("clean", "false"),
        ("fault", "true"),
        ("fault", "unknown"),
    ),
)
def test_wrong_or_unavailable_case_prevents_finite_match(case_id, prediction):
    results = tuple(
        result(r.case_id, prediction) if r.case_id == case_id else r
        for r in correct_results()
    )
    assert not summarize_predictions(plan(), results).matches_supplied_history
    assert not summarize_replays(plan(), results).admitted


def test_missing_fault_stays_in_denominator():
    summary = summarize_predictions(plan(), correct_results()[:1])
    assert not summary.matches_supplied_history
    assert summary.fault_cases == 1
    assert "missing_result" in summary.reasons


@pytest.mark.parametrize(
    "field,value",
    (
        ("model_sha256", "e" * 64),
        ("language_sha256", "e" * 64),
        ("schema_sha256", "e" * 64),
        ("reader_sha256", "e" * 64),
        ("source_sha256", "f" * 64),
        ("language_version", "synthetic/v2"),
        ("operator_profile", "other"),
    ),
)
def test_changed_dependency_requires_full_replay(field, value):
    results = (correct_results()[0], result("fault", "false", **{field: value}))
    summary = summarize_predictions(plan(), results)
    assert "identity_mismatch" in summary.reasons
    assert not summary.matches_supplied_history


def test_duplicate_and_unexpected_results_cannot_replace_a_missing_case():
    clean = correct_results()[0]
    summary = summarize_predictions(plan(), (clean, clean, result("extra", "false")))
    assert not summary.matches_supplied_history
    expected = {"duplicate_result", "missing_result", "unexpected_result"}
    assert expected <= set(summary.reasons)


def test_new_kind_requires_its_own_clean_and_fault_history():
    original = plan()
    extended = ReplayPlan(
        required_kinds=original.required_kinds
        + (ReplayKind(kind="create", identity=identity(model_sha256="e" * 64)),),
        labels=original.labels,
    )
    summary = summarize_predictions(extended, correct_results())
    assert "missing_clean_or_fault_kind" in summary.reasons
    assert not summary.matches_supplied_history


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
