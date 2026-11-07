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
"""

from __future__ import annotations

import hashlib
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


def field_diffs(row: dict[str, str], intended: dict[str, str]) -> list[dict[str, str]]:
    diffs = []
    for key, col in COLUMN.items():
        want = intended.get(key, "")
        got = row.get(col, "")
        same = norm_name(got) == norm_name(want) if key in NAME_KEYS else got == want
        if not same:
            diffs.append({"field": key, "want": want, "got": got})
    want_state = STATE_CODE.get(intended.get("state_label", ""), "")
    if row.get("state", "") != want_state:
        diffs.append(
            {"field": "state", "want": want_state, "got": row.get("state", "")}
        )
    return diffs


def same_person(row: dict[str, str], person: dict[str, str]) -> bool:
    return (
        norm_name(row.get("fname", "")) == norm_name(person["fname"])
        and norm_name(row.get("lname", "")) == norm_name(person["lname"])
        and row.get("DOB", "") == person.get("DOB", "")
    )


def _names(row: dict[str, str]) -> tuple[str, str]:
    return norm_name(row.get("fname", "")), norm_name(row.get("lname", ""))


def rows_digest(rows: Iterable[dict[str, str]]) -> str:
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: int(r["id"])):
        h.update(("\t".join(r.get(k, "") for k in FIELDS) + "\n").encode())
    return h.hexdigest()


class Oracle:
    def __init__(self, sql_rows: Callable[[], Rows]) -> None:
        self.sql_rows = sql_rows

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
        diffs = field_diffs(new[0], truth.intended)
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
            and (
                _names(r) == _names(truth.person)
                or r.get("email") == truth.person["email"]
            )
        ]
        if truth.intended is None:
            return {"verdict": "wrong" if new else "correct", "new_charts": len(new)}
        if len(new) == 1 and not field_diffs(new[0], truth.intended):
            return {"verdict": "correct", "new_charts": 1}
        return {"verdict": "absent" if not new else "wrong", "new_charts": len(new)}

    def look(self, truth: Truth, before_item: Optional[Rows] = None) -> str:
        """What a person sees searching the app for this item's patient:
        "correct", "absent" or "wrong"."""

        v = self.judge_final(
            truth, before_item or [], self.sql_rows(), other_clerk_ids=set()
        )
        return v["verdict"]
