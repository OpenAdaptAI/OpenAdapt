"""The independent verification-coverage suite: its inputs and its stand-in."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import yaml

BENCH = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCH))

from verification_coverage.independent import standin  # noqa: E402

SUITE = BENCH / "verification_coverage" / "independent"
OPS = {
    "register_referral",
    "register_patient",
    "overwrite_chart",
    "update_chart",
    "update_history",
    "delete_history",
    "add_note",
    "add_allergy",
    "add_insurance",
}


def test_every_case_names_its_spec_clauses_and_a_verdict() -> None:
    suite = yaml.safe_load((SUITE / "faults.yaml").read_text())
    spec = (SUITE / "SPEC.md").read_text()
    ids = [c["id"] for c in suite["cases"]]
    assert len(ids) == len(set(ids))
    for case in suite["cases"]:
        assert case["expect"] in ("fault", "clean")
        for clause in case["clauses"]:
            assert f"**{clause}." in spec, clause
        for step in case["steps"]:
            assert step["actor"] in ("bot", "clinician2", "none")
            assert step["op"] in OPS
    classes = {c["class"] for c in suite["cases"]}
    assert {
        "wrong_record",
        "unrelated_change",
        "misread_source",
        "missing_side_record",
        "concurrent_legitimate",
        "duplicate",
    } <= classes
    assert all(
        c["expect"] == "clean"
        for c in suite["cases"]
        if c["class"] == "concurrent_legitimate"
    )


DUMP = """\
CREATE TABLE `patient_data` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `uuid` binary(16) DEFAULT NULL,
  `fname` varchar(255) NOT NULL DEFAULT '',
  `providerID` int(11) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `patient_data` VALUES
(1,NULL,'O\\'Neil',5),
(2,NULL,'Line\\nTwo',NULL);
"""


def test_the_dump_converts_with_values_and_affinity(tmp_path: Path) -> None:
    dump = tmp_path / "baseline.sql"
    dump.write_text(DUMP)
    out = tmp_path / "copy.sqlite"
    counts = standin.build(dump, out)
    assert counts == {"patient_data": 2}
    con = sqlite3.connect(out)
    rows = con.execute("SELECT id, fname, providerID FROM patient_data").fetchall()
    assert rows == [(1, "O'Neil", 5), (2, "Line\nTwo", None)]
    # MariaDB compares '5' = 5; the stand-in must too.
    assert con.execute(
        "SELECT COUNT(*) FROM patient_data WHERE providerID = ?", ("5",)
    ).fetchone() == (1,)
    con.close()
