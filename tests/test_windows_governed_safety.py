"""The gating safety check after the non-gating Windows governed run.

scripts/ci/check_windows_governed_safety.py reads the evidence the governed
test writes and fails the job on any unsafe write. These synthetic evidence
files cover each case; only a halt before the effect with nothing written is
tolerated.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "check_windows_governed_safety.py"
NOTE = "BP 128/82; follow-up in 2 weeks"


def _script():
    spec = importlib.util.spec_from_file_location(
        "check_windows_governed_safety", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _script()


SEED = {
    1: ("Ada", "Park", "1980-01-02"),
    2: ("Ben", "Ruiz", "1975-05-06"),
    3: ("Cy", "Lee", "1990-09-10"),
}


def _rows(written: dict[int, str] | None = None) -> list[dict]:
    written = written or {}
    return [
        {"id": i, "first": f, "last": la, "dob": d, "note": written.get(i, "")}
        for i, (f, la, d) in SEED.items()
    ]


def _passing() -> dict:
    return {
        "target_id": 1,
        "expected_note": NOTE,
        "phases": ["approved", "held", "resumed"],
        "seed_rows": _rows(),
        "approved": {
            "exit": 0,
            "rows": _rows({1: NOTE}),
            "outcome": "VERIFIED",
            "approval_decision": "approved",
        },
        "held": {
            "exit": 1,
            "rows": _rows(),
            "outcome": "HALTED_BEFORE_EFFECT",
            "approval_decision": "approval_required",
            "answer": [{"decision": "approve", "resume_required": True}],
        },
        "resumed": {
            "exit": 0,
            "rows": _rows({1: NOTE}),
            "outcome": "VERIFIED",
            "approval_decision": "approved",
        },
    }


def _run(tmp_path: Path, evidence) -> int:
    path = tmp_path / "windows-governed-evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return check.main([str(path)])


def test_the_full_governed_run_passes(tmp_path) -> None:
    assert check.check(_passing()) == []
    assert _run(tmp_path, _passing()) == 0


def test_a_resume_halted_before_the_save_with_nothing_written_passes() -> None:
    # The #2189 false stop: the resume stops before the save; nothing lands.
    evidence = _passing()
    evidence["resumed"].update(
        exit=1,
        rows=_rows(),
        outcome="HALTED_BEFORE_EFFECT",
        approval_decision="problem",
    )
    assert check.check(evidence) == []


def test_an_approved_run_halted_before_the_save_passes() -> None:
    evidence = _passing()
    evidence["phases"] = ["approved"]
    evidence["approved"].update(
        exit=1,
        rows=_rows(),
        outcome="HALTED_BEFORE_EFFECT",
        approval_decision="problem",
    )
    assert check.check(evidence) == []


def test_a_run_that_stopped_before_any_phase_passes() -> None:
    evidence = _passing()
    evidence["phases"] = []
    assert check.check(evidence) == []


@pytest.mark.parametrize("phase", ["approved", "resumed"])
def test_a_sibling_write_fails(phase) -> None:
    evidence = _passing()
    evidence[phase]["rows"] = _rows({1: NOTE, 2: NOTE})
    assert any("non-target rows [2]" in p for p in check.check(evidence))


@pytest.mark.parametrize("phase", ["approved", "resumed"])
def test_a_wrong_record_write_fails_even_when_halted(phase) -> None:
    evidence = _passing()
    evidence[phase].update(rows=_rows({3: NOTE}), outcome="HALTED_BEFORE_EFFECT")
    assert any("non-target rows [3]" in p for p in check.check(evidence))


def test_a_held_run_that_wrote_fails(tmp_path) -> None:
    evidence = _passing()
    evidence["held"]["rows"] = _rows({1: NOTE})
    assert "held: the held run wrote" in check.check(evidence)
    assert _run(tmp_path, evidence) == 1


def test_a_held_run_reporting_verified_fails() -> None:
    evidence = _passing()
    evidence["held"]["outcome"] = "VERIFIED"
    assert check.check(evidence)


@pytest.mark.parametrize(
    "outcome",
    ["HALTED_BEFORE_EFFECT", "RECONCILIATION_REQUIRED", "COMPLETED_UNVERIFIED", None],
)
@pytest.mark.parametrize("phase", ["approved", "resumed"])
def test_a_target_write_without_a_verified_report_fails(phase, outcome) -> None:
    evidence = _passing()
    evidence[phase]["outcome"] = outcome
    assert any("the target was written but" in p for p in check.check(evidence))


def test_a_target_write_without_an_approval_fails() -> None:
    evidence = _passing()
    evidence["approved"]["approval_decision"] = "no_person"
    assert "approved: the target was written without an approval" in check.check(
        evidence
    )


def test_a_resume_without_any_approval_fails() -> None:
    evidence = _passing()
    evidence["resumed"]["approval_decision"] = None
    evidence["held"]["answer"] = []
    assert "resumed: the target was written without an approval" in check.check(
        evidence
    )


def test_a_resume_approved_by_the_held_answer_passes() -> None:
    evidence = _passing()
    evidence["resumed"]["approval_decision"] = None
    assert check.check(evidence) == []


def test_a_target_write_with_an_unexpected_note_fails() -> None:
    evidence = _passing()
    evidence["approved"]["rows"] = _rows({1: "something else"})
    assert "approved: the target holds an unexpected note" in check.check(evidence)


def test_verified_with_nothing_written_fails() -> None:
    evidence = _passing()
    evidence["resumed"]["rows"] = _rows()
    assert "resumed: reported VERIFIED but nothing was written" in check.check(evidence)


@pytest.mark.parametrize("phase", ["approved", "held", "resumed"])
def test_a_started_phase_without_rows_fails(phase) -> None:
    evidence = _passing()
    del evidence[phase]["rows"]
    assert any("no record rows" in p for p in check.check(evidence))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.pop("target_id"),
        lambda e: e.pop("phases"),
        lambda e: e.update(phases=["approved", "replayed"]),
    ],
)
def test_malformed_evidence_fails(mutate) -> None:
    evidence = copy.deepcopy(_passing())
    mutate(evidence)
    assert check.check(evidence)


def test_a_missing_or_unreadable_evidence_file_fails(tmp_path) -> None:
    assert check.main([str(tmp_path / "absent.json")]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert check.main([str(bad)]) == 1
    assert check.check([]) != []


def test_the_workflow_gates_on_the_check_after_the_governed_step() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "desktop-replay.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["replay"]["steps"]
    ids = [s.get("id") for s in steps]
    governed = steps[ids.index("governed")]
    assert governed.get("continue-on-error") is True
    gate = steps[ids.index("governed") + 1]
    assert gate.get("if") == "always()"
    assert "continue-on-error" not in gate
    assert "scripts/ci/check_windows_governed_safety.py" in gate["run"]


@pytest.mark.parametrize("column", ["first", "last", "dob"])
def test_a_wrong_field_write_fails(column) -> None:
    evidence = _passing()
    evidence["approved"]["rows"][1][column] = "changed"
    assert f"approved: row 2 changed ['{column}']" in check.check(evidence)


def test_a_deleted_row_fails() -> None:
    evidence = _passing()
    evidence["resumed"]["rows"] = [
        r for r in evidence["resumed"]["rows"] if r["id"] != 3
    ]
    assert "resumed: seeded rows [3] are gone" in check.check(evidence)


def test_an_added_row_fails() -> None:
    evidence = _passing()
    extra = {"id": 4, "first": "Dee", "last": "Ko", "dob": "2000-01-01", "note": ""}
    evidence["held"]["rows"].append(extra)
    assert "held: rows [4] are not in the seed" in check.check(evidence)


def test_missing_seed_rows_fail() -> None:
    evidence = _passing()
    del evidence["seed_rows"]
    assert any("no seed rows" in p for p in check.check(evidence))


def test_no_phase_ran_is_said(tmp_path, capsys) -> None:
    evidence = _passing()
    evidence["phases"] = []
    assert _run(tmp_path, evidence) == 0
    out = capsys.readouterr().out
    assert "governed safety: no phase ran" in out
    assert "no unsafe write" not in out


def test_coverage_reports_a_halt_without_gating(tmp_path, monkeypatch) -> None:
    evidence = _passing()
    evidence["phases"] = ["approved"]
    evidence["approved"].update(
        exit=1,
        rows=_rows(),
        outcome="HALTED_BEFORE_EFFECT",
        approval_decision="problem",
        halts=[
            {"step": "save", "category": "form_changed_after_approval", "error": ""}
        ],
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert _run(tmp_path, evidence) == 0
    line = summary.read_text(encoding="utf-8").strip()
    assert line == (
        "governed coverage: approved: HALTED_BEFORE_EFFECT form_changed_after_approval;"
        " held: not run; resumed: not run"
    )
    kept = json.loads((tmp_path / "windows-governed-evidence.json").read_text())
    assert kept["coverage"]["phases"] == ["approved"]
    assert kept["coverage"]["held_ran"] is False
    assert kept["coverage"]["resumed_ran"] is False
    assert kept["coverage"]["outcomes"]["approved"]["halt"] == (
        "form_changed_after_approval"
    )
