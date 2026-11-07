"""Generated predicate truth does not qualify a requested business effect.

These source controls do not replace the original 498aa tests or a native
qualification study. Every record and independent case label is synthetic.
"""

import pytest
from pydantic import ValidationError

from openadapt_flow import transforms
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
    ObservationPath,
    ObservationRequirements,
    bind_observations,
    evaluate_effect_model,
)
from tests.test_effect_model_integration import candidate_fixture


WRONG_AFTER = [{"id": "a", "value": "WRONG"}, {"id": "b", "value": "keep"}]
VACUOUS = (
    ("new == new", "old == old"),
    ("len(new) == len(old)",),
    ("len(new) >= 0 and len(old) >= 0",),
    ("true or new == old",),
    ("false and new == old or true",),
)
PREDICTION_CASES = tuple(zip(VACUOUS, (
    ("true", "true"), ("unknown",), ("unknown",), ("true",), ("true",),
))) + (
    (("count(new) == count(old)",), ("true",)),
    (("count(new) >= 0 and count(old) >= 0",), ("true",)),
)


def rebuilt(obligations, *, require_task=False):
    model, observations, required, identity = candidate_fixture()
    model = model.model_copy(update={"obligations": tuple(obligations)})
    identity = identity.model_copy(update={"model_sha256": model.content_sha256()})
    observations = observations.model_copy(update={"after": {"rows": WRONG_AFTER}})
    if require_task:
        required = ObservationRequirements(
            paths=required.paths + (
                ObservationPath(source="task", path=("target",)),
                ObservationPath(source="task", path=("value",)),
            )
        )
        observations = observations.model_copy(update={"covered": required.paths})
    return model, observations, required, identity


def assert_prediction_stays_unqualified(arguments, expected=None):
    model, observations, _, _ = arguments
    bindings = bind_observations(
        model, task=observations.task, before=observations.before, after=observations.after
    )
    raw = transforms.evaluate_effect_predicates(model.obligations, bindings)
    # Retain the actual mathematical truth; refusal or construction skips do
    # not stand in for reproducing the model-adequacy defect.
    expected = ("true",) * len(model.obligations) if expected is None else expected
    assert tuple(result.outcome for result in raw) == expected
    result = evaluate_effect_model(*arguments)
    assert result.predicate_outcomes == tuple(item.outcome for item in raw)
    assert result.prediction_scope == "unqualified_model_prediction"
    assert result.verdict == Verdict.INDETERMINATE and not result.judged
    assert result.reason == ("language_refused" if "unknown" in expected else "model_unqualified")


@pytest.mark.parametrize("obligations,expected", PREDICTION_CASES)
def test_literal_grok_and_supported_cardinality_cases_cannot_confirm_wrong_write(obligations, expected):
    assert_prediction_stays_unqualified(rebuilt(obligations), expected)


def test_all_task_references_still_do_not_prove_intended_value_use():
    arguments = rebuilt(
        ("requested == requested and target == target and new == new and old == old",),
        require_task=True,
    )
    assert_prediction_stays_unqualified(arguments)


def test_relational_but_wrong_task_model_is_also_unqualified():
    expected = "put(lookup(keyed(old, ['id']), target), 'value', 'WRONG')"
    arguments = rebuilt(
        (
            f"has_record(keyed(new, ['id']), {expected})",
            "allowed_delta(keyed(old, ['id']), keyed(new, ['id']), "
            f"keyed([{expected}], ['id']), keyed([], ['id']))",
            "requested == requested",
        ),
        require_task=True,
    )
    assert_prediction_stays_unqualified(arguments)


def test_actual_intended_edit_keeps_true_predicates_without_new_authority():
    assert_prediction_stays_unqualified(candidate_fixture())


def test_actual_unknown_predicate_diagnostic_remains_unqualified():
    arguments = rebuilt(("unavailable_operation(old, new)",))
    model, observations, _, _ = arguments
    raw = transforms.evaluate_effect_predicates(
        model.obligations,
        bind_observations(model, task=observations.task, before=observations.before, after=observations.after),
    )
    assert tuple(item.outcome for item in raw) == ("unknown",)
    result = evaluate_effect_model(*arguments)
    assert result.predicate_outcomes == ("unknown",)
    assert result.verdict == Verdict.INDETERMINATE and not result.judged
    assert result.reason == "language_refused"
    assert result.prediction_scope == "unqualified_model_prediction"


def test_no_source_identity_or_caller_boolean_qualifies_model_truth():
    _, _, _, identity = candidate_fixture()
    with pytest.raises(ValidationError, match="model prediction cannot confirm"):
        CandidateResult(
            identity=identity, verdict=Verdict.CONFIRMED, judged=True,
            reason="satisfied", predicate_outcomes=("true",),
        )
    with pytest.raises(ValidationError):
        CandidateResult(identity=identity, qualified=True)
    with pytest.raises(ValidationError):
        CandidateResult(identity=identity, prediction_scope="qualified")


