"""Intake extraction over synthetic referral documents (no real patient data)."""

from __future__ import annotations

import json
import sys
import zlib
from email.message import EmailMessage
from pathlib import Path

import pytest

from openadapt_flow import privacy
from openadapt_flow.intake import (
    IntakeNotConfirmed,
    apply_agent_response,
    build_agent_request,
    extract_fields,
    read_document,
)
from openadapt_flow.intake.document import _pdf_text_builtin
from openadapt_flow.ir import ParamKind, ParamSpec

REFERRAL_LINES = [
    "Synthetic Referral Form (test fixture)",
    "Patient Name: Jane Testcase",
    "DOB: 03/14/1980",
    "Insurance No: ZX-0042-TEST",
    "Visit Type: Follow-up",
    "Urgent: yes",
    "Referring provider: Dr. Example",
]

SPECS = {
    "patient_name": ParamSpec(name="patient_name"),
    "dob": ParamSpec(name="dob", type=ParamKind.DATE),
    "insurance_no": ParamSpec(name="insurance_no"),
    "visit_type": ParamSpec(
        name="visit_type", type=ParamKind.ENUM, choices=["New", "Follow-up"]
    ),
    "urgent": ParamSpec(name="urgent", type=ParamKind.BOOLEAN),
}


def make_pdf(lines: list[str], *, compress: bool = False) -> bytes:
    """Build a minimal one-page PDF with a Helvetica text layer."""
    ops = ["BT", "/F1 11 Tf", "72 720 Td", "14 TL"]
    for i, line in enumerate(lines):
        esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({esc}) Tj" if i == 0 else f"T* ({esc}) Tj")
    ops.append("ET")
    content = "\n".join(ops).encode("latin-1")
    extra = b""
    if compress:
        content = zlib.compress(content)
        extra = b" /Filter /FlateDecode"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d%s >>\nstream\n" % (len(content), extra)
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return bytes(out)


@pytest.mark.parametrize("compress", [False, True])
def test_builtin_pdf_reader_reads_synthetic_referral(compress: bool) -> None:
    text = _pdf_text_builtin(make_pdf(REFERRAL_LINES, compress=compress))
    assert text.splitlines() == REFERRAL_LINES


