"""Set up the write verifier for an app with no person decisions.

The contract generator (:mod:`openadapt_flow.generate.verify_contract`) asks
a person what it can't decide alone. This module answers those questions
itself, and admits an answer only when a sandbox shows it works:

1. **Sandbox.** A disposable copy of the app's database, beside it, made
   with an admin account the deployer gives once (MySQL and MariaDB: a
   database ``<db>_oa_sandbox``; PostgreSQL: a schema ``oa_sandbox``). Faults
   are planted only there, never in the app's own database.
2. **Answers.** Every answer comes from a fixed rule (below). None names a
   log table or a changed column beyond the generator's own proposal.
3. **Validation.** Held-out demonstrations (correct writes the generator
   never saw) are replayed in the sandbox, each between ``begin_write`` and
   ``verify_write``: every one must be ``CONFIRMED``, at the recheck too.
   Then faults are planted from each held-out write, named as in
   :mod:`openadapt_flow.fault_gate` where the class is the same: a wrong
   value, a wrong or look-alike record, a duplicate, an unrelated edit, a
   row in another table, a missing write, a delayed change, a changed
   bookkeeping cell. Every fault must be stopped.
4. **Admission.** A kind is admitted only when all of its held-out writes
   are confirmed and all of its faults are stopped. Anything else is not
   admitted: its tasks stop (``choose`` finds no contract), and
   ``EXCEPTIONS.md`` lists why. That list is what a person may look at; the
   setup needs no answer from them.

The faults are built from the held-out write's recorded diff, the schema
and the task's values only, never from the contracts under test, so the
planter doesn't share the generator's view of what matters. That is why
this doesn't call :func:`openadapt_flow.fault_gate.run_gate`: the gate plans
its faults from the check's declared fields, on one table slice, while a
whole-database contract must also be shown to catch changes to cells it
doesn't declare.

The rules for answers:

- ``recheck_after_s``: the generator's default, 300 seconds (a policy, not a
  judgment about the app). The sandbox uses a few seconds, so validation
  doesn't wait.
- ``phones``: every parameter the generator lists. A wrong digit is a
  planted fault, so a parameter that isn't a phone number fails validation.
- ``bookkeeping``: the generator's own proposal (``accept: true`` and its
  proposed ``unchecked`` columns), with ``now_checks: true``: an unchecked
  or excused time stamp of the main record must then hold a time near the
  read. Nothing is added to the proposal. A cell left open is caught by the
  bookkeeping faults, and its kind is not admitted.
- ``locations.<kind>``: each candidate place in turn; a candidate is kept
  only when its kind validates, and only when exactly one does.

Nothing here calls a model. Writes go only to the sandbox, through the admin
account; the contracts read through the read-only account, as in production.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import json
import os
import re
import sys
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from openadapt_flow.generate import sql_store as store
from openadapt_flow.generate import verify_contract as gen
from openadapt_flow.generate.demo_analysis import Demo, value_text
from openadapt_flow.generate.sql_store import RowChange, Schema, Table

#: The sandbox's name: a database (MySQL) or a schema (PostgreSQL). Only a
#: store with this name is ever dropped or written.
SANDBOX_SUFFIX = "_oa_sandbox"
PG_SANDBOX_SCHEMA = "oa_sandbox"
#: The recheck window in the sandbox, so validation doesn't wait minutes.
SANDBOX_RECHECK_S = 3
#: A planted bookkeeping time: far from any write's own time.
FAR_TIME = "2001-02-03 04:05:06"


class ZeroTouchError(RuntimeError):
    """The setup can't run as asked (no sandbox, a bad config)."""


class SandboxBroken(ZeroTouchError):
    """The sandbox couldn't be put back after a case: it no longer mirrors
    the app's store, so nothing validated in it can be admitted, and the
    setup stops."""


# -- the sandbox --------------------------------------------------------------------


