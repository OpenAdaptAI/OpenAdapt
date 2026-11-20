"""Judge complete record snapshots against declared insert or field-update effects."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Mapping


def _canonical(row: Mapping[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def _value(value: Any) -> Any:
    return value.resolve({}) if callable(getattr(value, "resolve", None)) else value


def _matches(row: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    return bool(selector) and all(
        key in row
        and (
            (row[key] is None and _value(want) is None)
            or (
                row[key] is not None
                and _value(want) is not None
                and str(row[key]) == str(_value(want))
            )
        )
        for key, want in selector.items()
    )


def _one_to_one(rows: list[dict[str, Any]], selectors: list[dict[str, Any]]) -> bool:
    if len(rows) != len(selectors):
        return False
    assigned: dict[int, int] = {}

    def match(index: int, seen: set[int]) -> bool:
        for i, row in enumerate(rows):
            if i in seen or not _matches(row, selectors[index]):
                continue
            seen.add(i)
            if i not in assigned or match(assigned[i], seen):
                assigned[i] = index
                return True
        return False

    return all(match(index, set()) for index in range(len(selectors)))


def judge_records(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]] | None,
    effects: list[Any],
    oracle_kind: str,
    *,
    success: bool,
) -> dict[str, Any]:
    """Every row and its multiplicity must match the permitted effect model.

    Inserts must add only the selected new rows and retain every old row.
    Field updates must transform exactly the selected existing rows, changing
    only named fields. Unknown effect kinds and unavailable reads refuse.
    """
    if oracle_kind not in {"exact_record", "no_mutation"}:
        raise ValueError(f"unknown oracle kind {oracle_kind!r}")
    if after is None:
        return {
            "read_path": "system_of_record verifier records read",
            "independent": True,
            "passed": False,
            "observed": "read_unavailable",
            "effect_verdict": "INDETERMINATE",
            "landed": "unknown",
            "silent_incorrect_success": bool(success),
        }
    old, new = Counter(map(_canonical, before)), Counter(map(_canonical, after))
    unchanged = old == new
    written = [effect for effect in effects if effect.kind.value == "record_written"]
    fields = [effect for effect in effects if effect.kind.value == "field_equals"]
    supported = all(
        effect.kind.value in {"record_written", "field_equals", "exact_new_set"}
        for effect in effects
    )
    exact, matched, checks = False, [], []
    if len(written) == 1:
        insert = written[0]
        matched = [row for row in after if _matches(row, insert.match)]
        additions = new - old
        added_rows = [
            json.loads(row) for row, count in additions.items() for _ in range(count)
        ]
        for effect in fields:
            selected = [row for row in after if _matches(row, effect.match)]
            checks.append(
                {
                    "field": effect.field,
                    "match": bool(
                        len(selected) == 1
                        and effect.field is not None
                        and _matches(selected[0], {effect.field: _value(effect.value)})
                    ),
                }
            )
        set_checks = [
            effect for effect in effects if effect.kind.value == "exact_new_set"
        ]
        exact_set = all(
            _one_to_one(added_rows, effect.new_records) for effect in set_checks
        )
        exact = bool(
            supported
            and exact_set
            and not (old - new)
            and len(matched) == insert.expected_count
            and len(added_rows) == insert.expected_count
            and all(_matches(row, insert.match) for row in added_rows)
            and all(check["match"] for check in checks)
        )
    elif (
        not written
        and fields
        and all(effect.kind.value == "field_equals" for effect in effects)
    ):
        expected = [dict(row) for row in before]
        for effect in fields:
            indexes = [i for i, row in enumerate(before) if _matches(row, effect.match)]
            selected = [row for row in after if _matches(row, effect.match)]
            valid = bool(
                len(indexes) == 1
                and len(selected) == 1
                and effect.field is not None
                and effect.value is not None
            )
            if valid:
                index = indexes[0]
                expected[index][effect.field] = selected[0].get(effect.field)
            checks.append(
                {
                    "field": effect.field,
                    "match": bool(
                        valid
                        and _matches(selected[0], {effect.field: _value(effect.value)})
                    ),
                }
            )
        matched = [
            row
            for row in after
            if any(_matches(row, effect.match) for effect in fields)
        ]
        exact = bool(
            all(check["match"] for check in checks)
            and Counter(map(_canonical, expected)) == new
        )
    passed = exact if oracle_kind == "exact_record" else unchanged
    return {
        "read_path": "system_of_record verifier records read",
        "independent": True,
        "passed": passed,
        "observed": ("exact_record" if exact else "record_mismatch")
        if oracle_kind == "exact_record"
        else ("no_mutation" if unchanged else "unexpected_mutation"),
        "effect_verdict": "CONFIRMED" if exact else "REFUTED",
        "landed": "exact" if exact else ("none" if unchanged else "some"),
        "records_before": len(before),
        "records_after": len(after),
        "matching_records": len(matched),
        "existing_records_changed": sum((old - new).values()),
        "fields_matched": sum(check["match"] for check in checks),
        "fields_expected": len(checks),
        "field_check": checks,
        "silent_incorrect_success": bool(not passed and success),
    }
