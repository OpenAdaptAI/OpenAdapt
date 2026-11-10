"""Typed effect operators use the published expression parser and evaluator."""

from copy import deepcopy

import pytest

from openadapt_flow import transforms as expr


def outcome(source, bindings, **kwargs):
    return expr.evaluate_effect_predicates((source,), bindings, **kwargs)[0].outcome


def test_null_is_a_value_but_missing_is_unknown():
    assert outcome("is_null(value)", {"value": None}) == "true"
    assert outcome("is_null(value)", {}) == "unknown"


def test_record_equality_preserves_bool_integer_and_text_types():
    for other in (1, "true", None):
        assert outcome("a == b", {"a": {"x": True}, "b": {"x": other}}) == "false"


def test_keyed_order_and_sequence_order_are_distinct():
    rows = [{"id": "a", "value": 1}, {"id": "b", "value": 2}]
    values = {"a": rows, "b": list(reversed(rows))}
    assert outcome("keyed(a, ['id']) == keyed(b, ['id'])", values) == "true"
    assert outcome("a == b", values) == "false"
    assert outcome("count(keyed(a, ['id'])) == 2", values) == "true"


@pytest.mark.parametrize(
    "rows",
    (
        [{"id": "a"}, {"id": "a"}],
        [{"other": "a"}],
        [{"id": None}],
    ),
)
def test_duplicate_missing_and_null_keys_refuse(rows):
    assert outcome("count(keyed(rows, ['id'])) == 1", {"rows": rows}) == "unknown"


def test_key_types_and_composite_keys_do_not_collapse():
    rows = [{"id": True}, {"id": 1}, {"id": "1"}]
    assert outcome("count(keyed(rows, ['id'])) == 3", {"rows": rows}) == "true"
    rows = [{"a": "x:y", "b": "z"}, {"a": "x", "b": "y:z"}]
    assert outcome("count(keyed(rows, ['a', 'b'])) == 2", {"rows": rows}) == "true"


def test_allowed_delta_checks_full_values_and_unexpected_changes():
    before = [{"id": "a", "value": "old"}, {"id": "b", "value": "keep"}]
    allowed = [{"id": "a", "value": "new"}]
    source = "allowed_delta(keyed(old, ['id']), keyed(new, ['id']), keyed(allowed, ['id']), keyed([], ['id']))"
    inputs = {"old": before, "new": allowed + before[1:], "allowed": allowed}
    saved = deepcopy(inputs)
    assert outcome(source, inputs) == "true"
    assert inputs == saved
    for after in (
        [{"id": "a", "value": "wrong"}, before[1]],
        allowed + [{"id": "b", "value": "wrong"}],
        allowed,
        allowed + before[1:] + [{"id": "c", "value": "extra"}],
        [{"id": "a", "value": "new", "extra": True}, before[1]],
    ):
        assert outcome(source, inputs | {"new": after}) == "false"


def test_delete_permission_matches_the_actual_prior_record():
    source = "allowed_delta(keyed(old, ['id']), keyed([], ['id']), keyed([], ['id']), keyed(deleted, ['id']))"
    record = {"id": "a", "value": "old"}
    assert outcome(source, {"old": [record], "deleted": [record]}) == "true"
    assert (
        outcome(source, {"old": [record], "deleted": [{"id": "a", "value": "wrong"}]})
        == "false"
    )


def test_allowance_alone_does_not_require_an_intended_edit():
    before = [{"id": "a", "value": "old"}]
    expected = {"id": "a", "value": "new"}
    assert (
        outcome(
            "allowed_delta(keyed(old, ['id']), keyed(old, ['id']), keyed([want], ['id']), keyed([], ['id']))",
            {"old": before, "want": expected},
        )
        == "true"
    )
    assert (
        outcome(
            "has_record(keyed(old, ['id']), want)", {"old": before, "want": expected}
        )
        == "false"
    )


def test_one_budget_covers_all_predicates_and_input_values():
    results = expr.evaluate_effect_predicates(
        ("x == x",) * 10, {"x": "a" * 20}, budget=expr.EvaluationBudget(work=100)
    )
    assert results[0].outcome == "true"
    assert results[-1].outcome == "unknown"
    assert (
        outcome("true", {"unused": "large"}, budget=expr.EvaluationBudget(bytes=3))
        == "unknown"
    )


@pytest.mark.parametrize(
    "source",
    (
        "matches(x, 'a{1000}')",
        "fuzzy_match(x, ['a'], 0.9)",
        "true or matches(x, 'a{1000}')",
    ),
)
def test_effect_profile_excludes_regex_and_fuzzy_before_their_checks(
    source, monkeypatch
):
    def trap(*args, **kwargs):
        raise AssertionError("excluded operator executed")

    monkeypatch.setattr(expr, "_check_pattern", trap)
    monkeypatch.setattr(expr, "_fuzzy", trap)
    assert outcome(source, {"x": "a"}) == "unknown"


def test_surrogates_and_unsupported_numeric_values_are_unknown():
    assert outcome("x == x", {"x": "\ud800"}) == "unknown"
    assert outcome("'\ud800' == '\ud800'", {}) == "unknown"
    assert outcome("x == x", {"x": float("nan")}) == "unknown"
    assert outcome("x == x", {"x": 0.1}) == "unknown"


def test_model_cannot_supply_python_objects_or_cycles():
    class Trap:
        def __eq__(self, other):
            raise AssertionError("custom equality executed")

    assert outcome("x == x", {"x": Trap()}) == "unknown"
    cyclic = {}
    cyclic["self"] = cyclic
    assert outcome("x == x", {"x": cyclic}) == "unknown"


def test_unknown_fields_in_records_are_preserved_in_comparison():
    assert (
        outcome("a == b", {"a": {"id": "a"}, "b": {"id": "a", "extra": None}})
        == "false"
    )


def test_keyed_comparison_spends_the_same_budget_when_rows_reorder():
    left = [{"id": "a", "value": "long" * 10}, {"id": "b", "value": "old"}]
    right = [{"id": "a", "value": "long" * 10}, {"id": "b", "value": "wrong"}]
    budgets = (expr.EvaluationBudget(), expr.EvaluationBudget())
    source = "keyed(a, ['id']) == keyed(b, ['id'])"
    assert outcome(source, {"a": left, "b": right}, budget=budgets[0]) == "false"
    assert (
        outcome(source, {"a": left[::-1], "b": right[::-1]}, budget=budgets[1])
        == "false"
    )
    assert budgets[0].work == budgets[1].work


def test_intermediate_text_is_charged_and_unicode_expansion_is_bounded():
    assert (
        outcome(
            "a + b == expected",
            {"a": "ab", "b": "cd", "expected": "abcd"},
            budget=expr.EvaluationBudget(bytes=38),
        )
        == "unknown"
    )
    assert outcome("lower(x) == lower(x)", {"x": "\u0130" * 5001}) == "unknown"


def test_typed_profile_reuses_scalar_transforms_and_calendar_dates():
    assert outcome("upper(trim(x)) == 'READY'", {"x": " ready "}) == "true"
    assert (
        outcome(
            "add_days(date(x), 1) == date(y)", {"x": "2026-01-31", "y": "2026-02-01"}
        )
        == "true"
    )
