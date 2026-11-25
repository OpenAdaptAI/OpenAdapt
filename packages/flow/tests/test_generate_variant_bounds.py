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


def _create_plan(optional_count: int, *, multi_demo: bool = False) -> gen.KindPlan:
    optional = [f"opt_{i:02d}" for i in reversed(range(optional_count))]
    locations = {
        name: Location((), (name,), "exact") for name in ("required", *optional)
    }
    demo1 = Demo(
        "create_record",
        {"required": "a", **{name: f"v_{name}" for name in optional}},
        Diff([]),
        {},
    )
    demos = [demo1]
    if multi_demo and optional_count >= 2:
        demo2 = Demo(
            "create_record",
            {"required": "b", **{name: f"w_{name}" for name in optional[1:]}},
            Diff([]),
            {},
        )
        demos.append(demo2)
    return gen.KindPlan(
        kind="create_record",
        mode="create",
        optional=optional,
        locations=locations,
        demos=demos,
    )


def _reference_create_variants(plan: gen.KindPlan, limit: int) -> list[gen.Variant]:
    located = set(plan.locations)
    base_sets = [
        frozenset(
            {
                k
                for k, v in d.values.items()
                if v is not None and not isinstance(v, list)
            }
            & located
        )
        for d in plan.demos
    ]
    optional = [p for p in plan.optional if p in located]
    seen: set[frozenset[str]] = set(base_sets)
    sets: list[frozenset[str]] = list(dict.fromkeys(base_sets))
    drops = {base: [p for p in optional if p in base] for base in base_sets}
    for n in range(1, max((len(v) for v in drops.values()), default=0) + 1):
        for base in base_sets:
            for combo in itertools.combinations(drops[base], n):
                s = frozenset(base - set(combo))
                if s not in seen:
                    seen.add(s)
                    sets.append(s)
    for s in list(sets):
        for p in optional:
            if p not in s:
                t = frozenset(s | {p})
                if t not in seen:
                    seen.add(t)
                    sets.append(t)
    return [gen.Variant(plan.kind, sorted(s)) for s in sets[:limit]]


@pytest.mark.parametrize(
    ("optional_count", "limit", "multi_demo"),
    [
        (0, 64, False),
        (1, 64, False),
        (4, 64, False),
        (6, 64, False),
        (7, 64, False),
        (5, 7, False),
        (6, 1, False),
        (5, 10, True),
        (6, 64, True),
    ],
)
def test_create_variants_keep_exact_ordered_prefix(
    monkeypatch: pytest.MonkeyPatch,
    optional_count: int,
    limit: int,
    multi_demo: bool,
) -> None:
    plan = _create_plan(optional_count, multi_demo=multi_demo)
    expected = _reference_create_variants(plan, limit)
    monkeypatch.setattr(gen, "MAX_VARIANTS", limit)

    actual = gen.variants(
        Schema("mysql", "synthetic", {}), plan, gen.KindSpec(plan.kind, []), {}
    )

    assert actual == expected
    assert [v.slug for v in actual] == [v.slug for v in expected]


def test_create_variants_do_not_consume_beyond_limit(
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
                    "create mode requested combinations beyond MAX_VARIANTS"
                )
            yield combo

    monkeypatch.setattr(gen.itertools, "combinations", bounded_combinations)
    plan = _create_plan(20)

    actual = gen.variants(
        Schema("mysql", "synthetic", {}), plan, gen.KindSpec(plan.kind, []), {}
    )

    assert consumed == gen.MAX_VARIANTS - 1
    assert len(actual) == gen.MAX_VARIANTS
