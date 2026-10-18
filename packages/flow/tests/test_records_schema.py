"""The record and field schema: shapes, JSON Schema and read grades."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openadapt_flow.records.schema import (
    READ_GRADES,
    ChannelRead,
    FieldRead,
    FieldSpec,
    RecordRead,
    RecordSchema,
    SealRef,
    SourceRegion,
    grade_at_least,
    grade_for,
    weakest_grade,
)

INVOICE = RecordSchema(
    name="vendor_invoice",
    fields=(
        FieldSpec(name="invoice_number", kind="code"),
        FieldSpec(name="vendor", kind="text"),
        FieldSpec(name="amount_due", kind="currency"),
        FieldSpec(name="due_date", kind="date", date_order="mdy"),
        FieldSpec(name="status", kind="enum", choices=("open", "paid")),
        FieldSpec(name="memo", kind="text", required=False),
    ),
    identity_fields=("invoice_number",),
)


def _read(channel: str, value: str, session: str = "acting") -> ChannelRead:
    return ChannelRead(channel=channel, raw=value, normalized=value, session=session)


def test_grades_order_weakest_first() -> None:
    assert READ_GRADES == (
        "unconfirmed",
        "screen_readback",
        "committed_record_readback",
        "verified",
    )
    assert weakest_grade(["verified", "screen_readback"]) == "screen_readback"
    assert weakest_grade([]) == "unconfirmed"
    assert grade_at_least("verified", "committed_record_readback")
    assert not grade_at_least("screen_readback", "committed_record_readback")


def test_grade_for_each_rung() -> None:
    clip, ocr = _read("clipboard", "INV-1"), _read("ocr", "INV-1")
    reopened = _read("clipboard", "INV-1", "reopened")
    export = _read("export", "INV-1", "independent")
    assert grade_for([clip], "single") == "unconfirmed"
    assert grade_for([clip, ocr], "disagree") == "unconfirmed"
    assert grade_for([clip, ocr], "ambiguous") == "unconfirmed"
    assert grade_for([clip, ocr], "agree") == "screen_readback"
    assert grade_for([clip, ocr, reopened], "agree") == "committed_record_readback"
    assert grade_for([clip, export], "agree") == "verified"


def test_verified_needs_an_independent_channel() -> None:
    reads = (_read("clipboard", "1"), _read("ocr", "1", "reopened"))
    with pytest.raises(ValidationError, match="stronger than the reads support"):
        FieldRead(
            field="invoice_number",
            value="1",
            reads=reads,
            agreement="agree",
            confidence=1.0,
            grade="verified",
        )


def test_screen_channel_cannot_claim_independence() -> None:
    with pytest.raises(ValidationError, match="independent"):
        ChannelRead(channel="ocr", raw="x", session="independent")
    with pytest.raises(ValidationError, match="independent"):
        ChannelRead(channel="database", raw="x", session="acting")


def test_needs_review_requires_reason() -> None:
    with pytest.raises(ValidationError, match="review_reason"):
        FieldRead(
            field="vendor",
            value=None,
            reads=(_read("ocr", "Acme"),),
            agreement="single",
            confidence=0.5,
            grade="unconfirmed",
            needs_review=True,
        )


def test_schema_rejects_bad_shapes() -> None:
    with pytest.raises(ValidationError, match="lower snake case"):
        FieldSpec(name="Bad Name")
    with pytest.raises(ValidationError, match="needs choices"):
        FieldSpec(name="state", kind="enum")
    with pytest.raises(ValidationError, match="is not a field"):
        RecordSchema(name="x", fields=(FieldSpec(name="a"),), identity_fields=("b",))
    with pytest.raises(ValidationError, match="must be required"):
        RecordSchema(
            name="x",
            fields=(FieldSpec(name="a", required=False),),
            identity_fields=("a",),
        )
    with pytest.raises(ValidationError, match="unique"):
        RecordSchema(
            name="x",
            fields=(FieldSpec(name="a"), FieldSpec(name="a")),
            identity_fields=("a",),
        )


def test_json_schema_shape() -> None:
    doc = INVOICE.to_json_schema()
    assert doc["$id"] == "urn:openadapt:record:vendor_invoice:v1"
    assert doc["required"] == [
        "invoice_number",
        "vendor",
        "amount_due",
        "due_date",
        "status",
    ]
    assert doc["properties"]["status"]["enum"] == ["open", "paid"]
    assert doc["properties"]["due_date"]["format"] == "date"
    assert doc["properties"]["memo"]["anyOf"][1] == {"type": "null"}
    assert doc["additionalProperties"] is False


def test_record_read_round_trip_and_check() -> None:
    region = SourceRegion(frame_sha256="a" * 64, bbox=(10, 20, 100, 16))
    seal = SealRef(run_id="run-1")
    field = FieldRead(
        field="invoice_number",
        value="inv-1",
        reads=(_read("clipboard", "INV-1"), _read("ocr", "INV-1")),
        agreement="agree",
        confidence=0.99,
        grade="screen_readback",
        region=region,
        seal=seal,
    )
    record = RecordRead(
        schema_name="vendor_invoice",
        schema_version=1,
        record_key="inv-1",
        run_id="run-1",
        read_at="2026-10-17T20:00:00-04:00",
        fields=(field,),
    )
    again = RecordRead.model_validate_json(record.model_dump_json())
    assert again == record
    assert again.grade == "screen_readback"
    assert not seal.sealed
    with pytest.raises(ValueError, match="required fields not read"):
        record.check_against(INVOICE)


def test_region_validation() -> None:
    with pytest.raises(ValidationError, match="sha256"):
        SourceRegion(frame_sha256="nothex", bbox=(0, 0, 1, 1))
    with pytest.raises(ValidationError, match="bbox"):
        SourceRegion(frame_sha256="a" * 64, bbox=(0, 0, 0, 1))
