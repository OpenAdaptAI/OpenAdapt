"""The independent oracle: reads OpenEMR's database directly, never the arm.

It runs as the database root inside the fixture's own database container, a
channel no arm uses. It judges an item twice:

- right after the arm returns (the arm's own outcome), counting only charts
  the arm could have written: rows the harness wrote itself (another clerk,
  the operator by hand) are attributed by row id and left out;
- after the operator has dealt with the item (the item's final state), over
  every chart for that person.

A chart matches the truth when every field matches: names trimmed and
compared without case (as a person reads a name), every other field exactly.

Which columns, value maps and identity rule apply is the app's
:class:`OracleSchema`. The defaults are OpenEMR's ``patient_data``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .workload import Truth

FIELDS = (
    "id",
    "pid",
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "state",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
)
#: Truth key -> patient_data column.
COLUMN = {
    "title": "title",
    "fname": "fname",
    "lname": "lname",
    "DOB": "DOB",
    "sex": "sex",
    "street": "street",
    "city": "city",
    "postal_code": "postal_code",
    "phone_home": "phone_home",
    "email": "email",
    "country_code": "country_code",
}
STATE_CODE = {"Massachusetts": "MA"}
NAME_KEYS = {"fname", "lname"}

Rows = list[dict[str, str]]


def norm_name(v: str) -> str:
    return " ".join(v.split()).lower()


@dataclass(frozen=True)
class OracleSchema:
    """How the oracle reads one app's records.

    - ``fields``: every column of a row, in a fixed order (the digest).
    - ``columns``: truth key -> column, compared exactly.
    - ``value_maps``: truth key -> (column, map from the entered value to the
      stored one), for a form that shows one value and stores another.
    - ``name_keys``: truth keys compared as a person reads a name.
    - ``same_keys``: truth keys that make two records the same person or
      item (``same_person``).
    - ``belongs_keys``: truth keys any one of which, when equal, ties a new
      record to the item in ``judge_final`` (names count together).
    """

    fields: tuple[str, ...]
    columns: dict[str, str]
    value_maps: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)
    name_keys: frozenset[str] = frozenset()
    same_keys: tuple[str, ...] = ()
    belongs_keys: tuple[str, ...] = ()


OPENEMR_SCHEMA = OracleSchema(
    fields=FIELDS,
    columns=COLUMN,
    value_maps={"state_label": ("state", STATE_CODE)},
    name_keys=frozenset(NAME_KEYS),
    same_keys=("fname", "lname", "DOB"),
    belongs_keys=("email",),
)


def _equal(schema: OracleSchema, key: str, got: str, want: str) -> bool:
    if key in schema.name_keys:
        return norm_name(got) == norm_name(want)
    return got == want


def field_diffs(
    row: dict[str, str],
    intended: dict[str, str],
    schema: OracleSchema = OPENEMR_SCHEMA,
) -> list[dict[str, str]]:
    diffs = []
    for key, col in schema.columns.items():
        want = intended.get(key, "")
        got = row.get(col, "")
        if not _equal(schema, key, got, want):
            diffs.append({"field": key, "want": want, "got": got})
    for key, (col, mapping) in schema.value_maps.items():
        want = mapping.get(intended.get(key, ""), "")
        if row.get(col, "") != want:
            diffs.append({"field": col, "want": want, "got": row.get(col, "")})
    return diffs


def same_person(
    row: dict[str, str],
    person: dict[str, str],
    schema: OracleSchema = OPENEMR_SCHEMA,
) -> bool:
    return all(
        _equal(
            schema,
            key,
            row.get(schema.columns.get(key, key), ""),
            person.get(key, ""),
        )
        for key in schema.same_keys
    )


def _names(
    row: dict[str, str], schema: OracleSchema = OPENEMR_SCHEMA
) -> tuple[str, ...]:
    return tuple(
        norm_name(row.get(schema.columns.get(k, k), ""))
        for k in sorted(schema.name_keys)
    )


def belongs(
    row: dict[str, str],
    person: dict[str, str],
    schema: OracleSchema = OPENEMR_SCHEMA,
) -> bool:
    """Whether a record carries this item's name or one of its keys."""

    if schema.name_keys and _names(row, schema) == tuple(
        norm_name(person.get(k, "")) for k in sorted(schema.name_keys)
    ):
        return True
    return any(
        person.get(k) and row.get(schema.columns.get(k, k)) == person.get(k)
        for k in schema.belongs_keys
    )


