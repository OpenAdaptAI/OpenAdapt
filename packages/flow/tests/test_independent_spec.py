"""The independent verification spec and its fault suite stay consistent.

``benchmark/verification_coverage/independent/SPEC.md`` and ``faults.yaml``
were written before any run and without reading OpenAdapt's generated checks,
so that coverage is measured against them rather than tuned to them. This
pins their shape: every case names spec clauses that exist and a verdict.
"""

from __future__ import annotations

from pathlib import Path

import yaml

SUITE = (
    Path(__file__).resolve().parents[1] / "benchmark/verification_coverage/independent"
)


def test_every_case_names_its_spec_clauses_and_a_verdict() -> None:
    suite = yaml.safe_load((SUITE / "faults.yaml").read_text())
    spec = (SUITE / "SPEC.md").read_text()
    ids = [c["id"] for c in suite["cases"]]
    assert len(ids) == len(set(ids)) == 17
    for case in suite["cases"]:
        assert case["expect"] in ("fault", "clean")
        for clause in case["clauses"]:
            assert f"**{clause}." in spec, clause
        for step in case["steps"]:
            assert step["actor"] in ("bot", "clinician2", "none")
    assert {c["class"] for c in suite["cases"]} >= {
        "wrong_record",
        "unrelated_change",
        "misread_source",
        "missing_side_record",
        "concurrent_legitimate",
        "duplicate",
    }
    assert all(
        c["expect"] == "clean"
        for c in suite["cases"]
        if c["class"] == "concurrent_legitimate"
    )
