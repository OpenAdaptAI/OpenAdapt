"""Snapshot a whole database for the frame: every table, every column.

A frame that compares only the surfaces a contract reads can't see a write
to anything else: a column the effects don't name, a field on a side row, a
table nobody thought to read. ``frame.database`` turns the frame into a diff
of the whole database. This module takes the snapshot; ``frame.judge_frame``
compares two of them like any other slice.

What a snapshot holds, and why:

- **No values on disk.** Each row is kept as a keyed MAC of its key and of
  its contents (without ``ignore_columns``), and each cell as a keyed MAC
  bound to its table and column: BLAKE2b, 128 bits, keyed by
  :func:`mac_key` from the run's salt and a host secret that is never in the
  baseline. A table over ``max_rows`` keeps cell MACs only for the columns
  attribution and side records read (``ref_columns``). Plain values are read
  in full, but kept in memory only for rows that differ from the baseline.
- **Rows by key.** A changed row is known by its key in every table, so it
  goes through attribution, and an added row is found by key for any
  primary key.
- **One consistent read.** Everything is read on one connection inside one
  read-only transaction (``START TRANSACTION WITH CONSISTENT SNAPSHOT, READ
  ONLY`` on MySQL and MariaDB; ``BEGIN ISOLATION LEVEL REPEATABLE READ, READ
  ONLY`` on PostgreSQL; ``BEGIN`` on SQLite). The audit log is read in the
  same transaction (``extra_queries``). MySQL's ``AUTO_INCREMENT`` and
  PostgreSQL's sequences aren't part of a snapshot, so they are read just
  before the snapshot starts: a row committed in between is in the snapshot,
  never missing from it.
- **Schema and code.** Each table's definition and the database's triggers,
  views, routines and events are digested. On MySQL a trigger is visible
  only to an account with ``TRIGGER``; without it the snapshot says so
  (``code_visible``), and the frame is not checked.
- **Counters.** ``sqlite_sequence``, ``AUTO_INCREMENT`` and a PostgreSQL
  sequence a column owns (on the column the database numbers, key or not)
  show a row written and deleted in the window. Any other PostgreSQL sequence
  is read as a one-row table of its own, so a value drawn from it is a
  change unless the allow-list skips it with a reason.
- **The clock.** ``CURRENT_TIMESTAMP`` at the snapshot, so a column the
  database fills with the time can be held to the window.
- **Allow-listed tables** are append-only by default: their existing rows
  must not change or go. ``skip`` reads nothing and needs a stated reason.
- **Exact values.** Every column is read, and no type is dropped. On MySQL
  and MariaDB a row is read as ``JSON_OBJECT``; a type whose value it can't
  carry as exact text is read another way (:func:`_mysql_encoding`: bytes,
  bit strings and geometries as hex, ``FLOAT`` widened to ``DOUBLE``, JSON as
  its stored text), and numbers parse as ``Decimal``. On PostgreSQL each
  column is read as text by a rule for its type (:func:`_pg_rule`), in a
  session pinned to UTC and to exact float output; a type without a rule
  makes the table unreadable. :func:`canonical` gives each value one exact
  text form.

Read-only: SQLite files open with ``mode=ro``; a driver connection should use
an account with SELECT (and, on MySQL, TRIGGER and SHOW VIEW; on PostgreSQL,
USAGE on each schema and SELECT on its sequences) on every schema the app can
write. The baseline must still be protected like the records:
whoever holds both it and the host secret can test guesses against the MACs.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Context, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Optional

Dialect = Literal["sqlite", "mysql", "postgresql"]
TableMode = Literal["compare", "append_only", "skip"]

#: Columns per JSON object call: SQLite caps a function at 127 arguments.
_CHUNK = 50
#: The synthetic identity field of a database slice's records.
KEY = "__row_key__"
#: The row's contents MAC in a database slice's records.
ROW = "__row_mac__"
#: A column default meaning any value: the table's own generated key.
ANY_VALUE = "\x00any"
#: A column default the database computes as the current time at insert.
CLOCK_DEFAULT = "\x00clock"
_SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")
#: Hex characters per MAC (128 bits).
MAC_HEX = 32


class SnapshotError(RuntimeError):
    """The database can't be snapshotted as the frame needs."""


# -- keys and MACs ------------------------------------------------------------


def _host_secret() -> bytes:
    """The host's frame secret: ``OPENADAPT_FRAME_SECRET`` (hex), the file
    ``OPENADAPT_FRAME_SECRET_FILE``, or ``~/.config/openadapt/frame.key``
    (created owner-only on first use). It is never written into a baseline.

    The default file only keeps the secret out of the baseline: an agent
    running as the same OS user can read it. A deployment sets
    ``OPENADAPT_FRAME_SECRET`` (or the file) from a store the agent's account
    can't read, so a baseline the agent can see still can't be tested
    against guesses."""

    env = os.environ.get("OPENADAPT_FRAME_SECRET")
    if env:
        return bytes.fromhex(env)
    path = Path(
        os.environ.get("OPENADAPT_FRAME_SECRET_FILE")
        or Path.home() / ".config" / "openadapt" / "frame.key"
    )
    if not path.exists():
        # Written whole to a private temp file, then linked into place: a
        # process that loses the race reads the winner's secret, never a
        # half-written one.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(32))
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
    secret = path.read_bytes()
    if len(secret) < 32:
        raise SnapshotError(f"the frame secret {path} is too short")
    return secret


def mac_key(salt: bytes) -> bytes:
    """The run's MAC key: the salt (in the baseline) keyed by the host
    secret (not in it), so the baseline alone can't be tested against."""

    return hashlib.blake2b(salt, key=_host_secret()[:64], digest_size=32).digest()


def canonical(value: Any) -> str:
    """One exact text form per value, so equal values hash equally and
    different ones don't.

    It covers what MySQL, MariaDB, PostgreSQL and SQLite drivers return:

    - numbers (``int``, ``float``, ``Decimal``) by value, every digit kept;
    - bytes (``bytes``, ``bytearray``, ``memoryview``) as a tagged hex;
    - ``datetime``, ``date`` and ``time`` as their ISO text with a space;
    - ``timedelta`` (a MySQL ``TIME``, a PostgreSQL ``interval``) as signed
      ``H:MM:SS[.ffffff]``, the text MySQL prints for a ``TIME``;
    - a ``set`` (a MySQL ``SET``) as its sorted members joined by commas;
    - ``dict``, ``list`` and ``tuple`` (``JSON``, ``jsonb``, arrays) as JSON
      with sorted keys and these rules inside;
    - any other type with its own ``str()`` (``UUID``, an IP address).

    A type without an exact text form (its ``str()`` is the default, which
    prints a memory address) raises ``TypeError``: a snapshot that can't
    read a column exactly is unreadable, never a column dropped.
    """

    if value is None:
        return "\x00null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, StoredNumber):
        return value.stored
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if not number.is_finite():
            return str(value)
        # The default context keeps 28 digits; a DECIMAL(65,30) has more.
        exact = Context(prec=max(len(number.as_tuple().digits), 1))
        return format(number.normalize(exact), "f")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\x00bytes:" + bytes(value).hex()
    if isinstance(value, datetime.timedelta):
        return _interval_text(value)
    if isinstance(value, (set, frozenset)):
        return ",".join(sorted(canonical(v) for v in value))
    if isinstance(value, (dict, list, tuple)):
        return _json_text(value)
    if isinstance(value, str):
        # The tags above start with NUL; a text that does too is escaped,
        # so the text "\x00null" never reads as NULL.
        return "\x00s" + value if value.startswith("\x00") else value
    if type(value).__str__ is object.__str__:
        raise TypeError(f"no exact text form for a {type(value).__name__} value")
    return str(value)


