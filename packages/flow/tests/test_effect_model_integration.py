"""Real shared-language evaluation behind independent reader prerequisites."""

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from openadapt_flow import transforms
from openadapt_flow.runtime.effects.effect import Verdict
from openadapt_flow.runtime.effects.effect_model import (
    CapturedObservations,
    EffectModel,
    EvaluationIdentity,
    ObservationBinding,
    ObservationPath,
    ObservationRequirements,
    bind_observations,
    evaluate_effect_model,
)
from openadapt_flow.shadow import evaluate_effect_candidate


def candidate_fixture():
    expected = "put(lookup(keyed(old, ['id']), target), 'value', requested)"
    model = EffectModel(
        kind="edit",
        language_version=transforms.LANGUAGE_VERSION,
        operator_profile=transforms.EFFECT_PROFILE,
        bindings=(
            ObservationBinding(name="old", source="before", path=("rows",)),
            ObservationBinding(name="new", source="after", path=("rows",)),
            ObservationBinding(name="target", source="task", path=("target",)),
            ObservationBinding(name="requested", source="task", path=("value",)),
        ),
        obligations=(
            f"has_record(keyed(new, ['id']), {expected})",
            "allowed_delta(keyed(old, ['id']), keyed(new, ['id']), "
            f"keyed([{expected}], ['id']), keyed([], ['id']))",
        ),
    )
    identity = EvaluationIdentity(
        model_sha256=model.content_sha256(),
        language_sha256=hashlib.sha256(
            Path(transforms.__file__).read_bytes()
        ).hexdigest(),
        language_version=transforms.LANGUAGE_VERSION,
        operator_profile=transforms.EFFECT_PROFILE,
        schema_sha256="c" * 64,
        reader_sha256="d" * 64,
        source_sha256="e" * 64,
    )
    requirements = ObservationRequirements(
        paths=(
            ObservationPath(source="before", path=("rows",)),
            ObservationPath(source="after", path=("rows",)),
        )
    )
    observations = CapturedObservations(
        task={"target": {"id": "a"}, "value": "new"},
        before={"rows": [{"id": "a", "value": "old"}, {"id": "b", "value": "keep"}]},
        after={"rows": [{"id": "a", "value": "new"}, {"id": "b", "value": "keep"}]},
        source_sha256=identity.source_sha256,
        schema_sha256=identity.schema_sha256,
        reader_sha256=identity.reader_sha256,
        covered=requirements.paths,
        complete=True,
        fresh=True,
        freshness_ref="native-revision:2",
    )
    return model, observations, requirements, identity


def test_shared_language_predicts_intended_edit_without_mutating_snapshots():
    args = candidate_fixture()
    before = args[1].model_dump_json()
    result = evaluate_effect_candidate(*args)
    model, observations, _, _ = args
    bindings = bind_observations(
        model,
        task=observations.task,
        before=observations.before,
        after=observations.after,
    )
    raw = transforms.evaluate_effect_predicates(model.obligations, bindings)
    assert tuple(item.outcome for item in raw) == ("true", "true")
    assert result.predicate_outcomes == ("true", "true")
    assert result.verdict == Verdict.INDETERMINATE and not result.judged
    assert result.reason == "model_unqualified"
    assert result.prediction_scope == "unqualified_model_prediction"
    assert args[1].model_dump_json() == before


@pytest.mark.parametrize(
    "after",
    (
        [{"id": "a", "value": "wrong"}, {"id": "b", "value": "keep"}],
        [{"id": "a", "value": "old"}, {"id": "b", "value": "new"}],
        [{"id": "a", "value": "new"}],
        [{"id": "a", "value": "new"}, {"id": "b", "value": "changed"}],
        [
            {"id": "a", "value": "new"},
            {"id": "b", "value": "keep"},
            {"id": "c", "value": "extra"},
        ],
        [{"id": "a", "value": "new", "unexpected": None}, {"id": "b", "value": "keep"}],
        [{"id": "a", "value": "old"}, {"id": "b", "value": "keep"}],
    ),
)
def test_observed_wrong_effect_is_positively_refuted(after):
    model, obs, required, identity = candidate_fixture()
    changed = obs.model_copy(update={"after": {"rows": after}})
    result = evaluate_effect_model(model, changed, required, identity)
    assert result.verdict == Verdict.REFUTED and result.judged
    assert "false" in result.predicate_outcomes
    assert result.prediction_scope == "unqualified_model_prediction"


