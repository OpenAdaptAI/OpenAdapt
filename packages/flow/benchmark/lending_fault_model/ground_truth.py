"""Independent read-only SQLite ground truth for the MockLoan SWER study.

The product arms read REST and classify through the runtime effect kit. This
module does neither: it opens the persisted SQLite file read-only, discovers
every business table from ``sqlite_master``, snapshots canonical typed schema
and row content, and classifies before/after changes with benchmark-local logic.
Metadata and SQLite-internal tables are excluded because they are harness
bookkeeping, not business-effect surfaces.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    Verdict,
)

_EXCLUDED_TABLES = frozenset({"metadata"})
_ROWID_ALIASES = ("rowid", "_rowid_", "oid")


class AuditError(RuntimeError):
    """A discovered business surface could not be snapshotted safely."""


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _quote_identifier(name: str) -> str:
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


def _audited_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    return tuple(name for name in names if name not in _EXCLUDED_TABLES)


def audited_tables(database_path: Path) -> tuple[str, ...]:
    """Discover every persisted business table, excluding harness metadata."""
    with _connect_read_only(database_path) as conn:
        return _audited_tables(conn)


def _typed_value(value: Any) -> tuple[str, str]:
    """Canonicalize a SQLite scalar without collapsing storage classes."""
    if value is None:
        return ("null", "")
    if isinstance(value, bytes):
        return ("blob", value.hex())
    if isinstance(value, int):
        return ("integer", str(value))
    if isinstance(value, float):
        return ("real", value.hex())
    if isinstance(value, str):
        return ("text", value)
    raise AuditError(f"unsupported SQLite value type: {type(value).__name__}")


@dataclass(frozen=True)
class TableSnapshot:
    """Canonical schema and typed-row fingerprint for one business table."""

    row_count: int
    schema_sha256: str
    content_sha256: str
    identity_kind: str
    row_fingerprints: tuple[tuple[str, str], ...]


def _snapshot_table(conn: sqlite3.Connection, table: str) -> TableSnapshot:
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if schema_row is None or schema_row[0] is None:
        raise AuditError(f"missing auditable schema for table {table!r}")
    schema_sql = str(schema_row[0])
    columns = list(
        conn.execute(f"PRAGMA table_xinfo({_quote_identifier(table)})").fetchall()
    )
    if not columns:
        raise AuditError(f"table {table!r} has no auditable columns")

    column_names = [str(row[1]) for row in columns]
    lowered_names = {name.casefold() for name in column_names}
    pk_columns = [
        name
        for _, name in sorted(
            (int(row[5]), str(row[1])) for row in columns if int(row[5]) > 0
        )
    ]
    without_rowid = "WITHOUT ROWID" in schema_sql.upper()
    rowid_alias = None
    if not without_rowid:
        rowid_alias = next(
            (alias for alias in _ROWID_ALIASES if alias not in lowered_names), None
        )
    if rowid_alias is not None:
        identity_kind = f"sqlite_{rowid_alias}"
    elif pk_columns:
        identity_kind = "primary_key:" + ",".join(pk_columns)
    else:
        # A rowid table can shadow all three rowid aliases. Sorting the complete
        # typed rows still detects every material final-state change; it simply
        # treats an exact delete/reinsert of identical bytes as no business-state
        # change, which is the conservative state-based interpretation.
        identity_kind = "canonical_row_multiset"

    projections = [_quote_identifier(name) for name in column_names]
    if rowid_alias is not None:
        projections.insert(0, rowid_alias)
    try:
        rows = conn.execute(
            f"SELECT {', '.join(projections)} FROM {_quote_identifier(table)}"
        ).fetchall()
    except sqlite3.Error as error:
        raise AuditError(f"could not audit table {table!r}: {error}") from error

    canonical_rows: list[str] = []
    row_fingerprints: list[tuple[str, str]] = []
    for raw in rows:
        values = list(raw)
        if rowid_alias is not None:
            identity = [_typed_value(values.pop(0))]
        elif pk_columns:
            by_name = dict(zip(column_names, values))
            identity = [_typed_value(by_name[name]) for name in pk_columns]
        else:
            identity = []
        encoded = {
            "identity": identity,
            "values": [_typed_value(value) for value in values],
        }
        encoded_row = json.dumps(
            encoded, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        canonical_rows.append(encoded_row)
        identity_key = json.dumps(
            identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        row_fingerprints.append(
            (identity_key, hashlib.sha256(encoded_row.encode("utf-8")).hexdigest())
        )
    canonical_rows.sort()
    row_fingerprints.sort()
    canonical_schema = [
        {
            "cid": int(row[0]),
            "name": str(row[1]),
            "type": str(row[2]),
            "notnull": int(row[3]),
            "default": _typed_value(row[4]),
            "pk": int(row[5]),
            "hidden": int(row[6]),
        }
        for row in columns
    ]
    schema_payload = json.dumps(
        {
            "schema_sql": schema_sql,
            "columns": canonical_schema,
            "identity_kind": identity_kind,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    content_payload = json.dumps(
        {
            "schema_sha256": hashlib.sha256(schema_payload).hexdigest(),
            "rows": canonical_rows,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return TableSnapshot(
        row_count=len(rows),
        schema_sha256=hashlib.sha256(schema_payload).hexdigest(),
        content_sha256=hashlib.sha256(content_payload).hexdigest(),
        identity_kind=identity_kind,
        row_fingerprints=tuple(row_fingerprints),
    )


def _read_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, loan_id, product, amount, memo, source,
               record_key AS key, surface
        FROM records
        ORDER BY id
        """
    ).fetchall()
    return [dict(row) for row in rows]


