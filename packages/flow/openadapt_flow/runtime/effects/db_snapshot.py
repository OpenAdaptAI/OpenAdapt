"""Snapshot a whole database for the frame: every table, every column.

A frame that compares only the surfaces a contract reads can't see a write
to anything else: a column the effects don't name, a field on a side row, a
table nobody thought to read. ``frame.database`` turns the frame into a diff
of the whole database. This module takes the snapshot; ``frame.judge_frame``
compares two of them like any other slice.

What a snapshot holds, and why:

- **No values on disk.** Each cell is a keyed hash (keyed BLAKE2b with the
  run's salt), so a baseline is not a copy of the records. Plain values are
  read only for rows the save added, and held in memory to judge them.
- **One consistent read.** Everything is read on one connection inside one
  read-only transaction (``START TRANSACTION WITH CONSISTENT SNAPSHOT, READ
  ONLY`` on MySQL and MariaDB; ``BEGIN`` on SQLite), so a row committed
  mid-snapshot is never half-seen. The audit log is read in the same
  transaction (``extra_queries``).
- **Large tables by bucket.** A table over ``max_rows`` with a single
  integer key is digested in buckets of 1024 keys (salted SHA-256 of its rows'
  hashes). Rows the save adds sit above the baseline's largest key and are
  read in full; a changed bucket below it means an existing row moved.
  A large table without an integer key gets one digest.
- **Schema and code.** Each table's definition (column types, defaults,
  nullability, extras) and the database's triggers, views, routines and
  events are digested; a change is reported, never ignored.
- **Counters.** ``sqlite_sequence`` and ``AUTO_INCREMENT`` show a row that
  was written and deleted inside the window.
- **The clock.** ``CURRENT_TIMESTAMP`` at the snapshot, so a column the
  database fills with the time can be held to the window.
- **Allow-listed tables** are append-only by default: their existing rows
  must not change (the audit log is the attribution channel). ``skip`` mode
  reads nothing and needs a stated reason.

Read-only: SQLite files open with ``mode=ro``; a driver connection should use
a SELECT-only account with SELECT on every schema the app can write.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional

Dialect = Literal["sqlite", "mysql"]
TableMode = Literal["compare", "append_only", "skip"]

#: Keys per bucket in a large table's digest.
BUCKET = 1024
#: Columns per JSON object call: SQLite caps a function at 127 arguments.
_CHUNK = 50
#: The synthetic identity field of a database slice's records.
KEY = "__row_key__"
#: A column default meaning any value: the table's own generated key.
ANY_VALUE = "\x00any"
#: A column default the database computes as the current time at insert.
CLOCK_DEFAULT = "\x00clock"
_SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")


class SnapshotError(RuntimeError):
    """The database can't be snapshotted as the frame needs."""


# -- hashing ------------------------------------------------------------------


def canonical(value: Any) -> str:
    """One text form per value, so equal values hash equally."""

    if value is None:
        return "\x00null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        return format(number.normalize(), "f") if number == number else str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


#: Hex characters per cell MAC (32 bits). A cell's MAC is only compared
#: with the same cell's MAC, and is keyed, so it can't be forged; the row
#: key MAC is 128 bits so distinct rows never share one.
CELL_HEX = 8


def cell_hash(key: bytes, value: Any) -> str:
    """A cell's keyed MAC (BLAKE2b, 32 bits)."""

    return hashlib.blake2b(
        canonical(value).encode(), digest_size=CELL_HEX // 2, key=key[:64]
    ).hexdigest()


def row_key_hash(key: bytes, text: str) -> str:
    """A row key's keyed MAC (BLAKE2b, 128 bits)."""

    return hashlib.blake2b(text.encode(), digest_size=16, key=key[:64]).hexdigest()


def unpack(columns: Sequence[str], packed: str) -> dict[str, str]:
    """A stored row's cell MACs by column (stored as one string per row)."""

    return {c: packed[i * CELL_HEX : (i + 1) * CELL_HEX] for i, c in enumerate(columns)}


# -- shapes -------------------------------------------------------------------


