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
from typing import Any, Literal, Mapping, Optional, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
        try:
            # The verifier's own whole-database read (openadapt_flow.verifier,
            # #1588), when this build has it; the same take_snapshot otherwise.
            from openadapt_flow.verifier import (  # type: ignore[import-not-found,unused-ignore]
                capture_database,
            )
        except ImportError:
            capture_database = None
        if capture_database is not None:
            try:
                snapshot: Snapshot = capture_database(
                    self.read_config,
                    self.dialect,
                    allow_tables=list(self.allow_tables),
                    max_rows=self.max_rows,
                )
                return snapshot
            except Exception as exc:  # noqa: BLE001 - BaselineUnreadable and kin
                raise StateUnreadable(
                    f"the database could not be read ({type(exc).__name__})"
                ) from exc
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

    def verifier_diff(
        self, before: Snapshot, after: Snapshot
    ) -> Optional[dict[str, Any]]:
        """The verifier's own ``diff_database`` of two reads (table -> counts),
        or None when this build doesn't have it."""
        try:
            from openadapt_flow.verifier import (  # type: ignore[import-not-found,unused-ignore]
                diff_database,
            )
        except ImportError:
            return None
        moved = diff_database(before, after, allow_tables=list(self.allow_tables))
        return {str(k): v for k, v in dict(moved).items()}


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


# ---------------------------------------------------------------------------
# An app's own bookkeeping: the shared per-app frame-exclusion profile
# ---------------------------------------------------------------------------


class TableRule(BaseModel):
    """How one table of an app's own bookkeeping may move.

    ``append_only``: rows may be added; an existing row that changes or goes
    is not excused. ``session``: per-user or per-session state; any change.
    ``columns``: existing rows may change only in ``columns``; no row may be
    added or removed.
    """

    model_config = ConfigDict(extra="forbid")

    rule: Literal["append_only", "session", "columns"]
    columns: list[str] = Field(default_factory=list)


class AppProfile(BaseModel):
    """The versioned list of what an app writes on its own (one per app).

    The same file serves the verifier's frame, rollback and the RL grader.
    No rule may name a patient-data table: ``patient_table_prefixes`` lists
    them, and loading refuses a profile that names one.
    """

    model_config = ConfigDict(extra="forbid")

    #: For example ``openemr-frame-exclusions/2026-10-28.1``.
    id: str = Field(min_length=1)
    tables: dict[str, TableRule]
    patient_table_prefixes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_patient_tables(self) -> "AppProfile":
        prefixes = tuple(self.patient_table_prefixes)
        bad = sorted(t for t in self.tables if prefixes and t.startswith(prefixes))
        if bad:
            raise ValueError(
                "an app profile can't excuse a patient-data table: " + ", ".join(bad)
            )
        for name, rule in self.tables.items():
            if rule.rule == "columns" and not rule.columns:
                raise ValueError(f"{name}: a columns rule names its columns")
        return self

    @classmethod
    def load(cls, path: Any) -> "AppProfile":
        import yaml

        raw = yaml.safe_load(open(path, encoding="utf-8").read())
        return cls.model_validate(raw)

    def excuses(self, change: "Change") -> bool:
        """Is this changed row the app's own bookkeeping, by its rule?"""
        table, key, before, after = change
        rule = self.tables.get(table)
        if rule is None or key == UNREAD_KEY:
            return False
        if rule.rule == "session":
            return True
        if rule.rule == "append_only":
            return before is None and after is not None
        if before is None or after is None:
            return False
        moved = {
            c
            for c in (set(before) | set(after)) - {KEY}
            if not _same(before.get(c), after.get(c))
        }
        return moved <= set(rule.columns)


def keep_rows(diff: StateDiff, rows: Sequence[Any]) -> StateDiff:
    """``diff`` limited to the rows in ``rows`` (``Change`` tuples)."""
    wanted = {(t, k) for t, k, *_rest in rows}
    out = StateDiff(ignored_columns=dict(diff.ignored_columns))
    for t in diff.tables:
        kept = TableDiff(
            table=t.table,
            unread=t.unread if (t.table, UNREAD_KEY) in wanted else None,
        )
        kept.added = [r for r in t.added if (t.table, str(r[KEY])) in wanted]
        kept.removed = [r for r in t.removed if (t.table, str(r[KEY])) in wanted]
        kept.changed = [c for c in t.changed if (t.table, c[0]) in wanted]
        out.tables.append(kept)
    return out


# ---------------------------------------------------------------------------
# Who changed a row: the frame's attribution, applied to a rollback
# ---------------------------------------------------------------------------

#: The row key of a table whose rows couldn't be compared.
UNREAD_KEY = "*unread*"

#: ``(table, row key, row before, row after)``; a side is None when the row
#: was added or removed.
Change = tuple[str, str, Optional[dict[str, Any]], Optional[dict[str, Any]]]


