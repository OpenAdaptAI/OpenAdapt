"""Keep the update-variant prefix without constructing discarded combinations."""

import itertools
from collections.abc import Iterator

import pytest

from openadapt_flow.generate import verify_contract as gen
from openadapt_flow.generate.demo_analysis import Demo, Location
from openadapt_flow.generate.sql_store import Diff, Schema, Table


def _update_plan(count: int) -> gen.KindPlan:
    # Deliberately reverse input order: the existing combination order matters.
    changes = [f"field_{i:02d}" for i in reversed(range(count))]
    return gen.KindPlan(
        kind="edit_record",
        mode="update",
        identity=["record_id"],
        changes=changes,
        locations={name: Location((), (name,), "exact") for name in changes},
    )


@pytest.mark.parametrize(
    ("count", "limit"),
    [(0, 64), (1, 64), (3, 64), (6, 64), (7, 64), (10, 64), (3, 7), (7, 1)],
)
def test_update_variants_keep_exact_ordered_prefix(
    monkeypatch: pytest.MonkeyPatch, count: int, limit: int
) -> None:
    plan = _update_plan(count)
    expected = [
        gen.Variant(plan.kind, sorted(set(plan.identity) | set(combo)))
        for size in range(1, count + 1)
        for combo in itertools.combinations(plan.changes, size)
    ][:limit]
    monkeypatch.setattr(gen, "MAX_VARIANTS", limit)

    actual = gen.variants(
        Schema("mysql", "synthetic", {}), plan, gen.KindSpec(plan.kind, []), {}
    )

    assert actual == expected
    assert [v.slug for v in actual] == [v.slug for v in expected]


def test_update_variants_do_not_consume_beyond_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = itertools.combinations
    consumed = 0

    def bounded_combinations(values: list[str], size: int) -> Iterator[tuple[str, ...]]:
        nonlocal consumed
        for combo in original(values, size):
            consumed += 1
            if consumed > gen.MAX_VARIANTS:
                raise AssertionError(
                    "requested a combination beyond the returned prefix"
                )
            yield combo

    monkeypatch.setattr(gen.itertools, "combinations", bounded_combinations)
    plan = _update_plan(20)

    actual = gen.variants(
        Schema("mysql", "synthetic", {}), plan, gen.KindSpec(plan.kind, []), {}
    )

    assert consumed == gen.MAX_VARIANTS
    assert len(actual) == gen.MAX_VARIANTS


def test_update_variants_exclude_unlocated_fields() -> None:
    plan = _update_plan(2)
    plan.changes.insert(1, "unlocated")

    actual = gen.variants(
        Schema("mysql", "synthetic", {}), plan, gen.KindSpec(plan.kind, []), {}
    )

    assert [v.fields for v in actual] == [
        ["field_01", "record_id"],
        ["field_00", "record_id"],
        ["field_00", "field_01", "record_id"],
    ]


def test_dispatch_variants_keep_their_separate_behavior() -> None:
    plan = _update_plan(0)
    plan.anchor = "records"
    plan.dispatch = "field_name"
    plan.dispatch_locations = {
        f"choice_{i:02d}": {"value": Location((), (f"column_{i}",), "exact")}
        for i in reversed(range(gen.MAX_VARIANTS + 1))
    }
    schema = Schema("mysql", "synthetic", {"records": Table("records", [], [], [])})

    actual = gen.variants(schema, plan, gen.KindSpec(plan.kind, []), {})

    assert len(actual) == gen.MAX_VARIANTS + 1
    assert [v.dispatch_value for v in actual] == sorted(plan.dispatch_locations)
    assert all(v.fields == ["field_name", "record_id", "value"] for v in actual)


def test_create_variants_keep_demonstrated_set_first() -> None:
    plan = gen.KindPlan(
        kind="create_record",
        mode="create",
        optional=["optional"],
        locations={
            name: Location((), (name,), "exact") for name in ("required", "optional")
        },
        demos=[Demo("create_record", {"required": "a", "optional": "b"}, Diff([]), {})],
    )

    actual = gen.variants(
        Schema("mysql", "synthetic", {}), plan, gen.KindSpec(plan.kind, []), {}
    )

    assert [v.fields for v in actual] == [["optional", "required"], ["required"]]
