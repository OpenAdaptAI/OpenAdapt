"""The sandbox a draft is qualified on: the app, its two actors, its store.

``sandbox.yaml`` (``openadapt.verify-sandbox/v1``) is what a person gives
once per system. It holds facts, not judgments: where the app answers, how
each of two users signs in, and two connections to the app's database (the
read-only one the contract will read with, and a privileged one that plants
faults and puts the store back between cases)::

    schema_version: openadapt.verify-sandbox/v1
    app:
      base_url: http://127.0.0.1:9000
    actors:
      agent:                      # the user the agent writes as
        id: "2"                   # that user's id as the store holds it
        headers: {Authorization: "Bearer ${AGENT_TOKEN}"}
      other:                      # a second, ordinary user
        id: "3"
        login: {method: POST, path: /login, form: {username: lee, password: ${LEE_PW}}}
    store:
      dialect: postgresql         # or sqlite, mysql
      read: {sql_driver: psycopg2, sql_connect_args: {...}, sql_password_env: APP_RO_PW}
      admin: {sql_driver: psycopg2, sql_connect_args: {...}, sql_password_env: APP_PW}

Nothing here names an app. The store is reset between cases by restoring
the rows read at the start (no reset hook to write), and faults are planted
with the privileged connection, as the fault gate does: a fault is what the
app should never do.
"""

from __future__ import annotations

import base64
import copy
import datetime as _dt
import decimal
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

SANDBOX_SCHEMA = "openadapt.verify-sandbox/v1"

#: Request headers a replay never copies from the trace: the actor's own
#: sign-in replaces them, and the transport sets the rest.
_DROP_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "content-length",
        "connection",
        "accept-encoding",
        "x-csrf-token",
        "x-xsrf-token",
    }
)
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SandboxError(RuntimeError):
    """The sandbox can't be used; the message says why."""


def _expand(value: Any) -> Any:
    """``${NAME}`` from the environment, recursively. A missing name is an
    error, never an empty string."""

    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise SandboxError(f"environment variable {name} is not set")
            return os.environ[name]

        return _ENV.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = "POST"
    path: str
    form: Optional[dict[str, str]] = None
    json_body: Optional[dict[str, Any]] = Field(default=None, alias="json")
    #: A JSON response field holding a bearer token (``access_token``).
    token_field: Optional[str] = None


class ActorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: The user's id as the store holds it (bound as the ``actor_id`` param).
    id: str
    headers: dict[str, str] = Field(default_factory=dict)
    login: Optional[Login] = None


class StoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dialect: str
    read: dict[str, Any]
    admin: dict[str, Any]
    #: Schemas to read on PostgreSQL (default ``public``).
    schemas: list[str] = Field(default_factory=lambda: ["public"])


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    timeout_s: float = 30.0


class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = SANDBOX_SCHEMA
    app: AppConfig
    actors: dict[str, ActorConfig]
    store: StoreConfig

    def actor(self, name: str) -> ActorConfig:
        if name not in self.actors:
            raise SandboxError(f"sandbox.actors has no {name!r}")
        return self.actors[name]


def load_sandbox(path: str) -> SandboxConfig:
    import yaml

    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    cfg = SandboxConfig.model_validate(data)
    if cfg.schema_version != SANDBOX_SCHEMA:
        raise SandboxError(f"sandbox schema_version must be {SANDBOX_SCHEMA}")
    if "agent" not in cfg.actors:
        raise SandboxError("sandbox.actors needs an agent")
    if cfg.store.dialect not in ("sqlite", "postgresql", "mysql"):
        raise SandboxError("store.dialect is sqlite, postgresql or mysql")
    return cfg


# -- the store -----------------------------------------------------------------


Row = dict[str, Any]
#: table -> primary key (a tuple of values, or the row's position) -> row
Rows = dict[str, dict[Any, Row]]


@dataclass
class Table:
    name: str
    columns: list[str]
    types: dict[str, str]
    pk: list[str]
    fks: dict[str, str] = field(default_factory=dict)  # column -> table
    #: Column sets the store holds unique (besides the primary key).
    unique: list[list[str]] = field(default_factory=list)


