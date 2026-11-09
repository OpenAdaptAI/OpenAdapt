"""Contracts for the counted per-OS qualification trial evidence.

The release campaign requires three complete live trials per operating
system against one exact candidate, with explicit silent-incorrect-success
and over-halt accounting. These tests pin the fail-closed aggregator that
turns trial JUnit XML into that evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.aggregate_qualification_trials import (
    QualificationEvidenceError,
    aggregate,
)


def _junit(
    path: Path,
    *,
    tests: int = 6,
    failures: int = 0,
    errors: int = 0,
    skipped: int = 0,
) -> Path:
    path.write_text(
        "<?xml version='1.0' encoding='utf-8'?>"
        "<testsuites><testsuite name='pytest' errors='%d' failures='%d' "
        "skipped='%d' tests='%d' time='1.0'></testsuite></testsuites>"
        % (errors, failures, skipped, tests),
        encoding="utf-8",
    )
    return path


def _three_clean(tmp_path: Path) -> list[Path]:
    return [
        _junit(tmp_path / f"trial-{trial}.xml") for trial in (1, 2, 3)
    ]


def test_three_clean_trials_pass_with_zero_counts(tmp_path: Path) -> None:
    summary = aggregate(_three_clean(tmp_path), os_name="linux")

    assert summary["passed"] is True
    assert summary["trials_run"] == 3
    assert summary["totals"]["tests"] == 18
    assert summary["silent_incorrect_success"] == 0
    assert summary["over_halt"] == 0
    assert summary["violations"] == []


def test_missing_trial_file_fails_closed(tmp_path: Path) -> None:
    _junit(tmp_path / "trial-1.xml")
    _junit(tmp_path / "trial-2.xml")
    missing = tmp_path / "trial-3.xml"

    with pytest.raises(QualificationEvidenceError, match="missing trial evidence"):
        aggregate([tmp_path / "trial-1.xml", tmp_path / "trial-2.xml", missing], os_name="macos")


def test_fewer_than_required_trials_fails_closed(tmp_path: Path) -> None:
    two_trials = _three_clean(tmp_path)[:2]
    with pytest.raises(QualificationEvidenceError, match="expected 3 trials, got 2"):
        aggregate(two_trials, os_name="linux")


def test_duplicate_trial_files_fail_closed(tmp_path: Path) -> None:
    only = _junit(tmp_path / "trial-1.xml")
    with pytest.raises(QualificationEvidenceError, match="duplicate trial files"):
        aggregate([only, only], os_name="linux")


def test_failed_trial_is_a_silent_incorrect_success_and_an_over_halt(
    tmp_path: Path,
) -> None:
    files = _three_clean(tmp_path)
    _junit(files[0], tests=6, failures=2)

    summary = aggregate(files, os_name="windows")

    assert summary["passed"] is False
    assert summary["over_halt"] == 2
    assert summary["silent_incorrect_success"] == 1
    assert any("unjustified halt" in violation for violation in summary["violations"])


def test_skipped_trial_violates_even_without_failures(tmp_path: Path) -> None:
    files = _three_clean(tmp_path)
    _junit(files[1], tests=6, skipped=1)

    summary = aggregate(files, os_name="linux")

    assert summary["passed"] is False
    assert summary["totals"]["skipped"] == 1
    assert summary["silent_incorrect_success"] == 1


def test_unparsable_xml_fails_closed(tmp_path: Path) -> None:
    broken = tmp_path / "trial-1.xml"
    broken.write_text("<testsuites><testsuite>", encoding="utf-8")
    clean = [_junit(tmp_path / f"trial-{trial}.xml") for trial in (2, 3)]

    with pytest.raises(QualificationEvidenceError, match="unreadable JUnit XML"):
        aggregate([broken, *clean], os_name="linux")


def test_suite_without_counts_fails_closed(tmp_path: Path) -> None:
    bare = tmp_path / "trial-1.xml"
    bare.write_text(
        "<testsuites><testsuite name='pytest'><testcase name='one'/></testsuite></testsuites>",
        encoding="utf-8",
    )
    clean = [_junit(tmp_path / f"trial-{trial}.xml") for trial in (2, 3)]

    with pytest.raises(QualificationEvidenceError, match="missing 'tests'"):
        aggregate([bare, *clean], os_name="linux")


def test_trial_running_zero_tests_fails_closed(tmp_path: Path) -> None:
    empty = _junit(tmp_path / "trial-1.xml", tests=0)
    clean = [_junit(tmp_path / f"trial-{trial}.xml") for trial in (2, 3)]

    with pytest.raises(QualificationEvidenceError, match="ran zero tests"):
        aggregate([empty, *clean], os_name="linux")


def test_summary_records_the_candidate_sha(tmp_path: Path) -> None:
    sha = "a" * 40

    summary = aggregate(_three_clean(tmp_path), os_name="linux", candidate_sha=sha)

    assert summary["candidate_sha"] == sha