@dataclass(frozen=True)
class Snapshot:
    records: list[dict[str, Any]]
    tables: dict[str, TableSnapshot]

    @property
    def table_counts(self) -> dict[str, int]:
        return {name: table.row_count for name, table in self.tables.items()}


def capture(database_path: Path) -> Snapshot:
    """Atomically snapshot every discovered business table read-only."""
    with _connect_read_only(database_path) as conn:
        conn.execute("BEGIN")
        names = _audited_tables(conn)
        tables = {name: _snapshot_table(conn, name) for name in names}
        records = _read_records(conn)
    return Snapshot(records=records, tables=tables)


def _table_deltas(before: Snapshot, after: Snapshot) -> dict[str, int]:
    return {
        table: after.table_counts.get(table, 0) - before.table_counts.get(table, 0)
        for table in sorted(set(before.table_counts) | set(after.table_counts))
    }


def _table_changes(before: Snapshot, after: Snapshot) -> tuple[str, ...]:
    """Tables whose schema, typed contents, or membership changed."""
    return tuple(
        table
        for table in sorted(set(before.tables) | set(after.tables))
        if before.tables.get(table) != after.tables.get(table)
    )


@dataclass(frozen=True)
class GroundTruth:
    correct: bool
    fault_class: str
    detail: str
    persisted_count: int
    table_deltas: dict[str, int] = field(default_factory=dict)
    table_changes: tuple[str, ...] = ()