@dataclass
class Table:
    mode: TableMode
    #: Digest of the table's definition: column names, types, defaults,
    #: nullability and extras.
    schema: str
    columns: list[str]
    primary_key: list[str]
    #: The single integer key the database generates, when there is one.
    generated_key: Optional[str]
    #: Column -> declared default as text (None: no default). Schema, not data.
    defaults: dict[str, Optional[str]]
    count: int = 0
    #: ``sqlite_sequence`` or ``AUTO_INCREMENT``, when the table has one.
    counter: Optional[int] = None
    #: The largest generated key (None for an empty table or no such key).
    max_key: Optional[int] = None
    #: Row key MAC -> the row's cell MACs in column order, concatenated
    #: (:func:`unpack`), for a table read row by row.
    rows: Optional[dict[str, str]] = None
    #: Bucket -> digest of the rows at or below ``max_key`` (large and
    #: append-only tables with a generated key).
    buckets: Optional[dict[str, str]] = None
    #: One digest of the whole table (large tables without one).
    digest: Optional[str] = None
    #: In memory only, never saved: plain values of rows read in full, by
    #: row key hash (a small table's rows; a large table's added rows).
    values: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    #: The engine keeps no snapshot isolation for it (MyISAM, MEMORY).
    non_transactional: bool = False


