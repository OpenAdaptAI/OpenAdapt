"""The independent verification-coverage suite: its inputs and its stand-in."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
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


# Resumed rows are bound to the measurement that produced them.

PRODUCTION_ROWS = (
    SUITE / "results" / "phase0" / "c526ab686" / "faults-standin-production.rows.json"
)


def _phase0_args(out: Path, baseline: Path, contract: str) -> list[str]:
    return [
        "standin",
        "--out",
        str(out),
        "--baseline-sql",
        str(baseline),
        "--contract",
        contract,
    ]


def _fail_if_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    from verification_coverage.independent import phase0

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("the run measured a case")

    monkeypatch.setattr(phase0, "standin_case", refuse)
    monkeypatch.setattr(phase0.standin, "build", refuse)


@pytest.mark.parametrize("contract", ["plus", "whole"])
def test_rows_without_a_context_are_not_reused_for_another_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contract: str
) -> None:
    from verification_coverage.independent import phase0

    _fail_if_measured(monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    rows = PRODUCTION_ROWS.read_bytes()
    (out / "rows.json").write_bytes(rows)
    baseline = tmp_path / "baseline.sql"
    baseline.write_text("-- synthetic\n")
    with pytest.raises(SystemExit) as refused:
        phase0.main(_phase0_args(out, baseline, contract))
    assert refused.value.code not in (0, None)
    assert (out / "rows.json").read_bytes() == rows
    assert sorted(p.name for p in out.iterdir()) == ["rows.json"]


def _measured_dir(tmp_path: Path, contract: str) -> tuple[Path, Path]:
    """A directory as a finished ``contract`` run leaves it (synthetic rows)."""

    from verification_coverage.independent import phase0

    suite = SUITE / "faults.yaml"
    cases = yaml.safe_load(suite.read_text())["cases"]
    baseline = tmp_path / "baseline.sql"
    baseline.write_text("-- synthetic\n")
    out = tmp_path / "out"
    out.mkdir()
    context = phase0.run_context("standin", contract, suite, baseline)
    (out / "context.json").write_text(json.dumps(context))
    rows = [
        {
            "id": c["id"],
            "class": c["class"],
            "expect": c["expect"],
            "contract": contract,
            "case_sha256": phase0.case_sha256(c),
            "verdict": {"outcome": "refuted", "verified": False, "score": "caught"},
        }
        for c in cases
    ]
    (out / "rows.json").write_text(json.dumps(rows))
    return out, baseline


def test_a_finished_run_resumes_only_under_its_own_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from verification_coverage.independent import phase0

    _fail_if_measured(monkeypatch)
    out, baseline = _measured_dir(tmp_path, "production")
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    assert phase0.main(_phase0_args(out, baseline, "production")) == 0
    assert "correct: 17/17" in capsys.readouterr().out
    for contract in ("plus", "whole"):
        with pytest.raises(SystemExit):
            phase0.main(_phase0_args(out, baseline, contract))
    baseline.write_text("-- another baseline\n")
    with pytest.raises(SystemExit):
        phase0.main(_phase0_args(out, baseline, "production"))
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_a_row_for_a_changed_case_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verification_coverage.independent import phase0

    _fail_if_measured(monkeypatch)
    out, baseline = _measured_dir(tmp_path, "production")
    rows = json.loads((out / "rows.json").read_text())
    rows[0]["case_sha256"] = "0" * 64
    (out / "rows.json").write_text(json.dumps(rows))
    with pytest.raises(SystemExit):
        phase0.main(_phase0_args(out, baseline, "production"))
