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
    channel: str, value: str, session: str = "acting", source_kind=None
) -> ChannelRead:
    return ChannelRead(
        channel=channel,
        raw=value,
        normalized=value,
        session=session,
        source_kind=source_kind,
    )


def test_grades_order_weakest_first() -> None:
    assert READ_GRADES == (
        "unchecked",
        "same_screen",
        "reopened",
        "system_of_record",
    )
    assert weakest_grade(["system_of_record", "same_screen"]) == "same_screen"
    assert weakest_grade([]) == "unchecked"
    assert grade_at_least("system_of_record", "reopened")
    assert not grade_at_least("same_screen", "reopened")


def test_grade_for_each_rung() -> None:
    clip, ocr = _read("clipboard", "INV-1"), _read("ocr", "INV-1")
    reopened = _read("clipboard", "INV-1", "reopened")
    export = _read("export", "INV-1", "independent", "committed_store")
    assert grade_for([clip], "single") == "unchecked"
    assert grade_for([clip, ocr], "disagree") == "unchecked"
    assert grade_for([clip, ocr], "ambiguous") == "unchecked"
    assert grade_for([clip, ocr], "agree") == "same_screen"
    assert grade_for([clip, ocr, reopened], "agree") == "reopened"
    assert grade_for([clip, export], "agree") == "system_of_record"


def test_system_of_record_needs_an_independent_channel() -> None:
    reads = (_read("clipboard", "1"), _read("ocr", "1", "reopened"))
    with pytest.raises(ValidationError, match="stronger than the reads support"):
        FieldRead(
            field="invoice_number",
            value="1",
            reads=reads,
            agreement="agree",
            confidence=1.0,
            grade="system_of_record",
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
            grade="unchecked",
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
        grade="same_screen",
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
    assert again.grade == "same_screen"
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
    if grade != "unchecked":
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
    record = _record("unchecked")
    assert record.grade == "unchecked"
    assert record.fields[0].seal is None


def test_grade_gate_halts_below_floor() -> None:
    gate = GradeGate(min_grade="reopened")
    with pytest.raises(GradeTooLow, match="below reopened"):
        gate.check(_record("unchecked"))
    gate.check(_record("system_of_record"))
    with pytest.raises(GradeTooLow):
        GradeGate(min_grade="unchecked").check(_record("unchecked", needs_review=True))
    with pytest.raises(GradeTooLow, match="vendor"):
        GradeGate(min_grade="unchecked", fields=("vendor",)).check(
            _record("system_of_record")
        )


def test_source_kind_decides_the_grade() -> None:
    clip = _read("clipboard", "INV-1")
    live = _read("export", "INV-1", "acting", "live_session")
    saved = _read("export", "INV-1", "independent", "committed_store")
    reopened = _read("clipboard", "INV-1", "reopened")
    assert not live.independent
    assert saved.independent
    assert grade_for([clip, live], "agree") == "same_screen"
    assert grade_for([clip, live, reopened], "agree") == "reopened"
    assert grade_for([clip, saved], "agree") == "system_of_record"
    # Two reads from the same live session never verify, however many.
    assert grade_for([live, _read("ocr", "INV-1")], "agree") == "same_screen"


def test_source_kind_is_required_and_consistent() -> None:
    with pytest.raises(ValidationError, match="needs source_kind"):
        ChannelRead(channel="export", raw="x", session="independent")
    with pytest.raises(ValidationError, match="acting session"):
        ChannelRead(
            channel="export",
            raw="x",
            session="independent",
            source_kind="live_session",
        )
    with pytest.raises(ValidationError, match="independent"):
        ChannelRead(channel="export", raw="x", source_kind="committed_store")
    with pytest.raises(ValidationError, match="export reads only"):
        ChannelRead(channel="ocr", raw="x", source_kind="live_session")


def test_live_export_cannot_claim_system_of_record() -> None:
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
            grade="system_of_record",
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
    saved = record_with(_read("export", "1", "independent", "committed_store"))
    lenient = GradeGate(min_grade="same_screen")
    lenient.check(live)
    lenient.check(saved)
    strict = GradeGate(min_grade="system_of_record")
    strict.check(saved)
    with pytest.raises(GradeTooLow, match="same_screen"):
        strict.check(live)
