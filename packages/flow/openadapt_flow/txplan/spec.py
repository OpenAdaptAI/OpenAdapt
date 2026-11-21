"""``openadapt.tx.yaml``: the external systems a plan may write, and how.

A plan is a batch of writes to systems outside this computer. Each system
says how OpenAdapt reads a record, lists a collection (the frame), makes each
kind of write, and undoes it::

    schema_version: openadapt.tx/v1
    holdback_s: 120                  # irreversible writes wait this long
    anomalies:
      - {field: amount, above: 10000, label: large amount}
      - {op: delete, label: delete}
      - {max_writes: 500, label: large batch}
    systems:
      - name: tasks
        kind: rest
        base_url_env: TASKS_URL
        auth: {header: Authorization, prefix: "Bearer ", value_env: TASKS_TOKEN}
        collections:
          - name: task
            id_field: id
            list: {path: /api/v1/tasks/all, page_param: page, per_page: 50}
            read: {path: "/api/v1/tasks/{id}"}
            create: {method: PUT, path: "/api/v1/projects/{project_id}/tasks"}
            update: {method: POST, path: "/api/v1/tasks/{id}", replace: true}
            delete: {method: DELETE, path: "/api/v1/tasks/{id}"}
            server_fields: [updated]
            derived: {done: [done_at]}
      - name: crm
        kind: sql
        dialect: mysql
        driver: pymysql
        connect_args: {host: 127.0.0.1, port: 3306, user: crm, database: crm}
        password_env: CRM_DB_PASSWORD
        tables: {customers: id}
      - name: outbox
        kind: mail
        smtp: {host: 127.0.0.1, port: 1025}
        mailbox: {kind: mailpit, base_url: "http://127.0.0.1:8025"}

Engine code never names an app: everything app-specific is in this file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

TX_SCHEMA = "openadapt.tx/v1"
_NAME = r"^[a-z][a-z0-9_-]{0,47}$"
_TEMPLATE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class TxSpecError(ValueError):
    """The tx file is missing, unreadable or invalid."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Auth(_Closed):
    header: str = "Authorization"
    prefix: str = ""
    #: The secret comes from this environment variable; never from the file.
    value_env: str


class Route(_Closed):
    method: str = "GET"
    path: str
    #: update only: the server replaces the whole record, so OpenAdapt sends
    #: the baseline merged with the planned fields (and the full baseline on undo).
    replace: bool = False
    #: list only: pagination.
    page_param: Optional[str] = None
    per_page_param: Optional[str] = "per_page"
    per_page: int = Field(default=50, ge=1, le=1000)
    #: create only: the field of the response that holds the new record's id.
    id_from: Optional[str] = None

    @model_validator(mode="after")
    def _upper(self) -> "Route":
        self.method = self.method.upper()
        return self

    def placeholders(self) -> list[str]:
        return _TEMPLATE.findall(self.path)


class Collection(_Closed):
    name: str = Field(pattern=_NAME)
    id_field: str = "id"
    list: Route
    read: Route
    create: Optional[Route] = None
    update: Optional[Route] = None
    delete: Optional[Route] = None
    #: Fields the server sets on any write (a timestamp, a version). They are
    #: left out of every comparison. Declare them before a measured run.
    server_fields: list[str] = Field(default_factory=list)
    #: Fields the server sets as a consequence of a planned field: changing
    #: ``done`` also changes ``done_at``. A plan shows them as expected changes
    #: with an unknown value; a change to one without its cause is a miss.
    derived: dict[str, list[str]] = Field(default_factory=dict)
    #: A delete that the API can't reverse exactly (the record gets a new id
    #: when recreated) is irreversible: it waits in the holdback queue.
    delete_reversible: bool = False


class RestSystem(_Closed):
    name: str = Field(pattern=_NAME)
    kind: Literal["rest"]
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    auth: Optional[Auth] = None
    #: Send ``Idempotency-Key`` (or this header) with each write, for servers
    #: that honor it. The ledger is the guard either way.
    idempotency_header: Optional[str] = None
    timeout_s: float = Field(default=20.0, gt=0, le=300)
    collections: list[Collection] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "RestSystem":
        if not (self.base_url or self.base_url_env):
            raise ValueError(f"system {self.name}: needs base_url or base_url_env")
        names = [c.name for c in self.collections]
        if len(set(names)) != len(names):
            raise ValueError(f"system {self.name}: collection names repeat")
        return self

    def url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        value = os.environ.get(str(self.base_url_env), "")
        if not value:
            raise TxSpecError(f"system {self.name}: ${self.base_url_env} is not set")
        return value.rstrip("/")

    def collection(self, name: Optional[str]) -> Collection:
        for c in self.collections:
            if c.name == name:
                return c
        raise TxSpecError(f"system {self.name}: no collection {name!r}")


