"""Records interchange v0: schema, conformance rules, fixture harness, L1 bridge."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import jsonschema
import pytest

from openadapt_flow import interchange
from openadapt_flow.interchange import (
    CODES,
    ExchangeNotConforming,
    check,
    check_file,
    compute_checksum,
    emit_exchange,
    run_cases,
)
from openadapt_flow.interchange.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "interchange"
SCHEMA = ROOT / "schemas" / "records-interchange-v0.json"


def _load(rel: str) -> dict:
    return json.loads((FIXTURES / rel).read_text())


def test_published_schema_matches_the_model() -> None:
    assert json.loads(SCHEMA.read_text()) == interchange.json_schema()


def test_core_format_names_no_domain() -> None:
    """The bank-back-office test: the core format must not name a domain."""
    text = (
        SCHEMA.read_text().lower()
        + (ROOT / "openadapt_flow" / "interchange" / "model.py").read_text().lower()
    )
    for word in (
        "patient",
        "clinic",
        "physician",
        "eye",
        "emr",
        "mrn",
        "diagnos",
        "invoice",
        "vendor",
    ):
        assert not re.search(rf"\b{word}", text), word


def test_every_fixture_case_passes() -> None:
    results = run_cases(FIXTURES)
    assert len(results) >= 20
    failed = [r for r in results if not r.ok]
    assert not failed, failed


def test_every_rule_has_an_invalid_fixture() -> None:
    cases = json.loads((FIXTURES / "cases.json").read_text())["cases"]
    covered = {c for case in cases for c in case["expect"]}
    assert covered == set(CODES)


def test_grades_are_check_level_names() -> None:
    from openadapt_flow.check_level import CheckLevel

    assert interchange.READ_GRADES == (
        "unchecked",
        "same_screen",
        "reopened",
        "system_of_record",
    )
    for grade in interchange.READ_GRADES:
        assert CheckLevel.from_label(grade).label == grade
    assert "verified" not in json.dumps(interchange.json_schema())


@pytest.mark.parametrize(
    "rel", sorted(p.name for p in (FIXTURES / "valid").glob("*.json"))
)
def test_valid_fixtures_pass_the_published_json_schema(rel: str) -> None:
    jsonschema.validate(_load(f"valid/{rel}"), json.loads(SCHEMA.read_text()))


def test_json_schema_rejects_domain_fields_at_top_level() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            _load("invalid/domain_field_in_core.json"), json.loads(SCHEMA.read_text())
        )


def test_checksum_ignores_key_order_and_seals() -> None:
    doc = _load("valid/invoice_posting_result.json")
    reordered = dict(reversed(list(doc.items())))
    reordered["seals"] = []
    assert compute_checksum(reordered) == doc["provenance"]["checksum"]


def test_checksum_is_required() -> None:
    doc = _load("valid/invoice_posting.json")
    doc["provenance"].pop("checksum")
    assert [v.code for v in check(doc)] == ["IX001"]
    assert "IX012" not in CODES


def test_model_suggestion_is_allowed_only_as_advisory() -> None:
    doc = _load("valid/referral_intake.json")
    assert doc["decisions"][0]["advisory"][0]["source"]["kind"] == "model"
    assert check_file(FIXTURES / "valid/referral_intake.json") == []


def test_emit_exchange_writes_l1_manifest_rows(tmp_path: Path) -> None:
    out = tmp_path / "extractions"
    refs = emit_exchange(FIXTURES / "valid/referral_intake.json", out)
    assert [r.path.name for r in refs] == ["TEST-0007_2026-10-29_referral.txt"]
    rows = list(csv.DictReader((out / "manifest.csv").open()))
    assert rows[0]["source_type"] == "fax_folder"
    assert rows[0]["session_id"] == "fax-2026-10-29"

    refs = emit_exchange(FIXTURES / "valid/referral_result.json", out)
    assert refs[0].path.suffix == ".json"
    assert json.loads(refs[0].path.read_text())["outcome"] == "committed"


def test_emit_exchange_refuses_a_non_conforming_exchange(tmp_path: Path) -> None:
    with pytest.raises(ExchangeNotConforming, match="IX030"):
        emit_exchange(
            FIXTURES / "invalid/system_of_record_without_independent_read.json",
            tmp_path,
        )
    assert not (tmp_path / "manifest.csv").exists()


def test_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cases", str(FIXTURES)]) == 0
    assert main(["check", str(FIXTURES / "invalid/self_approval.json")]) == 1
    assert "IX041" in capsys.readouterr().out


def _exchange_dir_with_payload(tmp_path: Path, payload: Path) -> Path:
    """Copy the invoice fixture into its own directory with ``payload`` as
    ``payloads/invoice-INV-20417.txt`` (a file or a symlink)."""
    root = tmp_path / "exchange"
    (root / "payloads").mkdir(parents=True)
    target = root / "payloads" / "invoice-INV-20417.txt"
    if payload.is_symlink():
        target.symlink_to(payload.readlink())
    else:
        target.write_bytes(payload.read_bytes())
    doc = _load("valid/invoice_posting.json")
    (root / "ex.json").write_text(json.dumps(doc))
    return root / "ex.json"


def test_payload_symlink_out_of_the_exchange_dir_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(
        (FIXTURES / "valid/payloads/invoice-INV-20417.txt").read_bytes()
    )
    link = tmp_path / "link"
    link.symlink_to(outside)
    ex = _exchange_dir_with_payload(tmp_path, link)
    assert [v.code for v in check_file(ex)] == ["IX071"]
    with pytest.raises(ExchangeNotConforming, match="IX071"):
        emit_exchange(ex, tmp_path / "extractions")
    assert not (tmp_path / "extractions").exists()


def test_payload_inside_the_exchange_dir_is_emitted(tmp_path: Path) -> None:
    ex = _exchange_dir_with_payload(
        tmp_path, FIXTURES / "valid/payloads/invoice-INV-20417.txt"
    )
    assert check_file(ex) == []
    refs = emit_exchange(ex, tmp_path / "extractions")
    assert [r.path.name for r in refs] == ["V-0042_2026-10-29_invoice.txt"]


@pytest.mark.parametrize("raw", ["..\\..\\x", "a\\b.txt", "C:/x", "c:x", "/etc/x"])
def test_payload_paths_that_are_not_relative_posix_are_refused(raw: str) -> None:
    doc = _load("valid/invoice_posting.json")
    doc["artifacts"][0]["payload"]["path"] = raw
    doc = interchange.with_checksum(doc)
    assert [v.code for v in check(doc)] == ["IX071"]


def test_reopened_grade_follows_the_records_rule() -> None:
    """The reading step's rule: an acting read and a reopened screen read."""
    from openadapt_flow.interchange.conformance import supported_grade
    from openadapt_flow.interchange.model import Read

    def reads(*pairs: tuple[str, str | None, str | None]) -> list[Read]:
        return [
            Read(channel=c, value="X", session=s, source_kind=k) for c, s, k in pairs
        ]

    assert supported_grade(reads(("ocr", "reopened", None))) == "unchecked"
    assert (
        supported_grade(
            reads(("ocr", "reopened", None), ("clipboard", "reopened", None))
        )
        == "same_screen"
    )
    assert (
        supported_grade(reads(("ocr", "acting", None), ("ocr", "reopened", None)))
        == "reopened"
    )
    assert (
        supported_grade(reads(("ocr", "acting", None), ("ocr", "acting", None)))
        == "unchecked"
    )
    assert (
        supported_grade(
            reads(("ocr", "acting", None), ("export", "independent", "committed_store"))
        )
        == "system_of_record"
    )
    assert (
        supported_grade(
            reads(("ocr", "acting", None), ("export", "acting", "live_session"))
        )
        == "same_screen"
    )