def test_prior_refutation_remains_sticky_when_prediction_is_tautological():
    model, observations, required, identity = rebuilt(VACUOUS[0])
    result = evaluate_effect_model(
        model, observations.model_copy(update={"prior_refuted": True, "fresh": False}),
        required, identity,
    )
    assert result.verdict == Verdict.REFUTED and result.reason == "prior_refutation"
    assert not result.judged and result.predicate_outcomes == ()


def prediction_history(obligations=None, *, fault_unavailable=False):
    model, clean, required, identity = candidate_fixture()
    if obligations is not None:
        model = model.model_copy(update={"obligations": obligations})
        identity = identity.model_copy(update={"model_sha256": model.content_sha256()})
    # The synthetic clean/fault labels are fixed before either model evaluation.
    plan = ReplayPlan(
        required_kinds=(ReplayKind(kind=model.kind, identity=identity),),
        labels=(
            ReplayLabel(case_id="clean", kind=model.kind, label="clean", authority_ref="synthetic-case:clean"),
            ReplayLabel(case_id="wrong-value", kind=model.kind, label="fault", authority_ref="synthetic-case:wrong-value"),
        ),
    )
    faulty = clean.model_copy(update={"after": {"rows": WRONG_AFTER}})
    if fault_unavailable:
        faulty = faulty.model_copy(update={"complete": False})
    results = (
        ReplayResult(case_id="clean", result=evaluate_effect_model(model, clean, required, identity)),
        ReplayResult(case_id="wrong-value", result=evaluate_effect_model(model, faulty, required, identity)),
    )
    return plan, results


def test_sound_predictions_match_only_the_supplied_finite_history():
    plan, results = prediction_history()
    before = plan.model_dump_json(), tuple(item.model_dump_json() for item in results)
    summary = summarize_predictions(plan, results)
    assert summary.matches_supplied_history and summary.reasons == ()
    assert summary.scope == "finite_supplied_history_only"
    assert (summary.clean_cases, summary.fault_cases) == (1, 1)
    assert not hasattr(summary, "admitted") and not hasattr(summary, "activate")
    # A finite match never changes a positive prediction into effect authority.
    assert results[0].result.verdict == Verdict.INDETERMINATE
    assert not summarize_replays(plan, results).admitted
    assert before == (plan.model_dump_json(), tuple(item.model_dump_json() for item in results))


def test_model_copy_cannot_turn_prediction_data_into_admission():
    plan, results = prediction_history()
    # Pydantic model_copy does not validate updates. A copied data object must
    # still not become an admission, even when its verdict field is forged.
    copied = results[0].model_copy(update={"result": results[0].result.model_copy(
        update={"verdict": Verdict.CONFIRMED, "judged": True, "reason": "satisfied"}
    )})
    summary = summarize_replays(plan, (copied, results[1]))
    assert not summary.admitted
    assert "unqualified_model_predictions" in summary.reasons


@pytest.mark.parametrize("obligations,expected", PREDICTION_CASES)
def test_tautology_or_unknown_cannot_match_the_independent_fault_label(obligations, expected):
    plan, results = prediction_history(obligations)
    assert all(replay.result.predicate_outcomes == expected for replay in results)
    summary = summarize_predictions(plan, results)
    assert not summary.matches_supplied_history
    assert "fault_prediction_not_contradicted" in summary.reasons
    assert (summary.clean_cases, summary.fault_cases) == (1, 1)


def test_unavailable_fault_prediction_is_not_fault_detection():
    plan, results = prediction_history(fault_unavailable=True)
    summary = summarize_predictions(plan, results)
    assert not summary.matches_supplied_history
    assert "fault_prediction_not_contradicted" in summary.reasons
    assert summary.fault_cases == 1


@pytest.mark.parametrize("field", (
    "model_sha256", "language_sha256", "schema_sha256", "reader_sha256",
    "source_sha256", "language_version", "operator_profile",
))
def test_changed_dependency_never_borrows_other_prediction_history(field):
    plan, results = prediction_history()
    changed = results[1].result.identity.model_copy(update={field: "0" * 64})
    forged = results[1].model_copy(update={"result": results[1].result.model_copy(update={"identity": changed})})
    summary = summarize_predictions(plan, (results[0], forged))
    assert not summary.matches_supplied_history
    assert "identity_mismatch" in summary.reasons


@pytest.mark.parametrize("selection", ((0,), (1,), (0, 0), (0, 1, 1)))
def test_missing_or_duplicate_prediction_keeps_full_case_denominator(selection):
    plan, results = prediction_history()
    summary = summarize_predictions(plan, tuple(results[index] for index in selection))
    assert not summary.matches_supplied_history
    assert (summary.clean_cases, summary.fault_cases) == (1, 1)
