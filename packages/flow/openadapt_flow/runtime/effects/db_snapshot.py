"""Snapshot a whole database for the frame: every table, every column.

A frame that compares only the surfaces a contract reads can't see a write
to anything else: a column the effects don't name, a field on a side row, a
table nobody thought to read (#1580's held-out misses). ``frame.database``
turns the frame into a diff of the whole database instead. This module takes
the snapshot; ``frame.judge_frame`` compares two of them like any other
slice.

The snapshot costs three statements however many tables there are, so a
database reached through a slow channel (``docker exec``) is still read in a
few seconds:

1. the schema: every table's columns, primary key and column defaults;
2. every table's row count (one ``UNION ALL``);
3. every row of every table at or under ``max_rows``, each row one JSON
   object (one ``UNION ALL``). Binary values are hex.

A table over ``max_rows`` (a translation table, a code set) is fingerprinted
instead (``CHECKSUM TABLE`` on MySQL and MariaDB; a hash of its rows on
SQLite): a change shows that it moved, without which rows. Tables in
``allow_tables`` are not read at all; that list is the reviewed statement of
which tables the app writes on its own.

Read-only: SQLite files open with ``mode=ro``; a driver connection should use
a SELECT-only account.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Dialect = Literal["sqlite", "mysql"]

#: Values a column holds when nothing set it.
_EMPTY = {"", "0", "0.0", "0.00", "0000-00-00", "0000-00-00 00:00:00", "NULL"}


@dataclass
class Table:
    columns: list[str]
    primary_key: list[str]
    #: Column -> its declared default, as text (None when it has none).
    defaults: dict[str, Optional[str]]
    count: int = 0
    #: The rows, or None for a table over ``max_rows`` (``digest`` then).
    rows: Optional[list[dict[str, Any]]] = None
    digest: Optional[str] = None


@dataclass
class Snapshot:
    tables: dict[str, Table] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            name: {
                "columns": t.columns,
                "primary_key": t.primary_key,
                "defaults": t.defaults,
                "count": t.count,
                "rows": t.rows,
                "digest": t.digest,
            }
            for name, t in self.tables.items()
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Snapshot":
        return cls(
            tables={
                name: Table(
                    columns=list(t["columns"]),
                    primary_key=list(t["primary_key"]),
                    defaults=dict(t["defaults"]),
                    count=int(t["count"]),
                    rows=None if t["rows"] is None else list(t["rows"]),
                    digest=t.get("digest"),
                )
                for name, t in data.items()
            }
        )


#: A column default meaning any value: the row's own key, which every new
#: row sets.
ANY_VALUE = "\x00any"


def is_default(value: Any, default: Optional[str]) -> bool:
    """Does a new row's ``value`` look like nothing set it?"""

    if default == ANY_VALUE or value is None:
        return True
    text = str(value).strip()
    if text in _EMPTY:
        return True
    if default is None:
        return False
    return text == default.strip().strip("'\"")


