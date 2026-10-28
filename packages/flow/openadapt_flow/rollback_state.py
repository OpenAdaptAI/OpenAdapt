"""The whole-store read a rollback is judged by, and the diff between two.

A compensation is done only when the system of record is back to what it was
before the run: every table, every row, every column, apart from the tables
the app writes on its own (a reviewed ``allow_tables`` list). This module
reads that state and compares two reads. ``rollback.py`` uses it at a halt,
and ``rollback qualify`` uses it on a test copy.

Two readers:

- :class:`DatabaseReader` reads every table of a database through
  ``frame.database`` settings (:mod:`runtime.effects.db_snapshot`).
- :class:`VerifierReader` reads one collection through an effect verifier
  (``capture_pre_state``), for an app reached only over REST or FHIR. It sees
  only that collection, and the report says so.

The diff's :meth:`StateDiff.summary` holds table names, counts and column
names, never values, so it can go into ``report.json``. The full rows a
person needs to repair a record are in :meth:`StateDiff.to_private_json`,
which callers write owner-only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from openadapt_flow.runtime.effects.db_snapshot import (
    KEY,
    Snapshot,
    Table,
    keyed_rows,
)


class StateUnreadable(RuntimeError):
    """The store could not be read. Nothing is inferred from a failed read."""


class StateReader(Protocol):
    """Reads the whole system of record the rollback is judged by."""

    #: ``database`` (every table) or ``collection`` (one verifier read).
    scope: str

    def read(self) -> Snapshot:
        """Every table and row. Raises :class:`StateUnreadable` on failure."""
        ...


@dataclass
class DatabaseReader:
    """Every table of a database, from ``frame.database`` settings."""

    dialect: str
    read_config: Mapping[str, Any]
    allow_tables: Sequence[str] = ()
    max_rows: int = 20000
    scope: str = "database"

    @classmethod
    def from_frame_database(cls, database: Any) -> "DatabaseReader":
        return cls(
            dialect=str(database.dialect),
            read_config=dict(database.read),
            allow_tables=tuple(database.allow_tables),
            max_rows=int(database.max_rows),
        )

    def read(self) -> Snapshot:
        from openadapt_flow.runtime.effects.db_snapshot import take_snapshot
        from openadapt_flow.verify_run import _db_connect

        try:
            return take_snapshot(
                _db_connect(self.read_config),
                self.dialect,  # type: ignore[arg-type]
                skip=list(self.allow_tables),
                max_rows=self.max_rows,
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable store decides nothing
            raise StateUnreadable(
                f"the database could not be read ({type(exc).__name__})"
            ) from exc


@dataclass
class VerifierReader:
    """One collection, read through an effect verifier's own read path."""

    verifier: Any
    table: str = "records"
    scope: str = "collection"

    def read(self) -> Snapshot:
        try:
            state = self.verifier.capture_pre_state()
        except Exception as exc:  # noqa: BLE001
            raise StateUnreadable(
                f"the system of record could not be read ({type(exc).__name__})"
            ) from exc
        if getattr(state, "reachable", False) is not True:
            raise StateUnreadable("the system of record could not be read")
        return records_snapshot([dict(r) for r in state.records], table=self.table)


def records_snapshot(
    records: list[dict[str, Any]], *, table: str = "records"
) -> Snapshot:
    """A one-table snapshot of plain records.

    A collection has no fixed schema (an empty read names no columns), so
    its columns are left empty and rows are compared field by field. Rows
    are keyed by ``id`` when every record has a distinct one.
    """
    ids = [r.get("id") for r in records]
    keyed = all(i is not None for i in ids) and len(set(map(str, ids))) == len(ids)
    return Snapshot(
        tables={
            table: Table(
                columns=[],
                primary_key=["id"] if keyed else [],
                defaults={},
                count=len(records),
                rows=list(records),
            )
        }
    )


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


@dataclass
class TableDiff:
    """What moved in one table between two reads."""

    table: str
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    #: ``(key, before row, after row, changed columns)``.
    changed: list[tuple[str, dict[str, Any], dict[str, Any], list[str]]] = field(
        default_factory=list
    )
    #: Why rows couldn't be compared: the table appeared, disappeared or
    #: changed shape, or it is fingerprinted and its fingerprint moved.
    unread: Optional[str] = None

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.unread)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "added": len(self.added),
            "removed": len(self.removed),
            "changed": len(self.changed),
        }
        columns = sorted({c for *_rest, cols in self.changed for c in cols})
        if columns:
            out["changed_columns"] = columns
        if self.unread:
            out["unread"] = self.unread
        return out


