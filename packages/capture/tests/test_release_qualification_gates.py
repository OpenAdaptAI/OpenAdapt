"""Tests for the fail-closed production qualification and release gates."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.candidate_lifecycle import (
    CONSUMER_RELEASES,
    CandidateLifecycleError,
    _wheel_requirement,
    verify_manifest,
)
from scripts.check_display_topology import DisplayTopologyError, qualify_topology
from scripts.check_junit_no_skips import JUnitQualificationError, check_reports
from scripts.check_release_ci import (
    EXPECTED_LIVE_QUALIFICATION_JOBS,
    EXPECTED_QUALIFICATION_JOBS,
    ReleaseEvidenceError,
    check_once,
    dispatch_missing_qualifications,
    validate_live_linux_jobs,
    validate_qualification_jobs,
    validate_test_jobs,
)


def _write_manifest(dist: Path, archives: list[Path]) -> Path:
    manifest = dist / "SHA256SUMS"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in archives
        ),
        encoding="utf-8",
    )
    return manifest


def test_candidate_manifest_accounts_for_exact_wheel_and_sdist(tmp_path: Path) -> None:
    wheel = tmp_path / "openadapt_capture-1.2.2-py3-none-any.whl"
    sdist = tmp_path / "openadapt_capture-1.2.2.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    manifest = _write_manifest(tmp_path, [wheel, sdist])

    assert set(verify_manifest(tmp_path, manifest)) == {wheel.name, sdist.name}


def test_candidate_manifest_rejects_unaccounted_archive(tmp_path: Path) -> None:
    wheel = tmp_path / "openadapt_capture-1.2.2-py3-none-any.whl"
    sdist = tmp_path / "openadapt_capture-1.2.2.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    manifest = _write_manifest(tmp_path, [wheel])

    with pytest.raises(CandidateLifecycleError, match="manifest and candidate archives differ"):
        verify_manifest(tmp_path, manifest)


def test_linux_extra_is_installed_from_the_exact_candidate_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "openadapt_capture-1.3.0-py3-none-any.whl"

    assert _wheel_requirement(wheel, with_linux_extra=True) == f"{wheel.resolve()}[linux]"
    assert _wheel_requirement(wheel, with_linux_extra=False) == str(wheel.resolve())


def test_candidate_contract_names_the_current_flow_and_desktop_releases() -> None:
    assert CONSUMER_RELEASES == {
        "openadapt-desktop": "0.16.0",
        "openadapt-flow": "1.34.0",
    }


def test_display_topology_requires_stable_multiple_monitors() -> None:
    snapshot = {
        "coordinate_space": "virtual_desktop_pixels",
        "origin": [-1920, 0],
        "viewport": [4480, 1440],
        "monitor_count": 2,
        "monitors": [[-1920, 0, 1920, 1080], [0, 0, 2560, 1440]],
    }

    evidence = qualify_topology(
        lambda: snapshot,
        minimum_monitors=2,
        samples=3,
        interval_seconds=0,
    )

    assert evidence["stable"] is True
    assert evidence["topology"] == snapshot


def test_display_topology_rejects_change_during_qualification() -> None:
    snapshots = iter(
        [
            {
                "coordinate_space": "virtual_desktop_pixels",
                "monitor_count": 2,
                "monitors": [[0, 0, 10, 10], [10, 0, 10, 10]],
            },
            {
                "coordinate_space": "virtual_desktop_pixels",
                "monitor_count": 2,
                "monitors": [[0, 0, 10, 10], [10, 0, 20, 10]],
            },
        ]
    )

    with pytest.raises(DisplayTopologyError, match="changed during qualification"):
        qualify_topology(
            lambda: next(snapshots),
            minimum_monitors=2,
            samples=2,
            interval_seconds=0,
        )


def test_junit_gate_rejects_a_skipped_test(tmp_path: Path) -> None:
    report = tmp_path / "qualification.xml"
    report.write_text(
        "<testsuites><testsuite><testcase name='ok'/><testcase name='no'>"
        "<skipped message='no display'/></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    with pytest.raises(JUnitQualificationError, match="skipped 1 of 2"):
        check_reports([report])


def test_junit_gate_accepts_complete_tests(tmp_path: Path) -> None:
    report = tmp_path / "qualification.xml"
    report.write_text(
        "<testsuites><testsuite><testcase name='one'/><testcase name='two'/>"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    assert check_reports([report]) == {
        "tests": 2,
        "skipped": 0,
        "failures": 0,
        "errors": 0,
    }


def _successful_jobs() -> list[dict[str, str]]:
    return [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in sorted(EXPECTED_QUALIFICATION_JOBS)
    ]


def _successful_live_linux_jobs() -> list[dict[str, str]]:
    required = {
        "Select live qualification platforms",
        "Build candidate distributions",
        "Interactive qualification (Linux X64)",
    }
    return [
        {
            "name": name,
            "status": "completed",
            "conclusion": "success" if name in required else "skipped",
        }
        for name in sorted(EXPECTED_LIVE_QUALIFICATION_JOBS)
    ]


def test_release_gate_rejects_missing_qualification_job() -> None:
    jobs = _successful_jobs()[:-1]

    with pytest.raises(ReleaseEvidenceError, match="job set differs"):
        validate_qualification_jobs(jobs)


def test_release_gate_rejects_skipped_qualification_job() -> None:
    jobs = _successful_jobs()
    jobs[0] = {**jobs[0], "conclusion": "skipped"}

    with pytest.raises(ReleaseEvidenceError, match="incomplete jobs"):
        validate_qualification_jobs(jobs)


def test_release_gate_requires_successful_package_contract() -> None:
    with pytest.raises(ReleaseEvidenceError, match="one package-contract"):
        validate_test_jobs([])

    with pytest.raises(ReleaseEvidenceError, match="package-contract is incomplete"):
        validate_test_jobs(
            [{"name": "package-contract", "status": "completed", "conclusion": "failure"}]
        )

    validate_test_jobs(
        [{"name": "package-contract", "status": "completed", "conclusion": "success"}]
    )


def test_release_gate_requires_successful_live_linux_qualification() -> None:
    jobs = _successful_live_linux_jobs()
    validate_live_linux_jobs(jobs)

    linux = next(job for job in jobs if job["name"] == "Interactive qualification (Linux X64)")
    linux["conclusion"] = "skipped"
    with pytest.raises(ReleaseEvidenceError, match="live Linux qualification"):
        validate_live_linux_jobs(jobs)


def test_release_gate_binds_both_workflows_and_jobs_to_exact_sha() -> None:
    sha = "a" * 40

    def get_json(url: str, _token: str):
        if "/test.yml/runs?" in url:
            return {
                "workflow_runs": [
                    {
                        "id": 10,
                        "head_sha": sha,
                        "event": "push",
                        "created_at": "2026-08-18T00:00:00Z",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        if "/production-qualification.yml/runs?" in url:
            return {
                "workflow_runs": [
                    {
                        "id": 20,
                        "head_sha": sha,
                        "event": "workflow_dispatch",
                        "created_at": "2026-08-18T00:01:00Z",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        if "/live-qualification.yml/runs?" in url:
            return {
                "workflow_runs": [
                    {
                        "id": 30,
                        "head_sha": sha,
                        "event": "workflow_dispatch",
                        "created_at": "2026-08-18T00:02:00Z",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        if "/actions/runs/20/jobs?" in url:
            return {"jobs": _successful_jobs()}
        if "/actions/runs/10/jobs?" in url:
            return {
                "jobs": [
                    {
                        "name": "package-contract",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        if "/actions/runs/30/jobs?" in url:
            return {"jobs": _successful_live_linux_jobs()}
        raise AssertionError(f"unexpected URL {url}")

    assert check_once(
        repository="OpenAdaptAI/openadapt-capture",
        sha=sha,
        token="test",
        get_json=get_json,
    ) == {
        "test.yml": 10,
        "production-qualification.yml": 20,
        "live-qualification.yml": 30,
    }


def test_release_orchestrator_starts_only_missing_exact_sha_workflows() -> None:
    sha = "a" * 40
    calls: list[tuple[str, str, str, str]] = []

    def get_json(url: str, _token: str):
        if "/test.yml/runs?" in url:
            return {"workflow_runs": [{"head_sha": sha, "event": "push", "id": 10}]}
        if "/production-qualification.yml/runs?" in url:
            return {"workflow_runs": []}
        if "/live-qualification.yml/runs?" in url:
            return {"workflow_runs": []}
        raise AssertionError(f"unexpected URL {url}")

    def dispatch(repository, requirement, *, ref, sha, token):  # type: ignore[no-untyped-def]
        calls.append((repository, requirement.file_name, ref, sha))
        assert token == "token"
        expected_inputs = {"candidate_sha": "{sha}"}
        if requirement.file_name == "live-qualification.yml":
            expected_inputs["platform"] = "linux"
        assert requirement.dispatch_inputs == expected_inputs

    started = dispatch_missing_qualifications(
        repository="OpenAdaptAI/openadapt-capture",
        sha=sha,
        ref="main",
        token="token",
        get_json=get_json,
        dispatch=dispatch,
    )

    assert started == ("production-qualification.yml", "live-qualification.yml")
    assert calls == [
        ("OpenAdaptAI/openadapt-capture", "production-qualification.yml", "main", sha),
        ("OpenAdaptAI/openadapt-capture", "live-qualification.yml", "main", sha),
    ]


def test_release_orchestrator_does_not_accept_a_non_linux_live_run() -> None:
    sha = "a" * 40
    dispatched: list[str] = []

    def get_json(url: str, _token: str):
        if "/test.yml/runs?" in url or "/production-qualification.yml/runs?" in url:
            return {"workflow_runs": [{"head_sha": sha, "event": "workflow_dispatch", "id": 10}]}
        if "/live-qualification.yml/runs?" in url:
            return {"workflow_runs": [{"head_sha": sha, "event": "workflow_dispatch", "id": 30}]}
        if "/actions/runs/30/jobs?" in url:
            jobs = _successful_live_linux_jobs()
            next(job for job in jobs if job["name"] == "Interactive qualification (Linux X64)")[
                "conclusion"
            ] = "skipped"
            return {"jobs": jobs}
        raise AssertionError(f"unexpected URL {url}")

    dispatch_missing_qualifications(
        repository="OpenAdaptAI/openadapt-capture",
        sha=sha,
        ref="main",
        token="token",
        get_json=get_json,
        dispatch=lambda _repository, requirement, **_kwargs: dispatched.append(
            requirement.file_name
        ),
    )

    assert dispatched == ["live-qualification.yml"]
