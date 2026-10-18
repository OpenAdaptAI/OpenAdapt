"""The record and field schema: shapes, JSON Schema and read grades."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openadapt_flow.records.schema import (
    READ_GRADES,
    ChannelRead,
    FieldRead,
    FieldSpec,
    GradeGate,
    GradeTooLow,
    ReadProof,
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


def _read(
    channel: str, value: str, session: str = "acting", export_source=None
) -> ChannelRead:
    return ChannelRead(
        channel=channel,
        raw=value,
        normalized=value,
        session=session,
        export_source=export_source,
    )


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
    export = _read("export", "INV-1", "independent", "committed_data")
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


def test_proof_is_off_by_default_and_overridable() -> None:
    assert INVOICE.proof == ReadProof()
    assert not INVOICE.proof_for("vendor").double_read
    assert INVOICE.field("vendor").min_confidence is None
    strict = INVOICE.model_copy(
        update={"proof": ReadProof(double_read=True, seal=True)}
    )
    assert strict.proof_for("vendor").seal
    per_field = RecordSchema(
        name="x",
        fields=(
            FieldSpec(name="a", proof=ReadProof(committed_readback=True)),
            FieldSpec(name="b"),
        ),
        identity_fields=("a",),
    )
    assert per_field.proof_for("a").committed_readback
    assert not per_field.proof_for("b").committed_readback


def _record(grade: str, needs_review: bool = False) -> RecordRead:
    reads = (_read("clipboard", "1"),)
    agreement = "single"
    if grade != "unconfirmed":
        reads = (_read("clipboard", "1"), _read("database", "1", "independent"))
        agreement = "agree"
    field = FieldRead(
        field="invoice_number",
        value="1",
        reads=reads,
        agreement=agreement,
        confidence=0.7,
        grade=grade,
        needs_review=needs_review,
        review_reason="x" if needs_review else None,
    )
    return RecordRead(
        schema_name="vendor_invoice",
        schema_version=1,
        record_key="1",
        run_id="r",
        read_at="2026-10-17T21:00:00-04:00",
        fields=(field,),
    )


def test_default_single_read_is_unconfirmed_without_seal() -> None:
    record = _record("unconfirmed")
    assert record.grade == "unconfirmed"
    assert record.fields[0].seal is None


def test_grade_gate_halts_below_floor() -> None:
    gate = GradeGate(min_grade="committed_record_readback")
    with pytest.raises(GradeTooLow, match="below committed_record_readback"):
        gate.check(_record("unconfirmed"))
    gate.check(_record("verified"))
    with pytest.raises(GradeTooLow):
        GradeGate(min_grade="unconfirmed").check(
            _record("unconfirmed", needs_review=True)
        )
    with pytest.raises(GradeTooLow, match="vendor"):
        GradeGate(min_grade="unconfirmed", fields=("vendor",)).check(
            _record("verified")
        )


def test_export_source_decides_the_grade() -> None:
    clip = _read("clipboard", "INV-1")
    live = _read("export", "INV-1", "acting", "live_session")
    saved = _read("export", "INV-1", "independent", "committed_data")
    reopened = _read("clipboard", "INV-1", "reopened")
    assert not live.independent
    assert saved.independent
    assert grade_for([clip, live], "agree") == "screen_readback"
    assert grade_for([clip, live, reopened], "agree") == "committed_record_readback"
    assert grade_for([clip, saved], "agree") == "verified"
    # Two reads from the same live session never verify, however many.
    assert grade_for([live, _read("ocr", "INV-1")], "agree") == "screen_readback"


def test_export_source_is_required_and_consistent() -> None:
    with pytest.raises(ValidationError, match="needs export_source"):
        ChannelRead(channel="export", raw="x", session="independent")
    with pytest.raises(ValidationError, match="acting session"):
        ChannelRead(
            channel="export",
            raw="x",
            session="independent",
            export_source="live_session",
        )
    with pytest.raises(ValidationError, match="independent"):
        ChannelRead(channel="export", raw="x", export_source="committed_data")
    with pytest.raises(ValidationError, match="export reads only"):
        ChannelRead(channel="ocr", raw="x", export_source="live_session")


def test_live_export_cannot_claim_verified() -> None:
    reads = (
        _read("clipboard", "1"),
        _read("export", "1", "acting", "live_session"),
    )
    with pytest.raises(ValidationError, match="stronger than the reads support"):
        FieldRead(
            field="invoice_number",
            value="1",
            reads=reads,
            agreement="agree",
            confidence=1.0,
            grade="verified",
        )


def test_write_gate_accepts_either_export_kind_per_floor() -> None:
    def record_with(export: ChannelRead) -> RecordRead:
        reads = (_read("clipboard", "1"), export)
        field = FieldRead(
            field="invoice_number",
            value="1",
            reads=reads,
            agreement="agree",
            confidence=1.0,
            grade=grade_for(reads, "agree"),
        )
        return RecordRead(
            schema_name="vendor_invoice",
            schema_version=1,
            record_key="1",
            run_id="r",
            read_at="2026-10-17T21:00:00-04:00",
            fields=(field,),
        )

    live = record_with(_read("export", "1", "acting", "live_session"))
    saved = record_with(_read("export", "1", "independent", "committed_data"))
    lenient = GradeGate(min_grade="screen_readback")
    lenient.check(live)
    lenient.check(saved)
    strict = GradeGate(min_grade="verified")
    strict.check(saved)
    with pytest.raises(GradeTooLow, match="screen_readback"):
        strict.check(live)
