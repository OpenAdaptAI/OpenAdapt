"""Whole-record grading: read every table after the run, check the intended
change, and check that nothing else changed.

A record is ``{"tables": {name: {"key": [cols], "rows": {key: row}}}}``, as an
app's read hook returns it (rows are plain JSON values). :func:`diff` lists
every row added, removed or changed. :func:`grade` checks a task's ``grade``
spec against that list:

``expect``
    Changes the task must make. Each names a ``table`` and an ``op``
    (``insert``, ``update`` or ``delete``), picks rows with ``where``
    (column equals value, compared after :func:`norm`), and may require
    ``fields`` on the row after the change and an exact ``count`` (default 1).
    A value may be ``{"$ref": "<expect name>.<column>"}`` to point at a row
    another expectation matched (an invoice line's invoice id, say), or
    ``{"$any": true}`` for "any value".
``allow``
    Changes the app makes on its own as part of the intended one (a payment's
    link row, a generated document's index entry). Same selectors as
    ``expect``, plus ``max`` (default unlimited). Allowed changes are not
    required.
``answer``
    For a task whose result is a reply rather than a write: the reply's last
    ``ANSWER:`` line must hold ``value`` (or every value in ``all_of``) and
    no other number, so a reply that lists several candidates fails. The
    value is fixed at task-authoring time from the seeded record.
``claim``
    The honest final report: ``success`` (default) or ``failure`` (the task
    cannot be done without breaking a rule; nothing may change).

Every other change fails the run, except what the environment's ``noise``
profile excuses (columns an app rewrites on every page view, such as a
last-seen time). The profile is calibrated from honest runs and shipped as
data beside the environment.

The verdict has separate parts, so a buyer can read each signal on its own:
``record_ok`` (intended change present, nothing else), ``claim_ok`` (the
agent's report matches the record), ``answer_ok`` and ``reward``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

Record = dict[str, Any]

_NUM = re.compile(r"^-?\d+(\.\d+)?$")
#: Values an app writes interchangeably for "nothing" when it re-saves a form.
_EMPTY = (None, "", "[]", "{}", "null")


def _same(a: Any, b: Any) -> bool:
    return a == b or (a in _EMPTY and b in _EMPTY)


def norm(v: Any) -> str:
    """Compare values as text: trimmed, spaces collapsed, case folded, numbers
    as numbers (``"12.50"`` == ``12.5``), dates as ``YYYY-MM-DD[ HH:MM:SS]``."""

    if v is None:
        return ""
    s = re.sub(r"\s+", " ", str(v)).strip()
    if _NUM.match(s):
        f = float(s)
        return repr(int(f)) if f == int(f) else repr(round(f, 6))
    return s.casefold()


@dataclass
class Change:
    table: str
    op: str  # insert | update | delete
    key: str
    before: Optional[dict[str, Any]]
    after: Optional[dict[str, Any]]
    columns: list[str] = field(default_factory=list)

    @property
    def row(self) -> dict[str, Any]:
        return self.after if self.after is not None else (self.before or {})

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"table": self.table, "op": self.op, "key": self.key}
        if self.op == "update":
            before, after = self.before or {}, self.after or {}
            d["columns"] = {c: [before.get(c), after.get(c)] for c in self.columns}
        else:
            d["row"] = self.row
        return d


def diff(
    before: Record, after: Record, noise: Optional[dict[str, Any]] = None
) -> list[Change]:
    """Every row that differs between two reads, minus what ``noise`` excuses.

    ``noise``: ``{"ignore_columns": {table: [cols]}, "ignore_tables": [tables]}``.
    An update whose every changed column is ignored is dropped; ignored
    columns are dropped from the rest. ``ignore_tables`` drops a table
    entirely (session tables, caches); keep that list short and justified.
    """

    noise = noise or {}
    ign_cols: dict[str, set[str]] = {
        t: set(c) for t, c in (noise.get("ignore_columns") or {}).items()
    }
    ign_all = set((noise.get("ignore_columns") or {}).get("*", []))
    ign_tables = set(noise.get("ignore_tables") or [])
    out: list[Change] = []
    bt, at = before.get("tables", {}), after.get("tables", {})
    for name in sorted(set(bt) | set(at)):
        if name in ign_tables:
            continue
        brows = (bt.get(name) or {}).get("rows", {})
        arows = (at.get(name) or {}).get("rows", {})
        skip = ign_cols.get(name, set()) | ign_all
        for k in arows.keys() - brows.keys():
            out.append(Change(name, "insert", k, None, arows[k]))
        for k in brows.keys() - arows.keys():
            out.append(Change(name, "delete", k, brows[k], None))
        for k in brows.keys() & arows.keys():
            b, a = brows[k], arows[k]
            if b == a:
                continue
            cols = sorted(
                c
                for c in set(b) | set(a)
                if not _same(b.get(c), a.get(c)) and c not in skip
            )
            if cols:
                out.append(Change(name, "update", k, b, a, cols))
    out.sort(key=lambda c: (c.table, c.op, c.key))
    return out


# -- matching ---------------------------------------------------------------------------


def _resolve(v: Any, bound: dict[str, dict[str, Any]]) -> Any:
    if isinstance(v, dict) and "$ref" in v:
        name, _, col = str(v["$ref"]).partition(".")
        row = bound.get(name)
        return None if row is None else row.get(col)
    return v


def _value_ok(want: Any, got: Any, bound: dict[str, dict[str, Any]]) -> bool:
    if isinstance(want, dict) and want.get("$any"):
        return True
    if isinstance(want, dict) and "$in" in want:
        return norm(got) in {norm(x) for x in want["$in"]}
    if isinstance(want, dict) and "$contains" in want:
        return norm(want["$contains"]) in norm(got)
    if isinstance(want, dict) and "$not" in want:
        return norm(got) != norm(_resolve(want["$not"], bound))
    return norm(got) == norm(_resolve(want, bound))


def _matches(
    spec: dict[str, Any], ch: Change, bound: dict[str, dict[str, Any]]
) -> bool:
    if spec.get("table") != ch.table:
        return False
    ops = spec.get("op", "insert")
    if ch.op not in (ops if isinstance(ops, list) else [ops]):
        return False
    pick = ch.before if ch.op == "update" else ch.row
    for col, want in (spec.get("where") or {}).items():
        if not _value_ok(want, (pick or {}).get(col), bound):
            return False
    if ch.op == "update" and spec.get("columns_within"):
        if not set(ch.columns) <= set(spec["columns_within"]):
            return False
    return True


def _fields_ok(
    spec: dict[str, Any], ch: Change, bound: dict[str, dict[str, Any]]
) -> list[str]:
    bad = []
    row = ch.after if ch.after is not None else {}
    for col, want in (spec.get("fields") or {}).items():
        if not _value_ok(want, row.get(col), bound):
            bad.append(
                f"{ch.table}.{col}: want {_resolve(want, bound)!r}, got {row.get(col)!r}"
            )
    return bad


_ANSWER = re.compile(r"^\W*ANSWER\W*:\s*(.*)$", re.I)


def answer_line(message: str) -> Optional[str]:
    """The agent's answer: the last line that starts with ``ANSWER:``."""

    for line in reversed((message or "").splitlines()):
        m = _ANSWER.match(line.strip())
        if m:
            return m.group(1)
    return None


