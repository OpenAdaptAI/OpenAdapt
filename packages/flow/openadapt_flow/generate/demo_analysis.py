"""Find where each parameter of a kind of write lands, from demonstration diffs.

A demonstration is one correct write of a kind, made by the person or the
setup agent, with the store snapshotted before and after
(:mod:`openadapt_flow.generate.sql_store`). This module reads the diffs and
says, for each parameter, which table and column holds its value, and by
which exact rule:

- ``exact``: the stored text equals the value's text;
- ``email``: equal after case folding (``ValueExpr`` kind ``email``);
- ``phone``: equal digits (``ValueExpr`` kind ``phone``);
- ``number``, ``date``, ``datetime``: equal by value, compared in SQL after a
  cast of the parameter to the column's type;
- ``minutes``: the number of minutes between two date-time columns of the
  row;
- a display column of a row another column points to (a declared foreign
  key, or a column whose value is that row's primary key), or two text
  columns joined by one space (a person's first and last name);
- a row the write added beside the main record, linked to it by a column
  that holds the main record's key.

Every rule is exact: no fuzzy matching, no model. A parameter found in more
than one place in every demonstration, or in none, is reported, and the
generator turns it into a question for a person.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from openadapt_flow.generate.sql_store import (
    Diff,
    RowChange,
    Schema,
    Table,
    TableRows,
)
from openadapt_flow.runtime.effects.effect import kind_form

#: Rules a value can be found by, most exact first.
RULES = ("exact", "email", "phone", "number", "date", "datetime", "minutes")

_IDLIKE = re.compile(r"(^id$|_id$|^id_|^fk_|_fk$|^parent$|^parent_id$)", re.I)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Table names an app's own logs usually carry. Only a proposal: a person
#: confirms which tables are logs (the bookkeeping question).
_LOG_NAME = re.compile(
    r"(log|history|audit|session|token|notification|queue|job|stat|cache|lock|event)",
    re.I,
)


def value_text(value: Any) -> str:
    """A parameter value as text, the way the verifier renders it."""

    from openadapt_flow.runtime.authorization import runtime_param_text

    if isinstance(value, str):
        return value
    return runtime_param_text(value)


def _decimal(text: Any) -> Optional[decimal.Decimal]:
    if text is None or isinstance(text, bool):
        return None
    try:
        d = decimal.Decimal(str(text).strip())
    except (decimal.InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _datetime(text: str) -> Optional[_dt.datetime]:
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%MZ",
    ):
        try:
            return _dt.datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def rule_for(stored: Optional[str], column_kind: str, value: Any) -> Optional[str]:
    """The most exact rule by which ``stored`` holds ``value``, or None."""

    if stored is None or value is None or isinstance(value, (list, dict)):
        return None
    text = value_text(value)
    if text == "":
        return None
    if stored == text and column_kind not in ("number", "bool", "date", "datetime"):
        # A number or date column is compared by value even when this
        # demonstration's text happens to match: another row of the same
        # column may print as ``5.5000``.
        return "exact"
    if column_kind == "text":
        if "@" in text and stored.casefold() == text.casefold():
            return "email"
        digits = sum(ch.isdigit() for ch in text)
        if digits >= 6:
            a, b = kind_form("phone", stored), kind_form("phone", text)
            if a is not None and a == b:
                return "phone"
        return None
    if column_kind in ("number", "bool"):
        a, b = _decimal(stored), _decimal(text)
        if a is not None and b is not None and a == b:
            return "number"
        return None
    if column_kind == "date":
        return "date" if stored[:10] == text and len(text) == 10 else None
    if column_kind == "datetime":
        a, b = _datetime(stored.split(".")[0]), _datetime(text)
        if a is not None and b is not None and a == b:
            return "datetime"
        return None
    return None


@dataclass(frozen=True)
class Hop:
    """One step from a row to another row.

    ``fk``: this row's ``column`` holds the key ``ref_column`` of a row of
    ``table``. ``child``: a row of ``table`` whose ``column`` holds this row's
    primary key ``ref_column``.
    """

    kind: str  # fk | child
    table: str
    column: str
    ref_column: str
    declared: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "table": self.table,
            "column": self.column,
            "ref_column": self.ref_column,
            "declared": self.declared,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Hop":
        return cls(
            str(d["kind"]),
            str(d["table"]),
            str(d["column"]),
            str(d["ref_column"]),
            bool(d.get("declared", False)),
        )


@dataclass(frozen=True)
class Location:
    """Where a value is: a path of hops from the main row, then the column
    (or two columns joined by a space) that holds it, and the rule."""

    hops: tuple[Hop, ...]
    columns: tuple[str, ...]
    rule: str

    @property
    def table_hint(self) -> str:
        return self.hops[-1].table if self.hops else ""

    def signature(self) -> str:
        path = "/".join(
            f"{h.kind}:{h.table}.{h.column}={h.ref_column}" for h in self.hops
        )
        return f"{path}#{'+'.join(self.columns)}@{self.rule}"

    def describe(self, anchor: str) -> str:
        where = anchor
        for h in self.hops:
            if h.kind == "fk":
                where = f"{where}.{h.column} -> {h.table}"
            else:
                where = f"{h.table} (whose {h.column} is the {where} key)"
        cols = " || ' ' || ".join(self.columns)
        return f"{where}.{cols} ({self.rule})"

    def to_json(self) -> dict[str, Any]:
        return {
            "hops": [h.to_json() for h in self.hops],
            "columns": list(self.columns),
            "rule": self.rule,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Location":
        return cls(
            tuple(Hop.from_json(h) for h in d.get("hops", [])),
            tuple(d["columns"]),
            str(d["rule"]),
        )


def _hop_cost(h: Hop) -> int:
    """0 for a declared foreign key, 1 for a column named after the table it
    points to (``status_id`` -> ``issue_statuses``), 2 for a value match only."""

    if h.declared:
        return 0
    stem = re.sub(r"(^fk_|_id$|_fk$|^id_)", "", h.column.lower()).rstrip("s")
    table = h.table.lower()
    return 1 if stem and len(stem) >= 3 and stem in table else 2


def _rank(loc: Location) -> tuple[int, int, int, int]:
    return (
        len(loc.hops),
        sum(_hop_cost(h) for h in loc.hops),
        RULES.index(loc.rule) if loc.rule in RULES else 99,
        len(loc.columns),
    )


@dataclass
class Demo:
    """One demonstration: the kind, the values given, and the diff."""

    kind: str
    values: dict[str, Any]
    diff: Diff
    after: Mapping[str, TableRows]
    started_at: str = ""
    finished_at: str = ""
    name: str = ""


class _Index:
    """Rows by table and single-column key, from one snapshot."""

    def __init__(self, schema: Schema, rows: Mapping[str, TableRows]) -> None:
        self.schema = schema
        self.rows = rows
        self._by_key: dict[str, dict[str, dict[str, Optional[str]]]] = {}
        for name, table in schema.tables.items():
            t = rows.get(name)
            if t is None or t.count_only is not None or len(table.primary_key) != 1:
                continue
            pk = table.primary_key[0]
            self._by_key[name] = {
                str(r.get(pk)): r for r in t.rows.values() if r.get(pk) is not None
            }

    def row(self, table: str, key: str) -> Optional[dict[str, Optional[str]]]:
        return self._by_key.get(table, {}).get(key)

    def keyed_tables(self) -> list[str]:
        return list(self._by_key)


def _fk_targets(
    schema: Schema, idx: _Index, table: Table, column: str, value: str
) -> list[tuple[str, str, bool]]:
    """Rows ``table.column = value`` may point to: (table, key column,
    declared)."""

    out = []
    for fk in table.foreign_keys:
        if fk.column == column and idx.row(fk.table, value) is not None:
            target = schema.tables.get(fk.table)
            if target is not None and target.primary_key == [fk.ref_column]:
                out.append((fk.table, fk.ref_column, True))
    if out:
        return out
    if not _IDLIKE.search(column) or value in ("", "0"):
        return []
    for name in idx.keyed_tables():
        if idx.row(name, value) is not None:
            out.append((name, schema.tables[name].primary_key[0], False))
    return out


def _display_matches(
    table: Table, row: Mapping[str, Optional[str]], value: Any
) -> list[tuple[tuple[str, ...], str]]:
    """Columns of ``row`` that hold ``value``, with the rule."""

    out: list[tuple[tuple[str, ...], str]] = []
    for col in table.columns:
        rule = rule_for(row.get(col.name), col.kind, value)
        if rule is not None:
            out.append(((col.name,), rule))
    if isinstance(value, str) and " " in value.strip():
        texts = [c for c in table.columns if c.kind == "text"]
        for i, a in enumerate(texts):
            for b in texts[i + 1 : i + 3]:
                va, vb = row.get(a.name), row.get(b.name)
                if va and vb and f"{va} {vb}" == value:
                    out.append(((a.name, b.name), "exact"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamps = [c for c in table.columns if c.kind == "datetime"]
        for a in stamps:
            for b in stamps:
                if a is b:
                    continue
                ta, tb = row.get(a.name), row.get(b.name)
                da = _datetime((ta or "").split(".")[0]) if ta else None
                db = _datetime((tb or "").split(".")[0]) if tb else None
                if da and db and db > da:
                    minutes = decimal.Decimal((db - da).total_seconds()) / 60
                    if minutes == _decimal(value_text(value)):
                        out.append(((a.name, b.name), "minutes"))
    return out


def locate(
    schema: Schema,
    demo: Demo,
    anchor: RowChange,
    value: Any,
    *,
    before: bool = False,
    changed_only: bool = False,
    max_hops: int = 2,
    links: Optional[Mapping[str, set[str]]] = None,
) -> list[Location]:
    """Every location of ``value`` from the main row ``anchor``.

    ``before``: look in the row as it was before the write (an update's
    identity). ``changed_only``: at depth 0, only columns the write changed
    (an update's new values).
    """

    idx = _Index(schema, demo.after)
    table = schema.tables[anchor.table]
    row = anchor.before if before else anchor.after
    if row is None:
        return []
    found: list[Location] = []
    changed = set(anchor.changed_columns)
    added = set()
    for ch in demo.diff.changes:
        t2 = schema.tables.get(ch.table)
        if (
            ch.kind == "added"
            and ch.after
            and t2 is not None
            and len(t2.primary_key) == 1
        ):
            added.add((ch.table, ch.after.get(t2.primary_key[0])))

    def walk(
        t: Table, r: Mapping[str, Optional[str]], hops: tuple[Hop, ...], depth: int
    ) -> None:
        for cols, rule in _display_matches(t, r, value):
            if depth == 0 and changed_only and not (set(cols) & changed):
                continue
            found.append(Location(hops, cols, rule))
        if depth >= max_hops:
            return
        for col in t.columns:
            v = r.get(col.name)
            if v is None or (col.name in t.primary_key and hops):
                continue
            if depth == 0 and changed_only and col.name not in changed:
                continue
            for target, ref, declared in _fk_targets(schema, idx, t, col.name, v):
                target_row = idx.row(target, v)
                if target_row is None or (target == anchor.table and hops):
                    continue  # back to the main record: not a location
                if not declared and not hops and (target, v) in added:
                    # The main row's key columns point at rows that were
                    # there before; a row the write added is reached from a
                    # row that names the main record instead.
                    continue
                walk(
                    schema.tables[target],
                    target_row,
                    hops + (Hop("fk", target, col.name, ref, declared),),
                    depth + 1,
                )
        if depth == 0 and len(t.primary_key) == 1 and not before:
            key = r.get(t.primary_key[0])
            if key is None:
                return
            for ch in demo.diff.changes:
                if ch.kind != "added" or ch.table == t.name or ch.after is None:
                    continue
                child = schema.tables.get(ch.table)
                if child is None:
                    continue
                for col in child.columns:
                    if ch.after.get(col.name) != key:
                        continue
                    if links is not None and col.name not in links.get(
                        ch.table, {col.name}
                    ):
                        continue
                    declared = any(
                        fk.column == col.name and fk.table == t.name
                        for fk in child.foreign_keys
                    )
                    if not declared and not _IDLIKE.search(col.name):
                        continue
                    walk(
                        child,
                        ch.after,
                        hops
                        + (
                            Hop(
                                "child", ch.table, col.name, t.primary_key[0], declared
                            ),
                        ),
                        depth + 1,
                    )

    walk(table, row, (), 0)
    unique: dict[str, Location] = {}
    for loc in found:
        unique.setdefault(loc.signature(), loc)
    return sorted(unique.values(), key=_rank)


def link_columns(
    schema: Schema, demos: Sequence[tuple["Demo", RowChange]]
) -> dict[str, set[str]]:
    """Per table, the columns that hold the main record's key in every row
    that table gained, across all demonstrations (a journal's
    ``journalized_id``, not its ``user_id`` that happens to be equal once)."""

    out: dict[str, set[str]] = {}
    for demo, anchor in demos:
        t = schema.tables.get(anchor.table)
        row = anchor.after or {}
        if t is None or len(t.primary_key) != 1 or row.get(t.primary_key[0]) is None:
            continue
        key = row[t.primary_key[0]]
        for ch in demo.diff.changes:
            if ch.kind != "added" or ch.table == anchor.table or ch.after is None:
                continue
            cols = {c for c, v in ch.after.items() if v == key}
            out[ch.table] = out[ch.table] & cols if ch.table in out else cols
    return out


def best(locations: Sequence[Location]) -> tuple[Optional[Location], list[Location]]:
    """The single best location, or None and the tied ones."""

    if not locations:
        return None, []
    ranked = sorted(locations, key=_rank)
    top = [loc for loc in ranked if _rank(loc) == _rank(ranked[0])]
    if len(top) == 1:
        return top[0], []
    return None, top


def pick_anchor(
    schema: Schema, demo: Demo, params: Sequence[str]
) -> tuple[Optional[RowChange], str, int]:
    """The main row of a demonstration: the added row (a create) or changed
    row (an update) that holds the most parameter values. ``mode`` is
    ``create`` or ``update``; ``score`` how many values it holds."""

    best_row: Optional[RowChange] = None
    best_mode, best_score, best_direct = "", 0, 0
    for ch in demo.diff.changes:
        if ch.kind == "removed" or ch.table not in schema.tables:
            continue
        mode = "create" if ch.kind == "added" else "update"
        score = direct = 0
        for p in params:
            v = demo.values.get(p)
            if v is None or isinstance(v, (list, dict)):
                continue
            if mode == "update":
                hit = locate(schema, demo, ch, v, before=True, max_hops=1) or locate(
                    schema, demo, ch, v, changed_only=True, max_hops=1
                )
            else:
                hit = locate(schema, demo, ch, v, max_hops=1)
            # Only the row's own columns, and keys named after the table they
            # point to: a row the write added beside the main record (an
            # issue's journal) reaches the record's values only through a
            # link back, and a key equal to some row's id by chance proves
            # nothing.
            hit = [
                loc
                for loc in hit
                if all(h.kind == "fk" and _hop_cost(h) <= 1 for h in loc.hops)
            ]
            score += 1 if hit else 0
            direct += 1 if any(not loc.hops for loc in hit) else 0
        if (score, direct) > (best_score, best_direct):
            best_row, best_mode, best_score, best_direct = ch, mode, score, direct
    return best_row, best_mode, best_score


def is_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENT.match(value))


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def looks_like_log(table: str) -> bool:
    return bool(_LOG_NAME.search(table))


@dataclass
class ColumnRole:
    """What a column of a new row is, when no parameter explains it."""

    role: str  # key | timestamp | actor | link | copied | constant | varies | default
    value: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)


def _within(stamp: Optional[str], start: str, end: str) -> bool:
    if not stamp or not start or not end:
        return False
    when = _datetime(stamp.split(".")[0].split("+")[0])
    if when is None:
        try:
            when = _dt.datetime.strptime(stamp[:10], "%Y-%m-%d")
        except ValueError:
            return False
    lo = _dt.datetime.fromisoformat(start.replace("Z", "")) - _dt.timedelta(days=1)
    hi = _dt.datetime.fromisoformat(end.replace("Z", "")) + _dt.timedelta(days=1)
    return lo <= when <= hi


def column_roles(
    schema: Schema,
    table: str,
    rows: Sequence[tuple[Demo, Mapping[str, Optional[str]]]],
    explained: set[str],
    actor_values: Mapping[str, str],
    link_values: Sequence[set[str]] = (),
) -> dict[str, ColumnRole]:
    """Roles of the columns of new rows of ``table`` that no parameter
    explains, across every demonstration row given (one row per demo or
    several). ``link_values``: per row, keys of rows the same write added
    (a column holding one is a link)."""

    t = schema.tables[table]
    out: dict[str, ColumnRole] = {}
    for col in t.columns:
        name = col.name
        if name in explained:
            continue
        values = [r.get(name) for _, r in rows]
        if col.auto or (name in t.primary_key and len(t.primary_key) == 1):
            out[name] = ColumnRole("key")
            continue
        if (
            col.kind in ("date", "datetime")
            and all(
                v is None or _within(v, d.started_at, d.finished_at)
                for (d, _), v in zip(rows, values)
            )
            and any(v is not None for v in values)
        ):
            out[name] = ColumnRole("timestamp")
            continue
        actor = [
            p
            for p, av in actor_values.items()
            if values and all(v is not None and v == av for v in values)
        ]
        if actor:
            out[name] = ColumnRole("actor", detail={"param": actor[0]})
            continue
        links = link_values or [set() for _ in rows]
        if values and all(
            v is not None
            and (v in lv or (len(t.primary_key) == 1 and v == r.get(t.primary_key[0])))
            for v, lv, (_, r) in zip(values, links, rows)
        ):
            out[name] = ColumnRole("link")
            continue
        if (
            len(t.primary_key) == 1
            and values
            and all(
                v is not None
                and r.get(t.primary_key[0]) is not None
                and str(r.get(t.primary_key[0])) in v
                and v != r.get(t.primary_key[0])
                for v, (_, r) in zip(values, rows)
            )
        ):
            out[name] = ColumnRole("varies", detail={"why": "holds the row's own key"})
            continue
        part = _date_part(values, [d for d, _ in rows])
        if part is not None:
            out[name] = ColumnRole(
                "varies", detail={"why": f"the {part} of a date the task gives"}
            )
            continue
        copied = _copied(schema, t, name, rows)
        if copied is not None:
            out[name] = ColumnRole("copied", detail=copied)
            continue
        distinct = {v for v in values}
        if col.kind == "text" and distinct <= {None, ""}:
            # Free text the task doesn't give and the demonstrations left
            # empty (a line's description, a note): an agent may fill it
            # from the record it copies, so it isn't checked.
            out[name] = ColumnRole("free")
            continue
        if len(distinct) == 1:
            v = values[0]
            default = col.default
            if v is None:
                out[name] = ColumnRole("constant", None)
            else:
                out[name] = ColumnRole(
                    "constant",
                    v,
                    detail={
                        "is_default": default is not None and default.strip("'") == v
                    },
                )
            continue
        out[name] = ColumnRole("varies")
    return out


def _date_part(
    values: Sequence[Optional[str]], demos: Sequence["Demo"]
) -> Optional[str]:
    """Is the column the year, month, ISO week or day of one date value the
    task gives, in every row? (Redmine stores a time entry's ``tyear``,
    ``tmonth`` and ``tweek``.) The app computes it, so it isn't pinned."""

    parts = {
        "year": lambda d: d.year,
        "month": lambda d: d.month,
        "week": lambda d: d.isocalendar()[1],
        "day": lambda d: d.day,
    }
    for label, fn in parts.items():
        ok = bool(values)
        for v, demo in zip(values, demos):
            dates = []
            for x in demo.values.values():
                if isinstance(x, str) and re.match(r"^\d{4}-\d{2}-\d{2}", x):
                    try:
                        dates.append(_dt.date.fromisoformat(x[:10]))
                    except ValueError:
                        pass
            if v is None or not any(str(fn(dd)) == v for dd in dates):
                ok = False
                break
        if ok:
            return label
    return None


def _copied(
    schema: Schema,
    t: Table,
    name: str,
    rows: Sequence[tuple[Demo, Mapping[str, Optional[str]]]],
) -> Optional[dict[str, Any]]:
    """Is the column a copy of one column of the row an FK points to, in
    every row? (An invoice line's unit price is its product's price.)"""

    col = t.column(name)
    if col is None or col.kind not in ("number", "text") or len(rows) < 2:
        # One row can't tell a copy from a coincidence (a user id equal to a
        # project id).
        return None
    candidates: Optional[set[tuple[str, str, str, str]]] = None
    for demo, r in rows:
        v = r.get(name)
        if v is None:
            return None
        idx = _Index(schema, demo.after)
        here: set[tuple[str, str, str, str]] = set()
        for other in t.columns:
            if (
                other.name == name
                or other.name in t.primary_key
                or r.get(other.name) is None
            ):
                continue
            for target, ref, _declared in _fk_targets(
                schema, idx, t, other.name, str(r.get(other.name))
            ):
                trow = idx.row(target, str(r.get(other.name)))
                if trow is None:
                    continue
                for tc in schema.tables[target].columns:
                    if tc.name == ref:
                        continue
                    if rule_for(trow.get(tc.name), tc.kind, v) in ("exact", "number"):
                        here.add((other.name, target, ref, tc.name))
        candidates = here if candidates is None else candidates & here
        if not candidates:
            return None
    if candidates and len(candidates) > 1:
        # Several columns of the row it points to hold the same value: keep
        # the one named like this column (``tva_tx`` from ``tva_tx``, then
        # ``subprice`` from ``price``), if exactly one is.
        def stem(col: str) -> str:
            return re.sub(r"(^fk_|_id$|_fk$|^id_)", "", col.lower())

        for test in (
            lambda c: c[3] == name,
            lambda c: c[3] in name,
            lambda c: c[3] in name and c[1].lower().endswith(stem(c[0])),
        ):
            named = {c for c in candidates if test(c)}
            if len(named) == 1:
                candidates = named
                break
    if candidates and len(candidates) == 1:
        via, target, ref, source = next(iter(candidates))
        return {"via": via, "table": target, "ref_column": ref, "column": source}
    return None
