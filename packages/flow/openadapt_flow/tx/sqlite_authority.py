"""Atomic row comparison and mutation enforced by SQLite's writer transaction.

The operator supplies this backend separately from a generated lens/pack. Reads,
comparison, writes and commit all use the same database. An HTTP response or a
profile flag cannot grant this capability. This proves a row-state predicate,
not all trigger effects or the correctness of application business rules.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

from openadapt_flow.tx.judge import strict_equal
from openadapt_flow.tx.types import canonical_sha256

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class AtomicRowError(ValueError):
    """Closed error for a database/profile this backend cannot safely mutate."""


@dataclass(frozen=True)
class AtomicRowResult:
    applied: bool
    conflict: bool
    record: Optional[dict[str, Any]] = None


class SQLiteAtomicRowAuthority:
    """Compare a complete row and mutate it inside one BEGIN IMMEDIATE transaction.

    Supports an existing database/table with one primary-key column, a monotonic
    integer revision column and ordinary scalar columns. No schema is installed
    or changed. App-specific field names belong in the supplied data profile.
    """

    __slots__ = (
        "database",
        "table",
        "id_column",
        "revision_column",
        "timeout_s",
        "_file_identity",
        "_columns",
        "_schema_version",
        "binding_sha256",
        "_sealed",
    )

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("atomic source profile is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        database: Path | str,
        *,
        table: str,
        id_column: str = "id",
        revision_column: str = "version",
        timeout_s: float = 10.0,
    ) -> None:
        self.database = Path(database).resolve(strict=True)
        if not self.database.is_file():
            raise AtomicRowError("atomic database must be an existing file")
        for name in (table, id_column, revision_column):
            if not _IDENTIFIER.fullmatch(name):
                raise AtomicRowError("unsupported SQL identifier")
        if id_column == revision_column:
            raise AtomicRowError("identity and revision columns must differ")
        self.table = table
        self.id_column = id_column
        self.revision_column = revision_column
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise AtomicRowError("positive finite SQLite timeout is required")
        self.timeout_s = timeout_s
        stat = self.database.stat()
        self._file_identity = (stat.st_dev, stat.st_ino)
        connection = self._open(check_schema=False)
        try:
            info = connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
            if not info or any(int(row[6]) != 0 for row in info):
                raise AtomicRowError("ordinary stored columns are required")
            columns = {str(row[1]) for row in info}
            primary = [str(row[1]) for row in info if int(row[5]) != 0]
            if primary != [id_column] or revision_column not in columns:
                raise AtomicRowError(
                    "one primary key and a revision column are required"
                )
            self._columns = frozenset(columns)
            self._schema_version = int(
                connection.execute("PRAGMA schema_version").fetchone()[0]
            )
        finally:
            connection.close()
        self.binding_sha256 = canonical_sha256(
            {
                "backend": "sqlite_atomic_row/v1",
                "database": str(self.database),
                "file": self._file_identity,
                "table": table,
                "id": id_column,
                "revision": revision_column,
                "schema_version": self._schema_version,
            }
        )

        self._sealed = True

    def _open(self, *, check_schema: bool = True) -> sqlite3.Connection:
        stat = self.database.stat()
        if (stat.st_dev, stat.st_ino) != self._file_identity:
            raise AtomicRowError("atomic database identity changed")
        connection = sqlite3.connect(
            "file:" + quote(str(self.database), safe="/") + "?mode=rw",
            uri=True,
            timeout=self.timeout_s,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if (
            check_schema
            and int(connection.execute("PRAGMA schema_version").fetchone()[0])
            != self._schema_version
        ):
            connection.close()
            raise AtomicRowError("atomic database schema changed")
        return connection

    @staticmethod
    def _validate_scalars(record: Mapping[str, Any]) -> None:
        for value in record.values():
            if value is not None and type(value) not in (str, int, float):
                raise AtomicRowError("unsupported atomic row value")
            if isinstance(value, float) and not math.isfinite(value):
                raise AtomicRowError("non-finite atomic row value")

    def _read(
        self, connection: sqlite3.Connection, entity_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            f'SELECT * FROM "{self.table}" WHERE "{self.id_column}" = ?', (entity_id,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        self._validate_scalars(record)
        if set(record) != self._columns or str(record[self.id_column]) != entity_id:
            raise AtomicRowError("atomic row identity or shape mismatch")
        revision = record[self.revision_column]
        if type(revision) is not int or not 0 <= revision < (2**63 - 1):
            raise AtomicRowError("unsupported atomic row revision")
        return record

    def read(self, entity_id: str) -> Optional[dict[str, Any]]:
        connection = self._open()
        try:
            return self._read(connection, entity_id)
        finally:
            connection.close()

    def compare_update(
        self, entity_id: str, *, expected: Mapping[str, Any], values: Mapping[str, Any]
    ) -> AtomicRowResult:
        """Apply only if the complete expected row still exists at the write boundary."""
        return self._compare_mutate(
            entity_id, expected=expected, values=values, delete=False
        )

    def compare_delete(
        self, entity_id: str, *, expected: Mapping[str, Any]
    ) -> AtomicRowResult:
        return self._compare_mutate(
            entity_id, expected=expected, values={}, delete=True
        )

    def _compare_mutate(
        self,
        entity_id: str,
        *,
        expected: Mapping[str, Any],
        values: Mapping[str, Any],
        delete: bool,
    ) -> AtomicRowResult:
        self._validate_scalars(expected)
        self._validate_scalars(values)
        if set(values) - self._columns or set(expected) != self._columns:
            raise AtomicRowError("atomic row shape mismatch")
        if self.id_column in values and str(values[self.id_column]) != entity_id:
            raise AtomicRowError("atomic row identity cannot change")
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if (
                int(connection.execute("PRAGMA schema_version").fetchone()[0])
                != self._schema_version
            ):
                raise AtomicRowError("atomic database schema changed")
            current = self._read(connection, entity_id)
            if current is None or not strict_equal(current, expected):
                connection.execute("ROLLBACK")
                return AtomicRowResult(applied=False, conflict=True)
            if delete:
                changed = connection.execute(
                    f'DELETE FROM "{self.table}" WHERE "{self.id_column}" = ?',
                    (entity_id,),
                )
                after = self._read(connection, entity_id)
                if after is not None:
                    raise AtomicRowError("atomic delete did not remove the row")
            else:
                desired = {**current, **dict(values)}
                desired[self.revision_column] = current[self.revision_column] + 1
                columns = sorted(self._columns - {self.id_column})
                assignments = ", ".join(f'"{column}" = ?' for column in columns)
                changed = connection.execute(
                    f'UPDATE "{self.table}" SET {assignments} WHERE "{self.id_column}" = ?',
                    [desired[column] for column in columns] + [entity_id],
                )
                after = self._read(connection, entity_id)
                if after is None or not strict_equal(after, desired):
                    raise AtomicRowError(
                        "atomic stored row differs from the proposed row"
                    )
            if changed.rowcount != 1:
                raise AtomicRowError("atomic mutation changed an unexpected row count")
            connection.execute("COMMIT")
            return AtomicRowResult(applied=True, conflict=False, record=after)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