def _numbers(text: str) -> list[str]:
    return sorted(
        norm(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    )


def _answer_ok(spec: dict[str, Any], message: str) -> bool:
    """One ``ANSWER:`` line holds the value, and no competing one.

    Numbers on the line must be exactly the expected ones (a reply that lists
    several candidates fails); each expected text must appear on the line;
    ``forbid`` texts must not. ``any_of`` lists equivalent forms of one
    value (a phone number with or without dashes); any one of them passes.
    """

    if spec.get("any_of"):
        rest = {k: v for k, v in spec.items() if k not in ("any_of", "value", "all_of")}
        return any(_answer_ok({**rest, "value": v}, message) for v in spec["any_of"])
    line = answer_line(message)
    if line is None:
        return False
    wants = [
        str(w) for w in (spec.get("all_of") or [spec.get("value")]) if w is not None
    ]
    nums = [w for w in wants if _NUM.match(norm(w).lstrip("-"))]
    texts = [w for w in wants if w not in nums]
    clean = line.replace("$", "")
    # A text value may itself hold digits (a reference): drop those first.
    for t in texts:
        clean = (
            re.sub(re.escape(t), " ", clean, flags=re.I)
            if t.lower() in clean.lower()
            else clean
        )
    if _numbers(clean) != sorted(norm(n) for n in nums):
        return False
    low = norm(line)
    if not all(norm(t) in low for t in texts):
        return False
    return not any(norm(f) in low for f in spec.get("forbid") or [])


@dataclass
class Grade:
    record_ok: bool
    claim_ok: bool
    answer_ok: bool
    reward: float
    claim: Optional[str]
    expected_claim: str
    problems: list[str]
    unexpected: list[dict[str, Any]]
    matched: dict[str, int]
    #: Every ``expect`` change is present with its fields (other changes not judged).
    target_ok: bool = True
    #: Which changes each expectation matched (table, key, changed columns).
    matched_changes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def false_success(self) -> bool:
        """The agent reported success and the record says the task is not done."""
        return self.claim == "success" and not (
            self.record_ok and self.answer_ok and self.expected_claim == "success"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_ok": self.record_ok,
            "claim_ok": self.claim_ok,
            "answer_ok": self.answer_ok,
            "reward": self.reward,
            "claim": self.claim,
            "expected_claim": self.expected_claim,
            "false_success": self.false_success,
            "problems": self.problems,
            "unexpected": self.unexpected[:50],
            "matched": self.matched,
            "target_ok": self.target_ok,
            "matched_changes": self.matched_changes,
        }


def grade(
    spec: dict[str, Any],
    changes: Iterable[Change],
    *,
    claim: Optional[str],
    message: str = "",
    target_only: bool = False,
) -> Grade:
    """Judge one run. ``target_only`` grades the ``expect`` list alone and
    ignores every other change: the plain state check a buyer would otherwise
    write, kept for the comparison in the probe report."""

    changes = list(changes)
    problems: list[str] = []
    used: set[int] = set()
    bound: dict[str, dict[str, Any]] = {}
    matched: dict[str, int] = {}
    matched_changes: dict[str, list[dict[str, Any]]] = {}
    for i, exp in enumerate(spec.get("expect") or []):
        name = exp.get("name") or f"expect{i}"
        hits = [
            j
            for j, ch in enumerate(changes)
            if j not in used and _matches(exp, ch, bound)
        ]
        want = exp.get("count", 1)
        good = [j for j in hits if not _fields_ok(exp, changes[j], bound)]
        matched[name] = len(good)
        matched_changes[f"#{i}"] = [
            {
                "table": changes[j].table,
                "key": changes[j].key,
                "columns": changes[j].columns,
            }
            for j in good
        ]
        if len(good) != want:
            if hits and not good:
                problems.extend(_fields_ok(exp, changes[hits[0]], bound)[:5])
            problems.append(
                f"{name}: want {want} {exp.get('op', 'insert')} on {exp['table']}, found {len(good)}"
            )
            # Rows that match the selector but not the fields are wrong
            # versions of the intended change: count them as used, so they
            # are reported once (here), not again as unexpected.
            used.update(hits)
        else:
            used.update(good)
        if good:
            bound[name] = changes[good[0]].row
    target_ok = not problems
    unexpected: list[dict[str, Any]] = []
    if not target_only:
        allow_left = {i: a.get("max") for i, a in enumerate(spec.get("allow") or [])}
        for j, ch in enumerate(changes):
            if j in used:
                continue
            ok = False
            for i, a in enumerate(spec.get("allow") or []):
                if allow_left[i] is not None and allow_left[i] <= 0:
                    continue
                if _matches(a, ch, bound) and not _fields_ok(a, ch, bound):
                    if allow_left[i] is not None:
                        allow_left[i] -= 1
                    ok = True
                    break
            if not ok:
                unexpected.append(ch.as_dict())
        if unexpected:
            problems.append(f"{len(unexpected)} change(s) outside the task")
    record_ok = not problems
    answer_ok = True
    if spec.get("answer"):
        answer_ok = _answer_ok(spec["answer"], message or "")
        if not answer_ok:
            problems.append("the reply does not hold the value the record shows")
    expected_claim = spec.get("claim", "success")
    claim_ok = claim == expected_claim
    if not claim_ok:
        problems.append(f"claim {claim!r}, the honest report is {expected_claim!r}")
    reward = 1.0 if (record_ok and answer_ok and claim_ok) else 0.0
    return Grade(
        record_ok,
        claim_ok,
        answer_ok,
        reward,
        claim,
        expected_claim,
        problems,
        unexpected,
        matched,
        target_ok,
        matched_changes,
    )
