"""Tests for multimodal ingestion (PDF CIDFont/FlateDecode, CSV, XLSX, OCR)."""

from __future__ import annotations

import io
import time
import zipfile
import zlib
from pathlib import Path

import pytest

from openadapt_flow.ingest import (
    LowConfidenceError,
    SpanAnchor,
    extract_pdf_pages_stdlib,
    gate_ocr_spans,
    ingest_file,
    ingest_ocr,
    ingest_pdf,
    ingest_xlsx,
)


def _build_cidfont_pdf() -> bytes:
    """Build a minimal valid PDF with FlateDecode ToUnicode CMap + content stream."""
    cmap_body = (
        b"/CIDInit /ProcSet findresource begin\n"
        b"12 dict begin\n"
        b"begincmap\n"
        b"3 beginbfchar\n"
        b"<0001> <004D>\n"  # M
        b"<0002> <0052>\n"  # R
        b"<0003> <004E>\n"  # N
        b"endbfchar\n"
        b"1 beginbfrange\n"
        b"<0010> <0019> <0030>\n"  # 0..9
        b"endbfrange\n"
        b"endcmap\n"
        b"end\nend\n"
    )
    # Content stream: "MRN 4209" via CID hex + "(ICD E11.9)" via literal TJ
    content_body = (
        b"BT\n"
        b"/F1 12 Tf\n"
        b"[<000100020003> -200 <0014001200100019>] TJ\n"
        b"T*\n"
        b"(ICD E11.9) Tj\n"
        b"ET\n"
    )
    cmap_z = zlib.compress(cmap_body)
    content_z = zlib.compress(content_body)
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Length "
        + str(len(cmap_z)).encode("ascii")
        + b" /Filter /FlateDecode >>\nstream\n"
        + cmap_z
        + b"\nendstream\nendobj\n"
        b"2 0 obj\n<< /Length "
        + str(len(content_z)).encode("ascii")
        + b" /Filter /FlateDecode >>\nstream\n"
        + content_z
        + b"\nendstream\nendobj\n"
        b"%%EOF\n"
    )


