"""The accounts-ledger records example runs end to end, and the OMOP
template stays outside the core."""

from __future__ import annotations

import csv
import importlib.util
import sqlite3
import sys
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "records"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_demo_end_to_end(tmp_path, capsys) -> None:
    demo = _load("records_demo", EXAMPLE / "demo.py")
    assert demo.main(["--out", str(tmp_path), "--simulated-ocr"]) == 0
    printed = capsys.readouterr().out
    assert "AC-1042: grade=system_of_record review=False" in printed
    assert "AC-3307: grade=unchecked review=True" in printed
    assert "review queue: 1 open item(s)" in printed
    assert "write gate: AC-1042 may be written" in printed
    assert "write gate: halt: record AC-3307: below reopened" in printed
    rows = {
        r["_record_key"]: r for r in csv.DictReader((tmp_path / "accounts.csv").open())
    }
    assert rows["AC-1042"]["_grade"] == "system_of_record"
    # A person settled AC-3307's balance. That is not a read, so the record
    # stays unchecked and an export that needs reopened skips it.
    assert "AC-3307" not in rows
    assert "skipped AC-3307: below reopened: balance" in printed
    with sqlite3.connect(tmp_path / "accounts.db") as conn:
        assert conn.execute("SELECT count(*) FROM accounts").fetchone() == (2,)


def test_omop_template(tmp_path) -> None:
    omop = _load("omop_measurement", EXAMPLE / "templates" / "omop" / "measurement.py")
    rows = [
        {
            "_record_key": "R-1",
            "read_date": "2026-10-01",
            "score": "12.5",
            "_grade": "system_of_record",
            "_receipt_sha256": "c" * 64,
        },
        {"_record_key": "R-unmapped", "read_date": "2026-10-01", "score": "3"},
    ]
    out = omop.to_measurements(
        rows,
        person_id={"R-1": 7},
        date_field="read_date",
        concepts={
            "score": omop.FieldConcept(measurement_concept_id=0, source_value="score")
        },
        type_concept_id=32817,
    )
    assert len(out) == 1
    assert out[0]["person_id"] == 7
    assert out[0]["value_as_number"] == "12.5"
    assert out[0]["openadapt_grade"] == "system_of_record"
    assert set(out[0]) == set(omop.MEASUREMENT_COLUMNS)


def test_core_records_package_has_no_domain_terms() -> None:
    core = Path(__file__).resolve().parents[1] / "openadapt_flow" / "records"
    banned = ("omop", "patient", "clinic", "emr", "diagnosis", "phi")
    for path in core.glob("*.py"):
        text = path.read_text().lower()
        for word in banned:
            assert f" {word}" not in text, f"{path.name} mentions {word!r}"
