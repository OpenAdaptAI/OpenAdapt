"""The reading step: double read, agreement, confidence, grades and review."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Optional

import pytest
from PIL import Image

from openadapt_flow.records.read import (
    add_read,
    channel_read,
    combine_reads,
    normalize_for,
    read_field,
)
from openadapt_flow.records.schema import FieldSpec, ReadProof, SealRef

DOUBLE = ReadProof(double_read=True)

CODE = FieldSpec(name="account_number", kind="code")
AMOUNT = FieldSpec(name="balance", kind="currency")
DUE = FieldSpec(name="due_date", kind="date", date_order="mdy")
STATUS = FieldSpec(name="status", kind="enum", choices=("Open", "Closed"))


def test_normalize_by_kind() -> None:
    assert normalize_for(CODE, " AC-10 42 ") == "ac1042"
    assert normalize_for(AMOUNT, "$1,234.50") == "1234.5"
    assert normalize_for(DUE, "03/04/2026") == "2026-03-04"
    assert normalize_for(STATUS, "open") == "Open"
    assert normalize_for(STATUS, "pending") is None
    assert normalize_for(FieldSpec(name="n", kind="integer"), "1,200") == "1200"
    assert normalize_for(FieldSpec(name="b", kind="boolean"), "Yes") == "true"
    assert normalize_for(FieldSpec(name="p", pattern=r"[a-z]{3}"), "abcd") is None
    assert normalize_for(CODE, "   ") is None


def test_clipboard_and_ocr_agree() -> None:
    reads = [
        channel_read(CODE, "clipboard", "AC-1042"),
        channel_read(CODE, "ocr", "AC 1042", engine_confidence=0.9),
    ]
    field = combine_reads(CODE, reads)
    assert field.agreement == "agree"
    assert field.value == "AC-1042"
    assert field.grade == "screen_readback"
    assert field.confidence == pytest.approx(0.995)
    assert not field.needs_review


def test_lookalike_glyphs_are_ambiguous_not_a_match() -> None:
    reads = [
        channel_read(CODE, "clipboard", "AC-10O2"),
        channel_read(CODE, "ocr", "AC-1002", engine_confidence=0.99),
    ]
    field = combine_reads(CODE, reads)
    assert field.agreement == "ambiguous"
    assert field.value is None
    assert field.grade == "unconfirmed"
    assert field.needs_review
    assert "look-alike" in (field.review_reason or "")


def test_disagreement_goes_to_review() -> None:
    reads = [
        channel_read(AMOUNT, "clipboard", "$1,234.50"),
        channel_read(AMOUNT, "ocr", "$1,284.50", engine_confidence=0.95),
    ]
    field = combine_reads(AMOUNT, reads)
    assert field.agreement == "disagree"
    assert field.value is None
    assert field.needs_review
    assert field.review_reason == "channels disagree"


def test_single_read_is_unconfirmed_not_reviewed_by_default() -> None:
    field = combine_reads(
        CODE, [channel_read(CODE, "ocr", "AC-1042", engine_confidence=1.0)]
    )
    assert field.agreement == "single"
    assert field.value == "AC-1042"
    assert field.grade == "unconfirmed"
    assert field.confidence == pytest.approx(0.6)
    assert not field.needs_review
    assert field.seal is None


def test_single_read_reviewed_when_double_read_or_confidence_asked() -> None:
    reads = [channel_read(CODE, "ocr", "AC-1042", engine_confidence=1.0)]
    assert combine_reads(CODE, reads, proof=DOUBLE).needs_review
    gated = FieldSpec(name="account_number", kind="code", min_confidence=0.9)
    assert "below 0.90" in (combine_reads(gated, reads).review_reason or "")


def test_invalid_value_is_flagged() -> None:
    reads = [
        channel_read(STATUS, "clipboard", "Open"),
        channel_read(STATUS, "ocr", "0pen"),
    ]
    field = combine_reads(STATUS, reads)
    assert field.needs_review
    assert "not a valid enum: ocr" in (field.review_reason or "")


def test_grade_ladder_with_add_read() -> None:
    first = combine_reads(
        CODE,
        [
            channel_read(CODE, "clipboard", "AC-1042"),
            channel_read(CODE, "ocr", "AC-1042"),
        ],
    )
    assert first.grade == "screen_readback"
    reopened = add_read(
        CODE, first, channel_read(CODE, "clipboard", "AC-1042", session="reopened")
    )
    assert reopened.grade == "committed_record_readback"
    verified = add_read(CODE, reopened, channel_read(CODE, "database", "AC1042"))
    assert verified.grade == "verified"
    assert verified.value == "AC1042"  # the database read is preferred


def test_disagreeing_independent_read_demotes_to_review() -> None:
    first = combine_reads(
        CODE,
        [
            channel_read(CODE, "clipboard", "AC-1042"),
            channel_read(CODE, "ocr", "AC-1042"),
        ],
    )
    later = add_read(
        CODE,
        first,
        channel_read(CODE, "export", "AC-1043", export_source="committed_data"),
    )
    assert later.grade == "unconfirmed"
    assert later.needs_review


def test_screen_reads_alone_never_verify() -> None:
    reads = [
        channel_read(CODE, "clipboard", "AC-1042"),
        channel_read(CODE, "ocr", "AC-1042"),
        channel_read(CODE, "structure", "AC-1042", session="reopened"),
    ]
    assert combine_reads(CODE, reads).grade == "committed_record_readback"


class _Backend:
    def __init__(self) -> None:
        image = Image.new("RGB", (200, 100), "white")
        out = io.BytesIO()
        image.save(out, format="PNG")
        self.png = out.getvalue()
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[str] = []

    def screenshot(self) -> bytes:
        return self.png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.clicks.append((x, y))

    def press(self, key: str) -> None:
        self.keys.append(key)


class _Clipboard:
    def __init__(self, on_copy: Optional[str]) -> None:
        self.text: Optional[str] = None
        self.on_copy = on_copy
        self.backend: Optional[_Backend] = None

    def write_text(self, text: str) -> None:
        self.text = text

    def read_text(self) -> Optional[str]:
        if self.backend is not None and self.backend.keys and self.on_copy is not None:
            return self.on_copy
        return self.text


def _ocr(text: str, confidence: float = 0.9):
    def run(png: bytes, *, region=None):
        return [SimpleNamespace(text=text, confidence=confidence, region=region)]

    return run


def test_read_field_live_double_read() -> None:
    backend = _Backend()
    clip = _Clipboard("AC-1042")
    clip.backend = backend
    field = read_field(
        CODE,
        backend,
        (10, 20, 80, 16),
        proof=ReadProof(double_read=True, seal=True),
        clipboard=clip,
        ocr=_ocr("AC-1042"),
        step_id="s3",
        seal=SealRef(run_id="r1"),
    )
    assert field.seal == SealRef(run_id="r1")
    assert backend.clicks == [(50, 28)]
    assert backend.keys == ["Meta+a", "Meta+c"]
    assert field.agreement == "agree"
    assert field.grade == "screen_readback"
    assert field.region is not None
    assert field.region.bbox == (10, 20, 80, 16)
    assert field.region.frame_size == (200, 100)
    assert field.region.crop_sha256 and len(field.region.crop_sha256) == 64
    assert field.region.step_id == "s3"


def test_read_field_stale_clipboard_is_no_read() -> None:
    backend = _Backend()
    clip = _Clipboard(None)  # the copy did nothing: the sentinel is still there
    clip.backend = backend
    field = read_field(
        CODE, backend, (0, 0, 50, 10), proof=DOUBLE, clipboard=clip, ocr=_ocr("AC-1042")
    )
    assert [r.channel for r in field.reads] == ["clipboard", "ocr"]
    assert field.reads[0].raw is None
    assert field.agreement == "single"
    assert field.needs_review
    assert "double read requested" in (field.review_reason or "")


def test_read_field_default_is_one_channel_no_seal() -> None:
    field = read_field(
        CODE,
        _Backend(),
        (0, 0, 50, 10),
        ocr=_ocr("AC-1042", 1.0),
        seal=SealRef(run_id="r"),
    )
    assert [r.channel for r in field.reads] == ["ocr"]
    assert field.grade == "unconfirmed"
    assert field.seal is None
    assert not field.needs_review
    backend = _Backend()
    clip = _Clipboard("AC-1042")
    clip.backend = backend

    def no_ocr(png, *, region=None):
        raise AssertionError("OCR must not run for a default clipboard read")

    field = read_field(CODE, backend, (0, 0, 50, 10), clipboard=clip, ocr=no_ocr)
    assert [r.channel for r in field.reads] == ["clipboard"]
    assert field.value == "AC-1042"


def test_read_field_rejects_unsatisfiable_proof() -> None:
    with pytest.raises(ValueError, match="needs a clipboard"):
        read_field(CODE, _Backend(), (0, 0, 5, 5), proof=DOUBLE, ocr=_ocr("x"))
    with pytest.raises(ValueError, match="no SealRef"):
        read_field(
            CODE, _Backend(), (0, 0, 5, 5), proof=ReadProof(seal=True), ocr=_ocr("x")
        )


def test_export_reads_grade_by_source() -> None:
    first = combine_reads(CODE, [channel_read(CODE, "clipboard", "AC-1042")])
    live = add_read(
        CODE,
        first,
        channel_read(CODE, "export", "AC-1042", export_source="live_session"),
    )
    assert live.reads[-1].session == "acting"
    assert live.grade == "screen_readback"
    saved = add_read(
        CODE,
        first,
        channel_read(CODE, "export", "AC1042", export_source="committed_data"),
    )
    assert saved.reads[-1].session == "independent"
    assert saved.grade == "verified"
    assert saved.value == "AC1042"  # the independent read is reported
    with pytest.raises(ValueError, match="needs export_source"):
        channel_read(CODE, "export", "AC-1042")
