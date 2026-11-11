"""Commit evidence for an operator-controlled SQLite insert.

The authority is limited to a local database and a private baseline directory
controlled by the operator. An independent connection checks the committed
row and a MAC written in the same transaction. A matching row, an actor name,
or a request ID without that MAC carries no write-attribution authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.deployment import EffectsConfig
from openadapt_flow.runtime.effects.effect import Effect, EffectKind
from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

EVENT_TABLE = "openadapt_owned_insert_events"
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]{0,62}"


class OwnedSqliteInsertConfig(BaseModel):
    """Opt into the controlled writer for one complete table and primary key."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    table: str = Field(pattern=rf"^{_IDENTIFIER}$")
    identity_field: str = Field(pattern=rf"^{_IDENTIFIER}$")


def _file_identity(path: Path) -> tuple[int, int]:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("owned SQLite needs an absolute non-aliased database path")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("owned SQLite needs a regular database file")
    return info.st_dev, info.st_ino


def _quote(name: str) -> str:
    if re.fullmatch(_IDENTIFIER, name) is None:
        raise ValueError("unsupported SQLite identifier")
    return f'"{name}"'


def _mac(salt: bytes, domain: str, value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hmac.new(
        salt, (domain + "\0" + payload).encode(), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True)
class OwnedSqliteInsert:
    database: Path
    file_identity: tuple[int, int]
    table: str
    identity_field: str
    cells: tuple[tuple[str, int | str], ...]

    @classmethod
    def bind(
        cls,
        spec: OwnedSqliteInsertConfig,
        config: EffectsConfig,
        verifier: object,
        effects: Sequence[Effect],
        params: Mapping[str, object],
    ) -> OwnedSqliteInsert:
        if (
            type(verifier) is not SqlRecordVerifier
            or config.kind != "sql"
            or not config.sqlite_database
            or config.sql_driver
            or config.sql_connect_args
            or config.sql_password_env
            or config.sql_query_params
            or config.candidates
            or config.surfaces
            or config.records_drop_fields
            or spec.table.casefold() == EVENT_TABLE
            or spec.table.casefold().startswith("sqlite_")
            or config.sql_query != f"SELECT * FROM {spec.table}"
            or len(effects) != 1
        ):
            raise ValueError("unsupported owned SQLite reader")
        effect = effects[0]
        if (
            effect.kind is not EffectKind.RECORD_WRITTEN
            or effect.expected_count != 1
            or effect.needs_operator_confirmation
            or spec.identity_field not in effect.match
            or not 1 <= len(effect.match) <= 64
        ):
            raise ValueError("owned SQLite needs one complete inserted row")
        cells: dict[str, int | str] = {}
        for column, expr in effect.match.items():
            _quote(column)
            if expr.model_fields_set - {"param", "literal"}:
                raise ValueError("unsupported owned SQLite value expression")
            value = params.get(expr.param) if expr.param is not None else expr.literal
            if type(value) is int and -(2**63) <= value < 2**63:
                cells[column] = value
            elif type(value) is str and 0 < len(value) <= 16_384:
                cells[column] = value
            else:
                raise ValueError("unsupported owned SQLite value type")
        if type(cells[spec.identity_field]) is not int:
            raise ValueError("owned SQLite needs an integer primary key")
        database = Path(config.sqlite_database)
        return cls(
            database,
            _file_identity(database),
            spec.table,
            spec.identity_field,
            tuple(sorted(cells.items())),
        )

    def reservation(self, execution_id: str, salt: bytes, owner: str | None) -> str:
        return _mac(
            salt,
            "openadapt-owned-sqlite-insert-reservation/v1",
            {
                "attempt": execution_id,
                "owner": owner,
                "database": str(self.database),
                "file_identity": self.file_identity,
                "operation": "insert",
                "table": self.table,
                "identity_field": self.identity_field,
                "cells": self.cells,
            },
        )

    def _witness(self, reservation: str, salt: bytes) -> str:
        return _mac(salt, "openadapt-owned-sqlite-insert-commit/v1", reservation)

    def _assert_database(self, connection: sqlite3.Connection) -> None:
        stores = connection.execute("PRAGMA database_list").fetchall()
        if stores != [(0, "main", str(self.database))] or (
            self.file_identity != _file_identity(self.database)
        ):
            raise ValueError("owned SQLite database identity changed")

    def _assert_schema(self, connection: sqlite3.Connection) -> None:
        tables = {row[1]: row for row in connection.execute("PRAGMA main.table_list")}
        if any(
            tables.get(name, (None, None, None))[2] != "table"
            for name in (self.table, EVENT_TABLE)
        ):
            raise ValueError("owned SQLite needs ordinary target and event tables")
        target = connection.execute(
            f"PRAGMA main.table_xinfo({_quote(self.table)})"
        ).fetchall()
        events = connection.execute(
            f"PRAGMA main.table_xinfo({_quote(EVENT_TABLE)})"
        ).fetchall()
        if tables[EVENT_TABLE][5] != 1 or events != [
            (0, "attempt", "TEXT", 1, None, 1, 0),
            (1, "witness", "TEXT", 1, None, 0, 0),
        ]:
            raise ValueError("owned SQLite event schema does not match")
        types = {
            column: "INTEGER" if type(value) is int else "TEXT"
            for column, value in self.cells
        }
        if (
            len(target) != len(types)
            or {row[1]: row[2].upper() for row in target} != types
            or [(row[1], row[5]) for row in target if row[5]]
            != [(self.identity_field, 1)]
            or any(row[6] for row in target)
            or connection.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE type='trigger' "
                "AND tbl_name IN (?, ?) LIMIT 1",
                (self.table, EVENT_TABLE),
            ).fetchone()
            is not None
        ):
            raise ValueError("owned SQLite target schema does not match")

    def _target_rows(self, connection: sqlite3.Connection) -> list[tuple[object, ...]]:
        columns = ", ".join(_quote(name) for name, _ in self.cells)
        key = dict(self.cells)[self.identity_field]
        return connection.execute(
            f"SELECT {columns} FROM main.{_quote(self.table)} "
            f"WHERE {_quote(self.identity_field)} = ? LIMIT 2",
            (key,),
        ).fetchall()

    def matches(self, row: Mapping[str, object]) -> bool:
        return set(row) == {name for name, _ in self.cells} and all(
            type(row[name]) is type(value) and row[name] == value
            for name, value in self.cells
        )

    def _matches_store(self, connection: sqlite3.Connection) -> bool:
        rows = self._target_rows(connection)
        if len(rows) != 1:
            return False
        return self.matches(dict(zip((name for name, _ in self.cells), rows[0])))

    def check_baseline(self, rows: Sequence[Mapping[str, object]]) -> None:
        key = dict(self.cells)[self.identity_field]
        for row in rows:
            observed = row.get(self.identity_field)
            if type(observed) is not int or observed == key:
                raise ValueError(
                    "the owned SQLite baseline does not establish an absent key"
                )
        uri = self.database.as_uri() + "?mode=ro"
        with (
            closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as connection,
            connection,
        ):
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            self._assert_database(connection)
            self._assert_schema(connection)
            self._assert_database(connection)

    def commit(self, execution_id: str, reservation: str, salt: bytes) -> None:
        uri = self.database.as_uri() + "?mode=rw"
        with (
            closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as connection,
            connection,
        ):
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            self._assert_database(connection)
            self._assert_schema(connection)
            if self._target_rows(connection):
                raise ValueError("owned SQLite target already exists")
            columns = ", ".join(_quote(name) for name, _ in self.cells)
            placeholders = ", ".join("?" for _ in self.cells)
            cursor = connection.execute(
                f"INSERT OR ABORT INTO main.{_quote(self.table)} ({columns}) VALUES ({placeholders})",
                tuple(value for _, value in self.cells),
            )
            if cursor.rowcount != 1 or not self._matches_store(connection):
                raise ValueError("owned SQLite did not insert the exact target")
            event = connection.execute(
                f"INSERT OR ABORT INTO main.{_quote(EVENT_TABLE)} (attempt, witness) VALUES (?, ?)",
                (execution_id, self._witness(reservation, salt)),
            )
            if event.rowcount != 1:
                raise ValueError("owned SQLite did not store the commit witness")
            self._assert_database(connection)

    def independently_observed(
        self, execution_id: str, reservation: str, salt: bytes
    ) -> bool:
        uri = self.database.as_uri() + "?mode=ro"
        with (
            closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as connection,
            connection,
        ):
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            self._assert_database(connection)
            self._assert_schema(connection)
            events = connection.execute(
                f"SELECT CASE WHEN length(witness) = 64 THEN witness END "
                f"FROM main.{_quote(EVENT_TABLE)} WHERE attempt = ? LIMIT 2",
                (execution_id,),
            ).fetchall()
            observed = (
                len(events) == 1
                and type(events[0][0]) is str
                and events[0][0].isascii()
                and hmac.compare_digest(events[0][0], self._witness(reservation, salt))
                and self._matches_store(connection)
            )
            self._assert_database(connection)
            return observed