def test_null_edit_is_exact_and_missing_field_cannot_substitute():
    model, obs, required, identity = candidate_fixture()
    after = deepcopy(obs.after)
    after["rows"][0]["value"] = None
    obs = obs.model_copy(
        update={"task": {"target": {"id": "a"}, "value": None}, "after": after}
    )
    result = evaluate_effect_model(model, obs, required, identity)
    raw = transforms.evaluate_effect_predicates(
        model.obligations,
        bind_observations(model, task=obs.task, before=obs.before, after=obs.after),
    )
    assert tuple(item.outcome for item in raw) == ("true", "true")
    assert result.predicate_outcomes == ("true", "true")
    assert result.verdict == Verdict.INDETERMINATE and not result.judged
    del after["rows"][0]["value"]
    assert (
        evaluate_effect_model(model, obs, required, identity).verdict == Verdict.REFUTED
    )


@pytest.mark.parametrize(
    "change",
    (
        {"complete": False},
        {"fresh": False},
        {"freshness_ref": None},
        {"covered": ()},
        {"source_sha256": "0" * 64},
        {"schema_sha256": "0" * 64},
        {"reader_sha256": "0" * 64},
    ),
)
def test_reader_prerequisites_cannot_be_excused_by_a_model(change, monkeypatch):
    model, obs, required, identity = candidate_fixture()

    def trap(*args, **kwargs):
        raise AssertionError("evaluation before independent prerequisites")

    monkeypatch.setattr(transforms, "evaluate_effect_predicates", trap)
    result = evaluate_effect_model(
        model, obs.model_copy(update=change), required, identity
    )
    assert result.verdict == Verdict.INDETERMINATE
    assert result.reason == "evidence_unavailable"


def test_model_cannot_drop_required_observations_or_leave_them_unused():
    model, obs, required, identity = candidate_fixture()
    for replacement in (
        model.model_copy(update={"bindings": model.bindings[1:]}),
        model.model_copy(update={"obligations": ("true",)}),
    ):
        updated = identity.model_copy(
            update={"model_sha256": replacement.content_sha256()}
        )
        assert (
            evaluate_effect_model(replacement, obs, required, updated).verdict
            == Verdict.INDETERMINATE
        )


@pytest.mark.parametrize(
    "field", ("model_sha256", "language_sha256", "language_version", "operator_profile")
)
def test_unmatched_frozen_model_or_language_is_unknown(field):
    model, obs, required, identity = candidate_fixture()
    changed = identity.model_copy(update={field: "0" * 64})
    assert (
        evaluate_effect_model(model, obs, required, changed).verdict
        == Verdict.INDETERMINATE
    )


def test_unexpected_language_error_is_unknown_and_value_free(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("private-record-marker")

    monkeypatch.setattr(transforms, "evaluate_effect_predicates", broken)
    result = evaluate_effect_candidate(*candidate_fixture())
    assert result.verdict == Verdict.INDETERMINATE and not result.judged
    assert "private-record-marker" not in result.model_dump_json()


def test_sticky_refutation_survives_unavailable_observations():
    model, obs, required, identity = candidate_fixture()
    result = evaluate_effect_model(
        model,
        obs.model_copy(update={"prior_refuted": True, "fresh": False}),
        required,
        identity,
    )
    assert result.verdict == Verdict.REFUTED
    assert result.reason == "prior_refutation" and not result.judged


def test_captured_input_does_not_accept_executor_claims():
    _, obs, _, _ = candidate_fixture()
    with pytest.raises(ValidationError):
        CapturedObservations(**(obs.model_dump() | {"agent_outcome": "CONFIRMED"}))
