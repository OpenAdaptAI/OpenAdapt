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
    assert covered == set(CODES) - {"IX012"}


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


def test_seal_without_checksum_is_refused() -> None:
    doc = _load("valid/invoice_posting_result.json")
    doc["provenance"].pop("checksum")
    assert [v.code for v in check(doc)] == ["IX012"]


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
            FIXTURES / "invalid/verified_without_independent_read.json", tmp_path
        )
    assert not (tmp_path / "manifest.csv").exists()


def test_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cases", str(FIXTURES)]) == 0
    assert main(["check", str(FIXTURES / "invalid/self_approval.json")]) == 1
    assert "IX041" in capsys.readouterr().out