def rows_digest(
    rows: Iterable[dict[str, str]], schema: OracleSchema = OPENEMR_SCHEMA
) -> str:
    h = hashlib.sha256()
    # Numeric ids sort as numbers; string ids (FHIR) sort too.
    for r in sorted(rows, key=lambda r: (len(r["id"]), r["id"])):
        h.update(("\t".join(r.get(k, "") for k in schema.fields) + "\n").encode())
    return h.hexdigest()


class Oracle:
    def __init__(
        self, sql_rows: Callable[[], Rows], schema: OracleSchema = OPENEMR_SCHEMA
    ) -> None:
        self.sql_rows = sql_rows
        self.schema = schema

    def snapshot(self) -> Rows:
        return self.sql_rows()

    def judge_arm(
        self,
        truth: Truth,
        before: Rows,
        after: Rows,
        *,
        harness_ids: set[str],
        harness_touched: set[str],
    ) -> dict[str, Any]:
        """The arm's outcome: "correct", "absent", "wrong" or "duplicate"."""

        before_ids = {r["id"] for r in before}
        new = [
            r for r in after if r["id"] not in before_ids and r["id"] not in harness_ids
        ]
        old_before = {r["id"]: r for r in before if r["id"] not in harness_touched}
        old_after = {r["id"]: r for r in after if r["id"] in old_before}
        collateral = sorted(i for i in old_before if old_before[i] != old_after.get(i))
        detail: dict[str, Any] = {
            "new_charts": len(new),
            "collateral_ids": collateral,
            "new_ids": [r["id"] for r in new],
        }
        if collateral:
            return {"verdict": "wrong", "why": "collateral", **detail}
        if truth.intended is None:
            if new:
                return {
                    "verdict": "wrong",
                    "why": "chart written where none wanted",
                    **detail,
                }
            return {"verdict": "correct", "why": "no new chart", **detail}
        if not new:
            return {"verdict": "absent", "why": "no new chart", **detail}
        if len(new) > 1:
            return {"verdict": "duplicate", "why": f"{len(new)} new charts", **detail}
        diffs = field_diffs(new[0], truth.intended, self.schema)
        if diffs:
            return {
                "verdict": "wrong",
                "why": "field mismatch",
                "diffs": diffs,
                **detail,
            }
        return {"verdict": "correct", "why": "one chart, every field right", **detail}

    def judge_final(
        self, truth: Truth, before_item: Rows, now: Rows, *, other_clerk_ids: set[str]
    ) -> dict[str, Any]:
        """The item's final state, whoever wrote it (other clerks aside).

        Every chart written since the item started that carries this
        person's name or email counts, so a chart with a mistyped date of
        birth is still found."""

        prior = {r["id"] for r in before_item}
        new = [
            r
            for r in now
            if r["id"] not in prior
            and r["id"] not in other_clerk_ids
            and belongs(r, truth.person, self.schema)
        ]
        if truth.intended is None:
            return {"verdict": "wrong" if new else "correct", "new_charts": len(new)}
        if len(new) == 1 and not field_diffs(new[0], truth.intended, self.schema):
            return {"verdict": "correct", "new_charts": 1}
        return {"verdict": "absent" if not new else "wrong", "new_charts": len(new)}

    def look(self, truth: Truth, before_item: Optional[Rows] = None) -> str:
        """What a person sees searching the app for this item's patient:
        "correct", "absent" or "wrong"."""

        v = self.judge_final(
            truth, before_item or [], self.sql_rows(), other_clerk_ids=set()
        )
        return v["verdict"]