class StoredNumber(Decimal):
    """A PostgreSQL ``numeric`` declared without a scale, which keeps the
    scale it was stored with: ``1.5`` and ``1.50`` are different stored
    values. It equals, and prints as, its plain value (``1.5``), so a
    selector or parameter compares by value; :func:`canonical` gives the
    stored text, so a change of scale alone still shows."""

    stored: str


def _stored_number(text: str) -> StoredNumber:
    number = Decimal(text)
    exact = Context(prec=max(len(number.as_tuple().digits), 1))
    value = StoredNumber(format(number.normalize(exact), "f"))
    value.stored = text
    return value


def _interval_text(value: datetime.timedelta) -> str:
    micro = (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
    sign, micro = ("-", -micro) if micro < 0 else ("", micro)
    seconds, fraction = divmod(micro, 1_000_000)
    hours, rest = divmod(seconds, 3600)
    text = f"{sign}{hours:02d}:{rest // 60:02d}:{rest % 60:02d}"
    return text + (f".{fraction:06d}" if fraction else "")


def _json_text(value: Any) -> str:
    if value is None or isinstance(value, (bool, str)):
        return json.dumps(value)
    if isinstance(value, (int, float, Decimal)):
        return canonical(value)
    if isinstance(value, Mapping):
        return (
            "{"
            + ",".join(
                json.dumps(str(k)) + ":" + _json_text(value[k])
                for k in sorted(value, key=str)
            )
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json_text(v) for v in value) + "]"
    return json.dumps(canonical(value))


def _mac(key: bytes, text: str) -> str:
    return hashlib.blake2b(
        text.encode(), digest_size=MAC_HEX // 2, key=key[:64]
    ).hexdigest()


def link_key() -> bytes:
    """The host's row-link key: one per host, never per run, derived from
    the host secret (not in any snapshot). A row's link MAC
    (:attr:`Table.links`) is the same in every snapshot taken on this host,
    which shows that two snapshots hold the same row key and nothing else."""

    return hashlib.blake2b(
        b"openadapt:row-link/v1", key=_host_secret()[:64], digest_size=32
    ).digest()


def cell_hash(key: bytes, value: Any, table: str = "", column: str = "") -> str:
    """A cell's keyed MAC, bound to its table and column, so equal values in
    different places don't share one."""

    return _mac(key, f"c\x1f{table}\x1f{column}\x1f{canonical(value)}")


def row_key_hash(key: bytes, text: str, table: str = "") -> str:
    """A row key's keyed MAC."""

    return _mac(key, f"k\x1f{table}\x1f{text}")


def row_hash(
    key: bytes, table: str, row: Mapping[str, Any], ignore: Sequence[str] = ()
) -> str:
    """A row's contents MAC, without ``ignore`` columns."""

    skip = set(ignore)
    body = json.dumps(
        sorted((c, canonical(v)) for c, v in row.items() if c not in skip),
        separators=(",", ":"),
    )
    return _mac(key, f"r\x1f{table}\x1f{body}")


def unpack(columns: Sequence[str], packed: str) -> dict[str, str]:
    """A stored row's cell MACs by column (stored as one string per row)."""

    return {c: packed[i * MAC_HEX : (i + 1) * MAC_HEX] for i, c in enumerate(columns)}


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
    #: The column the database numbers, and its largest value.
    auto_column: Optional[str] = None
    max_auto: Optional[int] = None
    #: The columns each row keeps cell MACs for (all, or ``ref_columns`` for
    #: a table over ``max_rows``; none for an append-only table).
    cell_columns: list[str] = field(default_factory=list)
    #: Row key MAC -> [row contents MAC, cell MACs of ``cell_columns``
    #: concatenated (:func:`unpack`)].
    rows: dict[str, list[str]] = field(default_factory=dict)
    #: In memory only, never saved: plain values of the rows that differ
    #: from the baseline (all rows of a read with no baseline: none kept).
    values: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    #: The engine keeps no snapshot isolation for it (MyISAM, MEMORY).
    non_transactional: bool = False
    #: Row key MAC -> the row key's link MAC (:func:`link_key`), for a table
    #: with a primary key, when the read asked for links (a late-write
    #: recheck). One row has one link MAC in every snapshot on this host, so
    #: two runs' snapshots can tell they hold the same row (a removal one of
    #: them saw) without values. Empty otherwise.
    links: dict[str, str] = field(default_factory=dict)


@dataclass
class Snapshot:
    dialect: str = "sqlite"
    clock: str = ""
    #: Digest of triggers, views, routines and events, and whether the
    #: account could see triggers at all.
    objects: str = ""
    code_visible: bool = True
    schemas: list[str] = field(default_factory=list)
    #: Schemas the allow-list leaves out (``<schema>.*``, skip, with a reason).
    schemas_skipped: list[str] = field(default_factory=list)
    tables: dict[str, Table] = field(default_factory=dict)
    #: In memory only: rows of ``extra_queries``, read in the same transaction.
    extra: dict[str, list[dict[str, Any]]] = field(default_factory=dict, repr=False)

    def to_json(self) -> dict[str, Any]:
        """The persisted form: MACs and schema only, never values."""

        return {
            "dialect": self.dialect,
            "clock": self.clock,
            "objects": self.objects,
            "code_visible": self.code_visible,
            "schemas": self.schemas,
            **(
                {"schemas_skipped": self.schemas_skipped}
                if self.schemas_skipped
                else {}
            ),
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
                    "auto_column": t.auto_column,
                    "max_auto": t.max_auto,
                    "cell_columns": t.cell_columns,
                    "rows": t.rows,
                    "non_transactional": t.non_transactional,
                    **({"links": t.links} if t.links else {}),
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
            code_visible=bool(data.get("code_visible", True)),
            schemas=list(data.get("schemas", [])),
            schemas_skipped=list(data.get("schemas_skipped", [])),
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
                    auto_column=t.get("auto_column"),
                    max_auto=t.get("max_auto"),
                    cell_columns=list(t.get("cell_columns", [])),
                    rows={k: list(v) for k, v in (t.get("rows") or {}).items()},
                    non_transactional=bool(t.get("non_transactional")),
                    links={str(k): str(v) for k, v in (t.get("links") or {}).items()},
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
    if dialect == "postgresql":
        return _pg_default_text(str(raw))
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


#: PostgreSQL defaults the database fills with the time (``pg_get_expr``).
_PG_CLOCK = re.compile(
    r"(now\(\)|current_timestamp|current_date|localtimestamp|"
    r"statement_timestamp\(\)|transaction_timestamp\(\)|clock_timestamp\(\)|"
    r"\('now'::text\)::[a-z ]+)(::[a-z ()0-9]+)?",
    re.IGNORECASE,
)


def _pg_default_text(expr: str) -> Optional[str]:
    """A PostgreSQL default (``pg_get_expr``) as the value it stores.

    Only literals are read: ``NULL``, a quoted string with a cast
    (``'open'::character varying``), a number (``0``, ``(-1)``) and
    ``true``/``false``. A time function is ``CLOCK_DEFAULT``. Any other
    expression (``nextval(...)``, ``gen_random_uuid()``) is kept as its
    text, which no stored value equals, so a new row that relies on it must
    list the column in ``new_row_fields``.
    """

    text = expr.strip()
    if _PG_CLOCK.fullmatch(text):
        return CLOCK_DEFAULT
    if re.fullmatch(r"NULL(::[^']+)?", text, re.IGNORECASE):
        return None
    m = re.fullmatch(r"'((?:[^']|'')*)'::[^']+", text, re.S)
    if m:
        return m.group(1).replace("''", "'")
    m = re.fullmatch(r"\(?(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\)?(::[^']+)?", text)
    if m:
        return m.group(1)
    return text


# -- reading ------------------------------------------------------------------


def _quote(dialect: Dialect, name: str) -> str:
    if dialect == "mysql":
        return ".".join("`" + p.replace("`", "``") + "`" for p in name.split(".", 1))
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


class _Session:
    """One connection, one read-only transaction (opened by :meth:`begin`)."""

    def __init__(self, connect: Callable[[], Any], dialect: Dialect) -> None:
        self.dialect = dialect
        self.con = connect()
        if dialect == "postgresql":
            # psycopg opens a transaction on the first statement unless told
            # not to; the snapshot's own BEGIN must be the one that counts.
            self.con.autocommit = True
        self.cur = self.con.cursor()
        if dialect == "postgresql":
            for sql in _PG_SESSION:
                self.cur.execute(sql)

    def begin(self) -> None:
        if self.dialect == "postgresql":
            self.cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        elif self.dialect == "mysql":
            # Only statements that can't fail: a driver may end the session
            # on any error. MySQL 8 caches AUTO_INCREMENT unless told not to;
            # MariaDB has no such variable.
            self.cur.execute(
                "SHOW SESSION VARIABLES LIKE 'information_schema_stats_expiry'"
            )
            if self.cur.fetchall():
                self.cur.execute("SET SESSION information_schema_stats_expiry = 0")
            # A TIMESTAMP prints in the session's time zone. In a zone with
            # daylight saving, two instants an hour apart print the same in
            # the repeated hour, so a change would be invisible. UTC has no
            # repeated hour.
            self.cur.execute("SET SESSION time_zone = '+00:00'")
            self.cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            self.cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        else:
            self.cur.execute("BEGIN")

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        self.cur.execute(sql)
        # A dict cursor (``pymysql.cursors.DictCursor``) yields mappings in
        # column order; a plain one yields tuples.
        return [
            tuple(r.values()) if isinstance(r, Mapping) else tuple(r)
            for r in self.cur.fetchall()
        ]

    def dicts(self, sql: str) -> list[dict[str, Any]]:
        self.cur.execute(sql)
        names = [d[0] for d in self.cur.description or []]
        return [
            dict(r) if isinstance(r, Mapping) else dict(zip(names, r))
            for r in self.cur.fetchall()
        ]

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
    #: Column -> how it is read when its JSON text isn't exact
    #: (:func:`_mysql_encoding`); every other column is read as it is.
    encode: dict[str, str]
    schema: str
    non_transactional: bool = False
    #: The column the database numbers (AUTO_INCREMENT, or SQLite's rowid
    #: alias), whether or not it is the primary key.
    auto_column: Optional[str] = None
    #: The quoted relation to read (PostgreSQL: ``"schema"."table"``).
    relation: str = ""
    #: Why the table can't be read exactly (PostgreSQL); it is refused
    #: unless the allow-list skips it.
    unreadable: str = ""
    #: Rows read already (a PostgreSQL sequence's one row), not from a table.
    fixed: Optional[list[dict[str, Any]]] = None
    #: The SELECT that reads the rows, when it isn't the table's own
    #: (PostgreSQL large objects), one text column per ``columns``.
    select: str = ""


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
                    [], [], None, {}, {}, "", engine not in ("innodb", "")
                )
            d.columns.append(str(column))
            d.defaults[str(column)] = _default_text(cdefault, "mysql")
            if str(key) == "PRI":
                d.primary_key.append(str(column))
            if "auto_increment" in str(extra).lower():
                d.generated_key = str(column)
                d.auto_column = str(column)
            how = _mysql_encoding(str(dtype))
            if how:
                d.encode[str(column)] = how
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
            {},
            _digest([sql]),
            auto_column=generated,
        )
    sqlite_code = [r for r in master if r[0] in ("trigger", "view")]
    return out, _digest(sqlite_code)


