"""The whole-store read a rollback is judged by, and the diff between two.

A compensation is done only when the run's changes are gone and nothing else
moved: every table, every row, apart from the bookkeeping the app's versioned
profile names (``openadapt_flow.app_profiles``, shared with the verifier's
frame and the RL grader). This module reads that state and compares two
reads. ``rollback.py`` uses it at a halt; ``rollback qualify`` on a test copy.

Two readers, one snapshot shape (``runtime.effects.db_snapshot``):

- :class:`DatabaseReader` reads every table in one read-only transaction
  (``take_snapshot``). Rows are kept as keyed MACs, never values on disk; a
  read given the earlier read as ``before`` keeps, in memory only, the plain
  values of the rows that differ from it. One MAC key per reader, so every
  read of one rollback compares. The app profile's append-only tables are
  enforced by the snapshot itself; its session tables aren't read; its column
  rules leave those columns out of each row's MAC.
- :class:`VerifierReader` reads one collection through an effect verifier
  (``capture_pre_state``), for an app reached only over REST or FHIR. It sees
  only that collection, and the report says so.

The diff's :meth:`StateDiff.summary` holds table names, counts and column
names, never values, so it can go into ``report.json``.
:meth:`StateDiff.to_private_json` holds the values the reads kept (the rows
that changed after the run started), which callers write owner-only.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from openadapt_flow.runtime.effects.db_snapshot import (
    KEY,
    Snapshot,
    Table,
    mac_key,
    row_hash,
    row_key_hash,
    unpack,
    unread_reasons,
)


class StateUnreadable(RuntimeError):
    """The store could not be read. Nothing is inferred from a failed read."""


class StateReader(Protocol):
    """Reads the whole system of record the rollback is judged by."""

    #: database (every table) or collection (one verifier read).
    scope: str

    def read(self, before: Optional[Snapshot] = None) -> Snapshot:
        """Every table. With before, rows that differ from it carry their
        values (in memory only). Raises :class:."""
        ...


def _new_key() -> bytes:
    return mac_key(secrets.token_bytes(16))


#: The MAC key for snapshots built without one (one per process, as in the
#: verifier's ``capture_database``): reads in one process compare.
_PROCESS_KEY = _new_key()


@dataclass
class DatabaseReader:
    """Every table of a database, from frame.database-style settings.

    profile is the app's bookkeeping profile (a name such as openemr,
    or a loaded AppProfile). allow_tables are kept append-only.
    """

    dialect: str
    read_config: Mapping[str, Any] = field(default_factory=dict)
    allow_tables: Sequence[str] = ()
    max_rows: int = 2000
    profile: Any = None
    connect: Optional[Callable[[], Any]] = None
    key: bytes = field(default_factory=_new_key, repr=False)
    scope: str = "database"

    @classmethod
    def from_frame_database(
        cls, database: Any, *, profile: Any = None
    ) -> "DatabaseReader":
        return cls(
            dialect=str(database.dialect),
            read_config=dict(database.read),
            allow_tables=[
                t if isinstance(t, str) else str(getattr(t, "table", t))
                for t in database.allow_tables
            ],
            max_rows=int(database.max_rows),
            profile=profile,
        )

    def _profile(self) -> Any:
        if self.profile is None or not isinstance(self.profile, str):
            return self.profile
        from openadapt_flow.app_profiles import load_profile

        return load_profile(self.profile)

    @property
    def profile_id(self) -> Optional[str]:
        p = self._profile()
        return None if p is None else f"{p.app}/{p.version}"

    def read(self, before: Optional[Snapshot] = None) -> Snapshot:
        from openadapt_flow.runtime.effects.db_snapshot import take_snapshot

        profile = self._profile()
        kwargs: dict[str, Any] = {}
        if profile is not None:
            kwargs["optional_modes"] = {
                t["table"]: t["mode"] for t in profile.allow_tables()
            }
            kwargs["ignore"] = profile.ignore_columns()
        try:
            if self.connect is not None:
                connect = self.connect
            else:
                from openadapt_flow.verify_run import _db_connect

                connect = _db_connect(self.read_config)
            return take_snapshot(
                connect,
                self.dialect,  # type: ignore[arg-type]
                key=self.key,
                modes=dict.fromkeys(self.allow_tables, "append_only"),
                max_rows=self.max_rows,
                before=before,
                **kwargs,
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
            from openadapt_flow.verifier import diff_database
        except ImportError:
            return None
        moved = diff_database(
            before, after, allow_tables=list(self.allow_tables), key=self.key
        )
        return {str(k): v for k, v in dict(moved).items()}


@dataclass
class VerifierReader:
    """One collection, read through an effect verifier's own read path."""

    verifier: Any
    table: str = "records"
    key: bytes = field(default_factory=_new_key, repr=False)
    scope: str = "collection"

    def read(self, before: Optional[Snapshot] = None) -> Snapshot:
        try:
            state = self.verifier.capture_pre_state()
        except Exception as exc:  # noqa: BLE001
            raise StateUnreadable(
                f"the system of record could not be read ({type(exc).__name__})"
            ) from exc
        if getattr(state, "reachable", False) is not True:
            raise StateUnreadable("the system of record could not be read")
        return records_snapshot(
            [dict(r) for r in state.records], table=self.table, key=self.key
        )