class Sandbox:
    """A copy of the app's tables, beside the app's own, that faults are
    planted in. ``admin``: a read config (``sql_driver``,
    ``sql_connect_args``, ``sql_password_env``) for an account that can
    create a database or schema and grant on it. ``read``: the read-only
    account's config, which the contracts use."""

    def __init__(self, admin: Mapping[str, Any], read: Mapping[str, Any]) -> None:
        self.admin = dict(admin)
        self.read = dict(read)
        self.dialect = store.dialect_of(read)
        args = dict(read.get("sql_connect_args") or {})
        self.source = str(args.get("database") or args.get("dbname") or "")
        if not self.source:
            raise ZeroTouchError("the read config names no database")
        self.name = (
            self.source + SANDBOX_SUFFIX
            if self.dialect == "mysql"
            else PG_SANDBOX_SCHEMA
        )
        self.schema: Optional[Schema] = None
        #: Foreign keys the sandbox couldn't copy. Contracts built without
        #: them differ from production ones, and are then not admitted.
        self.fk_errors: list[str] = []

    # Connections --------------------------------------------------------------------

    def _connect(self) -> Any:
        mod = importlib.import_module(str(self.admin["sql_driver"]))
        args = dict(self.admin.get("sql_connect_args") or {})
        env = self.admin.get("sql_password_env")
        if env:
            args["password"] = os.environ.get(str(env), "")
        if self.dialect == "mysql":
            args.pop("database", None)
            conn = mod.connect(**args, autocommit=True)
        else:
            args.setdefault("dbname", self.source)
            args.pop("database", None)
            conn = mod.connect(**args)
            conn.autocommit = True
        return conn

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, tuple(params))
            try:
                return list(cur.fetchall())
            except Exception:  # noqa: BLE001 - a statement with no rows
                return []
        finally:
            conn.close()

    def q(self, name: str) -> str:
        return store.quote(self.dialect, name)

    def t(self, table: str) -> str:
        return f"{self.q(self.name)}.{self.q(table)}"

    def _guard(self) -> None:
        ok = (
            self.name.endswith(SANDBOX_SUFFIX)
            if self.dialect == "mysql"
            else self.name == PG_SANDBOX_SCHEMA
        )
        if not ok or self.name == self.source:
            raise ZeroTouchError(f"refusing to write to {self.name!r}: not a sandbox")

    # Make and drop --------------------------------------------------------------------

    def create(self, schema: Schema, reader: str) -> None:
        """Copy every base table of ``schema`` into the sandbox, and let the
        read account (``reader``: MySQL ``'user'@'host'``, PostgreSQL a role)
        read it. An earlier sandbox of the same name is replaced."""

        self._guard()
        self.drop()
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.dialect == "mysql":
                cur.execute(f"CREATE DATABASE {self.q(self.name)}")
                src = self.q(self.source)
                for name in sorted(schema.tables):
                    cur.execute(
                        f"CREATE TABLE {self.t(name)} LIKE {src}.{self.q(name)}"
                    )
                    cur.execute(
                        f"INSERT INTO {self.t(name)} SELECT * FROM {src}.{self.q(name)}"
                    )
                # Declared foreign keys too: the generator reads them, so the
                # sandbox's contracts must see the same schema as the app's.
                cur.execute("SET FOREIGN_KEY_CHECKS=0")
                for name, t in sorted(schema.tables.items()):
                    for fk in t.foreign_keys:
                        try:
                            cur.execute(
                                f"ALTER TABLE {self.t(name)} ADD FOREIGN KEY "
                                f"({self.q(fk.column)}) REFERENCES "
                                f"{self.t(fk.table)} ({self.q(fk.ref_column)})"
                            )
                        except Exception as exc:  # noqa: BLE001 - recorded
                            self.fk_errors.append(f"{name}.{fk.column}: {exc}")
                user, _, host = reader.partition("@")
                cur.execute(
                    f"GRANT SELECT, SHOW VIEW, TRIGGER ON {self.q(self.name)}.* "
                    "TO %s@%s",
                    (user.strip("'`\""), host.strip("'`\"") or "%"),
                )
            else:
                cur.execute(f"CREATE SCHEMA {self.q(self.name)}")
                cur.execute("SELECT current_schema()")
                src = self.q(str(cur.fetchone()[0]))
                for name in sorted(schema.tables):
                    cur.execute(
                        f"CREATE TABLE {self.t(name)} (LIKE {src}.{self.q(name)} "
                        "INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)"
                    )
                    cur.execute(
                        f"INSERT INTO {self.t(name)} OVERRIDING SYSTEM VALUE "
                        f"SELECT * FROM {src}.{self.q(name)}"
                    )
                for name, t in sorted(schema.tables.items()):
                    for fk in t.foreign_keys:
                        try:
                            cur.execute(
                                f"ALTER TABLE {self.t(name)} ADD FOREIGN KEY "
                                f"({self.q(fk.column)}) REFERENCES "
                                f"{self.t(fk.table)} ({self.q(fk.ref_column)}) NOT VALID"
                            )
                        except Exception as exc:  # noqa: BLE001 - recorded
                            self.fk_errors.append(f"{name}.{fk.column}: {exc}")
                cur.execute(
                    f"GRANT USAGE ON SCHEMA {self.q(self.name)} TO {self.q(reader)}"
                )
                cur.execute(
                    f"GRANT SELECT ON ALL TABLES IN SCHEMA {self.q(self.name)} "
                    f"TO {self.q(reader)}"
                )
        finally:
            conn.close()
        self.schema = store.read_schema(
            store.connector(self.read_config()), self.dialect
        )

    def drop(self) -> None:
        self._guard()
        if self.dialect == "mysql":
            self._exec(f"DROP DATABASE IF EXISTS {self.q(self.name)}")
        else:
            self._exec(f"DROP SCHEMA IF EXISTS {self.q(self.name)} CASCADE")

    def read_config(self) -> dict[str, Any]:
        """The read-only account's config, pointed at the sandbox."""

        out = json.loads(json.dumps(self.read))
        args = out.setdefault("sql_connect_args", {})
        if self.dialect == "mysql":
            args["database"] = self.name
        else:
            args["options"] = f"-c search_path={self.name}"
        return out

    # Rows ---------------------------------------------------------------------------

    def _table(self, name: str) -> Table:
        assert self.schema is not None
        return self.schema.tables[name]

    def _where(
        self, table: Table, row: Mapping[str, Optional[str]]
    ) -> tuple[str, list[Any]]:
        if not table.primary_key:
            raise ZeroTouchError(
                f"{table.name} has no primary key; rows can't be planted"
            )
        parts, args = [], []
        for c in table.primary_key:
            v = row.get(c)
            if v is None:
                raise ZeroTouchError(f"{table.name}: a key cell is NULL")
            parts.append(f"{self.q(c)} = %s")
            args.append(v)
        return " AND ".join(parts), args

    def get(
        self, table: str, row: Mapping[str, Optional[str]]
    ) -> Optional[dict[str, Optional[str]]]:
        t = self._table(table)
        where, args = self._where(t, row)
        cols = t.column_names
        rows = self._exec(
            f"SELECT {', '.join(self.q(c) for c in cols)} FROM {self.t(table)} WHERE {where}",
            args,
        )
        if not rows:
            return None
        return {c: store.cell_text(v) for c, v in zip(cols, rows[0])}

    def rows(self, table: str, limit: int = 50) -> list[dict[str, Optional[str]]]:
        t = self._table(table)
        cols = t.column_names
        order = ", ".join(self.q(c) for c in t.primary_key) or self.q(cols[0])
        got = self._exec(
            f"SELECT {', '.join(self.q(c) for c in cols)} FROM {self.t(table)} "
            f"ORDER BY {order} LIMIT {int(limit)}"
        )
        return [{c: store.cell_text(v) for c, v in zip(cols, r)} for r in got]

    def unique_indexes(self, table: str) -> dict[str, list[str]]:
        """Unique indexes of a sandbox table other than its primary key:
        name -> columns in index order, from the catalog (read once per
        table)."""

        cache = self.__dict__.setdefault("_unique", {})
        if table not in cache:
            cache[table] = self._read_unique(table)
        return cache[table]

    def _read_unique(self, table: str) -> dict[str, list[str]]:
        """Plain unique indexes only: one that is partial, holds an
        expression or indexes a prefix of a column is left out, so it can
        never be taken as proof that a duplicate is impossible."""

        if self.dialect == "mysql":
            got = self._exec(
                "SELECT INDEX_NAME, COLUMN_NAME, SUB_PART FROM "
                "information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND "
                "TABLE_NAME = %s AND NON_UNIQUE = 0 AND INDEX_NAME <> 'PRIMARY' "
                "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
                (self.name, table),
            )
        else:
            got = self._exec(
                "SELECT i.relname, a.attname, NULL FROM pg_index x "
                "JOIN pg_class c ON c.oid = x.indrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN LATERAL unnest(x.indkey) WITH ORDINALITY k(attnum, ord) ON true "
                "LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
                "WHERE n.nspname = %s AND c.relname = %s AND x.indisunique "
                "AND NOT x.indisprimary AND x.indpred IS NULL "
                "AND x.indexprs IS NULL ORDER BY i.relname, k.ord",
                (self.name, table),
            )
        plain: dict[str, list[str]] = {}
        odd: set[str] = set()
        for name, col, sub in got:
            if col is None or sub is not None:
                odd.add(str(name))
            else:
                plain.setdefault(str(name), []).append(str(col))
        return {k: v for k, v in plain.items() if k not in odd}

    def holds(
        self,
        table: str,
        values: Mapping[str, Optional[str]],
        exclude: Optional[Mapping[str, Optional[str]]] = None,
    ) -> bool:
        """Whether a row of the table already holds every one of ``values``
        (compared by the store, so its collation decides, as its unique
        index would). ``exclude`` is the primary key of a row left out: the
        row the plant itself writes, whose own value never collides."""

        t = self._table(table)
        where = " AND ".join(f"{self.q(c)} = %s" for c in values)
        args = [_db_value(t, c, v) for c, v in values.items()]
        if exclude:
            own = " AND ".join(f"{self.q(c)} = %s" for c in exclude)
            where += f" AND NOT ({own})"
            args += [_db_value(t, c, v) for c, v in exclude.items()]
        got = self._exec(f"SELECT 1 FROM {self.t(table)} WHERE {where} LIMIT 1", args)
        return bool(got)

    def distinct(self, table: str, column: str, limit: int = 5) -> list[str]:
        """Up to ``limit`` values the column holds, as text."""

        got = self._exec(
            f"SELECT DISTINCT {self.q(column)} FROM {self.t(table)} "
            f"WHERE {self.q(column)} IS NOT NULL LIMIT {int(limit)}"
        )
        return [v for v in (store.cell_text(r[0]) for r in got) if v is not None]

    def max_key(self, table: str) -> Optional[int]:
        t = self._table(table)
        if len(t.primary_key) != 1:
            return None
        col = t.column(t.primary_key[0])
        if col is None or col.kind != "number":
            return None
        got = self._exec(f"SELECT MAX({self.q(col.name)}) FROM {self.t(table)}")
        return int(got[0][0] or 0)

    def put_many(self, ops: Sequence["Op"]) -> None:
        """Put each op's row in one transaction: every key is deleted first,
        then every row inserted, so rows swapping a unique key don't collide
        on the way, and a failure leaves the sandbox as it was. The last op
        for a key wins."""

        self._guard()
        last: dict[tuple[str, str], Op] = {}
        for op in ops:
            last[(op.table, store.row_key(self._table(op.table), op.key))] = op
        if not last:
            return
        conn = self._connect()
        try:
            if self.dialect == "mysql":
                conn.autocommit(False)  # pymysql: a method
            else:
                conn.autocommit = False
            cur = conn.cursor()
            self._no_key_checks(cur)
            for op in last.values():
                self._delete(cur, op.table, op.key)
            for op in last.values():
                if op.row is not None:
                    self._insert(cur, op.table, op.row)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _no_key_checks(self, cur: Any) -> None:
        # A planted fault may break a foreign key on purpose (a row the
        # write points to is gone): the store must take it.
        if self.dialect == "mysql":
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            return
        cur.execute("SAVEPOINT oa_role")
        try:  # a superuser only; else a key-breaking fault fails closed
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute("RELEASE SAVEPOINT oa_role")
        except Exception:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT oa_role")

    def _delete(self, cur: Any, table: str, key: Mapping[str, Optional[str]]) -> None:
        where, args = self._where(self._table(table), key)
        cur.execute(f"DELETE FROM {self.t(table)} WHERE {where}", tuple(args))

    def _insert(self, cur: Any, table: str, row: Mapping[str, Optional[str]]) -> None:
        t = self._table(table)
        cols = [c for c in t.column_names if c in row]
        over = "" if self.dialect == "mysql" else " OVERRIDING SYSTEM VALUE"
        cur.execute(
            f"INSERT INTO {self.t(table)} ({', '.join(self.q(c) for c in cols)})"
            f"{over} VALUES ({', '.join(['%s'] * len(cols))})",
            tuple(_db_value(t, c, row.get(c)) for c in cols),
        )

    def put(
        self,
        table: str,
        row: Optional[Mapping[str, Optional[str]]],
        key: Mapping[str, Optional[str]],
    ) -> None:
        """Make the row with ``key``'s primary key be ``row`` (None: absent)."""

        self.put_many([Op(table, dict(key), dict(row) if row is not None else None)])