#: MySQL and MariaDB types whose value ``JSON_OBJECT`` can't carry as text:
#: a bit string, a geometry or a vector is bytes, which turns the whole
#: object into a binary string that isn't valid JSON (Dolibarr's
#: ``llx_societe.geopoint``). They are read as the hex of their stored bytes.
_MYSQL_HEX_TYPES = frozenset(
    {
        "bit",
        "geometry",
        "point",
        "linestring",
        "polygon",
        "multipoint",
        "multilinestring",
        "multipolygon",
        "geometrycollection",
        "geomcollection",
        "vector",
    }
)


def _mysql_encoding(data_type: str) -> str:
    """How a MySQL or MariaDB column is read so its text is exact.

    - ``hex``: binary strings, blobs, bit strings, geometries and vectors, as
      the hex of the stored bytes.
    - ``double``: ``FLOAT``, widened to ``DOUBLE``. The server prints a
      ``FLOAT`` with six significant digits, so two different stored values
      can print the same; widening is exact, and a ``DOUBLE`` prints as the
      shortest text that reads back as the same number.
    - ``text``: ``JSON`` (and ``LONGTEXT``, MariaDB's type for it), as the
      stored text, so ``JSON_OBJECT`` doesn't parse and print it again.
    - ``""``: every other type, as ``JSON_OBJECT`` prints it. Integers,
      decimals, dates, times, ``ENUM`` and ``SET`` print exactly there.
    """

    t = data_type.strip().lower()
    if "binary" in t or "blob" in t or t in _MYSQL_HEX_TYPES:
        return "hex"
    if t == "float":
        return "double"
    if t in ("json", "longtext"):
        return "text"
    return ""


def _cell(dialect: Dialect, column: str, how: str = "") -> str:
    q = _quote(dialect, column)
    if dialect == "mysql":
        if how == "hex":
            return f"HEX({q})"
        if how == "double":
            return f"CAST({q} AS DOUBLE)"
        if how == "text":
            return f"CONCAT({q})"
        return q
    # SQLite's json_object prints a REAL with 15 digits, so two doubles can
    # print the same. Printed with 17 they can't; json() keeps it a number.
    return (
        f"CASE WHEN typeof({q}) = 'blob' THEN hex({q}) "
        f"WHEN typeof({q}) = 'real' THEN json(printf('%!.17g', {q})) ELSE {q} END"
    )


