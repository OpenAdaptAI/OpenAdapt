"""Schema-defined export: rows, provenance, gating, CSV, SQLite, webhook."""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.records.export import (
    ColumnMapping,
    CsvSink,
    DbApiSink,
    WebhookSink,
    export,
    export_rows,
)
from openadapt_flow.records.read import add_read, channel_read, combine_reads
from openadapt_flow.records.review import ReviewQueue, apply_decision
from openadapt_flow.records.schema import (
    FieldSpec,
    RecordRead,
    RecordSchema,
    SealRef,
    SourceRegion,
)

SCHEMA = RecordSchema(
    name="customer_account",
    fields=(
        FieldSpec(name="account_number", kind="code"),
        FieldSpec(name="holder", kind="text"),
        FieldSpec(name="open_items", kind="integer"),
        FieldSpec(name="active", kind="boolean"),
    ),
    identity_fields=("account_number",),
)
SEAL = SealRef(run_id="run-1", receipt_sha256="b" * 64)
REGION = SourceRegion(frame_sha256="a" * 64, bbox=(1, 2, 30, 10))


def _field(name: str, clip: str, ocr: str):
    spec = SCHEMA.field(name)
    return combine_reads(
        spec,
        [channel_read(spec, "clipboard", clip), channel_read(spec, "ocr", ocr)],
        region=REGION,
        seal=SEAL,
    )


def _record(key: str, holder_ocr: str = "Jane Roe") -> RecordRead:
    return RecordRead(
        schema_name="customer_account",
        schema_version=1,
        record_key=key,
        run_id="run-1",
        read_at="2026-10-17T21:00:00-04:00",
        fields=(
            _field("account_number", key.upper(), key.upper()),
            _field("holder", "Jane Roe", holder_ocr),
            _field("open_items", "3", "3"),
            _field("active", "Yes", "yes"),
        ),
    )


def test_rows_are_typed_with_provenance() -> None:
    result = export_rows([_record("ac1")], SCHEMA)
    (row,) = result.rows
    assert row["account_number"] == "AC1"
    assert row["open_items"] == 3
    assert row["active"] is True
    assert row["_grade"] == "same_screen"
    assert row["_receipt_sha256"] == "b" * 64
    prov = json.loads(row["_provenance"])
    assert prov[0]["channels"] == ["clipboard", "ocr"]
    assert prov[0]["bbox"] == [1, 2, 30, 10]


def test_unsettled_and_weak_records_are_skipped() -> None:
    result = export_rows(
        [_record("ac1"), _record("ac2", holder_ocr="John Doe")], SCHEMA
    )
    assert [r["_record_key"] for r in result.rows] == ["ac1"]
    assert result.skipped[0].reason == "needs review: holder"
    strict = export_rows([_record("ac1")], SCHEMA, min_grade="system_of_record")
    assert strict.rows == []
    assert "below system_of_record" in strict.skipped[0].reason


def test_system_of_record_only_with_independent_read() -> None:
    record = _record("ac1")
    upgraded = tuple(
        add_read(
            SCHEMA.field(f.field),
            f,
            channel_read(SCHEMA.field(f.field), "database", f.reads[0].raw),
        )
        for f in record.fields
    )
    record = record.model_copy(update={"fields": upgraded})
    result = export_rows([record], SCHEMA, min_grade="system_of_record")
    assert result.rows[0]["_grade"] == "system_of_record"


def test_reviewed_value_exports_with_honest_grade(tmp_path) -> None:
    record = _record("ac2", holder_ocr="John Doe")
    queue = ReviewQueue(tmp_path / "q")
    (item,) = queue.add_record(record, created_by="runner:h")
    decision = queue.decide(
        item.item_id,
        action="accept_read",
        channel="clipboard",
        reviewer="person:clerk",
        signing_key=Ed25519PrivateKey.generate(),
        schema=SCHEMA,
    )
    fields = tuple(
        apply_decision(f, decision) if f.field == "holder" else f for f in record.fields
    )
    settled = record.model_copy(update={"fields": fields})
    (row,) = export_rows([settled], SCHEMA).rows
    assert row["holder"] == "Jane Roe"
    assert row["_grade"] == "unchecked"


def test_csv_sink(tmp_path) -> None:
    path = tmp_path / "out.csv"
    export([_record("ac1")], SCHEMA, CsvSink(path))
    rows = list(csv.DictReader(path.open()))
    assert rows[0]["account_number"] == "AC1"
    assert rows[0]["_grade"] == "same_screen"


def test_sqlite_sink_upserts(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "out.db")
    sink = DbApiSink(conn, "accounts")
    export([_record("ac1")], SCHEMA, sink)
    export([_record("ac1"), _record("ac3")], SCHEMA, sink)
    got = conn.execute(
        'SELECT account_number, open_items, "_grade" FROM accounts ORDER BY 1'
    ).fetchall()
    assert got == [("AC1", 3, "same_screen"), ("AC3", 3, "same_screen")]


def test_db_sink_rejects_unsafe_identifiers() -> None:
    with pytest.raises(ValueError, match="unsafe table"):
        DbApiSink(sqlite3.connect(":memory:"), "x; DROP TABLE y")


def test_mapping_renames_and_adds_constants(tmp_path) -> None:
    mapping = ColumnMapping(
        table="crm_accounts",
        columns={"account_number": "AcctNo", "holder": "Name"},
        constants={"Source": "legacy-app"},
        include_provenance=False,
    )
    path = tmp_path / "m.csv"
    export([_record("ac1")], SCHEMA, CsvSink(path), mapping=mapping)
    rows = list(csv.DictReader(path.open()))
    assert rows == [{"AcctNo": "AC1", "Name": "Jane Roe", "Source": "legacy-app"}]


def test_webhook_refuses_remote_by_default() -> None:
    with pytest.raises(ValueError, match="not on this machine"):
        WebhookSink("https://example.com/hook")


def test_webhook_posts_to_loopback() -> None:
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/hook"
    export([_record("ac1")], SCHEMA, WebhookSink(url))
    thread.join(5)
    server.server_close()
    assert received[0]["schema"] == "urn:openadapt:record:customer_account:v1"
    assert received[0]["rows"][0]["account_number"] == "AC1"


def test_default_single_reads_export_as_unchecked() -> None:
    fields = []
    for name, raw in (
        ("account_number", "AC9"),
        ("holder", "Sam Lee"),
        ("open_items", "0"),
        ("active", "no"),
    ):
        spec = SCHEMA.field(name)
        fields.append(combine_reads(spec, [channel_read(spec, "clipboard", raw)]))
    record = RecordRead(
        schema_name="customer_account",
        schema_version=1,
        record_key="AC9",
        run_id="run-2",
        read_at="2026-10-17T21:00:00-04:00",
        fields=tuple(fields),
    )
    (row,) = export_rows([record], SCHEMA).rows
    assert row["_grade"] == "unchecked"
    assert row["_receipt_sha256"] is None
    assert row["active"] is False
    assert export_rows([record], SCHEMA, min_grade="same_screen").rows == []