def test_pypdf_reads_the_same_fixture_when_installed(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    path = tmp_path / "referral.pdf"
    path.write_bytes(make_pdf(REFERRAL_LINES))
    assert "Insurance No: ZX-0042-TEST" in read_document(path).lines


def test_local_extraction_types_and_confidence(tmp_path: Path) -> None:
    path = tmp_path / "referral.pdf"
    path.write_bytes(make_pdf(REFERRAL_LINES, compress=True))
    result = extract_fields(read_document(path), SPECS)
    values = {n: f.value for n, f in result.fields.items()}
    assert values == {
        "patient_name": "Jane Testcase",
        "dob": "1980-03-14",
        "insurance_no": "ZX-0042-TEST",
        "visit_type": "Follow-up",
        "urgent": "true",
    }
    assert result.needs_confirmation() == []
    assert result.to_params()["dob"] == "1980-03-14"


def test_weak_label_and_invalid_values_need_a_person(tmp_path: Path) -> None:
    lines = ["Name: Jane Testcase", "DOB: sometime in spring", "Visit Type: Walk-in"]
    path = tmp_path / "fax.txt"
    path.write_text("\n".join(lines))
    specs = {k: SPECS[k] for k in ("patient_name", "dob", "visit_type", "urgent")}
    result = extract_fields(read_document(path), specs)
    assert result.fields["patient_name"].confidence == pytest.approx(0.6)
    assert result.fields["dob"].error
    assert result.fields["visit_type"].error
    assert result.fields["urgent"].value is None
    assert set(result.needs_confirmation()) == {
        "patient_name",
        "dob",
        "visit_type",
        "urgent",
    }
    with pytest.raises(IntakeNotConfirmed):
        result.to_params()
    result.confirm("patient_name")
    with pytest.raises(ValueError):
        result.confirm("dob", "not a date")
    result.confirm("dob", "1980-03-14")
    result.confirm("visit_type", "new")
    result.confirm("urgent", "no")
    assert result.to_params() == {
        "patient_name": "Jane Testcase",
        "dob": "1980-03-14",
        "visit_type": "New",
        "urgent": "false",
    }


def test_conflicting_values_are_flagged(tmp_path: Path) -> None:
    path = tmp_path / "r.txt"
    path.write_text("Insurance No: A1\nInsurance Number: B2\n")
    result = extract_fields(
        read_document(path), {"insurance_no": SPECS["insurance_no"]}
    )
    assert result.needs_confirmation() == ["insurance_no"]


def test_optional_missing_field_does_not_block(tmp_path: Path) -> None:
    path = tmp_path / "r.txt"
    path.write_text("Patient Name: Jane Testcase\n")
    specs = {
        "patient_name": SPECS["patient_name"],
        "note": ParamSpec(name="note", required=False),
    }
    assert extract_fields(read_document(path), specs).to_params() == {
        "patient_name": "Jane Testcase"
    }


def test_email_with_pdf_attachment(tmp_path: Path) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Referral"
    msg.set_content("Please see attached.\nUrgent: no")
    msg.add_attachment(
        make_pdf(REFERRAL_LINES[:4]),
        maintype="application",
        subtype="pdf",
        filename="referral.pdf",
    )
    path = tmp_path / "referral.eml"
    path.write_bytes(bytes(msg))
    doc = read_document(path)
    assert doc.kind == "email"
    result = extract_fields(
        doc, {k: SPECS[k] for k in ("patient_name", "insurance_no", "urgent")}
    )
    assert result.to_params() == {
        "patient_name": "Jane Testcase",
        "insurance_no": "ZX-0042-TEST",
        "urgent": "false",
    }


def test_fax_image_uses_ocr_lines(tmp_path: Path) -> None:
    path = tmp_path / "fax.png"
    path.write_bytes(b"not really an image; OCR is injected")
    doc = read_document(
        path, ocr=lambda _data: ["Patient Name: Jane Testcase", "DOB: 1980-03-14"]
    )
    result = extract_fields(doc, {k: SPECS[k] for k in ("patient_name", "dob")})
    assert result.to_params() == {"patient_name": "Jane Testcase", "dob": "1980-03-14"}


class _FakeScrubber:
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        for secret in ("Jane Testcase", "ZX-0042-TEST", "03/14/1980"):
            text = text.replace(secret, "<REDACTED>")
        return text


def test_agent_mode_keeps_identifiers_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    privacy.set_text_scrubber(_FakeScrubber())
    try:
        path = tmp_path / "referral.pdf"
        path.write_bytes(make_pdf(REFERRAL_LINES))
        doc = read_document(path)
        request = build_agent_request(doc, SPECS)
        sent = json.dumps(request)
        for secret in ("Jane Testcase", "ZX-0042-TEST", "03/14/1980"):
            assert secret not in sent
        response = {
            "fields": {
                "patient_name": {
                    "line": 2,
                    "label": "Patient Name",
                    "confidence": 0.99,
                },
                "dob": {"line": 3, "label": "DOB", "confidence": 0.99},
                "insurance_no": {"line": 4, "label": "", "confidence": 0.99},
                "visit_type": {"line": 99, "label": "Visit Type"},
            }
        }
        result = apply_agent_response(doc, SPECS, response)
        assert result.fields["patient_name"].value == "Jane Testcase"
        assert result.fields["dob"].value == "1980-03-14"
        # No label: confidence capped, so a person confirms it.
        assert "insurance_no" in result.needs_confirmation()
        assert "visit_type" in result.needs_confirmation()
    finally:
        privacy.reset_scrubbers()


def test_agent_mode_refuses_without_scrubbing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "off")
    path = tmp_path / "r.txt"
    path.write_text("Patient Name: Jane Testcase")
    with pytest.raises(RuntimeError):
        build_agent_request(read_document(path), SPECS)


def test_intake_imports_no_model_client() -> None:
    import importlib

    for mod in [m for m in list(sys.modules) if m.startswith(("anthropic", "openai"))]:
        sys.modules.pop(mod)
    importlib.reload(importlib.import_module("openadapt_flow.intake.extract"))
    assert not any(m.startswith(("anthropic", "openai")) for m in sys.modules)


def test_cli_writes_params_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.__main__ import main
    from openadapt_flow.intake import cli

    monkeypatch.setattr(cli, "_specs_for_bundle", lambda _b: dict(SPECS))
    doc = tmp_path / "referral.pdf"
    doc.write_bytes(make_pdf(REFERRAL_LINES))
    out = tmp_path / "params.json"
    assert main(["intake", "bundle", str(doc), "--out", str(out), "--no-input"]) == 0
    assert json.loads(out.read_text())["insurance_no"] == "ZX-0042-TEST"

    weak = tmp_path / "weak.txt"
    weak.write_text("Name: Jane Testcase\n")
    out2 = tmp_path / "p2.json"
    monkeypatch.setattr(
        cli, "_specs_for_bundle", lambda _b: {"patient_name": SPECS["patient_name"]}
    )
    assert main(["intake", "b", str(weak), "--out", str(out2), "--no-input"]) == 2
    assert not out2.exists()
    assert (
        main(["intake", "b", str(weak), "--out", str(out2), "--accept", "patient_name"])
        == 0
    )
