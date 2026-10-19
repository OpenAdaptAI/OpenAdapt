"""Schema-defined export of record reads to CSV, a database or a webhook.

:func:`export_rows` turns record reads into rows: one column per schema
field, typed by its kind, plus provenance columns (``_record_key``,
``_run_id``, ``_read_at``, ``_grade``, ``_receipt_sha256`` and
``_provenance``, a JSON list of each field's grade, channels, confidence and
screen region). A record is exported only when no field still needs review
and every field is at least ``min_grade`` or was settled by a signed review
decision. The default ``min_grade`` is ``unchecked``, so every settled
record is exported. ``_grade`` is always the weakest read grade, so a value
is never labelled stronger than what was proven. Everything else is returned as skipped, with the reason.

Sinks:

- :class:`CsvSink`: one CSV file;
- :class:`DbApiSink`: any DB-API 2.0 connection. ``sqlite3`` works as is;
  pass a ``psycopg`` connection and ``paramstyle="format"`` for Postgres;
- :class:`WebhookSink`: POST JSON. Loopback only unless ``allow_remote`` is
  set, so a misconfigured URL cannot send records off the machine.

A :class:`ColumnMapping` renames fields and adds constant columns, for a
target layout the user defines. Standard layouts (for example an industry
data model) are templates outside the core; see ``examples/records/``.
"""

from __future__ import annotations

import csv
import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.network_scope import is_loopback_url
from openadapt_flow.records.schema import (
    FieldSpec,
    ReadGrade,
    RecordRead,
    RecordSchema,
    grade_at_least,
)

__all__ = [
    "PROVENANCE_COLUMNS",
    "ColumnMapping",
    "CsvSink",
    "DbApiSink",
    "ExportResult",
    "Skipped",
    "WebhookSink",
    "export",
    "export_rows",
]

PROVENANCE_COLUMNS: tuple[str, ...] = (
    "_record_key",
    "_run_id",
    "_read_at",
    "_grade",
    "_receipt_sha256",
    "_provenance",
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class ColumnMapping(BaseModel):
    """Map schema fields onto a user-defined target layout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str
    columns: dict[str, str] = Field(
        default_factory=dict, description="Schema field -> target column."
    )
    constants: dict[str, Any] = Field(
        default_factory=dict, description="Target column -> fixed value."
    )
    include_provenance: bool = True

    def apply(self, row: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in row.items():
            if key in PROVENANCE_COLUMNS:
                if self.include_provenance:
                    out[key] = value
            elif key in self.columns:
                out[self.columns[key]] = value
        out.update(self.constants)
        return out


@dataclass(frozen=True)
class Skipped:
    record_key: str
    reason: str


@dataclass
class ExportResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


def _typed(spec: FieldSpec, value: Optional[str]) -> Any:
    if value is None:
        return None
    if spec.kind == "integer":
        return int(value)
    if spec.kind == "boolean":
        return value == "true"
    return value  # currency and decimal stay strings so no precision is lost


def _provenance(record: RecordRead) -> str:
    entries = []
    for read in record.fields:
        entries.append(
            {
                "field": read.field,
                "grade": read.grade,
                "agreement": read.agreement,
                "confidence": read.confidence,
                "channels": [r.channel for r in read.reads],
                "frame_sha256": read.region.frame_sha256 if read.region else None,
                "bbox": list(read.region.bbox) if read.region else None,
                "crop_sha256": read.region.crop_sha256 if read.region else None,
                "review_decision": read.review_decision,
            }
        )
    return json.dumps(entries, sort_keys=True, separators=(",", ":"))


def export_rows(
    records: Iterable[RecordRead],
    schema: RecordSchema,
    *,
    min_grade: ReadGrade = "unchecked",
) -> ExportResult:
    """Rows for every record that is settled and at least ``min_grade``."""

    result = ExportResult()
    for record in records:
        try:
            record.check_against(schema)
        except ValueError as exc:
            result.skipped.append(Skipped(record.record_key, str(exc)))
            continue
        if record.needs_review:
            pending = [r.field for r in record.fields if r.needs_review]
            result.skipped.append(
                Skipped(record.record_key, f"needs review: {', '.join(pending)}")
            )
            continue
        grade = record.grade
        # A review decision settles a value but is not a read, so it does not
        # lift a field over min_grade.
        weak = [
            r.field for r in record.fields if not grade_at_least(r.grade, min_grade)
        ]
        if weak:
            result.skipped.append(
                Skipped(record.record_key, f"below {min_grade}: {', '.join(weak)}")
            )
            continue
        values = {read.field: read.value for read in record.fields}
        row: dict[str, Any] = {
            spec.name: _typed(spec, values.get(spec.name)) for spec in schema.fields
        }
        seals = {
            r.seal.receipt_sha256 for r in record.fields if r.seal and r.seal.sealed
        }
        row.update(
            {
                "_record_key": record.record_key,
                "_run_id": record.run_id,
                "_read_at": record.read_at,
                "_grade": grade,
                "_receipt_sha256": seals.pop() if len(seals) == 1 else None,
                "_provenance": _provenance(record),
            }
        )
        result.rows.append(row)
    return result


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> Any:
    """A cell value that a spreadsheet will not run as a formula.

    Text read off a screen can start with ``=`` or ``@``. Such cells get a
    leading apostrophe. Numbers, including negative amounts, are left as is.
    """

    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        try:
            float(value)
        except ValueError:
            return "'" + value
    return value


class CsvSink:
    """Write rows to one CSV file (overwrites)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def write(self, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns))
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _csv_cell(v) for k, v in row.items()})
        return len(rows)