def _row_json(dialect: Dialect, d: _Def) -> list[str]:
    obj = "JSON_OBJECT" if dialect == "mysql" else "json_object"
    return [
        obj
        + "("
        + ", ".join(
            f"{_literal(c)}, {_cell(dialect, c, d.encode.get(c, ''))}"
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
                row.update(_row_part(name, chunk))
        out.append(_plain_numbers(s.dialect, d, row))
    return out


def _float32_text(value: Decimal) -> Decimal:
    """The shortest decimal that reads back as the same 32-bit float."""

    single = struct.unpack("f", struct.pack("f", float(value)))[0]
    for digits in range(1, 10):
        text = f"{single:.{digits}g}"
        if struct.unpack("f", struct.pack("f", float(text)))[0] == single:
            return Decimal(text)
    return Decimal(repr(single))  # pragma: no cover - 9 digits always do


def _plain_numbers(dialect: Dialect, d: _Def, row: dict[str, Any]) -> dict[str, Any]:
    """Numbers in the form a person writes them, each still exact.

    A row's numbers parse as ``Decimal`` so no digit is lost; the plain form
    is what selectors and parameters compare with (``str()``):

    - a SQLite ``REAL`` (read with 17 digits) as the shortest text that reads
      back as the same double (``0.3``, not ``0.29999999999999999``);
    - a MySQL ``FLOAT`` (read widened to ``DOUBLE``) as the shortest text that
      reads back as the same 32-bit float (``1.1``, not
      ``1.100000023841858``);
    - any other MySQL decimal without trailing zeros (``1.5``, not
      ``1.50000000``).

    Each maps one stored value to one text, so a change still shows.
    """

    for column, value in row.items():
        if not isinstance(value, Decimal) or not value.is_finite():
            continue
        if dialect == "sqlite":
            row[column] = Decimal(repr(float(value)))
        elif d.encode.get(column) == "double":
            row[column] = _float32_text(value)
        else:
            exact = Context(prec=max(len(value.as_tuple().digits), 1))
            row[column] = Decimal(format(value.normalize(exact), "f"))
    return row


def _row_part(table: str, chunk: Any) -> dict[str, Any]:
    """One ``JSON_OBJECT`` result as a dict, with numbers kept exact.

    A driver returns the object as text, as bytes (when the server types the
    result as a binary string), or already parsed. Bytes must be UTF-8 JSON.
    Anything else can't be read exactly, so the table is unreadable; no
    column is dropped. A number with a fraction or an exponent parses as a
    ``Decimal``, never a ``float``, so a ``DECIMAL(30,10)`` keeps every digit.
    """

    if isinstance(chunk, (bytes, bytearray, memoryview)):
        try:
            chunk = bytes(chunk).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SnapshotError(
                f"table {table}: a row isn't UTF-8 JSON ({exc.reason}); a column "
                "type the snapshot doesn't read as hex"
            ) from exc
    if isinstance(chunk, str):
        try:
            parsed = json.loads(chunk, parse_float=Decimal)
        except ValueError as exc:
            raise SnapshotError(
                f"table {table}: a row isn't valid JSON ({exc}); a column type "
                "the snapshot doesn't read as hex"
            ) from exc
    else:
        parsed = chunk
    if not isinstance(parsed, Mapping):
        raise SnapshotError(
            f"table {table}: a row read as {type(parsed).__name__}, not a JSON object"
        )
    return dict(parsed)


# -- PostgreSQL -----------------------------------------------------------------

#: The session a PostgreSQL snapshot reads in. Each setting fixes how a type
#: prints, so one stored value has one text: timestamps in UTC (no repeated
#: hour), ISO dates and intervals, floats with every digit they need
#: (shortest exact on PostgreSQL 12 and later, 17 digits before), bytes as
#: hex, ``money`` in the C locale (two decimals, read as ``numeric``).
_PG_SESSION = (
    "SET TimeZone = 'UTC'",
    "SET DateStyle = 'ISO, YMD'",
    "SET IntervalStyle = 'iso_8601'",
    "SET extra_float_digits = 3",
    "SET bytea_output = 'hex'",
    "SET lc_monetary = 'C'",
    "SET client_encoding = 'UTF8'",
)

#: Schemas a PostgreSQL snapshot never reads: the catalogs, TOAST storage
#: and other sessions' temporary tables.
_PG_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")


def _pg_system(schema: str) -> bool:
    return schema in _PG_SYSTEM_SCHEMAS or schema.startswith(("pg_toast", "pg_temp_"))


#: Built-in PostgreSQL types (in ``pg_catalog``) and the rule each is read
#: by. Every rule reads the column as text in the pinned session
#: (:data:`_PG_SESSION`), whose output is exact for these types:
#:
#: - ``int``: integers and ``oid``, parsed as ``int``.
#: - ``numeric``: parsed as ``Decimal``, every digit kept (``NaN`` and the
#:   infinities too). Declared without a scale, as a :class:`StoredNumber`,
#:   which keeps the scale it was stored with.
#: - ``float4``/``float8``: parsed as ``Decimal``; the plain value is the
#:   shortest text that reads back as the same stored float.
#: - ``money``: read as ``numeric`` (in the C locale, hundredths).
#: - ``bool``: ``true``/``false`` (its text cast) as ``bool``.
#: - ``bytea``: the hex of the stored bytes, as ``bytes``.
#: - ``text``: the type's own text. Strings, ``uuid``, network addresses,
#:   dates and times (UTC), ``interval`` (ISO 8601, months, days and
#:   microseconds kept apart), bit strings, ``xml``, ``json`` (its stored
#:   text), ``jsonb`` (its canonical text: keys sorted, duplicates gone),
#:   ``tsvector`` and ``tsquery`` (sorted lexemes), and the geometric types
#:   (their floats printed exactly). Arrays, ranges, multiranges, enums and
#:   domains are read by their element, subtype or base type's rule.
_PG_RULES: dict[str, str] = {
    **dict.fromkeys(("int2", "int4", "int8", "oid"), "int"),
    "numeric": "numeric",
    "float4": "float4",
    "float8": "float8",
    "money": "money",
    "bool": "bool",
    "bytea": "bytea",
    **dict.fromkeys(
        (
            "text",
            "varchar",
            "bpchar",
            "name",
            "char",
            "uuid",
            "inet",
            "cidr",
            "macaddr",
            "macaddr8",
            "date",
            "time",
            "timetz",
            "timestamp",
            "timestamptz",
            "interval",
            "bit",
            "varbit",
            "xml",
            "json",
            "jsonb",
            "jsonpath",
            "tsvector",
            "tsquery",
            "point",
            "line",
            "lseg",
            "box",
            "path",
            "polygon",
            "circle",
            "pg_lsn",
        ),
        "text",
    ),
}


@dataclass
class _PgType:
    name: str
    schema: str
    kind: str  # pg_type.typtype: b(ase), d(omain), e(num), r(ange), m(ultirange), c(omposite), p(seudo)
    category: str
    element: int
    base: int
    typmod: int = -1  # a domain's own modifier (a numeric's scale)
    subtype: int = 0  # a range's subtype, or a multirange's range


def _pg_rule(
    types: Mapping[int, _PgType], oid: int, typmod: int = -1, depth: int = 0
) -> str:
    """The rule a PostgreSQL column type is read by (:data:`_PG_RULES`).

    Raises ``ValueError`` naming the type when there is none: an extension's
    type, a composite, a ``reg*`` alias (it prints by ``search_path``), and
    anything else whose text this module hasn't been shown to be exact.
    """

    t = types.get(oid)
    if t is None or depth > 8:
        raise ValueError(f"type oid {oid} is not in the catalog")
    label = f"{t.schema}.{t.name}"
    if t.kind == "d":
        return _pg_rule(types, t.base, t.typmod, depth + 1)
    if t.kind == "e":
        return "text"
    if t.kind in ("r", "m"):
        inner = _pg_rule(types, t.subtype, -1, depth + 1)
        if inner == "money":
            raise ValueError(f"{label} holds money, which prints by locale")
        return "text"
    if t.kind == "b" and t.category == "A" and t.element:
        inner = _pg_rule(types, t.element, -1, depth + 1)
        if inner == "money":
            raise ValueError(f"{label} holds money, which prints by locale")
        return "text"
    if t.kind == "b" and t.schema == "pg_catalog" and t.name in _PG_RULES:
        if t.name == "numeric" and typmod == -1:
            return "numeric_free"
        return _PG_RULES[t.name]
    raise ValueError(f"no exact reading rule for type {label}")


def _pg_cell(column: str, rule: str) -> str:
    q = _quote("postgresql", column)
    if rule == "bytea":
        return f"encode({q}, 'hex')"
    if rule == "money":
        return f"({q})::numeric::text"
    if rule == "numeric_free":
        return f"({q})::text"
    return f"({q})::text"


def _pg_value(rule: str, text: Optional[str]) -> Any:
    """A column's text (read by :func:`_pg_cell`) as its plain value."""

    if text is None:
        return None
    if rule == "int":
        return int(text)
    if rule == "bool":
        if text not in ("true", "false"):
            raise SnapshotError(f"a boolean read as {text!r}")
        return text == "true"
    if rule == "bytea":
        return bytes.fromhex(text)
    if rule == "numeric_free":
        return _stored_number(text)
    if rule in ("numeric", "money", "float4", "float8"):
        number = Decimal(text)
        if not number.is_finite():
            return number
        if rule == "float4":
            return _float32_text(number)
        if rule == "float8":
            return Decimal(repr(float(number)))
        exact = Context(prec=max(len(number.as_tuple().digits), 1))
        return Decimal(format(number.normalize(exact), "f"))
    return str(text)


def _pg_in(names: Sequence[str]) -> str:
    return "(" + ", ".join(_literal(n) for n in names) + ")"


@dataclass
class _PgSequence:
    name: str
    relation: str
    increment: int
    readable: bool
    definition: list[Any]
    #: The table and column that own it (``serial``, identity), if any.
    table: Optional[str] = None
    column: Optional[str] = None
    #: The last value drawn (the start minus one step when none was).
    last: Optional[int] = None
    #: ``last_value`` and ``is_called`` as stored.
    raw: Optional[int] = None
    is_called: Optional[bool] = None


def _pg_scope(
    s: _Session, schemas: Sequence[str], skipped: Sequence[str] = ()
) -> tuple[list[str], str]:
    """The schemas a PostgreSQL snapshot reads, and the default one (whose
    tables are named without it).

    Every schema but the system ones is read, as on MySQL. ``schemas`` must
    each be visible. A schema is left out only when the allow-list skips it
    (``skipped``: ``<schema>.*`` with mode ``skip`` and a reason); any other
    schema the account can't see is refused rather than left out."""

    every = {
        str(n): bool(u)
        for n, u in s.rows(
            "SELECT nspname, has_schema_privilege(oid, 'USAGE') FROM pg_namespace"
        )
        if not _pg_system(str(n))
    }
    unknown = sorted(set(skipped) - set(every))
    if unknown:
        raise SnapshotError(
            "allow-listed schemas not in the database: " + ", ".join(unknown)
        )
    missing = sorted(x for x in schemas if not every.get(x) or x in skipped)
    if missing:
        raise SnapshotError(
            "the read account can't see schema(s): " + ", ".join(missing)
        )
    hidden = sorted(n for n, u in every.items() if not u and n not in skipped)
    if hidden:
        raise SnapshotError(
            "the read account can't see schema(s) "
            + ", ".join(hidden)
            + ": grant it USAGE and SELECT there, or allow-list each as "
            "'<schema>.*' with mode skip and a reason"
        )
    scope = sorted(n for n in every if n not in skipped)
    default = str(s.rows("SELECT current_schema()")[0][0] or "")
    return scope, default


def _pg_name(schema: str, table: str, default: str) -> str:
    return table if schema == default else f"{schema}.{table}"


def _pg_relation(schema: str, table: str) -> str:
    return f"{_quote('postgresql', schema)}.{_quote('postgresql', table)}"


def _pg_sequences(
    s: _Session, scope: Sequence[str], default: str
) -> dict[str, _PgSequence]:
    """Every sequence in scope, with its value. Read before the snapshot
    starts (a sequence isn't transactional): a row committed in between is
    in the snapshot, so the counter never runs ahead of what it shows."""

    out: dict[str, _PgSequence] = {}
    for row in s.rows(
        "SELECT n.nspname, c.relname, s.seqincrement, s.seqstart, s.seqmin, "
        "s.seqmax, s.seqcycle, format_type(s.seqtypid, NULL), "
        "has_sequence_privilege(c.oid, 'SELECT'), tn.nspname, t.relname, a.attname "
        "FROM pg_sequence s JOIN pg_class c ON c.oid = s.seqrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_depend d ON d.classid = 'pg_class'::regclass "
        "AND d.objid = c.oid AND d.refclassid = 'pg_class'::regclass "
        "AND d.deptype IN ('a', 'i') AND d.refobjsubid > 0 "
        "LEFT JOIN pg_class t ON t.oid = d.refobjid "
        "LEFT JOIN pg_namespace tn ON tn.oid = t.relnamespace "
        "LEFT JOIN pg_attribute a ON a.attrelid = d.refobjid "
        "AND a.attnum = d.refobjsubid "
        f"WHERE n.nspname IN {_pg_in(scope)} ORDER BY 1, 2"
    ):
        schema, name, inc, start, low, high, cycle, typ, ok, tschema, table, col = row
        out[_pg_name(str(schema), str(name), default)] = _PgSequence(
            _pg_name(str(schema), str(name), default),
            _pg_relation(str(schema), str(name)),
            int(inc),
            bool(ok),
            [int(inc), int(start), int(low), int(high), bool(cycle), str(typ)],
            None if table is None else _pg_name(str(tschema), str(table), default),
            None if col is None else str(col),
        )
    readable = [q for q in out.values() if q.readable]
    if readable:
        for name, last, called in s.rows(
            " UNION ALL ".join(
                f"SELECT {_literal(q.name)}, last_value, is_called FROM {q.relation}"
                for q in readable
            )
        ):
            q = out[str(name)]
            q.raw, q.is_called = int(last), bool(called)
            q.last = q.raw if called else q.raw - q.increment
    return out


def _pg_definitions(
    s: _Session, scope: Sequence[str], default: str
) -> tuple[dict[str, _Def], str]:
    """Every PostgreSQL table's definition (partitions, not their parents),
    and the digest of the database's code."""

    within = _pg_in(scope)
    types: dict[int, _PgType] = {}
    for oid, name, schema, kind, cat, elem, base, mod in s.rows(
        "SELECT t.oid, t.typname, n.nspname, t.typtype, t.typcategory, t.typelem, "
        "t.typbasetype, t.typtypmod FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace"
    ):
        types[int(oid)] = _PgType(
            str(name), str(schema), str(kind), str(cat), int(elem), int(base), int(mod)
        )
    # pg_range.rngmultitypid exists from PostgreSQL 14; read through JSON so
    # an older server gives NULL instead of an error.
    for rng, sub, multi in s.rows(
        "SELECT r.rngtypid, r.rngsubtype, (to_jsonb(r) ->> 'rngmultitypid')::oid "
        "FROM pg_range r"
    ):
        types[int(rng)].subtype = int(sub)
        if multi is not None and int(multi) in types:
            types[int(multi)].subtype = int(rng)
    tables: dict[int, tuple[str, str, list[Any]]] = {}
    out: dict[str, _Def] = {}
    parents: list[Any] = []
    for row in s.rows(
        "SELECT c.oid, n.nspname, c.relname, c.relkind, c.relpersistence, "
        "c.relrowsecurity, c.relforcerowsecurity, has_table_privilege(c.oid, 'SELECT'), "
        "CASE WHEN c.relrowsecurity THEN row_security_active(c.oid) ELSE false END "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE c.relkind IN ('r', 'p', 'f') AND n.nspname IN {within} ORDER BY 2, 3"
    ):
        oid, schema, table, kind, persistence, rls, force, select, filtered = row
        if kind == "p":
            # A partitioned table holds no rows of its own; its partitions
            # are read. Its definition is part of the database's code.
            parents.append([schema, table])
            continue
        name = _pg_name(str(schema), str(table), default)
        tables[int(oid)] = (name, str(schema), [kind, persistence, rls, force])
        d = out[name] = _Def(
            [], [], None, {}, {}, "", relation=_pg_relation(str(schema), str(table))
        )
        if kind == "f":
            d.unreadable = "a foreign table: its rows live outside this database"
        elif not select:
            d.unreadable = "the read account has no SELECT on it"
        elif filtered:
            d.unreadable = "row-level security hides rows from the read account"
    parts: dict[str, list[Any]] = {n: list(meta) for n, _, meta in tables.values()}
    for (
        oid,
        column,
        typ,
        mod,
        ctype,
        notnull,
        cdefault,
        identity,
        generated,
        coll,
    ) in s.rows(
        "SELECT a.attrelid, a.attname, a.atttypid, a.atttypmod, "
        "format_type(a.atttypid, a.atttypmod), "
        "a.attnotnull, pg_get_expr(ad.adbin, ad.adrelid), a.attidentity, a.attgenerated, "
        "co.collname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
        "LEFT JOIN pg_collation co ON co.oid = a.attcollation "
        "WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'f') "
        f"AND n.nspname IN {within} ORDER BY a.attrelid, a.attnum"
    ):
        if int(oid) not in tables:
            continue
        name = tables[int(oid)][0]
        d = out[name]
        d.columns.append(str(column))
        d.defaults[str(column)] = _default_text(cdefault, "postgresql")
        try:
            d.encode[str(column)] = _pg_rule(types, int(typ), int(mod))
        except ValueError as exc:
            d.encode[str(column)] = ""
            d.unreadable = d.unreadable or f"column {column}: {exc}"
        parts[name].append(
            [column, ctype, notnull, cdefault, identity, generated, coll]
        )
    for oid, column in s.rows(
        "SELECT i.indrelid, a.attname FROM pg_index i "
        "CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
        "WHERE i.indisprimary ORDER BY i.indrelid, k.ord"
    ):
        if int(oid) in tables:
            out[tables[int(oid)][0]].primary_key.append(str(column))
    for oid, con, kind, definition in s.rows(
        "SELECT conrelid, conname, contype, pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE conrelid <> 0 ORDER BY 1, 2"
    ):
        if int(oid) in tables:
            parts[tables[int(oid)][0]].append([con, kind, definition])
    for name, d in out.items():
        d.schema = _digest(parts[name])
    code = [
        parents,
        s.rows(
            "SELECT n.nspname, c.relname, t.tgname, t.tgenabled, pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE NOT t.tgisinternal AND n.nspname IN {within} ORDER BY 1, 2, 3"
        ),
        s.rows(
            "SELECT n.nspname, c.relname, c.relkind, pg_get_viewdef(c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE c.relkind IN ('v', 'm') AND n.nspname IN {within} ORDER BY 1, 2"
        ),
        s.rows(
            "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), "
            "p.prokind, p.prosrc, p.probin, p.proconfig::text, p.prosecdef, l.lanname "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            f"WHERE n.nspname IN {within} ORDER BY 1, 2, 3"
        ),
        s.rows(
            "SELECT schemaname, tablename, rulename, definition FROM pg_rules "
            f"WHERE schemaname IN {within} ORDER BY 1, 2, 3"
        ),
        s.rows(
            "SELECT schemaname, tablename, policyname, permissive, roles::text, cmd, "
            f"qual, with_check FROM pg_policies WHERE schemaname IN {within} "
            "ORDER BY 1, 2, 3"
        ),
        s.rows(
            "SELECT evtname, evtevent, evtenabled, evtfoid::regproc::text, evttags::text "
            "FROM pg_event_trigger ORDER BY 1"
        ),
        s.rows("SELECT extname, extversion FROM pg_extension ORDER BY 1"),
        s.rows(
            "SELECT n.nspname, t.typname, t.typtype, "
            "format_type(t.typbasetype, t.typtypmod), t.typnotnull, t.typdefault, "
            "(SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) "
            "FROM pg_enum e WHERE e.enumtypid = t.oid), "
            "(SELECT string_agg(pg_get_constraintdef(c.oid), ';' ORDER BY c.conname) "
            "FROM pg_constraint c WHERE c.contypid = t.oid) "
            "FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            f"WHERE t.typtype IN ('d', 'e', 'r') AND n.nspname IN {within} "
            "ORDER BY 1, 2"
        ),
    ]
    return out, _digest(code)


def _pg_counters(
    defs: dict[str, _Def], sequences: Mapping[str, _PgSequence]
) -> tuple[dict[str, int], list[_PgSequence]]:
    """Each table's counter (the sequence its key, or else its first
    numbered column, owns), and every other sequence, which is read as a
    one-row table of its own."""

    owned: dict[str, list[_PgSequence]] = {}
    for q in sequences.values():
        if q.table in defs and q.column in defs[q.table].columns:
            owned.setdefault(q.table, []).append(q)
    counters: dict[str, int] = {}
    used: set[str] = set()
    for table, qs in owned.items():
        d = defs[table]
        qs.sort(key=lambda q: d.columns.index(q.column or ""))
        pick = next((q for q in qs if d.primary_key == [q.column]), qs[0])
        if pick.increment <= 0:
            continue  # a descending sequence is read as its own table
        d.auto_column = pick.column
        if d.primary_key == [pick.column]:
            d.generated_key = pick.column
        used.add(pick.name)
        if pick.last is not None:
            counters[table] = pick.last
        else:
            d.unreadable = d.unreadable or (
                f"the read account has no SELECT on its sequence {pick.name}"
            )
    return counters, [q for n, q in sequences.items() if n not in used]


def _pg_sequence_def(q: _PgSequence) -> _Def:
    """A sequence no table counts by, as a one-row table: a value drawn from
    it is a change, like any other row."""

    columns = ["sequence", "last_value", "is_called"]
    d = _Def(
        columns,
        ["sequence"],
        None,
        dict.fromkeys(columns),
        {},
        _digest(["sequence", *q.definition]),
        relation=q.relation,
        fixed=[],
    )
    if q.raw is None:
        d.unreadable = "a sequence the read account has no SELECT on"
    else:
        d.fixed = [{"sequence": q.name, "last_value": q.raw, "is_called": q.is_called}]
    return d


#: The name the frame and the allow-list use for PostgreSQL's large objects.
LARGE_OBJECTS = "pg_largeobject"


def _pg_large_objects() -> _Def:
    """Large objects as one table: each object's oid, owner, privileges and
    the SHA-256 of its content. They live in ``pg_catalog``, which is
    otherwise not read, and a column that holds an object's oid keeps it when
    the content changes (``lo_put``). The content is read only when there
    are objects; one the account can't read is refused."""

    columns = ["oid", "owner", "acl", "sha256"]
    return _Def(
        columns,
        ["oid"],
        None,
        dict.fromkeys(columns),
        dict.fromkeys(columns, "text"),
        _digest(["large objects", *columns]),
        relation="pg_catalog.pg_largeobject_metadata",
        select=(
            "SELECT m.oid::text, m.lomowner::regrole::text, m.lomacl::text, "
            "encode(sha256(lo_get(m.oid)), 'hex') FROM pg_largeobject_metadata m"
        ),
    )


def _read_rows_pg(s: _Session, d: _Def) -> list[dict[str, Any]]:
    if d.select:
        try:
            rows = s.rows(d.select)
        except Exception as exc:  # noqa: BLE001 - e.g. no SELECT on an object
            raise SnapshotError(
                f"large objects can't be read ({exc}); grant the read account "
                f"SELECT on them, or allow-list {LARGE_OBJECTS} with mode skip "
                "and a reason"
            ) from exc
        return [{c: _pg_value("text", v) for c, v in zip(d.columns, r)} for r in rows]
    cells = ", ".join(_pg_cell(c, d.encode[c]) for c in d.columns) or "NULL"
    return [
        {c: _pg_value(d.encode[c], v) for c, v in zip(d.columns, r)}
        for r in s.rows(f"SELECT {cells} FROM ONLY {d.relation}")
    ]


def key_text(primary_key: Sequence[str], row: Mapping[str, Any]) -> str:
    """A keyed row's key as text: a JSON array of its key columns'
    canonical values, so (``x|y``, ``z``) and (``x``, ``y|z``) differ."""

    return json.dumps([canonical(row.get(c)) for c in primary_key])


def _row_key(
    d: _Def, row: Mapping[str, Any], seen: dict[str, int], skip: Sequence[str] = ()
) -> str:
    if d.primary_key:
        return key_text(d.primary_key, row)
    # No key: the row's contents, without the columns a rule lets move (a
    # counter row stays the same row when its counter moves).
    ignored = set(skip)
    body = json.dumps(
        {c: v for c, v in row.items() if c not in ignored}, sort_keys=True, default=str
    )
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    seen[digest] = seen.get(digest, 0) + 1
    return f"{digest}#{seen[digest]}"


def _mysql_counters(s: _Session) -> dict[str, int]:
    default = s.rows("SELECT DATABASE()")[0][0]
    out: dict[str, int] = {}
    for schema, table, value in s.rows(
        "SELECT TABLE_SCHEMA, TABLE_NAME, AUTO_INCREMENT FROM information_schema.TABLES "
        "WHERE AUTO_INCREMENT IS NOT NULL"
    ):
        name = str(table) if schema == default else f"{schema}.{table}"
        out[name] = int(value)
    return out


def _sees_triggers(s: _Session) -> bool:
    """Can this MySQL account see triggers (it needs TRIGGER)?"""

    grants = " ".join(str(r[0]) for r in s.rows("SHOW GRANTS FOR CURRENT_USER()"))
    upper = grants.upper()
    return "ALL PRIVILEGES" in upper or "TRIGGER" in upper


def take_snapshot(
    connect: Callable[[], Any],
    dialect: Dialect,
    *,
    key: bytes,
    modes: Mapping[str, TableMode] = {},
    max_rows: int = 2000,
    before: Optional[Snapshot] = None,
    extra_queries: Mapping[str, str] = {},
    ignore: Mapping[str, Sequence[str]] = {},
    ref_columns: Sequence[str] = (),
    schemas: Sequence[str] = (),
    optional_modes: Mapping[str, TableMode] = {},
    links: bool = False,
) -> Snapshot:
    """Read every table in one read-only transaction.

    Args:
        connect: Opens one connection (DB-API 2.0: ``cursor()``, ``close()``).
        key: The MAC key (:func:`mac_key`).
        modes: Table -> ``append_only`` or ``skip`` (allow-listed tables);
            every other table is compared.
        max_rows: Over this many rows, a table keeps cell MACs only for
            ``ref_columns`` (and a contents MAC for every row).
        before: The baseline, when this is a read after the save: plain
            values are kept for the rows that differ from it.
        extra_queries: Name -> a SELECT to run in the same transaction (the
            audit log); rows land in :attr:`Snapshot.extra`, in memory only.
        ignore: Table -> columns left out of each row's contents MAC.
        ref_columns: Columns attribution and side records read (a record
            key, a link field): their cell MACs are kept in every table.
        schemas: Schemas the read must see; refused if one isn't. Every
            visible schema is read either way. On PostgreSQL a schema is left
            out only by a ``<schema>.*`` entry in ``modes`` with ``skip``.
        optional_modes: Like ``modes``, for tables an app profile names that
            this database may not have (a profile covers several versions).
            ``ignore`` may name ``*`` for every table.
        links: Keep each keyed row's link MAC (:attr:`Table.links`).

    Raises:
        SnapshotError: the database can't be read as the frame needs it.
    """

    try:
        s = _Session(connect, dialect)
    except Exception as exc:  # noqa: BLE001
        raise SnapshotError(f"could not open a connection: {exc}") from exc
    try:
        return _snapshot(
            s,
            key,
            modes,
            max_rows,
            before,
            extra_queries,
            ignore,
            ref_columns,
            schemas,
            optional_modes,
            link_key() if links else None,
        )
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
    ignore: Mapping[str, Sequence[str]],
    ref_columns: Sequence[str],
    schemas: Sequence[str],
    optional_modes: Mapping[str, TableMode] = {},
    links: Optional[bytes] = None,
) -> Snapshot:
    dialect = s.dialect
    counters: dict[str, int] = {}
    visible = True
    sequences: dict[str, _PgSequence] = {}
    scope: list[str] = []
    default_schema = ""
    if dialect == "mysql":
        # Before the snapshot starts: a row committed after this read is in
        # the snapshot, so the counter never runs ahead of what it shows.
        counters = _mysql_counters(s)
        visible = _sees_triggers(s)
    elif dialect == "postgresql":
        skipped_schemas = sorted(
            t[:-2] for t, m in modes.items() if t.endswith(".*") and m == "skip"
        )
        scope, default_schema = _pg_scope(s, schemas, skipped_schemas)
        sequences = _pg_sequences(s, scope, default_schema)  # before, as above
    s.begin()
    if dialect == "postgresql":
        # The first statement takes the snapshot; its start is the clock.
        clock, isolation, read_only = s.rows(
            "SELECT statement_timestamp()::text, "
            "current_setting('transaction_isolation'), "
            "current_setting('transaction_read_only')"
        )[0]
        if (isolation, read_only) != ("repeatable read", "on"):
            raise SnapshotError(
                f"the read ran as {isolation}, read_only={read_only}, not one "
                "read-only repeatable-read transaction"
            )
    else:
        clock = s.rows("SELECT CURRENT_TIMESTAMP")[0][0]
    snap = Snapshot(dialect=dialect, clock=_clock_text(clock), code_visible=visible)
    if dialect == "postgresql":
        defs, snap.objects = _pg_definitions(s, scope, default_schema)
        counters, others = _pg_counters(defs, sequences)
        for q in others:
            if q.name in defs:
                raise SnapshotError(f"sequence {q.name!r} has the name of a table")
            defs[q.name] = _pg_sequence_def(q)
        if LARGE_OBJECTS in defs:
            # A table of that name would be replaced, and so never read.
            raise SnapshotError(
                f"a table is named {LARGE_OBJECTS!r}, which the snapshot uses"
            )
        defs[LARGE_OBJECTS] = _pg_large_objects()
        snap.schemas = list(scope)
        snap.schemas_skipped = skipped_schemas
        modes = {t: m for t, m in modes.items() if not t.endswith(".*")}
    else:
        defs, snap.objects = _definitions(s)
    if dialect == "mysql":
        default = str(s.rows("SELECT DATABASE()")[0][0])
        seen = {default} | {t.split(".", 1)[0] for t in defs if "." in t}
        missing = sorted(set(schemas) - seen)
        if missing:
            raise SnapshotError(
                "the read account can't see schema(s): " + ", ".join(missing)
            )
        snap.schemas = sorted(seen)
    else:
        if dialect == "sqlite" and any(
            n == "sqlite_sequence"
            for (n,) in s.rows("SELECT name FROM sqlite_master WHERE type = 'table'")
        ):
            counters = {
                str(n): int(v)
                for n, v in s.rows("SELECT name, seq FROM sqlite_sequence")
            }
    for name, sql in extra_queries.items():
        snap.extra[name] = s.dicts(sql)
    unknown = sorted(set(modes) - set(defs))
    modes = {**{t: m for t, m in optional_modes.items() if t in defs}, **modes}
    if unknown:
        raise SnapshotError(
            "allow-listed tables not in the database: " + ", ".join(unknown)
        )
    names = sorted(t for t in defs if modes.get(t) != "skip")
    for t in sorted(t for t in defs if modes.get(t) == "skip"):
        d = defs[t]
        snap.tables[t] = Table("skip", d.schema, d.columns, d.primary_key, None, {})
    refused = sorted(f"{t} ({defs[t].unreadable})" for t in names if defs[t].unreadable)
    if refused:
        raise SnapshotError(
            "can't read exactly: "
            + "; ".join(refused)
            + ". Skip a table only by allow-listing it with mode skip and a reason"
        )
    if not names:
        return snap

    def rel(t: str) -> str:
        return defs[t].relation or _quote(dialect, t)

    stored = [t for t in names if defs[t].fixed is None]
    counts = {t: len(defs[t].fixed or []) for t in names if defs[t].fixed is not None}
    if stored:
        counts.update(
            (str(t), int(n))
            for t, n in s.rows(
                " UNION ALL ".join(
                    f"SELECT {_literal(t)}, COUNT(*) FROM {rel(t)}" for t in stored
                )
            )
        )
    numbered = [t for t in names if defs[t].auto_column]
    maxes = (
        {
            str(t): (None if m is None else int(m))
            for t, m in s.rows(
                " UNION ALL ".join(
                    f"SELECT {_literal(t)}, MAX({_quote(dialect, defs[t].auto_column or '')}) "
                    f"FROM {rel(t)}"
                    for t in numbered
                )
            )
        }
        if numbered
        else {}
    )
    refs = set(ref_columns)
    for t in names:
        d = defs[t]
        mode: TableMode = "append_only" if modes.get(t) == "append_only" else "compare"
        table = Table(
            mode,
            d.schema,
            d.columns,
            d.primary_key,
            d.generated_key,
            dict(d.defaults),
            counts.get(t, 0),
            counters.get(t),
            d.auto_column,
            maxes.get(t),
            non_transactional=d.non_transactional,
        )
        if d.generated_key:
            # Only the table's own generated key may hold any value.
            table.defaults[d.generated_key] = ANY_VALUE
        old = before.tables.get(t) if before is not None else None
        if mode == "append_only":
            table.cell_columns = []
        elif old is not None and old.mode == mode and old.schema == d.schema:
            table.cell_columns = list(old.cell_columns)  # read like the baseline
        elif table.count > max_rows:
            table.cell_columns = [c for c in d.columns if c in refs]
        else:
            table.cell_columns = list(d.columns)
        skip = [*ignore.get(t, ()), *ignore.get("*", ())]
        seen_keys: dict[str, int] = {}
        if d.fixed is not None:
            read = d.fixed
        elif not table.count:
            read = []
        elif dialect == "postgresql":
            read = _read_rows_pg(s, d)
        else:
            read = _read_rows(s, t, d)
        for row in read:
            text = _row_key(d, row, seen_keys, skip)
            k = row_key_hash(key, text, t)
            if links is not None and d.primary_key and mode == "compare":
                table.links[k] = row_key_hash(links, text, t)
            mac = row_hash(key, t, row, skip)
            table.rows[k] = [
                mac,
                "".join(cell_hash(key, row.get(c), t, c) for c in table.cell_columns),
            ]
            if old is not None and (k not in old.rows or old.rows[k][0] != mac):
                table.values[k] = dict(row)
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
    #: Moved in a way no row comparison explains: why (a definition change,
    #: an append-only table whose existing rows moved, a row written and
    #: removed).
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
    if old.mode == "append_only" and any(
        k not in new.rows or new.rows[k][0] != v[0] for k, v in old.rows.items()
    ):
        reasons.append("existing rows changed or removed")
    if new.counter is not None:
        # sqlite_sequence and a PostgreSQL sequence hold the last number
        # used; AUTO_INCREMENT the next.
        step = 1 if dialect == "mysql" else 0
        expected = max(
            old.counter or 0, 0 if new.max_auto is None else new.max_auto + step
        )
        if new.counter > expected:
            reasons.append("a row was written and removed (counter moved)")
        if old.counter is not None and new.counter < old.counter:
            # A PostgreSQL sequence can be set back (setval); later inserts
            # would then reuse numbers.
            reasons.append("the counter went back")
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
        moved = TableDiff(unread=unread_reasons(a, b, after.dialect))
        if a is not None and b is not None and a.mode == "compare" == b.mode:
            if a.columns == b.columns and a.cell_columns == b.cell_columns:
                moved.added = len(b.rows.keys() - a.rows.keys())
                moved.removed = len(a.rows.keys() - b.rows.keys())
                columns: set[str] = set()
                for k in a.rows.keys() & b.rows.keys():
                    if a.rows[k][0] == b.rows[k][0]:
                        continue
                    moved.changed += 1
                    old = unpack(a.cell_columns, a.rows[k][1])
                    new = unpack(b.cell_columns, b.rows[k][1])
                    cols = [c for c in a.cell_columns if old[c] != new[c]]
                    columns.update(cols or ["(columns not compared)"])
                moved.changed_columns = sorted(columns)
        if moved.added or moved.removed or moved.changed or moved.unread:
            out[name] = moved
    return out


def settled_state(a: Snapshot, b: Snapshot) -> bool:
    """Do two reads taken after the save show the same database, apart from
    rows added to append-only tables (the app's logs, which the frame
    excuses)? An append-only table's existing rows must not change, and
    every other table must be the same, as in :func:`same_state`."""

    if (a.objects, a.code_visible) != (b.objects, b.code_visible):
        return False
    if set(a.tables) != set(b.tables):
        return False
    for name, x in a.tables.items():
        y = b.tables[name]
        if (x.mode, x.schema) != (y.mode, y.schema):
            return False
        if x.mode == "append_only":
            if any(y.rows.get(k) != v for k, v in x.rows.items()):
                return False
            continue
        if (x.count, x.counter, x.max_auto, x.rows) != (
            y.count,
            y.counter,
            y.max_auto,
            y.rows,
        ):
            return False
    return True


def same_state(a: Snapshot, b: Snapshot) -> bool:
    """Do two reads taken after the save show the same database?"""

    if (a.objects, a.code_visible) != (b.objects, b.code_visible):
        return False
    if set(a.tables) != set(b.tables):
        return False
    return all(
        (x.mode, x.schema, x.count, x.counter, x.max_auto, x.rows)
        == (y.mode, y.schema, y.count, y.counter, y.max_auto, y.rows)
        for x, y in ((a.tables[n], b.tables[n]) for n in a.tables)
    )
