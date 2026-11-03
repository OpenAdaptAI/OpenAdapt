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
   Then faults are planted from each held-out write: a wrong value, a wrong
   or look-alike record, a duplicate, a collateral change, a lost write, a
   late change, a changed bookkeeping cell. Every fault must be stopped.
4. **Admission.** A kind is admitted only when all of its held-out writes
   are confirmed and all of its faults are stopped. Anything else is not
   admitted: its tasks stop (``choose`` finds no contract), and
   ``EXCEPTIONS.md`` lists why. That list is what a person may look at; the
   setup needs no answer from them.

The faults are built from the held-out write's recorded diff, the schema
and the task's values only, never from the contracts under test, so the
planter doesn't share the generator's view of what matters.

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
#: At most this many faults of one class per held-out write.
MAX_PER_CLASS = 12


class ZeroTouchError(RuntimeError):
    """The setup can't run as asked (no sandbox, a bad config)."""


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
                    cur.execute(f"CREATE TABLE {self.t(name)} LIKE {src}.{self.q(name)}")
                    cur.execute(
                        f"INSERT INTO {self.t(name)} SELECT * FROM {src}.{self.q(name)}"
                    )
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
                cur.execute(f"GRANT USAGE ON SCHEMA {self.q(self.name)} TO {self.q(reader)}")
                cur.execute(
                    f"GRANT SELECT ON ALL TABLES IN SCHEMA {self.q(self.name)} "
                    f"TO {self.q(reader)}"
                )
        finally:
            conn.close()
        self.schema = store.read_schema(store.connector(self.read_config()), self.dialect)

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

    def _where(self, table: Table, row: Mapping[str, Optional[str]]) -> tuple[str, list[Any]]:
        if not table.primary_key:
            raise ZeroTouchError(f"{table.name} has no primary key; rows can't be planted")
        parts, args = [], []
        for c in table.primary_key:
            v = row.get(c)
            if v is None:
                raise ZeroTouchError(f"{table.name}: a key cell is NULL")
            parts.append(f"{self.q(c)} = %s")
            args.append(v)
        return " AND ".join(parts), args

    def get(self, table: str, row: Mapping[str, Optional[str]]) -> Optional[dict[str, Optional[str]]]:
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

    def max_key(self, table: str) -> Optional[int]:
        t = self._table(table)
        if len(t.primary_key) != 1:
            return None
        col = t.column(t.primary_key[0])
        if col is None or col.kind != "number":
            return None
        got = self._exec(f"SELECT MAX({self.q(col.name)}) FROM {self.t(table)}")
        return int(got[0][0] or 0)

    def put(self, table: str, row: Optional[Mapping[str, Optional[str]]], key: Mapping[str, Optional[str]]) -> None:
        """Make the row with ``key``'s primary key be ``row`` (None: absent)."""

        self._guard()
        t = self._table(table)
        where, args = self._where(t, key)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {self.t(table)} WHERE {where}", tuple(args))
            if row is not None:
                cols = [c for c in t.column_names if c in row]
                over = "" if self.dialect == "mysql" else " OVERRIDING SYSTEM VALUE"
                cur.execute(
                    f"INSERT INTO {self.t(table)} ({', '.join(self.q(c) for c in cols)})"
                    f"{over} VALUES ({', '.join(['%s'] * len(cols))})",
                    tuple(_db_value(t, c, row.get(c)) for c in cols),
                )
        finally:
            conn.close()


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
        out.append(Op(ch.table, dict(ref or {}), dict(ch.before) if ch.before else None))
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


def shift_times(schema: Schema, ops: list[Op], demo: Demo, values_text: set[str]) -> list[Op]:
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
            if when is None or not (lo <= when <= hi) or row.get(c.name) in values_text:
                continue
            row[c.name] = (when + delta).isoformat(sep=" ", timespec="seconds")
        out.append(Op(op.table, op.key, row))
    return out


# -- faults ----------------------------------------------------------------------


@dataclass
class Case:
    """One validation run on a held-out write. ``write`` is put after
    ``begin_write``; ``late`` after a ``CONFIRMED`` and before the recheck.
    ``correct``: the run must be confirmed; else it must be stopped."""

    name: str
    cls: str
    write: list[Op]
    late: list[Op] = field(default_factory=list)
    correct: bool = False