def _schema_digest(columns: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(list(columns)).encode()).hexdigest()


def records_snapshot(
    records: list[dict[str, Any]],
    *,
    table: str = "records",
    key: Optional[bytes] = None,
) -> Snapshot:
    """A one-table snapshot of plain records, in the database snapshot shape.

    A collection has no fixed schema (an empty read names no columns), so its
    definition is left empty and rows are compared field by field. Rows are
    keyed by ``id`` when every record has a distinct one, else by content
    (numbered, so a doubled row counts twice). The records are in memory
    already, so every row keeps its values (never written).
    """
    key = key or _PROCESS_KEY
    ids = [r.get("id") for r in records]
    keyed = all(i is not None for i in ids) and len(set(map(str, ids))) == len(ids)
    rows: dict[str, list[str]] = {}
    values: dict[str, dict[str, Any]] = {}
    seen: dict[str, int] = {}
    for record in records:
        if keyed:
            text = str(record["id"])
        else:
            body = json.dumps(record, sort_keys=True, default=str)
            seen[body] = seen.get(body, 0) + 1
            text = f"{body}#{seen[body]}"
        k = row_key_hash(key, text, table)
        rows[k] = [row_hash(key, table, record), ""]
        values[k] = dict(record)
    return Snapshot(
        dialect="records",
        tables={
            table: Table(
                mode="compare",
                schema="",
                columns=[],
                primary_key=["id"] if keyed else [],
                generated_key=None,
                defaults={},
                count=len(records),
                rows=rows,
                values=values,
            )
        },
    )


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------

#: The row key of a table whose rows couldn't be compared.
UNREAD_KEY = "*unread*"

#: (table, row key MAC, row before, row after). A side is None when the
#: row was added or removed; a row's values are ``{}`` when no read kept
#: them (the read from before the run keeps none).
Change = tuple[str, str, Optional[dict[str, Any]], Optional[dict[str, Any]]]


@dataclass
class TableDiff:
    """What moved in one table between two reads."""

    table: str
    added: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    removed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    #: ``(row key, values before, values after, changed columns)``.
    changed: list[tuple[str, dict[str, Any], dict[str, Any], list[str]]] = field(
        default_factory=list
    )
    #: Why rows couldn't be compared (created, dropped, redefined, an
    #: append-only table whose existing rows moved, ...).
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

    @property
    def empty(self) -> bool:
        return all(t.empty for t in self.tables)

    def summary(self) -> dict[str, dict[str, Any]]:
        """Per table: counts and column names. Never a value."""
        return {t.table: t.summary() for t in self.tables if not t.empty}

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
        """The values the reads kept, for the person who repairs the record.
        Write owner-only. A row a read kept no values for is named by its
        key MAC only."""
        return {
            "schema": "openadapt.rollback-diff/v2",
            "tables": {
                t.table: {
                    "added": [{"key": k, "row": v} for k, v in t.added],
                    "removed": [{"key": k, "row": v} for k, v in t.removed],
                    "changed": [
                        {
                            "key": k,
                            "columns": cols,
                            "before": {c: before.get(c) for c in cols if c in before},
                            "after": {c: after.get(c) for c in cols if c in after},
                        }
                        for k, before, after, cols in t.changed
                    ],
                    **({"unread": t.unread} if t.unread else {}),
                }
                for t in self.tables
                if not t.empty
            },
        }


