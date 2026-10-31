"""Read a SQL database's schema and rows for the contract generator.

The generator (:mod:`openadapt_flow.generate.verify_contract`) needs two
things from the store a write lands in:

- the **schema**, from ``information_schema`` (MySQL, MariaDB, PostgreSQL):
  every base table, its columns with their types, defaults and nullability,
  the primary key, declared foreign keys, and the columns the database fills
  on its own (auto-increment keys, ``ON UPDATE CURRENT_TIMESTAMP``);
- **row snapshots** before and after a demonstration write, so a diff shows
  which tables, rows and columns one kind of write touches.

Everything is read through the read-only connection the contract will use
(``sql_driver``, ``sql_connect_args``, ``sql_password_env``), with the
driver's read-only session switched on. Nothing here writes, and nothing
calls a model.

A snapshot holds the rows' values. It is written only into the generator's
own working directory, which the person keeps private, like any database
export.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import importlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

#: Dialects this module reads.
DIALECTS = ("mysql", "postgresql")

#: Above this many rows a table is snapshotted by count and largest key only;
#: a demonstration diff can't say which of its rows changed.
DEFAULT_MAX_ROWS = 20000


class StoreError(RuntimeError):
    """The store can't be read as the generator needs."""


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool
    default: Optional[str]
    #: The database fills it on insert (auto-increment, a sequence, identity).
    auto: bool = False
    #: The database rewrites it on every update (``ON UPDATE CURRENT_TIMESTAMP``).
    on_update: bool = False

    @property
    def kind(self) -> str:
        """``text``, ``number``, ``date``, ``datetime``, ``time``, ``bool``,
        ``binary`` or ``other``."""

        t = self.data_type.lower()
        if t in ("tinyint(1)", "boolean", "bool"):
            return "bool"
        if any(
            t.startswith(p)
            for p in (
                "int",
                "tinyint",
                "smallint",
                "mediumint",
                "bigint",
                "integer",
                "decimal",
                "numeric",
                "float",
                "double",
                "real",
            )
        ):
            return "number"
        if t == "date":
            return "date"
        if t.startswith(("datetime", "timestamp")):
            return "datetime"
        if t.startswith("time"):
            return "time"
        if any(
            t.startswith(p)
            for p in ("char", "varchar", "text", "tinytext", "mediumtext", "longtext")
        ) or t in ("character varying", "character", "citext", "enum", "set"):
            return "text"
        if t.startswith(("character varying", "character")):
            return "text"
        if any(t.startswith(p) for p in ("blob", "binary", "varbinary", "bytea")):
            return "binary"
        return "other"


@dataclass
class ForeignKey:
    column: str
    table: str
    ref_column: str


@dataclass
class Table:
    name: str
    columns: list[Column]
    primary_key: list[str]
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    def column(self, name: str) -> Optional[Column]:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class Schema:
    dialect: str
    database: str
    tables: dict[str, Table]
    #: Views the read account sees, by name (they are read, never written).
    views: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "database": self.database,
            "views": list(self.views),
            "tables": {n: asdict(t) for n, t in self.tables.items()},
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Schema":
        tables = {}
        for name, t in dict(data["tables"]).items():
            tables[name] = Table(
                name=t["name"],
                columns=[Column(**c) for c in t["columns"]],
                primary_key=list(t["primary_key"]),
                foreign_keys=[ForeignKey(**f) for f in t.get("foreign_keys", [])],
            )
        return cls(
            str(data["dialect"]),
            str(data["database"]),
            tables,
            list(data.get("views", [])),
        )


def dialect_of(read: Mapping[str, Any]) -> str:
    """``mysql`` or ``postgresql``, from the read's driver."""

    driver = str(read.get("sql_driver") or "")
    if driver in ("pymysql", "MySQLdb", "mysql.connector"):
        return "mysql"
    if driver in ("psycopg2", "psycopg"):
        return "postgresql"
    raise StoreError(
        f"sql_driver {driver!r}: the generator reads MySQL/MariaDB (pymysql) "
        "or PostgreSQL (psycopg2)"
    )


def connector(read: Mapping[str, Any]) -> Callable[[], Any]:
    """A function that opens one read-only connection, as the contract will."""

    driver = str(read.get("sql_driver") or "")
    dialect_of(read)
    module = importlib.import_module(driver)
    args = dict(read.get("sql_connect_args") or {})
    env = read.get("sql_password_env")
    if env:
        if env not in os.environ:
            raise StoreError(
                f"environment variable {env} (the read password) is not set"
            )
        args["password"] = os.environ[str(env)]

    def connect() -> Any:
        from openadapt_flow.runtime.effects.sql import read_only_connection

        conn = module.connect(**args)
        return read_only_connection(conn, driver)

    return connect