def test_spec_lists_every_code_in_order() -> None:
    spec = (ROOT / "docs" / "INTERCHANGE.md").read_text()
    listed = re.findall(r"^\| `(IX\d+)` \|", spec, re.M)
    assert listed == list(CODES) == sorted(CODES)


def test_no_action_needs_no_write_evidence() -> None:
    assert check_file(FIXTURES / "valid/referral_no_action.json") == []
    for rel, code in (
        ("invalid/no_action_without_evidence.json", "IX082"),
        ("invalid/no_action_write_detected.json", "IX083"),
        ("invalid/no_action_below_commit_level.json", "IX082"),
    ):
        assert {v.code for v in check_file(FIXTURES / rel)} == {code}, rel


def test_no_action_evidence_needs_a_before_and_an_after_read() -> None:
    codes = {
        v.code
        for v in check_file(FIXTURES / "invalid/no_action_without_before_read.json")
    }
    assert codes == {"IX085"}
    doc = _load("valid/referral_no_action.json")
    doc["no_action"].pop("decided_at")
    doc["seals"] = []
    assert {v.code for v in check(interchange.with_checksum(doc))} == {"IX085"}


def test_a_nul_in_a_payload_path_is_ix071_not_an_error(tmp_path: Path) -> None:
    from openadapt_flow.interchange.conformance import resolve_payload

    doc = _load("invalid/payload_nul_path.json")
    assert [v.code for v in check(doc, base_dir=tmp_path)] == ["IX071"]
    assert resolve_payload(tmp_path, "a\x00b") is None


def test_numbers_compare_by_value() -> None:
    from openadapt_flow.interchange.conformance import _same

    assert _same(1, 1.0) and _same(1, "1") and _same(1.0, "1.00")
    assert not _same(1, "2") and not _same(True, 1) and not _same(None, 0)
    assert not _same("1.0", "1")  # two strings compare as text


def test_emit_refuses_a_payload_changed_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.interchange import l1

    ex = _exchange_dir_with_payload(
        tmp_path, FIXTURES / "valid/payloads/invoice-INV-20417.txt"
    )
    assert check_file(ex) == []
    real = l1.check_file

    def check_then_swap(path):
        out = real(path)
        (ex.parent / "payloads" / "invoice-INV-20417.txt").write_text("swapped")
        return out

    monkeypatch.setattr(l1, "check_file", check_then_swap)
    with pytest.raises(ExchangeNotConforming, match="IX070"):
        emit_exchange(ex, tmp_path / "extractions")
    assert not (tmp_path / "extractions" / "manifest.csv").exists()