def mutate(table: Table, column: str, text: Optional[str]) -> Optional[str]:
    """Another value of the column's type, plainly different."""

    col = table.column(column)
    kind = col.kind if col else "text"
    if kind == "datetime":
        return FAR_TIME if text != FAR_TIME else "2002-03-04 05:06:07"
    if kind == "date":
        return "2001-02-03" if text != "2001-02-03" else "2002-03-04"
    if kind == "bool":
        return "false" if text in ("true", "1") else "true"
    if kind == "number":
        try:
            n = float(text) if text is not None else 0.0
        except ValueError:
            return None
        return str(int(n) + 7) if n.is_integer() else str(n + 7)
    if kind == "text":
        return (text or "") + " (x)"
    return None


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


def faults(schema: Schema, sb: Sandbox, demo: Demo, write: list[Op]) -> list[Case]:
    """Faults planted from one held-out write: its diff, the schema and its
    task's values. The contracts are never read here."""

    values = _text_values(demo.values)
    out: list[Case] = [Case("lost", "lost_write", [])]
    touched = {op.table for op in write}
    counts: dict[str, int] = {}

    def add(case: Case) -> None:
        n = counts.get(case.cls, 0)
        if n < MAX_PER_CLASS:
            counts[case.cls] = n + 1
            out.append(case)

    for i, op in enumerate(write):
        t = schema.tables[op.table]
        if op.row is None:
            continue
        before = sb.get(op.table, op.key)
        is_new = before is None
        fks = {fk.column: fk for fk in t.foreign_keys}
        for c in t.columns:
            if c.name in t.primary_key:
                continue
            cur = op.row.get(c.name)
            if not is_new and before is not None and before.get(c.name) == cur:
                continue  # not changed by the write: see collateral_target
            if is_new and cur is None:
                continue
            new = None
            if c.name in fks:
                others = [
                    r for r in sb.rows(fks[c.name].table, 20)
                    if r.get(fks[c.name].ref_column) not in (None, cur)
                ]
                new = others[0].get(fks[c.name].ref_column) if others else None
            if new is None:
                new = mutate(t, c.name, cur)
            if new is None:
                continue
            cls = "wrong_value" if _is_value(cur, values) else "bookkeeping"
            row = dict(op.row)
            row[c.name] = new
            add(Case(f"{cls}:{op.table}.{c.name}", cls,
                     write[:i] + [Op(op.table, op.key, row)] + write[i + 1:]))
        if not is_new and before is not None:
            # The write's changes landed on another row of the table.
            changed = {c for c in t.column_names if before.get(c) != op.row.get(c)}
            for other in sb.rows(op.table, 20):
                if store.row_key(t, other) == store.row_key(t, op.key):
                    continue
                moved = dict(other)
                for c in changed:
                    moved[c] = op.row.get(c)
                add(Case(f"wrong_record:{op.table}", "wrong_record",
                         write[:i] + write[i + 1:] + [Op(op.table, other, moved)]))
                break
            # A column the write didn't change, changed on the target.
            for c in t.columns:
                if c.name in t.primary_key or c.name in changed:
                    continue
                new = mutate(t, c.name, op.row.get(c.name))
                if new is None:
                    continue
                row = dict(op.row)
                row[c.name] = new
                add(Case(f"collateral_target:{op.table}.{c.name}", "collateral_target",
                         write[:i] + [Op(op.table, op.key, row)] + write[i + 1:]))
        if is_new:
            top = sb.max_key(op.table)
            if top is not None:
                pk = t.primary_key[0]
                dup = dict(op.row)
                dup[pk] = str(top + 1000)
                add(Case(f"duplicate:{op.table}", "duplicate",
                         write + [Op(op.table, {pk: dup[pk]}, dup)]))
            # The values landed on an existing look-alike instead.
            for other in sb.rows(op.table, 20):
                look = dict(op.row)
                for c in t.primary_key:
                    look[c] = other.get(c)
                add(Case(f"look_alike:{op.table}", "look_alike",
                         write[:i] + write[i + 1:] + [Op(op.table, other, look)]))
                break
    # Another row of a touched table, and a new row in an untouched one.
    for name in sorted(touched):
        t = schema.tables[name]
        mine = {store.row_key(t, op.key) for op in write if op.table == name}
        for other in sb.rows(name, 20):
            if store.row_key(t, other) in mine:
                continue
            for c in t.columns:
                if c.name in t.primary_key:
                    continue
                new = mutate(t, c.name, other.get(c.name))
                if new is None:
                    continue
                row = dict(other)
                row[c.name] = new
                add(Case(f"collateral_row:{name}.{c.name}", "collateral_row",
                         write + [Op(name, other, row)]))
                add(Case(f"late:{name}.{c.name}", "late_write",
                         list(write), late=[Op(name, other, row)]))
                break
            break
    for name in sorted(set(schema.tables) - touched):
        top = sb.max_key(name)
        sample = sb.rows(name, 1)
        if top is None or not sample:
            continue
        row = dict(sample[0])
        pk = schema.tables[name].primary_key[0]
        row[pk] = str(top + 1000)
        add(Case(f"extra_row:{name}", "extra_row", write + [Op(name, {pk: row[pk]}, row)]))
        if counts.get("extra_row", 0) >= 2:
            break
    return out