@dataclass
class Snapshot:
    dialect: str = "sqlite"
    clock: str = ""
    #: Digest of triggers, views, routines and events.
    objects: str = ""
    tables: dict[str, Table] = field(default_factory=dict)
    #: In memory only: rows of ``extra_queries``, read in the same transaction.
    extra: dict[str, list[dict[str, Any]]] = field(default_factory=dict, repr=False)

    def to_json(self) -> dict[str, Any]:
        """The persisted form: hashes and schema only, never values."""

        return {
            "dialect": self.dialect,
            "clock": self.clock,
            "objects": self.objects,
            "tables": {
                name: {
                    "mode": t.mode,
                    "schema": t.schema,
                    "columns": t.columns,
                    "primary_key": t.primary_key,
                    "generated_key": t.generated_key,
                    "defaults": t.defaults,
                    "count": t.count,
                    "counter": t.counter,
                    "max_key": t.max_key,
                    "rows": t.rows,
                    "buckets": t.buckets,
                    "digest": t.digest,
                    "non_transactional": t.non_transactional,
                }
                for name, t in self.tables.items()
            },
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Snapshot":
        return cls(
            dialect=str(data.get("dialect", "sqlite")),
            clock=str(data.get("clock", "")),
            objects=str(data.get("objects", "")),
            tables={
                name: Table(
                    mode=t["mode"],
                    schema=t["schema"],
                    columns=list(t["columns"]),
                    primary_key=list(t["primary_key"]),
                    generated_key=t.get("generated_key"),
                    defaults=dict(t["defaults"]),
                    count=int(t["count"]),
                    counter=t.get("counter"),
                    max_key=t.get("max_key"),
                    rows=t.get("rows"),
                    buckets=t.get("buckets"),
                    digest=t.get("digest"),
                    non_transactional=bool(t.get("non_transactional")),
                )
                for name, t in data["tables"].items()
            },
        )


# -- defaults -----------------------------------------------------------------


def _number(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _clock_text(value: Any) -> str:
    return str(value).replace("T", " ")[:19]


def is_default(
    value: Any, default: Optional[str], clock: tuple[str, str] = ("", "")
) -> bool:
    """Does a new row's ``value`` equal what the column holds when nothing
    sets it?

    Only the declared default counts: NULL when the column has none, the
    stored text otherwise (numbers compared as numbers). A default the
    database computes as the time counts only when the value falls between
    the two snapshots' clocks. The table's own generated key is any value.
    """

    if default == ANY_VALUE:
        return True
    if default == CLOCK_DEFAULT:
        low, high = clock
        return (
            bool(low and high)
            and value is not None
            and (low <= _clock_text(value) <= high)
        )
    if default is None:
        return value is None
    if value is None:
        return False
    a, b = canonical(value), default
    na, nb = _number(a), _number(b)
    if na is not None and nb is not None:
        return na == nb
    return a == b


def _default_text(raw: Any, dialect: Dialect) -> Optional[str]:
    """A declared default as the value it stores, or ``CLOCK_DEFAULT``."""

    if raw is None:
        return None
    text = str(raw).strip()
    lowered = text.lower().strip("()")
    if lowered.startswith(
        ("current_timestamp", "now", "current_date", "localtime", "datetime('now'")
    ):
        return CLOCK_DEFAULT
    if text.upper() == "NULL":
        return None  # SQLite and MariaDB show DEFAULT NULL as the text NULL
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1].replace("''", "'")
    return text


# -- reading ------------------------------------------------------------------


def _quote(dialect: Dialect, name: str) -> str:
    if dialect == "mysql":
        return ".".join("`" + p.replace("`", "``") + "`" for p in name.split(".", 1))
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


class _Session:
    """One connection, one read-only transaction."""

    def __init__(self, connect: Callable[[], Any], dialect: Dialect) -> None:
        self.dialect = dialect
        self.con = connect()
        self.cur = self.con.cursor()
        if dialect == "mysql":
            # Only statements that can't fail: a driver may end the session
            # on any error. MySQL 8 caches AUTO_INCREMENT unless told not to;
            # MariaDB has no such variable.
            self.cur.execute(
                "SHOW SESSION VARIABLES LIKE 'information_schema_stats_expiry'"
            )
            if self.cur.fetchall():
                self.cur.execute("SET SESSION information_schema_stats_expiry = 0")
            self.cur.execute("SET SESSION group_concat_max_len = 1073741824")
            self.cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            self.cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        else:
            self.cur.execute("BEGIN")

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        self.cur.execute(sql)
        return [tuple(r) for r in self.cur.fetchall()]

    def dicts(self, sql: str) -> list[dict[str, Any]]:
        self.cur.execute(sql)
        names = [d[0] for d in self.cur.description or []]
        return [dict(zip(names, r)) for r in self.cur.fetchall()]

    def close(self) -> None:
        try:
            self.cur.execute("COMMIT" if self.dialect == "mysql" else "ROLLBACK")
        finally:
            self.con.close()


@dataclass
class _Def:
    columns: list[str]
    primary_key: list[str]
    generated_key: Optional[str]
    defaults: dict[str, Optional[str]]
    binary: set[str]
    schema: str
    non_transactional: bool = False


def _digest(parts: Sequence[Any]) -> str:
    return hashlib.sha256(
        json.dumps(list(parts), sort_keys=True, default=str).encode()
    ).hexdigest()


def _definitions(s: _Session) -> tuple[dict[str, _Def], str]:
    """Every table's definition, and the digest of the database's code."""

    out: dict[str, _Def] = {}
    if s.dialect == "mysql":
        default = s.rows("SELECT DATABASE()")[0][0]
        excluded = ", ".join(_literal(x) for x in _SYSTEM_SCHEMAS)
        engines = {
            (str(a), str(b)): str(c or "")
            for a, b, c in s.rows(
                "SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE FROM information_schema.TABLES "
                f"WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA NOT IN ({excluded})"
            )
        }
        for row in s.rows(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_DEFAULT, "
            "IS_NULLABLE, EXTRA, COLUMN_KEY, DATA_TYPE FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA NOT IN ({excluded}) "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        ):
            schema, table, column, ctype, cdefault, nullable, extra, key, dtype = row
            if (str(schema), str(table)) not in engines:
                continue  # a view
            name = str(table) if schema == default else f"{schema}.{table}"
            d = out.get(name)
            if d is None:
                engine = engines[(str(schema), str(table))].lower()
                d = out[name] = _Def(
                    [], [], None, {}, set(), "", engine not in ("innodb", "")
                )
            d.columns.append(str(column))
            d.defaults[str(column)] = _default_text(cdefault, "mysql")
            if str(key) == "PRI":
                d.primary_key.append(str(column))
            if "auto_increment" in str(extra).lower():
                d.generated_key = str(column)
            if "binary" in str(dtype).lower() or "blob" in str(dtype).lower():
                d.binary.add(str(column))
            d.schema = _digest(
                [d.schema, column, ctype, cdefault, nullable, extra, key]
            )
        code = [
            s.rows(
                "SELECT TRIGGER_SCHEMA, TRIGGER_NAME, EVENT_MANIPULATION, "
                "EVENT_OBJECT_TABLE, ACTION_TIMING, ACTION_STATEMENT "
                "FROM information_schema.TRIGGERS ORDER BY 1, 2"
            ),
            s.rows(
                "SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION "
                f"FROM information_schema.VIEWS WHERE TABLE_SCHEMA NOT IN ({excluded}) "
                "ORDER BY 1, 2"
            ),
            s.rows(
                "SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION "
                f"FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA NOT IN ({excluded}) "
                "ORDER BY 1, 2"
            ),
            s.rows(
                "SELECT EVENT_SCHEMA, EVENT_NAME, STATUS, EVENT_DEFINITION "
                "FROM information_schema.EVENTS ORDER BY 1, 2"
            ),
        ]
        for d in out.values():
            if len(d.primary_key) != 1 or d.generated_key != d.primary_key[0]:
                d.generated_key = None
        return out, _digest(code)
    master = s.rows("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY 1, 2")
    for kind, name, _tbl, sql in master:
        if kind != "table" or str(name).startswith("sqlite_"):
            continue
        info = s.rows(f"PRAGMA table_info({_quote('sqlite', str(name))})")
        cols = [str(r[1]) for r in info]
        pk = [str(r[1]) for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        generated = None
        if len(pk) == 1:
            ctype = next(str(r[2]) for r in info if str(r[1]) == pk[0])
            if ctype.strip().upper() == "INTEGER":
                generated = pk[0]  # a rowid alias: the database assigns it
        out[str(name)] = _Def(
            cols,
            pk,
            generated,
            {str(r[1]): _default_text(r[4], "sqlite") for r in info},
            set(),
            _digest([sql]),
        )
    code = [r for r in master if r[0] in ("trigger", "view")]
    return out, _digest(code)


def _cell(dialect: Dialect, column: str, binary: bool) -> str:
    q = _quote(dialect, column)
    if dialect == "mysql":
        return f"HEX({q})" if binary else q
    return f"CASE WHEN typeof({q}) = 'blob' THEN hex({q}) ELSE {q} END"


def _row_json(dialect: Dialect, d: _Def) -> list[str]:
    obj = "JSON_OBJECT" if dialect == "mysql" else "json_object"
    return [
        obj
        + "("
        + ", ".join(
            f"{_literal(c)}, {_cell(dialect, c, c in d.binary)}"
            for c in d.columns[i : i + _CHUNK]
        )
        + ")"
        for i in range(0, len(d.columns), _CHUNK)
    ]


def _read_rows(
    s: _Session, name: str, d: _Def, where: str = ""
) -> list[dict[str, Any]]:
    parts = _row_json(s.dialect, d)
    cells = ", ".join(f"{c} AS r{i}" for i, c in enumerate(parts))
    out = []
    for chunks in s.rows(f"SELECT {cells} FROM {_quote(s.dialect, name)} {where}"):
        row: dict[str, Any] = {}
        for chunk in chunks:
            if chunk is not None:
                row.update(json.loads(chunk) if isinstance(chunk, str) else chunk)
        out.append(row)
    return out


def _row_key(d: _Def, row: Mapping[str, Any], seen: dict[str, int]) -> str:
    if d.primary_key:
        return "|".join(canonical(row.get(c)) for c in d.primary_key)
    body = json.dumps(row, sort_keys=True, default=str)
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    seen[digest] = seen.get(digest, 0) + 1
    return f"{digest}#{seen[digest]}"


def _hash_rows(
    key: bytes, d: _Def, rows: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    hashed: dict[str, str] = {}
    plain: dict[str, dict[str, Any]] = {}
    seen: dict[str, int] = {}
    for row in rows:
        k = row_key_hash(key, _row_key(d, row, seen))
        hashed[k] = "".join(cell_hash(key, row.get(c)) for c in d.columns)
        plain[k] = dict(row)
    return hashed, plain


def _bucket_digests(
    s: _Session, name: str, d: _Def, key: bytes, upto: int
) -> dict[str, str]:
    """Salted digest per bucket of ``BUCKET`` keys, over keys <= ``upto``."""

    k = _quote(s.dialect, d.generated_key or "")
    if s.dialect == "mysql":
        row = "CONCAT(" + ", ".join(_row_json("mysql", d)) + ")"
        salt = _literal(key.hex())
        return {
            str(b): str(h)
            for b, h in s.rows(
                f"SELECT {k} DIV {BUCKET} AS b, SHA2(CONCAT({salt}, GROUP_CONCAT("
                f"SHA2({row}, 256) ORDER BY {k} SEPARATOR '')), 256) "
                f"FROM {_quote('mysql', name)} WHERE {k} <= {int(upto)} GROUP BY b"
            )
        }
    out: dict[str, hashlib._Hash] = {}
    for row in _read_rows(s, name, d, f"WHERE {k} <= {int(upto)} ORDER BY {k}"):
        bucket = str(int(row[d.generated_key or ""]) // BUCKET)
        h = out.setdefault(bucket, hashlib.sha256(key))
        h.update(json.dumps(row, sort_keys=True, default=str).encode())
    return {b: h.hexdigest() for b, h in out.items()}


def _table_digest(s: _Session, name: str, d: _Def, key: bytes) -> str:
    h = hashlib.sha256(key)
    rows = sorted(
        json.dumps(r, sort_keys=True, default=str) for r in _read_rows(s, name, d)
    )
    for r in rows:
        h.update(r.encode())
    return h.hexdigest()


def take_snapshot(
    connect: Callable[[], Any],
    dialect: Dialect,
    *,
    key: bytes,
    modes: Mapping[str, TableMode] = {},
    max_rows: int = 20000,
    before: Optional[Snapshot] = None,
    extra_queries: Mapping[str, str] = {},
) -> Snapshot:
    """Read every table in one read-only transaction.

    Args:
        connect: Opens one connection (DB-API 2.0: ``cursor()``, ``close()``).
        key: The run's salt, which keys every hash.
        modes: Table -> ``append_only`` or ``skip`` (allow-listed tables);
            every other table is compared.
        max_rows: Over this many rows, a table is digested by bucket.
        before: The baseline, when this is the read after the save: a large
            or append-only table's existing rows are digested up to the
            baseline's largest key, and rows above it are read in full.
        extra_queries: Name -> a SELECT to run in the same transaction (the
            audit log); rows land in :attr:`Snapshot.extra`, in memory only.

    Raises:
        SnapshotError: the database can't be read as the frame needs it.
    """

    try:
        s = _Session(connect, dialect)
    except Exception as exc:  # noqa: BLE001
        raise SnapshotError(f"could not open a read transaction: {exc}") from exc
    try:
        return _snapshot(s, key, modes, max_rows, before, extra_queries)
    except SnapshotError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SnapshotError(f"the database could not be read: {exc}") from exc
    finally:
        s.close()


def _snapshot(
    s: _Session,
    key: bytes,
    modes: Mapping[str, TableMode],
    max_rows: int,
    before: Optional[Snapshot],
    extra_queries: Mapping[str, str],
) -> Snapshot:
    dialect = s.dialect
    snap = Snapshot(
        dialect=dialect, clock=_clock_text(s.rows("SELECT CURRENT_TIMESTAMP")[0][0])
    )
    defs, snap.objects = _definitions(s)
    for name, sql in extra_queries.items():
        snap.extra[name] = s.dicts(sql)
    unknown = sorted(set(modes) - set(defs))
    if unknown:
        raise SnapshotError(
            "allow-listed tables not in the database: " + ", ".join(unknown)
        )
    names = sorted(t for t in defs if modes.get(t) != "skip")
    for t in sorted(t for t in defs if modes.get(t) == "skip"):
        d = defs[t]
        snap.tables[t] = Table("skip", d.schema, d.columns, d.primary_key, None, {})
    if not names:
        return snap
    counts = {
        str(t): int(n)
        for t, n in s.rows(
            " UNION ALL ".join(
                f"SELECT {_literal(t)}, COUNT(*) FROM {_quote(dialect, t)}"
                for t in names
            )
        )
    }
    keyed = [t for t in names if defs[t].generated_key]
    maxes = (
        {
            str(t): (None if m is None else int(m))
            for t, m in s.rows(
                " UNION ALL ".join(
                    f"SELECT {_literal(t)}, MAX({_quote(dialect, defs[t].generated_key or '')}) "
                    f"FROM {_quote(dialect, t)}"
                    for t in keyed
                )
            )
        }
        if keyed
        else {}
    )
    counters: dict[str, int] = {}
    if dialect == "sqlite":
        if any(
            n == "sqlite_sequence"
            for (n,) in s.rows("SELECT name FROM sqlite_master WHERE type = 'table'")
        ):
            counters = {
                str(n): int(v)
                for n, v in s.rows("SELECT name, seq FROM sqlite_sequence")
            }
    else:
        default = s.rows("SELECT DATABASE()")[0][0]
        for schema, table, value in s.rows(
            "SELECT TABLE_SCHEMA, TABLE_NAME, AUTO_INCREMENT FROM information_schema.TABLES "
            "WHERE AUTO_INCREMENT IS NOT NULL"
        ):
            name = str(table) if schema == default else f"{schema}.{table}"
            counters[name] = int(value)
    for t in names:
        d = defs[t]
        mode: TableMode = "append_only" if modes.get(t) == "append_only" else "compare"
        old = before.tables.get(t) if before is not None else None
        table = Table(
            mode,
            d.schema,
            d.columns,
            d.primary_key,
            d.generated_key,
            dict(d.defaults),
            counts.get(t, 0),
            counters.get(t),
            maxes.get(t),
            non_transactional=d.non_transactional,
        )
        if d.generated_key:
            # Only the table's own generated key may hold any value.
            table.defaults[d.generated_key] = ANY_VALUE
        if mode == "append_only" and not d.generated_key:
            raise SnapshotError(
                f"append-only table {t!r} has no generated key; mark it skip with "
                "a reason, or compare it"
            )
        # Read after the save the way the baseline was read, so a table that
        # crossed max_rows in the window is still compared like for like.
        if old is not None and old.mode == mode and old.schema == d.schema:
            by_bucket = old.buckets is not None
            whole = old.digest is not None
        else:
            by_bucket = bool(d.generated_key) and (
                mode == "append_only" or table.count > max_rows
            )
            whole = not by_bucket and table.count > max_rows
        if by_bucket:
            upto = (old.max_key if old is not None else table.max_key) or 0
            table.buckets = _bucket_digests(s, t, d, key, upto)
            if before is not None and mode == "compare":
                k = _quote(dialect, d.generated_key or "")
                added = _read_rows(s, t, d, f"WHERE {k} > {int(upto)}")
                _hashed, table.values = _hash_rows(key, d, added)
        elif whole:
            table.digest = _table_digest(s, t, d, key)
        else:
            rows = _read_rows(s, t, d) if table.count else []
            table.rows, table.values = _hash_rows(key, d, rows)
        snap.tables[t] = table
    return snap


# -- comparing ----------------------------------------------------------------


@dataclass
class TableDiff:
    """How one table moved between two snapshots. Counts and column names
    only, never values."""

    added: int = 0
    removed: int = 0
    changed: int = 0
    changed_columns: list[str] = field(default_factory=list)
    #: Moved, but not row by row: why (a schema change, a changed bucket of a
    #: large table, an append-only table whose existing rows moved, a
    #: counter that moved past the rows added).
    unread: list[str] = field(default_factory=list)


def unread_reasons(
    old: Optional[Table], new: Optional[Table], dialect: str = "sqlite"
) -> list[str]:
    """Why a table moved in a way no row comparison explains; [] if none."""

    if old is None or new is None:
        return ["created or dropped"]
    if old.mode != new.mode:
        return ["compared differently"]
    if old.mode == "skip":
        return []
    if old.schema != new.schema or old.columns != new.columns:
        return ["definition changed"]
    reasons = []
    if old.buckets is not None or new.buckets is not None:
        if old.buckets != new.buckets:
            reasons.append("existing rows changed (by bucket)")
    elif old.digest is not None or new.digest is not None:
        if old.digest is None or new.digest is None:
            raise SnapshotError("a large table has no digest")
        if old.digest != new.digest:
            reasons.append("rows changed (whole-table digest)")
    if new.counter is not None:
        # sqlite_sequence holds the last key used; AUTO_INCREMENT the next.
        step = 1 if dialect == "mysql" else 0
        expected = max(
            v
            for v in (
                old.counter or 0,
                0 if new.max_key is None else new.max_key + step,
            )
        )
        if new.counter > expected:
            reasons.append("a row was written and removed (counter moved)")
    return reasons


def diff(
    before: Snapshot,
    after: Snapshot,
    *,
    key: bytes,
    allow_tables: Sequence[str] = (),
) -> dict[str, TableDiff]:
    """Every table that moved between ``before`` and ``after``, except
    ``allow_tables`` and skipped ones. Empty when the database is back where
    it was. Database code (triggers, views, routines, events) that changed
    shows under ``"(database code)"``."""

    out: dict[str, TableDiff] = {}
    if before.objects != after.objects:
        out["(database code)"] = TableDiff(unread=["changed"])
    skip = set(allow_tables)
    for name in sorted(set(before.tables) | set(after.tables)):
        if name in skip:
            continue
        a, b = before.tables.get(name), after.tables.get(name)
        reasons = unread_reasons(a, b, after.dialect)
        moved = TableDiff(unread=reasons)
        if (
            a is not None
            and b is not None
            and a.mode == "compare"
            and not (
                reasons and reasons[0] in ("definition changed", "created or dropped")
            )
        ):
            old = {k: unpack(a.columns, v) for k, v in (a.rows or {}).items()}
            new = (
                {k: unpack(b.columns, v) for k, v in b.rows.items()}
                if b.rows is not None
                else {
                    k: {c: cell_hash(key, v.get(c)) for c in b.columns}
                    for k, v in b.values.items()
                }
            )
            if a.rows is not None:
                moved.added = len(new.keys() - old.keys())
                moved.removed = len(old.keys() - new.keys())
            else:
                moved.added = len(new)
            columns: set[str] = set()
            for k in old.keys() & new.keys():
                fields = [c for c in b.columns if old[k].get(c) != new[k].get(c)]
                if fields:
                    moved.changed += 1
                    columns.update(fields)
            moved.changed_columns = sorted(columns)
        if moved.added or moved.removed or moved.changed or moved.unread:
            out[name] = moved
    return out


def same_state(a: Snapshot, b: Snapshot, key: bytes) -> bool:
    """Do two reads taken after the save show the same database? Compared on
    what the frame judges: code, definitions, counts, counters, rows (by
    MAC), bucket digests, and the rows read in full."""

    if a.objects != b.objects or set(a.tables) != set(b.tables):
        return False
    for name, x in a.tables.items():
        y = b.tables[name]
        if (x.mode, x.schema, x.count, x.counter, x.max_key) != (
            y.mode,
            y.schema,
            y.count,
            y.counter,
            y.max_key,
        ):
            return False
        if (x.rows, x.buckets, x.digest) != (y.rows, y.buckets, y.digest):
            return False

        def macs(t: Table) -> dict[str, str]:
            return {
                k: "".join(cell_hash(key, v.get(c)) for c in t.columns)
                for k, v in t.values.items()
            }

        if macs(x) != macs(y):
            return False
    return True