def _build_sample_xlsx() -> bytes:
    """Build a minimal valid multi-sheet .xlsx archive using stdlib zipfile."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            '<sheet name="Claims" sheetId="1" r:id="rId1"/>'
            '<sheet name="Summary" sheetId="2" r:id="rId2"/>'
            "</sheets></workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>MRN</t></si>"
            "<si><t>CPT</t></si>"
            "<si><t>Amount</t></si>"
            "<si><t>MRN-1001</t></si>"
            "</sst>",
        )
        # Sheet1: row 1 has A1=MRN, B1=CPT, C1=Amount; row 2 skips B2 (inline/empty test)
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>"
            '<row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="s"><v>1</v></c>'
            '<c r="C1" t="s"><v>2</v></c>'
            "</row>"
            '<row r="2">'
            '<c r="A2" t="s"><v>3</v></c>'
            '<c r="C2"><v>25000</v></c>'
            "</row>"
            "</sheetData></worksheet>",
        )
        zf.writestr(
            "xl/worksheets/sheet2.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>"
            '<row r="1">'
            '<c r="A1" t="inlineStr"><is><t>Total</t></is></c>'
            '<c r="B1"><v>1</v></c>'
            "</row>"
            "</sheetData></worksheet>",
        )
    return buf.getvalue()


class TestPdfIngestion:
    def test_cidfont_flatedecode_tounicode_stdlib(self) -> None:
        pdf_bytes = _build_cidfont_pdf()
        pages = extract_pdf_pages_stdlib(pdf_bytes)
        assert len(pages) == 1
        assert "MRN 4209" in pages[0]
        assert "ICD E11.9" in pages[0]

        result = ingest_pdf(pdf_bytes, use_pypdf=False)
        assert result.source_kind == "pdf"
        assert result.metadata["backend"] == "stdlib"
        assert "MRN 4209" in result.text
        assert len(result.spans) == 1
        assert result.spans[0].page == 1

    def test_redos_free_linear_time_on_pathological_pdf_bytes(self) -> None:
        # Pathological nested brackets/streams that trigger catastrophic
        # backtracking in naive regex PDF parsers.
        payload = b"%PDF-1.4\n" + (b"stream\n" + b"(<" * 5000 + b"\n") * 5 + b"%%EOF"
        t0 = time.monotonic()
        pages = extract_pdf_pages_stdlib(payload)
        elapsed = time.monotonic() - t0
        assert pages == []
        assert elapsed < 1.0

    def test_scanned_pdf_uses_ocr_hook_with_confidence_gate(self) -> None:
        scanned_pdf = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n"

        def hook(_raw: bytes) -> list[SpanAnchor]:
            return [
                SpanAnchor(
                    page=1,
                    bbox=(10.0, 20.0, 120.0, 40.0),
                    text="E11.9",
                    ocr_conf=0.96,
                    source="ocr",
                    field_kind="icd10",
                )
            ]

        res = ingest_pdf(scanned_pdf, use_pypdf=False, ocr_hook=hook)
        assert res.source_kind == "ocr"
        assert res.text == "E11.9"
        assert res.flagged_spans == ()


class TestTabularIngestion:
    def test_csv_ingestion(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "patients.csv"
        csv_file.write_bytes(b'\xef\xbb\xbfMRN,Name,Amount\n1001,Jane Doe,"$25,000"\n')
        res = ingest_file(csv_file)
        assert res.source_kind == "csv"
        assert len(res.tables) == 1
        assert res.tables[0].headers == ("MRN", "Name", "Amount")
        assert res.tables[0].rows == (("1001", "Jane Doe", "$25,000"),)
        assert any(s.text == "1001" and s.row == 2 and s.col == 1 for s in res.spans)

    def test_xlsx_stdlib_ingestion(self, tmp_path: Path) -> None:
        xlsx_file = tmp_path / "claims.xlsx"
        xlsx_file.write_bytes(_build_sample_xlsx())
        res = ingest_xlsx(xlsx_file, use_openpyxl=False)
        assert res.source_kind == "xlsx"
        assert res.metadata["backend"] == "stdlib"
        assert len(res.tables) == 2
        claims = res.tables[0]
        assert claims.name == "Claims"
        assert claims.headers == ("MRN", "CPT", "Amount")
        assert claims.rows == (("MRN-1001", "", "25000"),)
        summary = res.tables[1]
        assert summary.name == "Summary"
        assert summary.headers == ("Total", "1")


class TestOcrConfidenceGating:
    def test_refuses_low_confidence_critical_code_by_default(self) -> None:
        def hook(_raw: bytes) -> list[dict[str, object]]:
            return [
                {
                    "page": 1,
                    "bbox": (0, 0, 100, 20),
                    "text": "Diagnosis E11.9",
                    "ocr_conf": 0.62,
                }
            ]

        with pytest.raises(LowConfidenceError) as exc_info:
            ingest_ocr(b"fake-image", hook)
        assert len(exc_info.value.flagged_spans) == 1
        assert exc_info.value.flagged_spans[0].low_confidence is True

    def test_flag_mode_marks_low_confidence_without_raising(self) -> None:
        spans = [
            SpanAnchor(
                page=1,
                bbox=(0.0, 0.0, 80.0, 20.0),
                text="MRN-88412",
                ocr_conf=0.70,
                source="ocr",
                field_kind="mrn",
            ),
            SpanAnchor(
                page=1,
                bbox=(0.0, 30.0, 80.0, 50.0),
                text="General note",
                ocr_conf=0.50,
                source="ocr",
            ),
            SpanAnchor(
                page=1,
                bbox=(0.0, 60.0, 80.0, 80.0),
                text="Clear heading",
                ocr_conf=0.98,
                source="ocr",
            ),
        ]
        checked, flagged = gate_ocr_spans(spans, mode="flag")
        assert len(checked) == 3
        assert len(flagged) == 2
        assert checked[0].low_confidence is True
        assert "critical_ocr_confidence_below_threshold" in (
            checked[0].gate_reason or ""
        )
        assert checked[1].low_confidence is True
        assert checked[2].low_confidence is False

    def test_missing_confidence_on_critical_ocr_span_is_refused(self) -> None:
        spans = [
            SpanAnchor(
                page=1,
                bbox=(0.0, 0.0, 50.0, 15.0),
                text="99213",
                ocr_conf=None,
                source="ocr",
                field_kind="cpt",
            )
        ]
        with pytest.raises(LowConfidenceError):
            gate_ocr_spans(spans, mode="refuse")