class DbApiSink:
    """Insert rows into a table through a DB-API 2.0 connection.

    The table is created if missing. Rows are upserted on ``_record_key`` when
    provenance columns are present, so re-running an export does not
    duplicate records. Identifiers are checked; values are always bound
    parameters.
    """

    def __init__(
        self, connection: Any, table: str, *, paramstyle: str = "qmark"
    ) -> None:
        if not _IDENT.match(table):
            raise ValueError(f"unsafe table name {table!r}")
        self.connection = connection
        self.table = table
        self.placeholder = "?" if paramstyle == "qmark" else "%s"

    def write(
        self,
        rows: Sequence[Mapping[str, Any]],
        columns: Sequence[str],
        types: Optional[Mapping[str, str]] = None,
    ) -> int:
        for column in columns:
            if not _IDENT.match(column):
                raise ValueError(f"unsafe column name {column!r}")
        types = types or {}
        keyed = "_record_key" in columns
        defs = ", ".join(
            f'"{c}" {types.get(c, "TEXT")}'
            + (" PRIMARY KEY" if c == "_record_key" else "")
            for c in columns
        )
        cursor = self.connection.cursor()
        cursor.execute(f'CREATE TABLE IF NOT EXISTS "{self.table}" ({defs})')
        names = ", ".join(f'"{c}"' for c in columns)
        marks = ", ".join(self.placeholder for _ in columns)
        sql = f'INSERT INTO "{self.table}" ({names}) VALUES ({marks})'
        if keyed:
            updates = ", ".join(
                f'"{c}" = excluded."{c}"' for c in columns if c != "_record_key"
            )
            sql += f' ON CONFLICT ("_record_key") DO UPDATE SET {updates}'
        for row in rows:
            cursor.execute(sql, [row.get(c) for c in columns])
        self.connection.commit()
        return len(rows)


class WebhookSink:
    """POST ``{"schema": ..., "rows": [...]}`` as JSON to one URL."""

    def __init__(
        self, url: str, *, allow_remote: bool = False, timeout: float = 10.0
    ) -> None:
        if not allow_remote and not is_loopback_url(url):
            raise ValueError(
                "webhook URL is not on this machine; pass allow_remote=True to send off it"
            )
        self.url = url
        self.timeout = timeout

    def write(self, rows: Sequence[Mapping[str, Any]], schema_id: str) -> int:
        body = json.dumps({"schema": schema_id, "rows": list(rows)}).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # No proxy: a loopback URL must not be sent through HTTP(S)_PROXY.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=self.timeout) as response:
            if response.status >= 300:
                raise RuntimeError(f"webhook returned HTTP {response.status}")
        return len(rows)


_SQL_TYPES = {"integer": "INTEGER", "boolean": "BOOLEAN"}


def export(
    records: Iterable[RecordRead],
    schema: RecordSchema,
    sink: CsvSink | DbApiSink | WebhookSink,
    *,
    mapping: Optional[ColumnMapping] = None,
    min_grade: ReadGrade = "unchecked",
) -> ExportResult:
    """Export settled records to ``sink``; returns the rows and what was skipped."""

    result = export_rows(records, schema, min_grade=min_grade)
    rows = result.rows
    columns = [spec.name for spec in schema.fields] + list(PROVENANCE_COLUMNS)
    types = {spec.name: _SQL_TYPES.get(spec.kind, "TEXT") for spec in schema.fields}
    if mapping is not None:
        rows = [mapping.apply(row) for row in rows]
        columns = [mapping.columns[c] for c in columns if c in mapping.columns]
        columns += list(mapping.constants)
        if mapping.include_provenance:
            columns += list(PROVENANCE_COLUMNS)
        types = {mapping.columns.get(k, k): v for k, v in types.items()}
    if isinstance(sink, CsvSink):
        sink.write(rows, columns)
    elif isinstance(sink, DbApiSink):
        sink.write(rows, columns, types)
    else:
        sink.write(rows, f"urn:openadapt:record:{schema.name}:v{schema.version}")
    result.rows = rows
    return result
