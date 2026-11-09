"""A completed genuine predicate still refuses a deadline crossed in its call."""

from types import SimpleNamespace

import pytest

from openadapt_flow import transforms as expr
from openadapt_flow.runtime.effects.effect import Verdict
from openadapt_flow.runtime.effects.effect_model import (
    bind_observations,
    evaluate_effect_model,
)
from tests.test_effect_model_integration import candidate_fixture


@pytest.mark.parametrize("entrypoint", ("predicate", "model"))
@pytest.mark.parametrize("operator", ("unchanged", "allowed_delta"))
@pytest.mark.parametrize("shape", ("root", "not", "and", "or"))
@pytest.mark.parametrize("matches", (False, True))
@pytest.mark.parametrize("expired", (False, True))
def test_effect_completion_checks_deadline_before_any_outcome(
    entrypoint, operator, shape, matches, expired, monkeypatch
):
    model, observations, requirements, identity = candidate_fixture()
    if operator == "unchanged":
        source = "unchanged(old, new)"
        after = (
            observations.before
            if matches
            else {"rows": [{"id": "a", "value": "wrong"}]}
        )
    else:
        intended = "put(lookup(keyed(old, ['id']), target), 'value', requested)"
        source = (
            "allowed_delta(keyed(old, ['id']), keyed(new, ['id']), "
            f"keyed([{intended}], ['id']), keyed([], ['id']))"
        )
        after = (
            observations.after if matches else {"rows": [{"id": "a", "value": "wrong"}]}
        )
    source = {
        "root": source,
        "not": f"not {source}",
        "and": f"{source} and true",
        "or": f"{source} or false",
    }[shape]
    model = model.model_copy(update={"obligations": (source,)})
    observations = observations.model_copy(update={"after": after})
    identity = identity.model_copy(update={"model_sha256": model.content_sha256()})
    clock = [100.0]
    monkeypatch.setattr(expr, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = expr._effect_call
    completed = []

    def complete_then_advance(name, args, budget):
        result = original(name, args, budget)
        if name == operator:
            # The original operator computes the real Boolean first. Only
            # the expression clock changes; pytest and the host clock do not.
            completed.append(result)
            if expired:
                clock[0] += expr.MAX_EVAL_SECONDS + 1.0
        return result

    monkeypatch.setattr(expr, "_effect_call", complete_then_advance)
    if entrypoint == "predicate":
        bindings = bind_observations(
            model,
            task=observations.task,
            before=observations.before,
            after=observations.after,
        )
        results = expr.evaluate_effect_predicates(model.obligations, bindings)
        assert len(results) == 1
        result = results[0]
        assert completed == [matches]
        if expired:
            assert result.outcome is expr.Outcome.UNKNOWN
            assert result.refusal is expr.RefusalCode.BOUNDS
            assert not result.passed
        else:
            expected = not matches if shape == "not" else matches
            assert result.outcome is (
                expr.Outcome.TRUE if expected else expr.Outcome.FALSE
            )
            assert result.refusal is None
    else:
        result = evaluate_effect_model(model, observations, requirements, identity)
        assert completed == [matches]
        if expired:
            assert result.predicate_outcomes == ("unknown",)
            assert result.verdict is Verdict.INDETERMINATE
            assert result.reason == "language_refused"
            assert not result.judged
        else:
            expected = not matches if shape == "not" else matches
            assert result.predicate_outcomes == (("true",) if expected else ("false",))
            assert result.verdict is (
                Verdict.INDETERMINATE if expected else Verdict.REFUTED
            )
            assert result.reason == (
                "model_unqualified" if expected else "contradiction"
            )
        assert result.verdict is not Verdict.CONFIRMED