@dataclass
class StateDiff:
    """Every table that differs between two reads."""

    tables: list[TableDiff] = field(default_factory=list)
    ignored_columns: dict[str, list[str]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return all(t.empty for t in self.tables)

    def summary(self) -> dict[str, dict[str, Any]]:
        """Per table: counts and column names. Never a value."""
        return {t.table: t.summary() for t in self.tables if not t.empty}

    def keys(self) -> set[tuple[str, str]]:
        """``(table, row key)`` of every row that moved."""
        out: set[tuple[str, str]] = set()
        for t in self.tables:
            out |= {(t.table, str(r[KEY])) for r in t.added}
            out |= {(t.table, str(r[KEY])) for r in t.removed}
            out |= {(t.table, key) for key, *_rest in t.changed}
        return out

    def describe(self) -> str:
        """One line a person reads; counts and column names only."""
        parts = []
        for name, s in self.summary().items():
            bits = []
            if s.get("unread"):
                bits.append(s["unread"])
            for word in ("added", "removed", "changed"):
                if s[word]:
                    bits.append(f"{s[word]} {word}")
            if s.get("changed_columns"):
                bits.append("columns " + ", ".join(s["changed_columns"]))
            parts.append(f"{name}: " + ", ".join(bits))
        return "; ".join(parts) if parts else "no difference"

    def to_private_json(self) -> dict[str, Any]:
        """The full rows, for the person who repairs them. Write owner-only."""
        return {
            "schema": "openadapt.rollback-diff/v1",
            "tables": {
                t.table: {
                    "added": [_plain(r) for r in t.added],
                    "removed": [_plain(r) for r in t.removed],
                    "changed": [
                        {
                            "key": key,
                            "columns": cols,
                            "before": {c: before.get(c) for c in cols},
                            "after": {c: after.get(c) for c in cols},
                        }
                        for key, before, after, cols in t.changed
                    ],
                    **({"unread": t.unread} if t.unread else {}),
                }
                for t in self.tables
                if not t.empty
            },
        }


def _plain(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != KEY}


def _same(a: Any, b: Any) -> bool:
    if a == b:
        return True
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str
    )


def diff_states(
    before: Snapshot,
    after: Snapshot,
    *,
    ignore_columns: Optional[Mapping[str, Sequence[str]]] = None,
) -> StateDiff:
    """Compare two reads table by table, row by row, column by column.

    ``ignore_columns`` maps a table (or ``"*"`` for every table) to columns
    that change on their own (a last-modified time). Ignoring a column is a
    person's reviewed decision; it is recorded in the diff.
    """
    ignore = {k: set(v) for k, v in (ignore_columns or {}).items()}
    out = StateDiff(ignored_columns={k: sorted(v) for k, v in ignore.items()})
    for name in sorted(set(before.tables) | set(after.tables)):
        skip = ignore.get(name, set()) | ignore.get("*", set())
        a, b = before.tables.get(name), after.tables.get(name)
        diff = TableDiff(table=name)
        if a is None or b is None:
            diff.unread = "table appeared" if a is None else "table disappeared"
        elif a.columns != b.columns:
            diff.unread = "table changed shape"
        elif a.rows is None or b.rows is None:
            if (a.count, a.digest) != (b.count, b.digest):
                diff.unread = "fingerprinted table moved"
        else:
            old = {str(r[KEY]): r for r in keyed_rows(a)}
            new = {str(r[KEY]): r for r in keyed_rows(b)}
            diff.added = [new[k] for k in new if k not in old]
            diff.removed = [old[k] for k in old if k not in new]
            for key in sorted(set(old) & set(new)):
                cols = sorted(
                    c
                    for c in (set(old[key]) | set(new[key])) - skip - {KEY}
                    if not _same(old[key].get(c), new[key].get(c))
                )
                if cols:
                    diff.changed.append((key, old[key], new[key], cols))
        out.tables.append(diff)
    return out


__all__ = [
    "DatabaseReader",
    "StateDiff",
    "StateReader",
    "StateUnreadable",
    "TableDiff",
    "VerifierReader",
    "diff_states",
    "records_snapshot",
]
