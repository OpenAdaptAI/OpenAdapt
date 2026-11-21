"""Row versions held from the baseline: count who saved the target.

Two snapshots can't see a lost update (``versions.py``). A "last saved"
marker on the row, such as PostgreSQL's ``xmin`` or an ``updated_at``
column, can't either: after another user's save and the agent's stale save,
it reads the same as after the agent's save alone. A marker only proves that
nobody saved.

PostgreSQL keeps more than the marker. A read-only REPEATABLE READ
transaction opened at the baseline keeps seeing the row version that was
current then, and that old version's ``xmax`` names the transaction that
replaced it. The current version's ``xmin`` names the transaction that wrote
it. So, per target row:

- ``xmin`` unchanged: no transaction wrote the row in the window (0).
- the old ``xmax`` equals the current ``xmin``: one transaction replaced the
  version the baseline saw, and nothing replaced it since (1).
- anything else: two or more transactions wrote the row (2+). A lock taken
  while the row was being updated also lands here; that is a stop, never a
  pass.

``frame.row_versions`` names the target's table and key column. The
verifier opens the held read before it reads the baseline records, keeps it
in this process until the judge, and closes it there. A judge in another
process, a read that fails, or a target the held read can't find stops the
run as not checked. Never a pass.

A target that existed may have been written by at most one transaction, and
one that changed by exactly one: two writers mean another user's save sits
inside the window. That is the lost-update rule of ``versions.py`` (one step
per save) with the row's own transaction ids as the steps.

Known limit: a lock shared by two transactions sets ``xmax`` to a multixact
id, a separate counter. If that number happened to equal the later writer's
transaction id, two writers would read as one. Nothing here can tell the two
counters apart without the ``pageinspect`` extension.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from openadapt_flow.runtime.effects.versions import VersionVerdict

#: Where the baseline says whether the held read was opened (no values).
DETAIL = "openadapt:frame_row_versions"

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class FrameRowVersions(BaseModel):
    """``frame.row_versions``: the target table whose row versions are held."""

    model_config = ConfigDict(extra="forbid")

    #: Only PostgreSQL keeps the replaced version visible to an old snapshot.
    source: str = Field(default="postgresql_xmin", pattern="^postgresql_xmin$")
    #: The target's table, ``table`` or ``schema.table`` (plain identifiers).
    table: str = Field(min_length=1)
    #: The table's key column, whose value is the target record's
    #: ``target_field``.
    key_column: str = Field(default="id", min_length=1)
    #: The target record's field holding the key. Unset:
    #: ``frame.identity_field``, else ``id``.
    target_field: Optional[str] = None
    #: Only records matched on this surface are targets. Unset: every record
    #: a confirmed write effect matched.
    target_surface: Optional[str] = None

    @model_serializer(mode="wrap")
    def _compact(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in ("target_field", "target_surface"):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    def names(self) -> tuple[Optional[str], str, str]:
        """(schema or None, table, key column), each a plain identifier."""

        parts = self.table.split(".")
        if len(parts) > 2 or not all(_NAME.match(p) for p in parts):
            raise ValueError(
                f"frame.row_versions.table {self.table!r} is not a plain "
                "table or schema.table name"
            )
        if not _NAME.match(self.key_column):
            raise ValueError(
                f"frame.row_versions.key_column {self.key_column!r} is not a "
                "plain column name"
            )
        schema = parts[0] if len(parts) == 2 else None
        return schema, parts[-1], self.key_column


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _query(spec: FrameRowVersions) -> str:
    schema, table, key = spec.names()
    relation = (_quote(schema) + "." if schema else "") + _quote(table)
    return (
        f"SELECT CAST({_quote(key)} AS TEXT), CAST(xmin AS TEXT), "
        f"CAST(xmax AS TEXT) FROM {relation} "
        f"WHERE CAST({_quote(key)} AS TEXT) = ANY(%s)"
    )


def _relkind_query(spec: FrameRowVersions) -> tuple[str, tuple[str, ...]]:
    schema, table, _key = spec.names()
    if schema is None:
        return (
            "SELECT c.relkind FROM pg_class c WHERE c.oid = to_regclass(%s)",
            (_quote(table),),
        )
    return (
        "SELECT c.relkind FROM pg_class c WHERE c.oid = to_regclass(%s)",
        (_quote(schema) + "." + _quote(table),),
    )


@dataclass
class HeldRead:
    """An open read-only REPEATABLE READ transaction from the baseline."""

    conn: Any
    opened: float

    def rows(self, spec: FrameRowVersions, keys: Sequence[str]) -> dict[str, tuple]:
        cur = self.conn.cursor()
        cur.execute(_query(spec), (list(keys),))
        return {str(k): (x, m) for k, x, m in cur.fetchall()}

    def close(self) -> None:
        try:
            self.conn.cursor().execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - closing changes nothing
            pass
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


_LOCK = threading.Lock()
_HELD: dict[str, HeldRead] = {}
#: A held read older than this is closed when the next one opens (seconds).
MAX_HOLD_S = 3600.0


def open_held(spec: FrameRowVersions, connect: Callable[[], Any]) -> HeldRead:
    """Open the held read and take its snapshot now. Raises on any failure
    (the caller records the baseline as not held)."""

    conn = connect()
    try:
        if type(conn).__module__.split(".")[0] not in ("psycopg2", "psycopg"):
            raise ValueError("frame.row_versions reads PostgreSQL only")
        # Autocommit off would start its own transaction at the first
        # statement, at the default isolation; begin one explicitly instead.
        conn.autocommit = True
        cur = conn.cursor()
        # The server ends a hold the judge never closes.
        cur.execute(
            "SET idle_in_transaction_session_timeout = %s", (f"{int(MAX_HOLD_S)}s",)
        )
        cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute(
            "SELECT current_setting('transaction_isolation'), "
            "current_setting('transaction_read_only')"
        )
        if tuple(cur.fetchone() or ()) != ("repeatable read", "on"):
            raise ValueError("the held read is not a read-only snapshot")
        sql, args = _relkind_query(spec)
        cur.execute(sql, args)
        kind = cur.fetchone()
        # An ordinary or partitioned table keeps row versions; a view doesn't.
        if kind is None or str(kind[0]) not in ("r", "p"):
            raise ValueError("frame.row_versions.table is not a table")
    except Exception:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    return HeldRead(conn, time.monotonic())


def hold(token: str, held: HeldRead) -> None:
    now = time.monotonic()
    with _LOCK:
        for key in [k for k, h in _HELD.items() if now - h.opened > MAX_HOLD_S]:
            _HELD.pop(key).close()
        old = _HELD.pop(token, None)
        _HELD[token] = held
    if old is not None:
        old.close()


def take(token: str) -> Optional[HeldRead]:
    with _LOCK:
        return _HELD.pop(token, None)


def judge_row_versions(
    spec: FrameRowVersions,
    held: Optional[HeldRead],
    connect: Callable[[], Any],
    targets: Sequence[tuple[Optional[str], bool, bool]],
) -> VersionVerdict:
    """Count the writers of every target that existed. ``targets``: per
    record a confirmed write effect matched, its key (None when it has
    none), whether it existed before the write, and whether it changed."""

    if held is None:
        return VersionVerdict(
            "not_checked",
            "unreadable",
            reason="the row versions held from the baseline are not open in "
            "this process, so who saved the target can't be told",
        )
    try:
        keys = sorted({t for t, existed, _c in targets if existed and t is not None})
        old = held.rows(spec, keys) if keys else {}
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(_query(spec), (keys,))
            now = {str(k): (x, m) for k, x, m in cur.fetchall()}
            conn.rollback()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - unreadable -> the check stops the run
        return VersionVerdict(
            "not_checked",
            "unreadable",
            reason="the target's row versions could not be read",
        )
    finally:
        held.close()
    out = VersionVerdict("held")
    worst: Optional[VersionVerdict] = None

    def stop(status: str, problem: str, reason: str) -> None:
        nonlocal worst
        if worst is None or (worst.status == "not_checked" and status == "refuted"):
            worst = VersionVerdict(status, problem, reason=reason)  # type: ignore[arg-type]

    seen: set[Optional[str]] = set()
    for target, existed, changed in targets:
        if target in seen or not existed:
            continue  # created in the window: no earlier version to overwrite
        seen.add(target)
        if target is None:
            stop(
                "not_checked",
                "no_target_id",
                "a target record has no key field, so its row can't be found",
            )
            continue
        out.targets += 1
        base, cur = old.get(target), now.get(target)
        if base is None or cur is None:
            stop(
                "not_checked",
                "missing",
                "the target existed but its row isn't in the table "
                "frame.row_versions names, before or after the write",
            )
            continue
        (base_xmin, base_xmax), (cur_xmin, _cur_xmax) = base, cur
        if cur_xmin == base_xmin:
            writers = 0
        elif base_xmax not in (None, "0") and base_xmax == cur_xmin:
            writers = 1
        else:
            writers = 2
        out.new_versions += min(writers, 2)
        if writers >= 2:
            stop(
                "refuted",
                "extra_steps",
                "lost update: two or more transactions wrote the target inside "
                "the window; one save is the agent's, and nothing says whose "
                "the others are",
            )
        elif changed and writers == 0:
            stop(
                "not_checked",
                "no_new_version",
                "the target changed in the window but its row version didn't, "
                "so who saved it can't be told",
            )
    if worst is not None:
        worst.targets, worst.new_versions = out.targets, out.new_versions
        return worst
    out.reason = f"{out.targets} target(s): at most one save each in the window"
    return out


def baseline_detail(held: Optional[HeldRead], error: Optional[str]) -> dict[str, Any]:
    return {"held": held is not None, "error": error}


def stored_held(detail: Optional[Mapping[str, Any]]) -> bool:
    return isinstance(detail, Mapping) and detail.get("held") is True