# -- running a case ---------------------------------------------------------------


@dataclass
class Outcome:
    case: str
    cls: str
    correct: bool
    outcome: str
    passed: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Confirmed when correct; stopped when a fault."""

        return self.passed if self.correct else not self.passed


def _put_all(sb: Sandbox, ops: Sequence[Op]) -> None:
    for op in ops:
        sb.put(op.table, op.row, op.key)


def run_case(
    sb: Sandbox,
    manifest: Path,
    demo: Demo,
    case: Case,
    actor_id: str,
    extra: Mapping[str, Any],
) -> Outcome:
    """Bring the held-out write's rows back to before it, then run the case
    between ``begin_write`` and ``verify_write`` (and the recheck), then put
    every touched row back."""

    from openadapt_flow.verifier import Actor, begin_write, recheck, verify_write

    undo = backward(demo.diff.changes)
    all_ops = undo + case.write + case.late
    saved: dict[tuple[str, str], tuple[dict[str, Optional[str]], Optional[dict[str, Optional[str]]]]] = {}
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
            return Outcome(case.name, case.cls, case.correct, "NO_CONTRACT", False, [str(exc)])
        actor = Actor("agent", actor_id)
        pending = begin_write(contract, params, actor=actor, ledger=None,
                              write_key=f"zt-{time.time_ns()}")  # fmt: skip
        _put_all(sb, case.write)
        r = verify_write(pending)
        outcome = str(getattr(r.outcome, "value", r.outcome))
        reasons = list(r.reasons)
        passed = outcome == "CONFIRMED"
        if passed and getattr(r, "recheck", None) is not None:
            _put_all(sb, case.late)
            rc = recheck(contract, r, params=params, actor=actor, wait=True, ledger=None)
            rc_out = str(getattr(rc.outcome, "value", rc.outcome))
            if rc_out != "CONFIRMED":
                passed, outcome = False, f"RECHECK_{rc_out}"
                reasons += list(rc.reasons)
        elif passed and case.late:
            passed, outcome = False, "NO_RECHECK"
            reasons.append("the contract schedules no recheck, so a late write can't be seen")
            # A contract with no recheck can't stop a late write: count it as
            # a pass of the fault, which fails validation.
            passed = True
        return Outcome(case.name, case.cls, case.correct, outcome, passed, reasons[:6])
    except Exception as exc:  # noqa: BLE001 - a case that can't run admits nothing
        return Outcome(case.name, case.cls, case.correct, "ERROR", False,
                       [f"{type(exc).__name__}: {exc}", traceback.format_exc(limit=3)])
    finally:
        for (table, _), (key, row) in saved.items():
            sb.put(table, row, key)


# -- answers ----------------------------------------------------------------------


def auto_answers(report: Mapping[str, Any], picks: Mapping[str, Mapping[str, int]] = {}) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The answers this module gives, and a ledger saying by which rule."""

    answers: dict[str, Any] = {"now_checks": True}
    ledger: list[dict[str, Any]] = [
        {"key": "now_checks", "answer": True,
         "rule": "an unchecked or excused time stamp of the main record must hold a time near the read"},
    ]
    for q in report.get("questions") or []:
        key = str(q.get("key"))
        proposed = q.get("proposal")
        if key == "recheck_after_s":
            answers[key] = gen.DEFAULT_RECHECK_S
            ledger.append({"key": key, "answer": gen.DEFAULT_RECHECK_S, "rule": "policy default"})
        elif key == "phones":
            answers[key] = list(proposed or [])
            ledger.append({"key": key, "answer": answers[key],
                           "rule": "every listed parameter; wrong digits are planted faults"})
        elif key == "bookkeeping":
            prop = dict(proposed or {}) if isinstance(proposed, Mapping) else {}
            ans = {"accept": True}
            if prop.get("unchecked"):
                ans["unchecked"] = prop["unchecked"]
            answers[key] = ans
            ledger.append({"key": key, "answer": ans,
                           "rule": "the generator's own proposal; nothing added (no logs_add, no columns_add)"})
        elif key.startswith("locations."):
            if key in picks:
                answers[key] = dict(picks[key])
                ledger.append({"key": key, "answer": answers[key],
                               "rule": "the one candidate whose kind validated"})
    return answers, ledger