def _same(a: Any, b: Any) -> bool:
    if a == b:
        return True
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str
    )


def _changed_columns(a: Table, b: Table, k: str) -> list[str]:
    old_v, new_v = a.values.get(k), b.values.get(k)
    if old_v is not None and new_v is not None:
        return sorted(
            c
            for c in set(old_v) | set(new_v)
            if c != KEY and not _same(old_v.get(c), new_v.get(c))
        )
    if a.cell_columns and a.cell_columns == b.cell_columns:
        old_c = unpack(a.cell_columns, a.rows[k][1])
        new_c = unpack(b.cell_columns, b.rows[k][1])
        cols = sorted(c for c in a.cell_columns if old_c.get(c) != new_c.get(c))
        if cols:
            return cols
    return ["(row)"]


def diff_states(before: Snapshot, after: Snapshot) -> StateDiff:
    """Compare two reads table by table, row by row (by key MAC).

    Bookkeeping the app profile names never appears: the reads left it out
    (session tables), kept it append-only (a moved existing row shows as
    unread), or left its columns out of each row's MAC.
    """
    out = StateDiff()
    if before.objects != after.objects:
        out.tables.append(TableDiff(table="(database code)", unread="changed"))
    for name in sorted(set(before.tables) | set(after.tables)):
        a, b = before.tables.get(name), after.tables.get(name)
        diff = TableDiff(table=name)
        reasons = (
            unread_reasons(a, b, after.dialect)
            if after.dialect != "records"
            else ([] if a is not None and b is not None else ["created or dropped"])
        )
        if reasons:
            diff.unread = "; ".join(reasons)
        if a is not None and b is not None and a.mode == "compare" == b.mode:
            diff.added = [
                (k, dict(b.values.get(k, {}))) for k in b.rows if k not in a.rows
            ]
            diff.removed = [
                (k, dict(a.values.get(k, {}))) for k in a.rows if k not in b.rows
            ]
            for k in sorted(set(a.rows) & set(b.rows)):
                if a.rows[k][0] != b.rows[k][0]:
                    diff.changed.append(
                        (
                            k,
                            dict(a.values.get(k, {})),
                            dict(b.values.get(k, {})),
                            _changed_columns(a, b, k),
                        )
                    )
        out.tables.append(diff)
    return out


def changes(diff: StateDiff) -> list[Change]:
    """Every row that moved, one entry per row."""
    out: list[Change] = []
    for t in diff.tables:
        if t.unread:
            # Rows can't be compared: one change nothing can attribute.
            out.append((t.table, UNREAD_KEY, None, None))
        out += [(t.table, k, None, v) for k, v in t.added]
        out += [(t.table, k, v, None) for k, v in t.removed]
        out += [(t.table, k, before, after) for k, before, after, _c in t.changed]
    return out


def keep_rows(diff: StateDiff, rows: Sequence[Any]) -> StateDiff:
    """diff limited to the rows in rows (Change tuples)."""
    wanted = {(t, k) for t, k, *_rest in rows}
    out = StateDiff()
    for t in diff.tables:
        kept = TableDiff(
            table=t.table,
            unread=t.unread if (t.table, UNREAD_KEY) in wanted else None,
        )
        kept.added = [r for r in t.added if (t.table, r[0]) in wanted]
        kept.removed = [r for r in t.removed if (t.table, r[0]) in wanted]
        kept.changed = [c for c in t.changed if (t.table, c[0]) in wanted]
        out.tables.append(kept)
    return out


# ---------------------------------------------------------------------------
# Who changed a row: the frame's attribution, applied to a rollback
# ---------------------------------------------------------------------------


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
    "Attributor",
    "Change",
    "DatabaseReader",
    "StateDiff",
    "StateReader",
    "StateUnreadable",
    "TableDiff",
    "UNREAD_KEY",
    "VerifierReader",
    "changes",
    "diff_states",
    "keep_rows",
    "records_snapshot",
]