def _quote(dialect: Dialect, name: str) -> str:
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def _literal(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _rows(cursor: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in cursor.fetchall()]


def _query(connect: Callable[[], Any], sql: str) -> list[tuple[Any, ...]]:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(sql)
        return _rows(cur)
    finally:
        con.close()


def _schema(
    connect: Callable[[], Any], dialect: Dialect
) -> dict[str, tuple[list[str], list[str], dict[str, Optional[str]], set[str]]]:
    """Table -> (columns, primary key, defaults, binary columns)."""

    out: dict[str, tuple[list[str], list[str], dict[str, Optional[str]], set[str]]]
    out = {}
    if dialect == "mysql":
        rows = _query(
            connect,
            "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.COLUMN_KEY, c.COLUMN_DEFAULT, "
            "c.DATA_TYPE FROM information_schema.COLUMNS c JOIN "
            "information_schema.TABLES t ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND "
            "t.TABLE_NAME = c.TABLE_NAME WHERE c.TABLE_SCHEMA = DATABASE() AND "
            "t.TABLE_TYPE = 'BASE TABLE' ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION",
        )
        for table, column, key, default, data_type in rows:
            cols, pk, defaults, binary = out.setdefault(str(table), ([], [], {}, set()))
            cols.append(str(column))
            if str(key or "") == "PRI":
                pk.append(str(column))
            defaults[str(column)] = None if default is None else str(default)
            if "binary" in str(data_type).lower() or "blob" in str(data_type).lower():
                binary.add(str(column))
        return out
    tables = [
        str(r[0])
        for r in _query(
            connect,
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )
    ]
    for table in tables:
        info = _query(connect, f"PRAGMA table_info({_quote(dialect, table)})")
        cols = [str(r[1]) for r in info]
        pk = [str(r[1]) for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        defaults = {str(r[1]): None if r[4] is None else str(r[4]) for r in info}
        out[table] = (cols, pk, defaults, set())
    return out


def _cell(dialect: Dialect, column: str, binary: bool) -> str:
    q = _quote(dialect, column)
    if dialect == "mysql":
        return f"HEX({q})" if binary else q
    return f"CASE WHEN typeof({q}) = 'blob' THEN hex({q}) ELSE {q} END"


#: Columns per JSON object call: SQLite caps a function at 127 arguments.
_CHUNK = 50


def _row_json(dialect: Dialect, cols: Sequence[str], binary: set[str]) -> list[str]:
    """One row as JSON objects of at most ``_CHUNK`` columns each.

    Merged in Python, not in SQL: a JSON merge patch drops null values.
    """

    obj = "JSON_OBJECT" if dialect == "mysql" else "json_object"
    return [
        obj
        + "("
        + ", ".join(
            f"{_literal(c)}, {_cell(dialect, c, c in binary)}"
            for c in cols[i : i + _CHUNK]
        )
        + ")"
        for i in range(0, len(cols), _CHUNK)
    ]


def take_snapshot(
    connect: Callable[[], Any],
    dialect: Dialect,
    *,
    skip: Sequence[str] = (),
    max_rows: int = 20000,
) -> Snapshot:
    """Read every table except ``skip``. Raises when the database can't be read."""

    schema = _schema(connect, dialect)
    names = sorted(t for t in schema if t not in set(skip))
    snap = Snapshot()
    if not names:
        return snap
    counts = {
        str(t): int(n)
        for t, n in _query(
            connect,
            " UNION ALL ".join(
                f"SELECT {_literal(t)}, COUNT(*) FROM {_quote(dialect, t)}"
                for t in names
            ),
        )
    }
    small = [t for t in names if 0 < counts.get(t, 0) <= max_rows]
    large = [t for t in names if counts.get(t, 0) > max_rows]
    for t in names:
        cols, pk, defaults, _binary = schema[t]
        snap.tables[t] = Table(
            columns=cols,
            primary_key=pk,
            defaults=defaults,
            count=counts.get(t, 0),
            rows=None if t in large else [],
        )
    if small:
        parts = {t: _row_json(dialect, schema[t][0], schema[t][3]) for t in small}
        width = max(len(p) for p in parts.values())
        selects = []
        for t in small:
            padded = parts[t] + ["NULL"] * (width - len(parts[t]))
            cells = ", ".join(f"{c} AS r{i}" for i, c in enumerate(padded))
            selects.append(
                f"SELECT {_literal(t)} AS t, {cells} FROM {_quote(dialect, t)}"
            )
        for t, *chunks in _query(connect, " UNION ALL ".join(selects)):
            rows = snap.tables[str(t)].rows
            assert rows is not None
            row: dict[str, Any] = {}
            for chunk in chunks:
                if chunk is not None:
                    row.update(json.loads(chunk) if isinstance(chunk, str) else chunk)
            rows.append(row)
    if large:
        if dialect == "mysql":
            for t, checksum in _query(
                connect,
                "CHECKSUM TABLE " + ", ".join(_quote(dialect, t) for t in large),
            ):
                name = str(t).split(".")[-1]
                if name in snap.tables:
                    snap.tables[name].digest = str(checksum)
        else:
            for t in large:
                h = hashlib.sha256()
                for row in _query(connect, f"SELECT * FROM {_quote(dialect, t)}"):
                    h.update(repr(row).encode())
                snap.tables[t].digest = h.hexdigest()
    return snap


def row_key(table: Table, row: Mapping[str, Any]) -> Optional[str]:
    """A row's identity: its primary key, or None when the table has none."""

    if not table.primary_key:
        return None
    return "|".join(str(row.get(c)) for c in table.primary_key)


def keyed_rows(table: Table) -> list[dict[str, Any]]:
    """The rows, each carrying ``_key``: the primary key, or for a table with
    none, the row's own content numbered by occurrence (so a doubled
    identical row still counts twice)."""

    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for row in table.rows or []:
        key = row_key(table, row)
        if key is None:
            body = json.dumps(row, sort_keys=True, default=str)
            digest = hashlib.sha256(body.encode()).hexdigest()[:16]
            seen[digest] = seen.get(digest, 0) + 1
            key = f"{digest}#{seen[digest]}"
        out.append({**row, KEY: key})
    return out


#: The synthetic identity field of a database slice's records.
KEY = "__row_key__"