def location_options(report: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """``locations.<kind>`` -> parameter -> how many candidate places."""

    out: dict[str, dict[str, int]] = {}
    for q in report.get("questions") or []:
        key = str(q.get("key"))
        if not key.startswith("locations."):
            continue
        for name, opts in re.findall(r"`([^`]+)` is in several places: (.*?)(?= `|$)", str(q.get("text"))):
            out.setdefault(key, {})[name] = len(re.findall(r"\[\d+\]", opts))
    return out


# -- the whole setup ----------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(Path(path).read_text()) if path else None


def validate(
    sb: Sandbox,
    manifest: Path,
    holdout: Sequence[Demo],
    actor_id: str,
    extra: Mapping[str, Any],
    log: Callable[[str], None] = lambda s: None,
) -> dict[str, list[Outcome]]:
    """Every held-out write, replayed and faulted, by kind."""

    assert sb.schema is not None
    by_kind: dict[str, list[Outcome]] = {}
    for demo in holdout:
        values = _text_values(demo.values)
        write = shift_times(sb.schema, forward(demo.diff.changes), demo, values)
        cases = [Case("correct", "correct", write, correct=True)]
        # Faults are planted from the store as it was before the write.
        undo = backward(demo.diff.changes)
        saved = [(op, sb.get(op.table, op.key)) for op in undo]
        _put_all(sb, undo)
        try:
            cases += faults(sb.schema, sb, demo, write)
        finally:
            for op, row in saved:
                sb.put(op.table, row, op.key)
        for case in cases:
            res = run_case(sb, manifest, demo, case, actor_id, extra)
            log(f"{demo.name} {case.name}: {res.outcome} {'ok' if res.ok else 'FAIL'}")
            by_kind.setdefault(demo.kind, []).append(res)
    return by_kind


def summarize(by_kind: Mapping[str, Sequence[Outcome]], kinds: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in kinds:
        rs = list(by_kind.get(k) or [])
        correct = [r for r in rs if r.correct]
        fl = [r for r in rs if not r.correct]
        out[k] = {
            "admitted": bool(correct) and all(r.ok for r in rs),
            "correct_writes": len(correct),
            "false_stops": [f"{r.outcome}: {'; '.join(r.reasons[:2])}" for r in correct if not r.ok],
            "faults": len(fl),
            "faults_stopped": sum(1 for r in fl if r.ok),
            "faults_passed": [r.case for r in fl if not r.ok],
            "by_class": {
                c: f"{sum(1 for r in fl if r.cls == c and r.ok)}/{sum(1 for r in fl if r.cls == c)}"
                for c in sorted({r.cls for r in fl})
            },
        }
        if not rs:
            out[k]["why"] = "no held-out write of this kind"
    return out


def exceptions_md(summary: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    lines = ["# What setup could not validate", "",
             "No person decision was asked for. The kinds below are not admitted, so",
             "their tasks stop (no contract). Each line says why.", ""]
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
            lines.append(f"   - a planted fault passed: {f}")
        for r in ((report.get("kinds") or {}).get(k) or {}).get("refused") or []:
            lines.append(f"   - generator: {r}")
    if n == 0:
        lines.append("Every kind is admitted.")
    return "\n".join(lines) + "\n"


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
    holdout = gen.load_demos(holdout_dir)
    schema = store.read_schema(store.connector(read), store.dialect_of(read))
    sb = Sandbox(admin, read)
    sb.create(schema, reader)
    sread = sb.read_config()
    work = out / "sandbox"
    try:
        report = gen.propose(read=sread, kinds=kinds, demo_dir=demo_dir, out=work / "q",
                             actor_params=actor_params, answers={})  # fmt: skip
        options = location_options(report)
        trials: list[dict[str, dict[str, int]]] = [{}]
        for key, params in options.items():
            for name, n in params.items():
                trials += [{key: {name: i}} for i in range(n)]
        kind_names = [k.kind for k in kinds]
        best: dict[str, Any] = {}
        results: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for picks in trials:
            answers, ledger = auto_answers(report, picks)
            sans = {**answers, "recheck_after_s": SANDBOX_RECHECK_S}
            tag = "base" if not picks else "pick-" + "-".join(
                f"{k}.{p}.{i}" for k, d in picks.items() for p, i in d.items())
            log(f"propose ({tag})")
            rep = gen.propose(read=sread, kinds=kinds, demo_dir=demo_dir, out=work / tag,
                              actor_params=actor_params, answers=sans)  # fmt: skip
            by_kind = validate(sb, work / tag / "manifest.yaml", holdout, actor_id,
                               dict(actor_params), log)  # fmt: skip
            summary = summarize(by_kind, kind_names)
            results.append((picks, answers, summary))
            best = best or {"answers": answers, "ledger": ledger, "summary": summary, "report": rep}
        # A location pick is kept when exactly one candidate admits its kind.
        final_picks: dict[str, dict[str, int]] = {}
        for key, params in options.items():
            kind = key.split(".", 1)[1]
            for name in params:
                ok = [p[key][name] for p, _, s in results
                      if key in p and name in p[key] and s[kind]["admitted"]]
                if len(ok) == 1:
                    final_picks.setdefault(key, {})[name] = ok[0]
        answers, ledger = auto_answers(report, final_picks)
        if final_picks:
            sans = {**answers, "recheck_after_s": SANDBOX_RECHECK_S}
            rep = gen.propose(read=sread, kinds=kinds, demo_dir=demo_dir, out=work / "final",
                              actor_params=actor_params, answers=sans)  # fmt: skip
            summary = summarize(validate(sb, work / "final" / "manifest.yaml", holdout,
                                         actor_id, dict(actor_params), log), kind_names)  # fmt: skip
        else:
            summary, rep = best["summary"], best["report"]
    finally:
        if not keep_sandbox:
            sb.drop()
    # The production contracts: the same answers, against the app's own store.
    prod = gen.propose(read=read, kinds=kinds, demo_dir=demo_dir, out=out / "contracts",
                       actor_params=actor_params, answers=answers)  # fmt: skip
    manifest_path = out / "contracts" / "manifest.yaml"
    manifest = _load_yaml(manifest_path)
    admitted = sorted(k for k, s in summary.items() if s["admitted"])
    for k in list((manifest.get("kinds") or {})):
        if k not in admitted:
            manifest["kinds"].pop(k)
    manifest["zero_touch"] = {"admitted": admitted}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    result = {
        "minutes": round((time.time() - t0) / 60, 2),
        "person_decisions": 0,
        "answers": answers,
        "ledger": ledger,
        "admitted": admitted,
        "kinds": summary,
        "exceptions": sum(1 for s in summary.values() if not s["admitted"]),
    }
    (out / "ONBOARDING.json").write_text(json.dumps(result, indent=2, default=str))
    (out / "EXCEPTIONS.md").write_text(exceptions_md(summary, prod))
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m openadapt_flow.generate.zero_touch",
        description="Set up verify contracts with no person decisions: answer the "
        "generator's questions by rule, validate them in a sandbox copy with "
        "held-out writes and planted faults, and admit only what validates.",
    )
    ap.add_argument("--read", required=True, type=Path, help="read-only account (read.yaml)")
    ap.add_argument("--admin", required=True, type=Path,
                    help="admin account that may create the sandbox beside the app's store")
    ap.add_argument("--reader", required=True,
                    help="the read account to grant on the sandbox: MySQL 'user'@'host', PostgreSQL role")
    ap.add_argument("--kinds", required=True, type=Path)
    ap.add_argument("--demos", required=True, type=Path, help="demonstrations the generator learns from")
    ap.add_argument("--holdout", required=True, type=Path,
                    help="other correct writes, recorded the same way, used only to validate")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--actor-param", action="append", default=[], help="NAME=VALUE")
    ap.add_argument("--actor-id", required=True, help="the agent's user id as the verifier's Actor")
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args(argv)
    result = onboard(
        read=gen.load_read(args.read), admin=gen.load_read(args.admin), reader=args.reader,
        kinds_path=args.kinds, demo_dir=args.demos, holdout_dir=args.holdout, out=args.out,
        actor_params=gen._parse_actor(args.actor_param), actor_id=args.actor_id,
        keep_sandbox=args.keep_sandbox,
    )  # fmt: skip
    print(json.dumps({k: result[k] for k in ("minutes", "person_decisions", "admitted", "exceptions")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
