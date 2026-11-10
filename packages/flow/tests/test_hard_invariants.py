"""Fresh synthetic controls for the three named deterministic predicates."""

from __future__ import annotations

import json

import pytest

from openadapt_flow.hard_invariants import (
    HardInvariantVerdict,
    InvariantResult,
    judge_hard_invariants,
)


def observations(**over):
    return {
        "outside_scope_changes": 0,
        "undeclared_deletes": 0,
        "before_matches": 0,
        "after_matches": 1,
        "history_matches": 1,
        "scope_complete": True,
        "deletes_complete": True,
        "history_complete": True,
        "search_complete": True,
        **over,
    }


def check(result, name):
    return next(c for c in result.checks if c.name == name)


def test_complete_observations_prove_exactly_three_named_invariants():
    result = judge_hard_invariants(**observations())
    assert result.status == "held"
    assert [c.name for c in result.checks] == [
        "declared_scope",
        "at_most_one",
        "no_undeclared_deletes",
    ]
    assert {c.status for c in result.checks} == {"held"}
    assert result.evidence()["model_calls"] == 0
    assert result.evidence()["schema_version"] == "openadapt.hard-invariants/v1"


@pytest.mark.parametrize(
    "field, name, reason",
    [
        ("outside_scope_changes", "declared_scope", "outside_scope_changes"),
        ("undeclared_deletes", "no_undeclared_deletes", "undeclared_deletes"),
        ("before_matches", "at_most_one", "duplicate_business_key"),
        ("after_matches", "at_most_one", "duplicate_business_key"),
        ("history_matches", "at_most_one", "duplicate_business_key"),
    ],
)
def test_observed_violation_refutes_even_if_other_observations_are_missing(
    field, name, reason
):
    data = {k: None for k in observations()}
    data[field] = 2
    result = judge_hard_invariants(**data)
    assert result.status == "refuted"
    assert check(result, name).reason == reason
    assert check(result, name).observed_count == 2


@pytest.mark.parametrize(
    "flag, name",
    [
        ("scope_complete", "declared_scope"),
        ("deletes_complete", "no_undeclared_deletes"),
        ("history_complete", "at_most_one"),
        ("search_complete", "at_most_one"),
    ],
)
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_only_explicit_completeness_proves_a_predicate(flag, name, value):
    result = judge_hard_invariants(**observations(**{flag: value}))
    assert result.status == "not_checked"
    assert check(result, name).status == "not_checked"


@pytest.mark.parametrize(
    "field",
    [
        "outside_scope_changes",
        "undeclared_deletes",
        "before_matches",
        "after_matches",
        "history_matches",
    ],
)
@pytest.mark.parametrize("value", [None, -1, True, False, 0.5, "0"])
def test_invalid_counts_never_prove_an_invariant(field, value):
    result = judge_hard_invariants(**observations(**{field: value}))
    assert result.status == "not_checked"


def test_empty_search_does_not_erase_deleted_historical_duplicate():
    result = judge_hard_invariants(
        **observations(before_matches=0, after_matches=0, history_matches=2)
    )
    assert result.status == "refuted"
    assert check(result, "at_most_one").observed_count == 2


@pytest.mark.parametrize(
    "checks",
    [(), (InvariantResult("declared_scope", "held", "ok"),) * 3],
)
def test_partial_or_duplicate_check_lists_are_never_held(checks):
    assert HardInvariantVerdict(checks).status == "not_checked"


def test_evidence_has_only_fixed_names_reasons_and_counts():
    encoded = json.dumps(
        judge_hard_invariants(**observations()).evidence(), sort_keys=True
    )
    assert "record_id" not in encoded
    assert "business_key" not in encoded
    assert "value" not in encoded
    assert set(json.loads(encoded)) == {
        "schema_version",
        "status",
        "checks",
        "model_calls",
    }


def test_complete_history_count_cannot_be_less_than_a_snapshot_count():
    result = judge_hard_invariants(**observations(history_matches=0))
    assert result.status == "not_checked"
    assert (
        check(result, "at_most_one").reason == "business_key_observations_inconsistent"
    )