class SqlSystem(_Closed):
    name: str = Field(pattern=_NAME)
    kind: Literal["sql"]
    dialect: Literal["sqlite", "mysql", "postgresql"]
    #: sqlite: the file (relative to the tx file).
    path: Optional[str] = None
    #: mysql / postgresql: a DB-API module and its connect() arguments.
    driver: Optional[str] = None
    connect_args: dict[str, object] = Field(default_factory=dict)
    password_env: Optional[str] = None
    #: The tables a plan may write, each with its key column.
    tables: dict[str, str] = Field(default_factory=dict)
    #: Tables whose rows the frame doesn't compare (a session table, a cache).
    #: Declare them before a measured run.
    frame_ignore_tables: list[str] = Field(default_factory=list)
    #: Columns the frame doesn't compare, as ``table.column`` (``*.tms``).
    frame_ignore_columns: list[str] = Field(default_factory=list)
    max_frame_rows: int = Field(default=200_000, ge=1)

    @model_validator(mode="after")
    def _check(self) -> "SqlSystem":
        if self.dialect == "sqlite" and not self.path:
            raise ValueError(f"system {self.name}: sqlite needs path")
        if self.dialect != "sqlite" and not self.driver:
            raise ValueError(f"system {self.name}: {self.dialect} needs driver")
        ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
        for table, key in self.tables.items():
            if not (ident.match(table) and ident.match(key)):
                raise ValueError(f"system {self.name}: bad table or key {table}.{key}")
        return self


class Smtp(_Closed):
    host: str
    port: int = Field(ge=1, le=65535)


class Mailbox(_Closed):
    #: Where sent mail can be read back: a Mailpit API.
    kind: Literal["mailpit"]
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None

    def url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        value = os.environ.get(str(self.base_url_env or ""), "")
        if not value:
            raise TxSpecError("mailbox: base_url or base_url_env must be set")
        return value.rstrip("/")


class MailSystem(_Closed):
    name: str = Field(pattern=_NAME)
    kind: Literal["mail"]
    smtp: Smtp
    mailbox: Mailbox
    sender: str = "openadapt@localhost"


class Anomaly(_Closed):
    label: str = Field(min_length=1, max_length=60)
    field: Optional[str] = None
    above: Optional[float] = None
    op: Optional[Literal["create", "update", "delete", "send", "sql"]] = None
    #: A field set from a non-empty value to an empty one.
    clears: bool = False
    max_writes: Optional[int] = Field(default=None, ge=1)
    #: SQL: more rows than this touched in total (the blast radius).
    max_rows: Optional[int] = Field(default=None, ge=0)
    #: SQL: rows of ``table`` whose ``column`` equals ``equals`` before the
    #: statement ("3 admins"), from the staged transaction.
    table: Optional[str] = None
    column: Optional[str] = None
    equals: Optional[object] = None

    @model_validator(mode="after")
    def _check(self) -> "Anomaly":
        if self.above is not None and self.field is None:
            raise ValueError(f"anomaly {self.label}: above needs field")
        if (self.table is None) != (self.column is None):
            raise ValueError(f"anomaly {self.label}: table and column go together")
        if not any([self.field, self.op, self.max_writes, self.clears, self.max_rows is not None, self.table]):
            raise ValueError(f"anomaly {self.label}: says nothing to check")
        return self


System = RestSystem | SqlSystem | MailSystem


class TxSpec(_Closed):
    schema_version: Literal["openadapt.tx/v1"]
    #: Seconds an irreversible write (an email, a delete the API can't undo)
    #: waits, cancelable, before it is released.
    holdback_s: float = Field(default=120.0, ge=0, le=86_400)
    anomalies: list[Anomaly] = Field(default_factory=list)
    systems: list[System] = Field(min_length=1)  # type: ignore[valid-type]
    source: Optional[Path] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check(self) -> "TxSpec":
        names = [s.name for s in self.systems]
        if len(set(names)) != len(names):
            raise ValueError("system names repeat")
        return self

    @property
    def root(self) -> Path:
        return self.source.parent if self.source is not None else Path.cwd()

    def system(self, name: str) -> System:  # type: ignore[valid-type]
        for s in self.systems:
            if s.name == name:
                return s
        raise TxSpecError(f"no system {name!r} in the tx file")


def load_spec(path: Path) -> TxSpec:
    import yaml

    path = Path(path)
    if not path.is_file():
        raise TxSpecError(f"{path} is not a file")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise TxSpecError(f"cannot read {path}: {exc}") from exc
    try:
        spec = TxSpec.model_validate(data)
    except ValidationError as exc:
        raise TxSpecError(f"{path} is not a valid {TX_SCHEMA} file: {exc}") from exc
    spec.source = path.resolve()
    return spec