def _rows(cur: Any, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    cur.execute(sql, tuple(params)) if params else cur.execute(sql)
    return [tuple(r) for r in cur.fetchall()]


def read_schema(connect: Callable[[], Any], dialect: str) -> Schema:
    """Every base table the read account sees, with columns, keys and FKs."""

    conn = connect()
    try:
        cur = conn.cursor()
        if dialect == "mysql":
            return _mysql_schema(cur)
        if dialect == "postgresql":
            return _pg_schema(cur)
        raise StoreError(f"dialect {dialect!r} is not one of {DIALECTS}")
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def _mysql_schema(cur: Any) -> Schema:
    (db,) = _rows(cur, "SELECT DATABASE()")[0]
    if not db:
        raise StoreError("the connection names no database (sql_connect_args.database)")
    kinds = dict(
        _rows(
            cur,
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s",
            (db,),
        )
    )
    cols = _rows(
        cur,
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, "
        "COLUMN_DEFAULT, EXTRA, COLUMN_KEY FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION",
        (db,),
    )
    keys = _rows(
        cur,
        "SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_NAME, "
        "REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, CONSTRAINT_NAME, "
        "ORDINAL_POSITION",
        (db,),
    )
    tables: dict[str, Table] = {}
    for tname, cname, ctype, dtype, nullable, default, extra, _key in cols:
        if kinds.get(tname) != "BASE TABLE":
            continue
        extra = str(extra or "").lower()
        ctype = str(ctype).lower()
        t = tables.setdefault(tname, Table(tname, [], []))
        t.columns.append(
            Column(
                name=str(cname),
                data_type="tinyint(1)"
                if ctype.startswith("tinyint(1)")
                else str(dtype).lower(),
                nullable=str(nullable).upper() == "YES",
                default=None if default is None else str(default),
                auto="auto_increment" in extra,
                on_update="on update" in extra,
            )
        )
    for tname, cname, cons, rtable, rcol in keys:
        kt = tables.get(tname)
        if kt is None:
            continue
        t = kt
        if cons == "PRIMARY":
            t.primary_key.append(str(cname))
        elif rtable:
            t.foreign_keys.append(ForeignKey(str(cname), str(rtable), str(rcol)))
    views = sorted(n for n, k in kinds.items() if k == "VIEW")
    return Schema("mysql", str(db), tables, views)


def _pg_schema(cur: Any) -> Schema:
    (schema,) = _rows(cur, "SELECT current_schema()")[0]
    (db,) = _rows(cur, "SELECT current_database()")[0]
    kinds = dict(
        _rows(
            cur,
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = %s",
            (schema,),
        )
    )
    cols = _rows(
        cur,
        "SELECT table_name, column_name, data_type, is_nullable, column_default, "
        "is_identity FROM information_schema.columns WHERE table_schema = %s "
        "ORDER BY table_name, ordinal_position",
        (schema,),
    )
    tables: dict[str, Table] = {}
    for tname, cname, dtype, nullable, default, identity in cols:
        if kinds.get(tname) != "BASE TABLE":
            continue
        t = tables.setdefault(tname, Table(tname, [], []))
        default_s = None if default is None else str(default)
        t.columns.append(
            Column(
                name=str(cname),
                data_type=str(dtype).lower(),
                nullable=str(nullable).upper() == "YES",
                default=default_s,
                auto=(default_s or "").startswith("nextval(")
                or str(identity).upper() == "YES",
            )
        )
    # pg_catalog: information_schema hides constraints of tables the account
    # doesn't own, and the read account owns none.
    cons = _rows(
        cur,
        "SELECT c.conrelid::regclass::text, c.contype, a.attname, "
        "c.confrelid::regclass::text, fa.attname "
        "FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
        "LEFT JOIN pg_attribute fa ON fa.attrelid = c.confrelid "
        "AND fa.attnum = c.confkey[k.ord] "
        "WHERE n.nspname = %s AND c.contype IN ('p', 'f') "
        "ORDER BY 1, c.conname, k.ord",
        (schema,),
    )
    for tname, ctype, cname, rtable, rcol in cons:
        ct = tables.get(str(tname).split(".")[-1].strip('"'))
        if ct is None:
            continue
        t = ct
        if ctype == "p":
            t.primary_key.append(str(cname))
        elif rtable and rcol:
            t.foreign_keys.append(
                ForeignKey(str(cname), str(rtable).split(".")[-1].strip('"'), str(rcol))
            )
    views = sorted(n for n, k in kinds.items() if k == "VIEW")
    return Schema("postgresql", str(db), tables, views)


def quote(dialect: str, name: str) -> str:
    """A quoted identifier. Names with a quote character are refused."""

    if "`" in name or '"' in name or "\x00" in name:
        raise StoreError(f"identifier {name!r} holds a quote character")
    return f"`{name}`" if dialect == "mysql" else f'"{name}"'


def cell_text(value: Any) -> Optional[str]:
    """One cell as text, the way the generator compares values: ``None``
    stays ``None``; bytes are ``0x`` + hex; dates are ISO; a Decimal keeps its
    stored digits."""

    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


@dataclass
class TableRows:
    #: Row key (the primary key's values joined by ``|``, or the whole row
    #: for a table without one) -> column -> text.
    rows: dict[str, dict[str, Optional[str]]]
    #: Set when the table had more rows than the snapshot keeps.
    count_only: Optional[int] = None


def row_key(table: Table, row: Mapping[str, Optional[str]]) -> str:
    cols = table.primary_key or table.column_names
    return "|".join("\x00" if row.get(c) is None else str(row.get(c)) for c in cols)


def snapshot(
    connect: Callable[[], Any],
    schema: Schema,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> dict[str, TableRows]:
    """Every table's rows as text, in one read-only transaction."""

    conn = connect()
    out: dict[str, TableRows] = {}
    try:
        cur = conn.cursor()
        if schema.dialect == "mysql":
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        else:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        for name in sorted(schema.tables):
            table = schema.tables[name]
            q = quote(schema.dialect, name)
            (count,) = _rows(cur, f"SELECT COUNT(*) FROM {q}")[0]
            if int(count) > max_rows:
                out[name] = TableRows({}, count_only=int(count))
                continue
            cols = table.column_names
            cur.execute(
                "SELECT "
                + ", ".join(quote(schema.dialect, c) for c in cols)
                + f" FROM {q}"
            )
            rows: dict[str, dict[str, Optional[str]]] = {}
            for raw in cur.fetchall():
                row = {c: cell_text(v) for c, v in zip(cols, raw)}
                key = row_key(table, row)
                while key in rows:  # identical rows in a table without a key
                    key += "+"
                rows[key] = row
            out[name] = TableRows(rows)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
    return out


def save_snapshot(path: Path, rows: Mapping[str, TableRows]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {n: {"rows": t.rows, "count_only": t.count_only} for n, t in rows.items()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_snapshot(path: Path) -> dict[str, TableRows]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {n: TableRows(dict(t["rows"]), t.get("count_only")) for n, t in data.items()}


@dataclass
class RowChange:
    table: str
    key: str
    kind: str  # added | removed | changed
    before: Optional[dict[str, Optional[str]]]
    after: Optional[dict[str, Optional[str]]]

    @property
    def changed_columns(self) -> list[str]:
        if self.before is None or self.after is None:
            return []
        return sorted(
            c
            for c in set(self.before) | set(self.after)
            if self.before.get(c) != self.after.get(c)
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Diff:
    changes: list[RowChange]
    #: Tables too large to diff row by row whose count changed.
    unread: dict[str, tuple[int, int]] = field(default_factory=dict)

    def tables(self) -> list[str]:
        return sorted({c.table for c in self.changes} | set(self.unread))

    def of(self, table: str, kind: Optional[str] = None) -> list[RowChange]:
        return [
            c
            for c in self.changes
            if c.table == table and (kind is None or c.kind == kind)
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            "changes": [c.to_json() for c in self.changes],
            "unread": {k: list(v) for k, v in self.unread.items()},
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Diff":
        return cls(
            [RowChange(**c) for c in data.get("changes", [])],
            {
                k: (int(v[0]), int(v[1]))
                for k, v in dict(data.get("unread", {})).items()
            },
        )


def diff(before: Mapping[str, TableRows], after: Mapping[str, TableRows]) -> Diff:
    """Rows added, removed or changed, table by table."""

    changes: list[RowChange] = []
    unread: dict[str, tuple[int, int]] = {}
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if a is None or b is None:
            unread[name] = (
                0 if a is None else len(a.rows),
                0 if b is None else len(b.rows),
            )
            continue
        if a.count_only is not None or b.count_only is not None:
            ca = a.count_only if a.count_only is not None else len(a.rows)
            cb = b.count_only if b.count_only is not None else len(b.rows)
            if ca != cb:
                unread[name] = (ca, cb)
            continue
        for key in sorted(set(a.rows) | set(b.rows)):
            ra, rb = a.rows.get(key), b.rows.get(key)
            if ra == rb:
                continue
            kind = "added" if ra is None else "removed" if rb is None else "changed"
            changes.append(RowChange(name, key, kind, ra, rb))
    return Diff(changes, unread)