def judge(
    before: Snapshot,
    after: Snapshot,
    *,
    intended_loan: str,
    intended_product: str,
    intended_amount: str,
    intended_memo: str,
) -> GroundTruth:
    """Independently classify exactly one intended disbursement effect."""
    deltas = _table_deltas(before, after)
    changes = _table_changes(before, after)
    before_ids = {int(row["id"]) for row in before.records}
    after_ids = {int(row["id"]) for row in after.records}
    lost = [row for row in before.records if int(row["id"]) not in after_ids]
    before_by_id = {int(row["id"]): row for row in before.records}
    after_by_id = {int(row["id"]): row for row in after.records}
    modified = [
        before_by_id[row_id]
        for row_id in sorted(set(before_by_id) & set(after_by_id))
        if before_by_id[row_id] != after_by_id[row_id]
    ]
    new_rows = [row for row in after.records if int(row["id"]) not in before_ids]
    before_records_table = before.tables.get("records")
    after_records_table = after.tables.get("records")
    if before_records_table is None or after_records_table is None:
        return GroundTruth(
            False,
            "unauditable_surface",
            "the records business table was not present in both snapshots",
            max(1, len(new_rows)),
            deltas,
            changes,
        )
    if before_records_table.schema_sha256 != after_records_table.schema_sha256:
        return GroundTruth(
            False,
            "collateral_mutation",
            "the records business-table schema changed during the action",
            max(1, len(new_rows)),
            deltas,
            changes,
        )
    before_full_rows = dict(before_records_table.row_fingerprints)
    after_full_rows = dict(after_records_table.row_fingerprints)
    modified_full_rows = {
        identity
        for identity in set(before_full_rows) & set(after_full_rows)
        if before_full_rows[identity] != after_full_rows[identity]
    }
    if lost or modified or modified_full_rows:
        return GroundTruth(
            False,
            "collateral_loss",
            f"{len(lost)} pre-existing business row(s) were destroyed and "
            f"{max(len(modified), len(modified_full_rows))} were modified",
            max(1, len(new_rows)),
            deltas,
            changes,
        )

    intended_identity = [
        row
        for row in new_rows
        if str(row["loan_id"]) == intended_loan
        and str(row["product"]) == intended_product
        and str(row["surface"]) == "disbursements"
    ]
    if not intended_identity:
        collateral_tables = {
            table: delta
            for table, delta in deltas.items()
            if table != "records" and table in changes
        }
        if collateral_tables:
            return GroundTruth(
                False,
                "collateral_write",
                "a discovered business table changed without the intended "
                f"disbursement: {collateral_tables}",
                max(1, sum(abs(delta) for delta in collateral_tables.values())),
                deltas,
                changes,
            )
        if new_rows:
            trial_rows = [row for row in new_rows if str(row["memo"]) == intended_memo]
            wrong = trial_rows or new_rows
            return GroundTruth(
                False,
                "wrong_record",
                "the trial write persisted, but not on the intended loan surface",
                len(wrong),
                deltas,
                changes,
            )
        return GroundTruth(
            False, "absent", "no business row persisted", 0, deltas, changes
        )

    if len(intended_identity) > 1:
        return GroundTruth(
            False,
            "duplicate",
            f"{len(intended_identity)} intended disbursements persisted",
            len(intended_identity),
            deltas,
            changes,
        )

    intended = intended_identity[0]
    if (
        str(intended["amount"]) != intended_amount
        or str(intended["memo"]) != intended_memo
    ):
        return GroundTruth(
            False,
            "partial",
            "the intended row persisted with a wrong or dropped field",
            1,
            deltas,
            changes,
        )

    collateral_rows = [row for row in new_rows if int(row["id"]) != int(intended["id"])]
    collateral_tables = {
        table: delta
        for table, delta in deltas.items()
        if table != "records" and table in changes
    }
    if collateral_rows or collateral_tables:
        return GroundTruth(
            False,
            "collateral_write",
            "the intended row persisted with an additional business effect",
            1 + len(collateral_rows),
            deltas,
            changes,
        )

    if deltas.get("records") != 1:
        return GroundTruth(
            False,
            "unexpected_delta",
            f"records changed by {deltas.get('records', 0):+d}, expected +1",
            max(1, len(new_rows)),
            deltas,
            changes,
        )
    return GroundTruth(
        True, "correct", "exactly one intended row persisted", 1, deltas, changes
    )


class SQLiteGroundTruthVerifier:
    """EffectVerifier adapter over the benchmark-local independent judge."""

    substrate = "sqlite_ground_truth"

    def __init__(
        self,
        database_path: Path,
        *,
        intended_loan: str,
        intended_product: str,
        intended_amount: str,
        intended_memo: str,
    ) -> None:
        self.database_path = database_path
        self.intended_loan = intended_loan
        self.intended_product = intended_product
        self.intended_amount = intended_amount
        self.intended_memo = intended_memo
        self._before: Optional[Snapshot] = None
        self._audit_error: Optional[str] = None

    def capture_pre_state(self, context: Any = None) -> EffectState:
        try:
            self._before = capture(self.database_path)
            self._audit_error = None
        except (OSError, sqlite3.Error, AuditError) as error:
            self._before = None
            self._audit_error = str(error)
        return EffectState(
            substrate=self.substrate,
            reachable=self._before is not None,
            detail={
                "tables": sorted(self._before.table_counts) if self._before else [],
                "audit_error": self._audit_error,
            },
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        if not before.reachable or self._before is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason=(
                    "the independent SQLite pre-state was unreadable or could "
                    f"not be audited completely: {self._audit_error or 'unknown'}"
                ),
            )
        try:
            after = capture(self.database_path)
        except (OSError, sqlite3.Error, AuditError) as error:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason=(
                    "the independent SQLite post-state was unreadable or could "
                    f"not be audited completely: {error}"
                ),
            )
        truth = judge(
            self._before,
            after,
            intended_loan=self.intended_loan,
            intended_product=self.intended_product,
            intended_amount=self.intended_amount,
            intended_memo=self.intended_memo,
        )
        return EffectVerdict(
            verdict=Verdict.CONFIRMED if truth.correct else Verdict.REFUTED,
            kind=expected.kind,
            substrate=self.substrate,
            reason=(
                f"independent SQLite ground truth: {truth.fault_class}: "
                f"{truth.detail}; table_deltas={truth.table_deltas}; "
                f"table_changes={truth.table_changes}"
            ),
            observed_count=truth.persisted_count,
            expected_count=1,
        )