def _db_value(table: Table, column: str, text: Optional[str]) -> Any:
    """A snapshot cell back as a value the driver can bind."""

    if text is None:
        return None
    col = table.column(column)
    if col is not None and col.kind == "binary" and text.startswith("0x"):
        return bytes.fromhex(text[2:])
    if col is not None and col.kind == "bool" and text in ("true", "false"):
        return text == "true"
    return text


# -- operations: a write as rows to put ---------------------------------------------


@dataclass
class Op:
    """Make row ``key`` of ``table`` be ``row`` (None: absent)."""

    table: str
    key: dict[str, Optional[str]]
    row: Optional[dict[str, Optional[str]]]


def forward(diff_changes: Sequence[RowChange]) -> list[Op]:
    out = []
    for ch in diff_changes:
        ref = ch.after if ch.after is not None else ch.before
        out.append(Op(ch.table, dict(ref or {}), dict(ch.after) if ch.after else None))
    return out


def backward(diff_changes: Sequence[RowChange]) -> list[Op]:
    out = []
    for ch in diff_changes:
        ref = ch.before if ch.before is not None else ch.after
        out.append(
            Op(ch.table, dict(ref or {}), dict(ch.before) if ch.before else None)
        )
    return out


def _parse_time(text: Optional[str]) -> Optional[_dt.datetime]:
    if not text:
        return None
    raw = text.replace("T", " ").split("+")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def shift_times(
    schema: Schema, ops: list[Op], demo: Demo, values_text: set[str]
) -> list[Op]:
    """Move the write's own time stamps to now. A date-time cell within a
    day of when the demonstration was made, and not a task's value, moves by
    the time since then, so a replay looks like a write made now (in
    whatever time zone the app stores)."""

    start = _parse_time(demo.started_at)
    if start is None:
        return ops
    delta = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - start
    lo, hi = start - _dt.timedelta(days=1), start + _dt.timedelta(days=1)
    out = []
    for op in ops:
        if op.row is None:
            out.append(op)
            continue
        t = schema.tables[op.table]
        row = dict(op.row)
        for c in t.columns:
            if c.kind != "datetime" or c.name in t.primary_key:
                continue
            when = _parse_time(row.get(c.name))
            if (
                when is None
                or not (lo <= when <= hi)
                or _is_value(row.get(c.name), values_text)
            ):
                continue
            row[c.name] = (when + delta).isoformat(sep=" ", timespec="seconds")
        out.append(Op(op.table, op.key, row))
    return out


# -- faults ----------------------------------------------------------------------


@dataclass
class Case:
    """One validation run on a held-out write. ``write`` is put after
    ``begin_write``; ``late`` after a ``CONFIRMED`` and before the recheck.
    ``correct``: the run must be confirmed; else it must be stopped.
    ``unplantable``: why no fault could be planted here (the case fails)."""

    name: str
    cls: str
    write: list[Op]
    late: list[Op] = field(default_factory=list)
    correct: bool = False
    unplantable: str = ""
    #: For a ``duplicate``: the table, and the row copied with the copy, so a
    #: refusal by a unique key can be checked against the schema.
    copy_of: Optional[
        tuple[str, dict[str, Optional[str]], dict[str, Optional[str]], set[str]]
    ] = None


#: Fault classes every kind's held-out writes must have produced before the
#: kind can be admitted. A create also needs ``CREATE_CLASSES``, an update
#: ``UPDATE_CLASSES``.
REQUIRED_CLASSES = ("missing", "unrelated_edit", "delayed", "other_table")
CREATE_CLASSES = ("duplicate", "look_alike")
UPDATE_CLASSES = ("wrong_record",)

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _fresh_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def mutate(table: Table, column: str, text: Optional[str]) -> Optional[str]:
    """Another value of the column's type, plainly different, or None when
    the type has no generator. Text keeps its length (a length limit or a
    fixed-width column still takes it)."""

    col = table.column(column)
    kind = col.kind if col else "text"
    dtype = (col.data_type if col else "").lower()
    if dtype in ("json", "jsonb"):
        return '{"oa_fault": 1}' if text != '{"oa_fault": 1}' else '{"oa_fault": 2}'
    if dtype == "uuid" or (text is not None and _UUID.match(text)):
        return _fresh_uuid()
    if kind == "datetime":
        return FAR_TIME if text != FAR_TIME else "2002-03-04 05:06:07"
    if kind == "date":
        return "2001-02-03" if text != "2001-02-03" else "2002-03-04"
    if kind == "time":
        return "01:02:03" if text != "01:02:03" else "02:03:04"
    if kind == "bool":
        return "false" if text in ("true", "1") else "true"
    if kind == "number":
        try:
            n = float(text) if text is not None else 0.0
        except ValueError:
            return None
        return str(int(n) + 1) if n.is_integer() else str(n + 1)
    if kind == "binary":
        if text and text.startswith("0x") and len(text) > 2:
            last = int(text[-2:], 16) ^ 0x01
            return text[:-2] + f"{last:02x}"
        return "0x01"
    if kind == "text":
        if not text:
            return "x"
        return text[:-1] + ("x" if text[-1] != "x" else "y")
    return None


def plant_value(
    sb: "Sandbox", table: Table, column: str, current: Optional[str]
) -> Optional[str]:
    """A value to plant in ``table.column`` instead of ``current``: another
    row's key for a declared foreign key; for a type the store takes any
    value of (a date or time, a flag, JSON, a UUID, bytes), a value made by
    :func:`mutate`, far from anything a write would store; for text and
    numbers, a value the column already holds in another row (a length or an
    enum the schema doesn't show still accepts it), else :func:`mutate`."""

    fk = next((f for f in table.foreign_keys if f.column == column), None)
    if fk is not None:
        for r in sb.rows(fk.table, 20):
            v = r.get(fk.ref_column)
            if v is not None and v != current:
                return v
    col = table.column(column)
    if col is not None and col.kind not in ("text", "number"):
        made = mutate(table, column, current)
        if made is not None:
            return made
    for v in sb.distinct(table.name, column, 5):
        if v is not None and v != current:
            return v
    return mutate(table, column, current)


def unique_columns(sb: "Sandbox", table: str) -> set[str]:
    """Columns in any unique index of ``table`` other than its primary key
    (none when the sandbox can't say)."""

    get = getattr(sb, "unique_indexes", None)
    if get is None:
        return set()
    return {c for cols in get(table).values() for c in cols}


def fresh_value(
    sb: "Sandbox", t: Table, col: str, current: Optional[str]
) -> Optional[str]:
    """A value for a column of a unique index that no row holds: made by
    :func:`mutate` (another row's value would collide), changed again until
    the column doesn't hold it, a few times at most."""

    taken = set(sb.distinct(t.name, col, 1000))
    v = mutate(t, col, current)
    for _ in range(5):
        if v is None or v not in taken:
            return v
        v = mutate(t, col, v)
    return None


def unique_free(
    sb: "Sandbox",
    t: Table,
    col: str,
    row: Mapping[str, Optional[str]],
    value: Optional[str],
) -> bool:
    """Whether ``row`` with ``col`` set to ``value`` leaves every unique
    index of the table (other than its primary key) free: no other row
    already holds the index's values. ``row`` itself, found by its primary
    key, is left out: the plant writes that row, so a value only it holds
    (an update's old value) is free. Without a whole primary key no row is
    left out. A NULL in an index never collides."""

    get = getattr(sb, "unique_indexes", None)
    if get is None or value is None:
        return True
    holds = getattr(sb, "holds", None)
    key = {c: row.get(c) for c in t.primary_key}
    own = key if key and all(v is not None for v in key.values()) else None

    def is_own(r: Mapping[str, Optional[str]]) -> bool:
        return own is not None and all(r.get(c) == v for c, v in own.items())

    for cols in get(t.name).values():
        if col not in cols:
            continue
        want = {c: (value if c == col else row.get(c)) for c in cols}
        if any(v is None for v in want.values()):
            continue
        if holds is not None:
            if holds(t.name, want, exclude=own):
                return False
        elif any(
            not is_own(r) and all(r.get(c) == v for c, v in want.items())
            for r in sb.rows(t.name, 1000)
        ):
            return False
    return True


def plant_free(
    sb: "Sandbox", t: Table, col: str, row: Mapping[str, Optional[str]]
) -> Optional[str]:
    """:func:`plant_value` for ``row``'s ``col``, kept out of the table's
    unique indexes: where the planted value would give a row another row
    already holds (another row's code, or an index over several columns), a
    value that doesn't: another the key or column holds, else one made by
    :func:`fresh_value`. None when there is none (the fault is then
    unplantable, which fails admission)."""

    current = row.get(col)
    v = plant_value(sb, t, col, current)
    if col not in unique_columns(sb, t.name) or unique_free(sb, t, col, row, v):
        return v
    tried = {v, current}
    pool: list[Optional[str]] = []
    fk = next((f for f in t.foreign_keys if f.column == col), None)
    if fk is not None:
        pool += [r.get(fk.ref_column) for r in sb.rows(fk.table, 50)]
    else:
        pool += sb.distinct(t.name, col, 50)
    pool.append(fresh_value(sb, t, col, current))
    for cand in pool:
        if cand is None or cand in tried:
            continue
        tried.add(cand)
        if unique_free(sb, t, col, row, cand):
            return cand
    return None