def _connect(spec: Mapping[str, Any], dialect: str) -> Any:
    spec = _expand(dict(spec))
    if dialect == "sqlite" or spec.get("sqlite_database"):
        import sqlite3

        con = sqlite3.connect(spec["sqlite_database"], timeout=30)
        con.row_factory = None
        return con
    driver = spec.get("sql_driver")
    args = dict(spec.get("sql_connect_args") or {})
    env = spec.get("sql_password_env")
    if env:
        if env not in os.environ:
            raise SandboxError(f"environment variable {env} is not set")
        args["password"] = os.environ[env]
    if driver == "psycopg2":
        import psycopg2
        import psycopg2.extras

        psycopg2.extras.register_uuid()
        con = psycopg2.connect(**args)
        return con
    if driver == "pymysql":
        import pymysql

        args.setdefault("charset", "utf8mb4")
        return pymysql.connect(**args)
    raise SandboxError(f"unsupported sql_driver {driver!r}")


class Store:
    """The app's database through the privileged connection.

    Reads every table with values (synthetic sandbox data only), puts the
    store back to a snapshot, and plants faults.
    """

    def __init__(self, cfg: StoreConfig) -> None:
        self.cfg = cfg
        self.dialect = cfg.dialect
        self.tables = self._introspect()

    # -- plumbing

    def _con(self) -> Any:
        return _connect(self.cfg.admin, self.dialect)

    def q(self, name: str) -> str:
        if self.dialect == "mysql":
            return "`" + name.replace("`", "``") + "`"
        if "." in name and self.dialect == "postgresql":
            schema, _, table = name.partition(".")
            return f'"{schema}"."{table}"'
        return '"' + name.replace('"', '""') + '"'

    @property
    def mark(self) -> str:
        return "?" if self.dialect == "sqlite" else "%s"

    def _introspect(self) -> dict[str, Table]:
        con = self._con()
        try:
            cur = con.cursor()
            out: dict[str, Table] = {}
            if self.dialect == "sqlite":
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
                for (name,) in cur.fetchall():
                    cur.execute(f"PRAGMA table_info({self.q(name)})")
                    info = cur.fetchall()
                    cols = [r[1] for r in info]
                    types = {r[1]: (r[2] or "").lower() for r in info}
                    pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
                    cur.execute(f"PRAGMA foreign_key_list({self.q(name)})")
                    fks = {r[3]: r[2] for r in cur.fetchall()}
                    unique = []
                    cur.execute(f"PRAGMA index_list({self.q(name)})")
                    for idx in cur.fetchall():
                        if idx[2]:
                            cur.execute(f"PRAGMA index_info({self.q(idx[1])})")
                            unique.append([r[2] for r in cur.fetchall()])
                    out[name] = Table(name, cols, types, pk, fks, unique)
                return out
            if self.dialect == "postgresql":
                schemas = tuple(self.cfg.schemas)
                cur.execute(
                    "SELECT table_schema, table_name, column_name, data_type "
                    "FROM information_schema.columns WHERE table_schema = ANY(%s) "
                    "AND table_name IN (SELECT table_name FROM information_schema.tables "
                    "WHERE table_type='BASE TABLE' AND table_schema = ANY(%s)) "
                    "ORDER BY table_schema, table_name, ordinal_position",
                    (list(schemas), list(schemas)),
                )
                rows = cur.fetchall()
                cur.execute(
                    "SELECT tc.table_schema, tc.table_name, kcu.column_name, tc.constraint_type, "
                    "COALESCE(ccu.table_name, tc.constraint_name) FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
                    "LEFT JOIN information_schema.constraint_column_usage ccu "
                    "ON tc.constraint_name = ccu.constraint_name AND tc.constraint_type='FOREIGN KEY' "
                    "WHERE tc.table_schema = ANY(%s) AND tc.constraint_type IN ('PRIMARY KEY','FOREIGN KEY','UNIQUE') "
                    "ORDER BY kcu.ordinal_position",
                    (list(schemas),),
                )
                cons = cur.fetchall()
            else:
                cur.execute("SELECT DATABASE()")
                db = cur.fetchone()[0]
                cur.execute(
                    "SELECT table_schema, table_name, column_name, data_type FROM "
                    "information_schema.columns WHERE table_schema=%s AND table_name IN "
                    "(SELECT table_name FROM information_schema.tables WHERE "
                    "table_type='BASE TABLE' AND table_schema=%s) ORDER BY table_name, "
                    "ordinal_position",
                    (db, db),
                )
                rows = cur.fetchall()
                cur.execute(
                    "SELECT table_schema, table_name, column_name, IF(constraint_name="
                    "'PRIMARY','PRIMARY KEY','FOREIGN KEY'), referenced_table_name FROM "
                    "information_schema.key_column_usage WHERE table_schema=%s AND "
                    "(constraint_name='PRIMARY' OR referenced_table_name IS NOT NULL) "
                    "ORDER BY ordinal_position",
                    (db,),
                )
                cons = cur.fetchall()
            for schema, table, col, dtype in rows:
                name = self._name(schema, table)
                t = out.setdefault(name, Table(name, [], {}, []))
                t.columns.append(col)
                t.types[col] = str(dtype).lower()
            for schema, table, col, kind, ref in cons:
                t = out.get(self._name(schema, table))
                if t is None:
                    continue
                if kind == "PRIMARY KEY" and col not in t.pk:
                    t.pk.append(col)
                elif kind == "FOREIGN KEY" and ref:
                    t.fks[col] = self._name(schema, ref)
                elif kind == "UNIQUE":
                    groups = getattr(t, "_ugroups", {})
                    groups.setdefault(ref, []).append(col)
                    t._ugroups = groups  # type: ignore[attr-defined]
            for t in out.values():
                t.unique = [cols for cols in getattr(t, "_ugroups", {}).values()]
            return out
        finally:
            con.close()

    def _name(self, schema: str, table: str) -> str:
        if self.dialect == "postgresql" and schema != "public":
            return f"{schema}.{table}"
        return table

    def schema_text(self) -> str:
        """Tables, columns, types, keys: the introspected description."""

        lines = []
        for t in self.tables.values():
            cols = []
            for c in t.columns:
                bits = [c, t.types.get(c, "")]
                if c in t.pk:
                    bits.append("PK")
                if c in t.fks:
                    bits.append(f"-> {t.fks[c]}")
                cols.append(" ".join(b for b in bits if b))
            lines.append(f"{t.name}({', '.join(cols)})")
        return "\n".join(lines)

    # -- reads

    def _key(self, t: Table, row: Row, pos: int) -> Any:
        if t.pk:
            return tuple(row[c] for c in t.pk)
        return ("#", pos, _canon(row))

    def read(self, tables: Optional[Iterable[str]] = None) -> Rows:
        names = list(tables) if tables is not None else list(self.tables)
        con = self._con()
        try:
            cur = con.cursor()
            out: Rows = {}
            for name in names:
                t = self.tables[name]
                cols = ", ".join(self.q(c) for c in t.columns)
                cur.execute(f"SELECT {cols} FROM {self.q(name)}")
                rows = {}
                for i, values in enumerate(cur.fetchall()):
                    row = dict(zip(t.columns, values))
                    rows[self._key(t, row, i)] = row
                out[name] = rows
            if self.dialect != "sqlite":
                con.rollback()
            return out
        finally:
            con.close()

    # -- writes (privileged)

    def _exec(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        con = self._con()
        out = []
        try:
            cur = con.cursor()
            if self.dialect == "postgresql":
                cur.execute("SET session_replication_role = replica")
            elif self.dialect == "mysql":
                cur.execute("SET FOREIGN_KEY_CHECKS=0")
            else:
                cur.execute("PRAGMA foreign_keys=OFF")
            for sql, args in statements:
                cur.execute(sql, [_adapt(a, self.dialect) for a in args])
                out.append(cur.lastrowid if self.dialect != "postgresql" else None)
            con.commit()
            return out
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def digests(self) -> dict[str, str]:
        """One digest per table, computed in the database where it can be,
        so finding what moved doesn't read every row."""

        if self.dialect == "sqlite":
            return {name: _digest_rows(rows) for name, rows in self.read().items()}
        con = self._con()
        try:
            cur = con.cursor()
            if self.dialect == "postgresql":
                parts = [
                    f"SELECT {i} AS n, md5(coalesce(string_agg(md5(t::text), '' "
                    f"ORDER BY md5(t::text)), '')) FROM {self.q(name)} t"
                    for i, name in enumerate(self.tables)
                ]
                cur.execute(" UNION ALL ".join(parts))
                names = list(self.tables)
                out = {names[int(n)]: str(d) for n, d in cur.fetchall()}
            else:
                cur.execute(
                    "CHECKSUM TABLE " + ", ".join(self.q(n) for n in self.tables)
                )
                found = {str(r[0]).split(".", 1)[-1]: str(r[1]) for r in cur.fetchall()}
                out = {n: found.get(n, "") for n in self.tables}
            con.rollback()
            return out
        finally:
            con.close()

    def remember(self, base: Rows) -> None:
        """Remember ``base`` (a full read) and its digests, so restoring it
        and reading what moved since touch only the tables that moved."""

        self._marked = (base, self.digests())

    def _moved_tables(self, snapshot: Rows) -> Optional[list[str]]:
        marked = getattr(self, "_marked", None)
        if marked is None or marked[0] is not snapshot:
            return None
        now = self.digests()
        return [t for t, d in marked[1].items() if now.get(t) != d]

    def read_moved(self, base: Rows) -> Rows:
        """A full read, fetching only the tables that moved since ``base``
        (when ``base`` is the marked read)."""

        moved = self._moved_tables(base)
        if moved is None:
            return self.read()
        out = dict(base)
        out.update(self.read(moved))
        return out

    def restore(self, snapshot: Rows) -> int:
        """Put every table that differs from ``snapshot`` back. Returns how
        many tables were rewritten."""

        moved = self._moved_tables(snapshot)
        now = self.read(moved) if moved is not None else self.read()
        statements: list[tuple[str, Sequence[Any]]] = []
        changed = 0
        for name, rows in snapshot.items():
            if moved is not None and name not in moved:
                continue
            if _digest_rows(now.get(name, {})) == _digest_rows(rows):
                continue
            changed += 1
            t = self.tables[name]
            statements.append((f"DELETE FROM {self.q(name)}", ()))
            cols = ", ".join(self.q(c) for c in t.columns)
            marks = ", ".join([self.mark] * len(t.columns))
            for row in rows.values():
                statements.append(
                    (
                        f"INSERT INTO {self.q(name)} ({cols}) VALUES ({marks})",
                        [row[c] for c in t.columns],
                    )
                )
        if statements:
            self._exec(statements)
        return changed

    def update(self, table: str, key: tuple, changes: Mapping[str, Any]) -> None:
        t = self.tables[table]
        sets = ", ".join(f"{self.q(c)} = {self.mark}" for c in changes)
        where = " AND ".join(f"{self.q(c)} = {self.mark}" for c in t.pk)
        self._exec(
            [
                (
                    f"UPDATE {self.q(table)} SET {sets} WHERE {where}",
                    list(changes.values()) + list(key),
                )
            ]
        )

    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        cols = ", ".join(self.q(c) for c in row)
        marks = ", ".join([self.mark] * len(row))
        self._exec(
            [
                (
                    f"INSERT INTO {self.q(table)} ({cols}) VALUES ({marks})",
                    list(row.values()),
                )
            ]
        )

    def delete(self, table: str, key: tuple) -> None:
        t = self.tables[table]
        where = " AND ".join(f"{self.q(c)} = {self.mark}" for c in t.pk)
        self._exec([(f"DELETE FROM {self.q(table)} WHERE {where}", list(key))])

    def new_key_row(self, table: str, row: Row, existing: Rows) -> Row:
        """``row`` with a primary key no row of ``table`` holds, and with
        any other unique column the store would refuse to repeat changed
        (a second chart under a near-identical number)."""

        t = self.tables[table]
        out = dict(row)
        taken = existing.get(table, {})
        for c in t.pk:
            out[c] = _fresh(
                row[c],
                [
                    k[t.pk.index(c)]
                    for k in taken
                    if isinstance(k, tuple) and len(k) == len(t.pk)
                ],
            )
        for cols in t.unique:
            if any(out.get(c) is None for c in cols):
                continue
            for c in cols:
                if c not in t.pk:
                    out[c] = _fresh(row[c], [r.get(c) for r in taken.values()])
        return out


def _fresh(value: Any, taken: Sequence[Any]) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max([v for v in taken if isinstance(v, int)] + [value]) + 1000
    if isinstance(value, uuid.UUID):
        return uuid.uuid4()
    if isinstance(value, str):
        try:
            uuid.UUID(value)
            return str(uuid.uuid4()) if "-" in value else uuid.uuid4().hex
        except ValueError:
            pass
        if value.isdigit():
            return str(
                max([int(v) for v in taken if str(v).isdigit()] + [int(value)]) + 1000
            )
        return value + "-copy"
    return value


def _adapt(value: Any, dialect: str) -> Any:
    if dialect == "postgresql" and isinstance(value, (dict, list)):
        import psycopg2.extras

        return psycopg2.extras.Json(value)
    if dialect == "mysql" and isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _canon(value: Any) -> Any:
    if isinstance(value, dict):
        return json.dumps({k: _canon(v) for k, v in value.items()}, sort_keys=True)
    if isinstance(value, (list, tuple)):
        return json.dumps([_canon(v) for v in value])
    if isinstance(value, (bytes, memoryview)):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(
        value, (_dt.date, _dt.datetime, _dt.time, decimal.Decimal, uuid.UUID)
    ):
        return str(value)
    return value


def _digest_rows(rows: Mapping[Any, Row]) -> str:
    import hashlib

    items = sorted(
        json.dumps({k: _canon(v) for k, v in r.items()}, sort_keys=True, default=str)
        for r in rows.values()
    )
    return hashlib.sha256("\n".join(items).encode()).hexdigest()


def text(value: Any) -> str:
    """A stored value as comparable text."""

    v = _canon(value)
    return "" if v is None else str(v)


# -- moves between two reads ---------------------------------------------------


@dataclass
class Move:
    table: str
    key: Any
    change: str  # added | removed | changed
    columns: list[str]
    before: Optional[Row]
    after: Optional[Row]


def moves(before: Rows, after: Rows) -> list[Move]:
    out: list[Move] = []
    for table in sorted(set(before) | set(after)):
        b, a = before.get(table, {}), after.get(table, {})
        for key in a.keys() - b.keys():
            out.append(Move(table, key, "added", sorted(a[key]), None, a[key]))
        for key in b.keys() - a.keys():
            out.append(Move(table, key, "removed", sorted(b[key]), b[key], None))
        for key in a.keys() & b.keys():
            cols = sorted(c for c in a[key] if text(a[key][c]) != text(b[key].get(c)))
            if cols:
                out.append(Move(table, key, "changed", cols, b[key], a[key]))
    return out


# -- the app -------------------------------------------------------------------


@dataclass
class Request:
    """One request from the trace."""

    method: str
    url: str
    headers: dict[str, str]
    body: Optional[str]
    mime: str
    status: Optional[int] = None
    response: Optional[str] = None

    @property
    def path(self) -> str:
        return urlsplit(self.url).path


class App:
    """The app over HTTP, as one of the sandbox's actors."""

    def __init__(self, cfg: SandboxConfig) -> None:
        self.cfg = cfg
        self._sessions: dict[str, Any] = {}

    def login_paths(self) -> set[str]:
        return {a.login.path for a in self.cfg.actors.values() if a.login}

    def session(self, actor: str) -> Any:
        import requests

        if actor in self._sessions:
            return self._sessions[actor]
        spec = self.cfg.actor(actor)
        s = requests.Session()
        s.headers.update(_expand(dict(spec.headers)))
        if spec.login is not None:
            login = spec.login
            url = self.cfg.app.base_url.rstrip("/") + login.path
            r = s.request(
                login.method,
                url,
                data=_expand(login.form) if login.form else None,
                json=_expand(login.json_body) if login.json_body else None,
                allow_redirects=False,
                timeout=self.cfg.app.timeout_s,
            )
            if r.status_code >= 400:
                raise SandboxError(f"actor {actor} could not sign in ({r.status_code})")
            if login.token_field:
                token = (r.json() or {}).get(login.token_field)
                if not token:
                    raise SandboxError(
                        f"sign-in for {actor} returned no {login.token_field}"
                    )
                s.headers["Authorization"] = f"Bearer {token}"
        self._sessions[actor] = s
        return s

    def send(
        self,
        writes: Sequence[Request],
        actor: str,
        swap: Mapping[str, Any],
        *,
        repeat: int = 1,
    ) -> list[int]:
        """Replay the trace's writes as ``actor``, with each demonstrated
        value in ``swap`` replaced by its new value. Returns the statuses."""

        s = self.session(actor)
        base = urlsplit(self.cfg.app.base_url)
        statuses = []
        for _ in range(repeat):
            for req in writes:
                parts = urlsplit(req.url)
                path = _swap_path(parts.path, swap)
                query = urlencode(
                    [
                        (k, _swap_text(v, swap))
                        for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    ]
                )
                url = urlunsplit((base.scheme, base.netloc, path, query, ""))
                headers = {
                    k: v
                    for k, v in req.headers.items()
                    if k.lower() not in _DROP_HEADERS
                }
                body = _swap_body(req.body, req.mime, swap)
                r = s.request(
                    req.method,
                    url,
                    data=body.encode("utf-8") if isinstance(body, str) else body,
                    headers=headers,
                    allow_redirects=False,
                    timeout=self.cfg.app.timeout_s,
                )
                statuses.append(r.status_code)
        return statuses


def _swap_text(value: str, swap: Mapping[str, Any]) -> str:
    return str(swap[value]) if value in swap else value


def _swap_path(path: str, swap: Mapping[str, Any]) -> str:
    return "/".join(
        quote(str(swap[seg]), safe="") if seg in swap else seg
        for seg in path.split("/")
    )


def _swap_json(value: Any, swap: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: _swap_json(v, swap) for k, v in value.items()}
    if isinstance(value, list):
        return [_swap_json(v, swap) for v in value]
    if value is None or isinstance(value, bool):
        return value
    key = str(value)
    if key in swap:
        new = swap[key]
        if isinstance(value, (int, float)) and not isinstance(new, (int, float)):
            try:
                return type(value)(new)
            except (TypeError, ValueError):
                return new
        return new
    return value


def _swap_body(
    body: Optional[str], mime: str, swap: Mapping[str, Any]
) -> Optional[str]:
    if body is None or not swap:
        return body
    if "json" in mime:
        try:
            return json.dumps(_swap_json(json.loads(body), swap))
        except ValueError:
            return body
    if "x-www-form-urlencoded" in mime:
        return urlencode(
            [
                (k, _swap_text(v, swap))
                for k, v in parse_qsl(body, keep_blank_values=True)
            ]
        )
    return body


def swap_map(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Demonstrated value (as text) -> new value, for each param that changes."""

    out: dict[str, Any] = {}
    for name, value in new.items():
        if name in old and old[name] is not None and text(old[name]) != text(value):
            out[text(old[name])] = value
    return out


def deep(value: Any) -> Any:
    return copy.deepcopy(value)