def changes(diff: StateDiff) -> list[Change]:
    """Every row that moved, one entry per row."""
    out: list[Change] = []
    for t in diff.tables:
        if t.unread:
            # Rows can't be compared (fingerprinted, or the table changed
            # shape): one change nothing can attribute.
            out.append((t.table, UNREAD_KEY, None, None))
        out += [(t.table, str(r[KEY]), None, r) for r in t.added]
        out += [(t.table, str(r[KEY]), r, None) for r in t.removed]
        out += [(t.table, key, before, after) for key, before, after, _c in t.changed]
    return out


class Attributor:
    """Says who changed a row, with ``frame.attribution`` settings.

    The same two sources ``verify-run``'s frame uses: ``audit_read`` (the
    app's audit log, read before the run and again later; the entries in
    between are the window) or ``record_field`` (each record carries its last
    modifier, a field only the server sets). A row nothing attributes is
    ``unattributed`` and counts as the run's: a rollback never sets aside a
    change it can't explain.
    """

    def __init__(
        self,
        spec: Any,
        *,
        self_actors: set[str],
        audit_read: Optional[Any] = None,
    ) -> None:
        self.spec = spec
        self.self_actors = {str(a) for a in self_actors}
        self.audit_read = audit_read
        self._before: Optional[list[dict[str, Any]]] = None

    @classmethod
    def from_spec(
        cls, spec: Any, params: Mapping[str, Any], audit_read: Optional[Any] = None
    ) -> Optional["Attributor"]:
        from openadapt_flow.runtime.effects.frame import _resolve_self

        actors = _resolve_self(spec, params)
        if actors is None:
            return None
        return cls(spec, self_actors=actors, audit_read=audit_read)

    def start(self) -> None:
        """Read the audit log before the run (``audit_read`` only)."""
        if self.spec.kind == "audit_read" and self.audit_read is not None:
            self._before = self.audit_read()

    def window(self) -> Optional[list[dict[str, Any]]]:
        """Audit entries since :meth:`start`; None when either read failed."""
        if self.spec.kind != "audit_read" or self.audit_read is None:
            return None
        if self._before is None:
            return None
        after = self.audit_read()
        if after is None:
            return None
        from openadapt_flow.runtime.effects.effect import stable_id

        seen = {stable_id(e) for e in self._before}
        return [e for e in after if stable_id(e) not in seen]

    def _ref(
        self,
        table: str,
        key: str,
        before: Optional[dict[str, Any]],
        after: Optional[dict[str, Any]],
    ) -> Optional[str]:
        field = self.spec.record_keys.get(table, self.spec.record_key)
        if not field:
            return key
        record = after if after is not None else before
        value = None if record is None else record.get(field)
        return None if value in (None, "") else str(value)

    def _actors(
        self, ref: Optional[str], window: Optional[list[dict[str, Any]]]
    ) -> set[str]:
        from openadapt_flow.runtime.effects.frame import get_path

        if ref is None or window is None:
            return set()
        return {
            str(get_path(e, self.spec.actor_field))
            for e in window
            if str(get_path(e, self.spec.record_field or "")) == ref
            and get_path(e, self.spec.actor_field) not in (None, "")
        }

    def attribute(self, change: Change, window: Optional[list[dict[str, Any]]]) -> str:
        """``self``, ``other`` or ``unattributed`` for one changed row."""
        from openadapt_flow.runtime.effects.frame import _same, get_path

        table, key, before, after = change
        if self.spec.kind == "audit_read":
            actors = self._actors(self._ref(table, key, before, after), window)
            if actors & self.self_actors:
                return "self"
            return "other" if actors else "unattributed"
        if after is None:
            return "unattributed"
        actor = get_path(after, self.spec.actor_field)
        if actor in (None, ""):
            return "unattributed"
        if str(actor) in self.self_actors:
            return "self"
        if self.spec.version_field:
            new = get_path(after, self.spec.version_field)
            old = None if before is None else get_path(before, self.spec.version_field)
            if new in (None, "") or (before is not None and _same(old, new)):
                return "unattributed"
        elif before is not None and _same(
            get_path(before, self.spec.actor_field), actor
        ):
            return "unattributed"
        return "other"

    def others_touched(
        self, change: Change, window: Optional[list[dict[str, Any]]]
    ) -> bool:
        """Did another actor also write this row in the window?"""
        from openadapt_flow.runtime.effects.frame import get_path

        table, key, before, after = change
        if self.spec.kind == "audit_read":
            return bool(
                self._actors(self._ref(table, key, before, after), window)
                - self.self_actors
            )
        if after is None:
            return False
        actor = get_path(after, self.spec.actor_field)
        return actor not in (None, "") and str(actor) not in self.self_actors


__all__ = [
    "AppProfile",
    "TableRule",
    "keep_rows",
    "Attributor",
    "Change",
    "changes",
    "DatabaseReader",
    "StateDiff",
    "StateReader",
    "StateUnreadable",
    "TableDiff",
    "VerifierReader",
    "diff_states",
    "records_snapshot",
]