def edit_value(
    sb: "Sandbox",
    t: Table,
    col: str,
    current: Optional[str],
    written: Optional[str],
    row: Optional[Mapping[str, Optional[str]]] = None,
) -> Optional[str]:
    """The value an unrelated edit sets on another row (``row``): what the
    write set (the same change elsewhere), except in a column of a unique
    index, where that value would make the store refuse the edit; there, a
    value no row holds in any of the table's unique indexes."""

    if col in unique_columns(sb, t.name):
        v = fresh_value(sb, t, col, current)
        if row is None or (v is not None and unique_free(sb, t, col, row, v)):
            return v
        return plant_free(sb, t, col, row)
    return written


def _text_values(values: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()

    def walk(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, Mapping):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        else:
            out.add(value_text(v))

    walk(values)
    return out


def _is_value(text: Optional[str], values: set[str]) -> bool:
    if text is None:
        return False
    if text in values:
        return True
    for v in values:
        try:
            if float(text) == float(v):
                return True
        except ValueError:
            pass
        if len(v) >= 10 and text.startswith(v):  # a date stored as a date-time
            return True
    return False


def _new_key(
    sb: "Sandbox", t: Table, row: Mapping[str, Optional[str]]
) -> Optional[dict[str, Optional[str]]]:
    """A key for a copy of ``row`` that no row holds yet, or None."""

    if len(t.primary_key) != 1:
        return None
    pk = t.primary_key[0]
    top = sb.max_key(t.name)
    if top is not None:
        return {pk: str(top + 1000)}
    current = row.get(pk)
    if current is not None and _UUID.match(current):
        return {pk: _fresh_uuid()}
    return None


def _copy_row(
    t: Table, row: Mapping[str, Optional[str]], key: Mapping[str, Optional[str]]
) -> dict[str, Optional[str]]:
    """``row`` under ``key``, with every UUID-shaped cell made new (a unique
    token copied twice is refused by the store, not by the contract)."""

    out = dict(row)
    out.update(key)
    for c, v in list(out.items()):
        if c not in key and v is not None and _UUID.match(v):
            out[c] = _fresh_uuid()
    return out


_BOUND = (
    re.compile(r"^([A-Za-z0-9_$]+)\.([A-Za-z0-9_$]+)\b"),
    re.compile(r"^([A-Za-z0-9_$]+) \(whose .*?\)\.([A-Za-z0-9_$]+)\b"),
)


def bound_columns(report: Mapping[str, Any], kind: str) -> set[tuple[str, str]]:
    """The columns a task's parameters are stored in, as the generator's
    report places them (``table.column``, or a row beside the record's
    ``table (whose ...).column``). A place in any other form is left out, so
    fewer columns count as the task's: never more."""

    out: set[tuple[str, str]] = set()
    entry = (report.get("kinds") or {}).get(kind) or {}
    for place in (entry.get("locations") or {}).values():
        for pat in _BOUND:
            m = pat.match(str(place))
            if m:
                out.add((m.group(1), m.group(2)))
                break
    return out


def faults(
    schema: Schema,
    sb: "Sandbox",
    demo: Demo,
    write: list[Op],
    bound: Optional[set[tuple[str, str]]] = None,
) -> list[Case]:
    """Faults planted from one held-out write: its diff, the schema and its
    task's values. The contracts are never read here.

    Every column of every row the write adds or changes gets its own fault,
    NULL cells included; a column no value can be planted in gets an
    ``unplantable`` case, which fails admission."""

    values = _text_values(demo.values)
    out: list[Case] = [Case("missing", "missing", [])]
    touched = {op.table for op in write}

    def swap(i: int, op: Op, row: dict[str, Optional[str]]) -> list[Op]:
        return write[:i] + [Op(op.table, op.key, row)] + write[i + 1 :]

    def column_fault(cls: str, i: int, op: Op, col: str) -> None:
        t = schema.tables[op.table]
        assert op.row is not None
        new = plant_free(sb, t, col, op.row)
        name = f"{cls}:{op.table}.{col}"
        if new is None:
            c = t.column(col)
            kind = c.data_type if c is not None else "?"
            out.append(Case(name, cls, [], unplantable=f"no value of type "
                            f"{kind} can be planted"))  # fmt: skip
            return
        row = dict(op.row)
        row[col] = new
        out.append(Case(name, cls, swap(i, op, row)))

    for i, op in enumerate(write):
        t = schema.tables[op.table]
        if len(write) > 1:
            # Part of the write is lost (a child row, a journal entry).
            out.append(
                Case(f"partial:{op.table}", "partial", write[:i] + write[i + 1 :])
            )
        if op.row is None:
            continue
        before = sb.get(op.table, op.key)
        is_new = before is None
        changed = [
            c.name
            for c in t.columns
            if c.name not in t.primary_key
            and (is_new or before is None or before.get(c.name) != op.row.get(c.name))
        ]
        for col in changed:
            cls = "wrong_value" if _is_value(op.row.get(col), values) else "bookkeeping"
            column_fault(cls, i, op, col)
        if is_new:
            # A new row's own key, when the store doesn't fill it (a code,
            # a user name): a wrong one is planted like any other value.
            for col in t.primary_key:
                pk = t.column(col)
                if pk is None or pk.auto:
                    continue
                new = plant_value(sb, t, col, op.row.get(col))
                cls = (
                    "wrong_value"
                    if _is_value(op.row.get(col), values)
                    else "bookkeeping"
                )
                if new is None:
                    out.append(Case(f"{cls}:{op.table}.{col}", cls, [],
                                    unplantable="no other key value can be planted"))  # fmt: skip
                    continue
                row = dict(op.row)
                row[col] = new
                key = {k: row.get(k) for k in t.primary_key}
                out.append(Case(f"{cls}:{op.table}.{col}", cls,
                                write[:i] + [Op(op.table, key, row)] + write[i + 1 :]))  # fmt: skip
            # Each value the new row holds, set the same way on another row
            # of the table: an excuse must hold only on the rows the write
            # declares.
            other = next((r for r in sb.rows(op.table, 20)
                          if store.row_key(t, r) != store.row_key(t, op.key)), None)  # fmt: skip
            if other is not None:
                for col in changed:
                    v = edit_value(sb, t, col, other.get(col), op.row.get(col), other)
                    if v is None or other.get(col) == v:
                        continue
                    edit = dict(other)
                    edit[col] = v
                    out.append(Case(f"unrelated_edit:{op.table}.{col}", "unrelated_edit",
                                    write + [Op(op.table, other, edit)]))  # fmt: skip
        if not is_new and before is not None:
            for c in t.columns:
                if c.name not in t.primary_key and c.name not in changed:
                    column_fault("collateral_target", i, op, c.name)
            # The write's changes land on another row of the table: an
            # existing one, or a copy of the target when there is none.
            other = next((r for r in sb.rows(op.table, 20)
                          if store.row_key(t, r) != store.row_key(t, op.key)), None)  # fmt: skip
            if other is None:
                copy_key = _new_key(sb, t, before)
                other = _copy_row(t, before, copy_key) if copy_key is not None else None
            if other is not None:
                moved = dict(other)
                for col in changed:
                    moved[col] = op.row.get(col)
                out.append(Case(f"wrong_record:{op.table}", "wrong_record",
                                write[:i] + write[i + 1 :] + [Op(op.table, other, moved)]))  # fmt: skip
                # The same change on another row: an excuse must hold only
                # on the rows the write declares.
                for col in changed:
                    v = edit_value(sb, t, col, other.get(col), op.row.get(col), other)
                    if v is None or other.get(col) == v:
                        continue
                    edit = dict(other)
                    edit[col] = v
                    out.append(Case(f"unrelated_edit:{op.table}.{col}", "unrelated_edit",
                                    write + [Op(op.table, other, edit)]))  # fmt: skip
            # A late change to the target itself: its first changed column.
            if changed:
                late = dict(op.row)
                v = plant_free(sb, t, changed[0], op.row)
                if v is not None:
                    late[changed[0]] = v
                    out.append(Case(f"delayed:{op.table}.{changed[0]}", "delayed",
                                    list(write), late=[Op(op.table, op.key, late)]))  # fmt: skip
        if is_new:
            dup_key = _new_key(sb, t, op.row)
            if dup_key is None:
                out.append(Case(f"duplicate:{op.table}", "duplicate", [],
                                unplantable="no new key can be made for a copy"))  # fmt: skip
            else:
                dup = _copy_row(t, op.row, dup_key)
                # A unique value the app makes (an invoice or ticket number)
                # is made again for a second submit: plant a fresh one, so
                # the copy is a duplicate the store accepts.
                for col in unique_columns(sb, op.table) - set(dup_key):
                    if (op.table, col) not in (bound or set()):
                        v = fresh_value(sb, t, col, dup.get(col))
                        if v is not None:
                            dup[col] = v
                # The task's columns: where the generator stores its
                # parameters (bindings, never a value that happens to match).
                given = {c for c in op.row if (op.table, c) in (bound or set())}
                out.append(Case(f"duplicate:{op.table}", "duplicate",
                                write + [Op(op.table, dup_key, dup)],
                                copy_of=(op.table, dict(op.row), dup, given)))  # fmt: skip
            # The values land on an existing look-alike instead.
            for other in sb.rows(op.table, 20):
                look = dict(op.row)
                for col in t.primary_key:
                    look[col] = other.get(col)
                out.append(Case(f"look_alike:{op.table}", "look_alike",
                                write[:i] + write[i + 1 :] + [Op(op.table, other, look)]))  # fmt: skip
                break
    # Another row of each touched table, now and late.
    for name in sorted(touched):
        t = schema.tables[name]
        mine = {store.row_key(t, op.key) for op in write if op.table == name}
        other = next(
            (r for r in sb.rows(name, 20) if store.row_key(t, r) not in mine), None
        )
        if other is None:
            continue
        for c in t.columns:
            if c.name in t.primary_key:
                continue
            v = plant_free(sb, t, c.name, other)
            if v is None:
                continue
            row = dict(other)
            row[c.name] = v
            out.append(Case(f"unrelated_edit:{name}.{c.name}", "unrelated_edit",
                            write + [Op(name, other, row)]))  # fmt: skip
            out.append(Case(f"delayed:{name}.{c.name}", "delayed", list(write),
                            late=[Op(name, other, row)]))  # fmt: skip
            break
    planted = 0
    for name in sorted(set(schema.tables) - touched):
        t = schema.tables[name]
        sample = sb.rows(name, 1)
        new_key = _new_key(sb, t, sample[0]) if sample else None
        if new_key is None:
            continue
        row = _copy_row(t, sample[0], new_key)
        for col in unique_columns(sb, name) - set(new_key):
            v = fresh_value(sb, t, col, row.get(col))
            if v is not None:
                row[col] = v  # a copied unique code would be refused
        out.append(Case(f"other_table:{name}", "other_table",
                        write + [Op(name, new_key, row)]))  # fmt: skip
        planted += 1
        if planted >= 2:
            break
    return out


# -- running a case ---------------------------------------------------------------

#: Verdicts the verifier gives for a state it read and didn't pass. Only
#: these count as a stopped fault: an error, a missing contract, a fault
#: that couldn't be planted or a read that failed (``READ_FAILED``) never
#: does.
STOP_VERDICTS = ("REFUTED", "UNCERTAIN", "RECHECK_REFUTED", "RECHECK_UNCERTAIN")
#: A case whose verdict came from a failed read is run this many times in
#: all before it counts as not stopped.
READ_ATTEMPTS = 3
_READ_FAILURE = re.compile(
    r"could(?:n't| not) be read|can't be read|still (?:moving|changing)"
    r"|no readable baseline|could not be judged again"
)


def write_judged(r: Any) -> bool:
    """``verify_write`` read the state it judged: a receipt was issued, the
    frame was read, and the effect was observed (present, absent or
    conflicting, not unknown)."""

    frame = dict((getattr(r, "evidence", None) or {}).get("frame") or {})
    return (
        bool(getattr(r, "receipt", None))
        and bool(frame)
        and frame.get("status") != "not_checked"
        and getattr(r, "observed", "unknown") != "unknown"
    )


def recheck_judged(rc: Any) -> bool:
    """The recheck read the database and judged its frame."""

    return bool(getattr(rc, "read_at", "")) and getattr(rc, "frame", "") in (
        "held",
        "refuted",
    )


def read_failed(reasons: Sequence[str]) -> bool:
    """The verifier gave its verdict because it couldn't read the store (a
    timeout, a lost connection, rows still moving), not from the state it
    read."""

    return any(_READ_FAILURE.search(r) for r in reasons)


@dataclass
class Outcome:
    case: str
    cls: str
    correct: bool
    outcome: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    #: The verifier read the planted state and judged it (structured
    #: evidence: a receipt, a frame read, an observed effect; at the
    #: recheck, a read and a judged frame). False: an UNCERTAIN from a read
    #: that failed, which is never a stopped fault.
    judged: bool = True

    @property
    def ok(self) -> bool:
        """Confirmed when correct; for a fault, a verifier verdict that
        stops it, on a state that was planted."""

        if self.correct:
            return self.passed
        return not self.passed and self.outcome in STOP_VERDICTS and self.judged


def _put_all(sb: Sandbox, ops: Sequence[Op]) -> None:
    sb.put_many(ops)


def _unique_violation(exc: BaseException) -> Optional[str]:
    """The constraint name when the store refused a row for a duplicate
    unique key (MySQL 1062, PostgreSQL 23505, SQLite UNIQUE), else None.
    An empty string: a unique violation whose constraint isn't named."""

    if getattr(exc, "pgcode", None) == "23505":
        diag = getattr(exc, "diag", None)
        return str(getattr(diag, "constraint_name", "") or "")
    args = getattr(exc, "args", ())
    if args and args[0] == 1062:
        m = re.search(r"for key '([^']+)'", str(args[-1]))
        return m.group(1) if m else ""
    text = str(exc)
    if text.startswith("UNIQUE constraint failed"):
        return ""
    return None


def blocked_by_constraint(
    sb: "Sandbox", case: "Case", exc: BaseException
) -> Optional[str]:
    """The unique index that makes ``case``'s duplicate impossible, or None.

    Only when the store refused the copy for a unique violation; the
    violated index is a plain unique index of that table the catalog
    declares (not the primary key, not partial, no expression); every
    column of it holds a value the task gave; and each holds the same value
    in the copy as in the row it copies. Then the same task can't store a
    second such row. An index on a value the app makes (a number it
    assigns) proves nothing: that value is planted fresh, so the store takes
    the copy."""

    if case.cls != "duplicate" or case.copy_of is None:
        return None
    name = _unique_violation(exc)
    if not name:
        return None
    table, row, copy, given = case.copy_of
    if "." in name:
        owner, name = name.rsplit(".", 1)
        if owner.split(".")[-1].strip('`"') != table:
            return None
    cols = sb.unique_indexes(table).get(name)
    if not cols:
        return None
    if all(c in given and row.get(c) == copy.get(c) for c in cols):
        return f"{table}.{name} ({', '.join(cols)})"
    return None


def run_case(
    sb: Sandbox,
    manifest: Path,
    demo: Demo,
    case: Case,
    actor_id: str,
    extra: Mapping[str, Any],
    workdir: Optional[Path] = None,
) -> Outcome:
    """Bring the held-out write's rows back to before it, then run the case
    between ``begin_write`` and ``verify_write`` (and the recheck), then put
    every touched row back."""

    from openadapt_flow.verifier import Actor, begin_write, recheck, verify_write

    if case.unplantable:
        return Outcome(case.name, case.cls, case.correct, "NOT_PLANTED", False,
                       [case.unplantable])  # fmt: skip
    undo = backward(demo.diff.changes)
    all_ops = undo + case.write + case.late
    saved: dict[
        tuple[str, str],
        tuple[dict[str, Optional[str]], Optional[dict[str, Optional[str]]]],
    ] = {}
    assert sb.schema is not None
    for op in all_ops:
        k = (op.table, store.row_key(sb.schema.tables[op.table], op.key))
        if k not in saved:
            saved[k] = (op.key, sb.get(op.table, op.key))
    try:
        _put_all(sb, undo)
        try:
            contract, params = gen.choose(manifest, demo.kind, demo.values, extra)
        except gen.GenerateError as exc:
            return Outcome(
                case.name, case.cls, case.correct, "NO_CONTRACT", False, [str(exc)]
            )
        actor = Actor("agent", actor_id)
        pending = begin_write(contract, params, actor=actor, ledger=None,
                              write_key=f"zt-{time.time_ns()}", workdir=workdir)  # fmt: skip
        _put_all(sb, case.write)
        r = verify_write(pending)
        outcome = str(getattr(r.outcome, "value", r.outcome))
        reasons = list(r.reasons)
        passed = outcome == "CONFIRMED"
        judged = outcome != "UNCERTAIN" or write_judged(r)
        if passed and getattr(r, "recheck", None) is not None:
            _put_all(sb, case.late)
            rc = recheck(
                contract, r, params=params, actor=actor, wait=True, ledger=None
            )
            rc_out = str(getattr(rc.outcome, "value", rc.outcome))
            if rc_out != "CONFIRMED":
                passed, outcome = False, f"RECHECK_{rc_out}"
                reasons += list(rc.reasons)
                judged = rc_out != "UNCERTAIN" or recheck_judged(rc)
        elif passed and case.late:
            # No recheck is scheduled, so the late change is never read: the
            # fault passes, which fails validation.
            outcome = "CONFIRMED_NO_RECHECK"
            reasons.append(
                "the contract schedules no recheck, so a late write can't be seen"
            )
        return Outcome(
            case.name, case.cls, case.correct, outcome, passed, reasons[:6], judged
        )
    except Exception as exc:  # noqa: BLE001 - a case that can't run admits nothing
        blocked = blocked_by_constraint(sb, case, exc)
        if blocked:
            # The store's own unique index refuses this copy: such a
            # duplicate can't exist, so it is neither stopped nor passed.
            return Outcome(case.name, case.cls, case.correct, "NOT_APPLICABLE",
                           False, [f"impossible by constraint {blocked}"])  # fmt: skip
        return Outcome(
            case.name,
            case.cls,
            case.correct,
            "ERROR",
            False,
            [f"{type(exc).__name__}: {exc}", traceback.format_exc(limit=3)],
        )
    finally:
        try:
            sb.put_many(
                [Op(table, key, row) for (table, _), (key, row) in saved.items()]
            )
        except Exception as exc:  # noqa: BLE001 - the sandbox is no longer known
            raise SandboxBroken(f"the sandbox could not be restored: {exc}") from exc


# -- answers ----------------------------------------------------------------------


def auto_answers(
    report: Mapping[str, Any], picks: Mapping[str, Mapping[str, int]] = {}
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The answers this module gives, and a ledger saying by which rule."""

    answers: dict[str, Any] = {"now_checks": True, "strict_pins": True}
    ledger: list[dict[str, Any]] = [
        {
            "key": "now_checks",
            "answer": True,
            "rule": "an unchecked or excused time stamp of the main record must hold a time near the read",
        },
    ]
    ledger.append({
        "key": "strict_pins",
        "answer": True,
        "rule": "a new-row column two readings fit in every demonstration is checked both ways",
    })  # fmt: skip
    for q in report.get("questions") or []:
        key = str(q.get("key"))
        proposed = q.get("proposal")
        if key == "recheck_after_s":
            answers[key] = gen.DEFAULT_RECHECK_S
            ledger.append(
                {"key": key, "answer": gen.DEFAULT_RECHECK_S, "rule": "policy default"}
            )
        elif key == "phones":
            answers[key] = list(proposed or [])
            ledger.append(
                {
                    "key": key,
                    "answer": answers[key],
                    "rule": "every listed parameter; wrong digits are planted faults",
                }
            )
        elif key == "bookkeeping":
            prop = dict(proposed or {}) if isinstance(proposed, Mapping) else {}
            ans = {"accept": True}
            if prop.get("unchecked"):
                ans["unchecked"] = prop["unchecked"]
            answers[key] = ans
            ledger.append(
                {
                    "key": key,
                    "answer": ans,
                    "rule": "the generator's own proposal; nothing added (no logs_add, no columns_add)",
                }
            )
        elif key.startswith("locations."):
            if key in picks:
                answers[key] = dict(picks[key])
                ledger.append(
                    {
                        "key": key,
                        "answer": answers[key],
                        "rule": "the one candidate whose kind validated",
                    }
                )
    return answers, ledger


def location_options(report: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """``locations.<kind>`` -> parameter -> how many candidate places."""

    out: dict[str, dict[str, int]] = {}
    for q in report.get("questions") or []:
        key = str(q.get("key"))
        if not key.startswith("locations."):
            continue
        for name, opts in re.findall(
            r"`([^`]+)` is in several places: (.*?)(?= `|$)", str(q.get("text"))
        ):
            out.setdefault(key, {})[name] = len(re.findall(r"\[\d+\]", opts))
    return out


# -- the whole setup ----------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) if path else None


def run_verdict(
    sb: Sandbox,
    manifest: Path,
    demo: Demo,
    case: Case,
    actor_id: str,
    extra: Mapping[str, Any],
) -> Outcome:
    """:func:`run_case`, run again while its verdict came from a failed read,
    up to :data:`READ_ATTEMPTS` times. A read that keeps failing gives
    ``READ_FAILED``: the planted state was never judged, so it isn't
    stopped."""

    for _ in range(READ_ATTEMPTS):
        res = run_case(sb, manifest, demo, case, actor_id, extra,
                       manifest.parent / "state")  # fmt: skip
        if not (
            res.outcome in STOP_VERDICTS
            and (not res.judged or read_failed(res.reasons))
        ):
            return res
    res.reasons = [f"read failed {READ_ATTEMPTS} times"] + res.reasons
    res.outcome = "READ_FAILED"
    return res


def validate(
    sb: Sandbox,
    manifest: Path,
    holdout: Sequence[Demo],
    actor_id: str,
    extra: Mapping[str, Any],
    log: Callable[[str], None] = lambda s: None,
    record: Optional[list[dict[str, Any]]] = None,
    modes: Optional[dict[str, set[str]]] = None,
) -> dict[str, list[Outcome]]:
    """Every held-out write, replayed and faulted, by kind. ``modes`` is
    filled with which rows each kind's held-out writes added or changed."""

    if modes is None:
        modes = {}
    try:
        report = json.loads(
            (Path(manifest).parent / "report.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        report = {}  # no bindings: no column counts as the task's

    assert sb.schema is not None
    by_kind: dict[str, list[Outcome]] = {}
    for demo in holdout:
        for ch in demo.diff.changes:
            modes.setdefault(demo.kind, set()).add(
                "create" if ch.kind == "added" else "update"
            )
        values = _text_values(demo.values)
        write = shift_times(sb.schema, forward(demo.diff.changes), demo, values)
        cases = [Case("correct", "correct", write, correct=True)]
        # Faults are planted from the store as it was before the write.
        undo = backward(demo.diff.changes)
        saved = [(op, sb.get(op.table, op.key)) for op in undo]
        _put_all(sb, undo)
        try:
            cases += faults(sb.schema, sb, demo, write,
                            bound_columns(report, demo.kind))  # fmt: skip
        finally:
            try:
                sb.put_many([Op(op.table, op.key, row) for op, row in saved])
            except Exception as exc:  # noqa: BLE001 - the sandbox is no longer known
                raise SandboxBroken(
                    f"the sandbox could not be restored: {exc}"
                ) from exc
        for case in cases:
            res = run_verdict(sb, manifest, demo, case, actor_id, extra)
            log(f"{demo.name} {case.name}: {res.outcome} {'ok' if res.ok else 'FAIL'}"
                + ("" if res.ok or not res.reasons else f" | {res.reasons[0][:200]}"))  # fmt: skip
            if record is not None:
                record.append({"demo": demo.name, "kind": demo.kind, "case": res.case,
                               "class": res.cls, "correct": res.correct, "outcome": res.outcome,
                               "ok": res.ok, "reasons": [r[:300] for r in res.reasons[:3]]})  # fmt: skip
            by_kind.setdefault(demo.kind, []).append(res)
    return by_kind


def summarize(
    by_kind: Mapping[str, Sequence[Outcome]],
    kinds: Sequence[str],
    modes: Mapping[str, set[str]] = {},
) -> dict[str, Any]:
    """Per kind: admitted only when it has a held-out write, every held-out
    write was confirmed, every fault was planted and stopped, and every
    required class was planted. ``modes``: kind -> ``{"create", "update"}``,
    which rows its held-out writes added or changed."""

    out: dict[str, Any] = {}
    for k in kinds:
        rs = list(by_kind.get(k) or [])
        correct = [r for r in rs if r.correct]
        fl = [r for r in rs if not r.correct and r.outcome != "NOT_APPLICABLE"]
        need = list(REQUIRED_CLASSES)
        if "create" in modes.get(k, set()):
            need += CREATE_CLASSES
        if "update" in modes.get(k, set()):
            need += UPDATE_CLASSES
        planted = {r.cls for r in fl if r.outcome != "NOT_PLANTED"}
        blocked = [r for r in rs if r.outcome == "NOT_APPLICABLE"]
        impossible = sorted({r.reasons[0] for r in blocked if r.reasons})
        if (
            "duplicate" in need
            and "duplicate" not in planted
            and blocked
            and all(r.cls == "duplicate" for r in blocked)
        ):
            # Every duplicate the kind's writes could make is refused by a
            # unique index the schema declares: recorded, not required.
            need.remove("duplicate")
        missing = [c for c in need if c not in planted]
        out[k] = {
            "admitted": bool(correct)
            and all(r.ok for r in rs if r.outcome != "NOT_APPLICABLE")
            and not missing,
            "correct_writes": len(correct),
            "false_stops": [
                f"{r.outcome}: {'; '.join(r.reasons[:2])}" for r in correct if not r.ok
            ],
            "faults": len(fl),
            "faults_stopped": sum(1 for r in fl if r.ok),
            "faults_passed": [f"{r.case} ({r.outcome})" for r in fl if not r.ok],
            "classes_not_planted": missing,
            "duplicates_impossible_by_constraint": impossible,
            "by_class": {
                c: f"{sum(1 for r in fl if r.cls == c and r.ok)}/{sum(1 for r in fl if r.cls == c)}"
                for c in sorted({r.cls for r in fl})
            },
        }
        if not rs:
            out[k]["why"] = "no held-out write of this kind"
    return out


def exceptions_md(summary: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    lines = [
        "# What setup could not validate",
        "",
        "No person decision was asked for. The kinds below are not admitted, so",
        "their tasks stop (no contract). Each line says why.",
        "",
    ]
    n = 0
    for k, s in summary.items():
        if s["admitted"]:
            continue
        n += 1
        lines.append(f"{n}. `{k}`:")
        if s.get("why"):
            lines.append(f"   - {s['why']}")
        for fs in s["false_stops"]:
            lines.append(f"   - a held-out correct write stopped: {fs}")
        for f in s["faults_passed"]:
            lines.append(f"   - a fault was not planted and stopped: {f}")
        for c in s.get("classes_not_planted") or []:
            lines.append(f"   - no `{c}` fault could be planted")
        for r in ((report.get("kinds") or {}).get(k) or {}).get("refused") or []:
            lines.append(f"   - generator: {r}")
    if n == 0:
        lines.append("Every kind is admitted.")
    return "\n".join(lines) + "\n"


def check_holdout(
    demos: Sequence[Demo], holdout: Sequence[Demo], kinds: Sequence[str]
) -> None:
    """Refuse a held-out set that isn't held out: a held-out write with the
    same kind and values as a demonstration, or the same diff, or a kind
    with no held-out write."""

    def key(d: Demo) -> str:
        return json.dumps([d.kind, d.values], sort_keys=True, default=str)

    def diff_key(d: Demo) -> str:
        return json.dumps(d.diff.to_json(), sort_keys=True, default=str)

    seen = {key(d) for d in demos}
    seen_diffs = {diff_key(d) for d in demos}
    for d in holdout:
        if key(d) in seen or diff_key(d) in seen_diffs:
            raise ZeroTouchError(
                f"held-out write {d.name} repeats a demonstration; a held-out "
                "write must have values the generator never saw"
            )
    missing = sorted(set(kinds) - {d.kind for d in holdout})
    if missing:
        raise ZeroTouchError(
            "no held-out write for kind(s): "
            + ", ".join(missing)
            + "; every kind needs at least one to be validated"
        )


_INTERVAL = re.compile(r"INTERVAL (?:'\d+ seconds'|\d+ SECOND)")


def _normal(doc: Any) -> Any:
    """A contract with what differs between the sandbox and the app's own
    store taken out: connections, the recheck window and the time-check
    window that follows from it."""

    if isinstance(doc, Mapping):
        return {
            k: _normal(v)
            for k, v in doc.items()
            if k
            not in (
                "sql_connect_args",
                "sql_password_env",
                "recheck_after_s",
                "environment_id",
            )  # fmt: skip
        }
    if isinstance(doc, list):
        return [_normal(v) for v in doc]
    if isinstance(doc, str):
        return _INTERVAL.sub("INTERVAL <w>", doc)
    return doc


def fingerprint(manifest_dir: Path, kind: str) -> str:
    """A digest of one kind's listed contracts and manifest entry, as
    :func:`_normal` sees them."""

    import hashlib

    manifest = _load_yaml(manifest_dir / "manifest.yaml") or {}
    entry = (manifest.get("kinds") or {}).get(kind)
    files: dict[str, Any] = {}
    for var in (entry or {}).get("variants") or []:
        for k, f in var.items():
            if k in ("create", "update", "found") and isinstance(f, str):
                files[f] = _normal(_load_yaml(manifest_dir / f))
    body = json.dumps({"entry": _normal(entry), "files": files}, sort_keys=True,
                      default=str)  # fmt: skip
    return hashlib.sha256(body.encode()).hexdigest()


def field_set(values: Mapping[str, Any]) -> tuple[list[str], dict[str, int]]:
    """A task's field set as the manifest writes it: its non-null scalar
    fields, and its lists by length."""

    given = sorted(
        k for k, v in values.items() if v is not None and not isinstance(v, list)
    )
    lists = {k: len(v) for k, v in values.items() if isinstance(v, list)}
    return given, lists


def install(
    staged: Path,
    final: Path,
    admitted: Sequence[str],
    validated: Optional[
        Mapping[str, Sequence[tuple[list[str], dict[str, int]]]]
    ] = None,
) -> None:
    """Keep only the admitted kinds in ``staged`` (manifest entries, pinned
    digests and contract files), and of each kind only the field sets a
    held-out write validated (``validated``: kind -> field sets; None keeps
    every listed one). Contracts are never built at run time from a
    zero-touch manifest: ``choose`` stops a task whose field set isn't
    listed. Then move it to ``final`` in one rename."""

    import shutil

    path = staged / "manifest.yaml"
    manifest = _load_yaml(path) or {}
    dropped = [k for k in list(manifest.get("kinds") or {}) if k not in admitted]
    if validated is not None:
        for k, entry in (manifest.get("kinds") or {}).items():
            if k in dropped:
                continue
            listed = [(sorted(v.get("fields") or []), dict(v.get("lists") or {}))
                      for v in entry.get("variants") or []]  # fmt: skip
            for given, lists in validated.get(k, []):
                if (sorted(given), dict(lists)) in listed:
                    continue
                # Validated through a contract built on demand: build the
                # same one here, from the same pinned inputs, and list it.
                try:
                    files = gen._field_set(
                        path, manifest, k, entry, set(given), dict(lists)
                    )
                except gen.GenerateError:
                    files = None
                if files:
                    var: dict[str, Any] = {"fields": sorted(given), **files}
                    if lists:
                        var["lists"] = dict(lists)
                    entry.setdefault("variants", []).append(var)
                    listed.append((sorted(given), dict(lists)))
            keep = []
            for var in entry.get("variants") or []:
                fs = (sorted(var.get("fields") or []), dict(var.get("lists") or {}))
                if fs in [(sorted(f), dict(lst)) for f, lst in validated.get(k, [])]:
                    keep.append(var)
                else:
                    for flavor in ("create", "update", "found"):
                        if isinstance(var.get(flavor), str):
                            (staged / var[flavor]).unlink(missing_ok=True)
            entry["variants"] = keep
    # Dropped kinds go only now: building a field set checks every pinned
    # file of the manifest as ``propose`` wrote it.
    for k in dropped:
        manifest["kinds"].pop(k)
        shutil.rmtree(staged / "contracts" / k, ignore_errors=True)
    # No run-time builds: they would be contracts no sandbox validated.
    manifest.pop("field_sets", None)
    for k in dropped:
        shutil.rmtree(staged / "field_sets" / k, ignore_errors=True)
    manifest["zero_touch"] = {"admitted": sorted(admitted)}
    path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")  # fmt: skip
    if final.exists():
        old = final.with_name(final.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        final.rename(old)
        staged.rename(final)
        shutil.rmtree(old, ignore_errors=True)
    else:
        staged.rename(final)


def onboard(
    *,
    read: dict[str, Any],
    admin: dict[str, Any],
    reader: str,
    kinds_path: Path,
    demo_dir: Path,
    holdout_dir: Path,
    out: Path,
    actor_params: Mapping[str, str],
    actor_id: str,
    keep_sandbox: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Propose, answer, validate in the sandbox, and write the admitted
    contracts for the app's own store to ``out/contracts``."""

    t0 = time.time()
    kinds = gen.load_kinds(kinds_path)
    kind_names = [k.kind for k in kinds]
    if Path(holdout_dir).resolve() == Path(demo_dir).resolve():
        raise ZeroTouchError("the held-out folder is the demonstrations folder")
    holdout = gen.load_demos(holdout_dir)
    check_holdout(gen.load_demos(demo_dir), holdout, kind_names)
    schema = store.read_schema(store.connector(read), store.dialect_of(read))
    sb = Sandbox(admin, read)
    sb.create(schema, reader)
    sread = sb.read_config()
    work = out / "sandbox"
    broken = ""
    try:
        report = gen.propose(read=sread, kinds=kinds, demo_dir=demo_dir, out=work / "q",
                             actor_params=actor_params, answers={})  # fmt: skip
        options = location_options(report)
        trials: list[dict[str, dict[str, int]]] = [{}]
        for key, params in options.items():
            for name, n in params.items():
                trials += [{key: {name: i}} for i in range(n)]
        best: dict[str, Any] = {}
        results: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []

        def answer(picks: Mapping[str, Mapping[str, int]], tag: str) -> tuple[Any, Any]:
            # The bookkeeping proposal depends on where values are placed: ask
            # with the picks first, then answer with that run's own proposal.
            first = gen.propose(read=sread, kinds=kinds, demo_dir=demo_dir,
                                out=work / tag / "q", actor_params=actor_params,
                                answers={**{k: dict(v) for k, v in picks.items()},
                                         "recheck_after_s": SANDBOX_RECHECK_S})  # fmt: skip
            return auto_answers(first, picks)

        def failed(tag: str, why: str) -> dict[str, Any]:
            blank = {"admitted": False, "why": why, "correct_writes": 0,
                     "false_stops": [], "faults": 0, "faults_stopped": 0,
                     "faults_passed": [], "classes_not_planted": [], "by_class": {}}  # fmt: skip
            return {"answers": {}, "ledger": [], "report": {}, "cases": [],
                    "dir": work / tag, "summary": {k: dict(blank) for k in kind_names}}  # fmt: skip

        def trial(picks: Mapping[str, Mapping[str, int]], tag: str) -> dict[str, Any]:
            log(f"propose ({tag})")
            try:
                answers, ledger = answer(picks, tag)
                sans = {**answers, "recheck_after_s": SANDBOX_RECHECK_S}
                rep = gen.propose(read=sread, kinds=kinds, demo_dir=demo_dir,
                                  out=work / tag, actor_params=actor_params,
                                  answers=sans)  # fmt: skip
                cases: list[dict[str, Any]] = []
                modes: dict[str, set[str]] = {}
                by_kind = validate(sb, work / tag / "manifest.yaml", holdout, actor_id,
                                   dict(actor_params), log, cases, modes)  # fmt: skip
            except SandboxBroken:
                raise  # the whole setup stops: see onboard
            except (gen.GenerateError, ZeroTouchError) as exc:
                # The answers don't fit together, or the sandbox broke:
                # nothing is admitted.
                return failed(tag, f"not validated: {exc}")
            return {"answers": answers, "ledger": ledger, "report": rep, "cases": cases,
                    "dir": work / tag, "summary": summarize(by_kind, kind_names, modes)}  # fmt: skip

        for picks in trials:
            tag = "base" if not picks else "pick-" + "-".join(
                f"{k}.{p}.{i}" for k, d in picks.items() for p, i in d.items())  # fmt: skip
            got = trial(picks, tag)
            results.append((picks, got["answers"], got["summary"]))
            best = best or got
        # A location pick is kept when exactly one candidate admits its kind.
        final_picks: dict[str, dict[str, int]] = {}
        for key, params in options.items():
            kind = key.split(".", 1)[1]
            for name in params:
                ok = [p[key][name] for p, _, s in results
                      if key in p and name in p[key] and s[kind]["admitted"]]  # fmt: skip
                if len(ok) == 1:
                    final_picks.setdefault(key, {})[name] = ok[0]
        chosen = trial(final_picks, "final") if final_picks else best
        answers, ledger = chosen["answers"], chosen["ledger"]
        summary, cases = chosen["summary"], chosen["cases"]
        validated = chosen["dir"]
    except SandboxBroken as exc:
        broken = str(exc)
    finally:
        if not keep_sandbox:
            sb.drop()
    if broken:
        # Nothing validated in a sandbox that stopped mirroring the store
        # can be trusted: no contract is installed, and every kind says why.
        import shutil

        shutil.rmtree(out / ".contracts.staged", ignore_errors=True)
        if (out / "contracts").exists():
            # Kept aside for a look, never served: choose reads contracts/.
            aside = out / f"contracts.stopped-{time.strftime('%Y%m%dT%H%M%S')}"
            (out / "contracts").rename(aside)
        why = f"setup stopped: {broken}"
        summary = {k: {"admitted": False, "why": why, "correct_writes": 0,
                       "false_stops": [], "faults": 0, "faults_stopped": 0,
                       "faults_passed": [], "classes_not_planted": [],
                       "by_class": {}} for k in kind_names}  # fmt: skip
        result = {"minutes": round((time.time() - t0) / 60, 2), "person_decisions": 0,
                  "answers": {}, "ledger": [], "admitted": [], "kinds": summary,
                  "exceptions": len(summary), "stopped": broken}  # fmt: skip
        (out / "ONBOARDING.json").write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
        (out / "EXCEPTIONS.md").write_text(exceptions_md(summary, {}), encoding="utf-8")
        return result
    # The production contracts: the same answers, against the app's own
    # store, staged beside the final folder. A kind is kept only when its
    # contracts are the validated ones, connections and windows aside.
    staged = out / ".contracts.staged"
    import shutil

    shutil.rmtree(staged, ignore_errors=True)
    prod = gen.propose(read=read, kinds=kinds, demo_dir=demo_dir, out=staged,
                       actor_params=actor_params, answers=answers)  # fmt: skip
    for k, s in summary.items():
        if s["admitted"] and fingerprint(staged, k) != fingerprint(validated, k):
            s["admitted"] = False
            s["why"] = ("the contracts proposed against the app's own store differ "
                        "from the ones validated in the sandbox")  # fmt: skip
    admitted = sorted(k for k, s in summary.items() if s["admitted"])
    validated_sets: dict[str, list[tuple[list[str], dict[str, int]]]] = {}
    for d in holdout:
        validated_sets.setdefault(d.kind, []).append(field_set(d.values))
    install(staged, out / "contracts", admitted, validated_sets)
    result = {
        "minutes": round((time.time() - t0) / 60, 2),
        "person_decisions": 0,
        "answers": answers,
        "ledger": ledger,
        "admitted": admitted,
        "kinds": summary,
        "exceptions": sum(1 for s in summary.values() if not s["admitted"]),
        "validated_field_sets": validated_sets,
        "sandbox_fk_errors": list(sb.fk_errors),
    }
    (out / "ONBOARDING.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )
    text = exceptions_md(summary, prod)
    if sb.fk_errors:
        text += ("\nForeign keys the sandbox couldn't copy (contracts that depend "
                 "on them differ from production and are not admitted):\n"
                 + "".join(f"- {e}\n" for e in sb.fk_errors))  # fmt: skip
    (out / "EXCEPTIONS.md").write_text(text, encoding="utf-8")
    (out / "VALIDATION.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8"
    )
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m openadapt_flow.generate.zero_touch",
        description="Set up verify contracts with no person decisions: answer the "
        "generator's questions by rule, validate them in a sandbox copy with "
        "held-out writes and planted faults, and admit only what validates.",
    )
    ap.add_argument(
        "--read", required=True, type=Path, help="read-only account (read.yaml)"
    )
    ap.add_argument(
        "--admin",
        required=True,
        type=Path,
        help="admin account that may create the sandbox beside the app's store",
    )
    ap.add_argument(
        "--reader",
        required=True,
        help="the read account to grant on the sandbox: MySQL 'user'@'host', PostgreSQL role",
    )
    ap.add_argument("--kinds", required=True, type=Path)
    ap.add_argument(
        "--demos",
        required=True,
        type=Path,
        help="demonstrations the generator learns from",
    )
    ap.add_argument(
        "--holdout",
        required=True,
        type=Path,
        help="other correct writes, recorded the same way, used only to validate",
    )
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--actor-param", action="append", default=[], help="NAME=VALUE")
    ap.add_argument(
        "--actor-id", required=True, help="the agent's user id as the verifier's Actor"
    )
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args(argv)
    result = onboard(
        read=gen.load_read(args.read), admin=gen.load_read(args.admin), reader=args.reader,
        kinds_path=args.kinds, demo_dir=args.demos, holdout_dir=args.holdout, out=args.out,
        actor_params=gen._parse_actor(args.actor_param), actor_id=args.actor_id,
        keep_sandbox=args.keep_sandbox,
    )  # fmt: skip
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("minutes", "person_decisions", "admitted", "exceptions")
            }
        )
    )
    return 2 if result.get("stopped") else 0


if __name__ == "__main__":
    sys.exit(main())
