"""Behavioral tests for the fail-closed publication qualification gate."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from scripts.check_release_ci import (
    EXPECTED_MATRIX_JOBS,
    QualificationError,
    QualificationPending,
    require_exact_full_matrix,
)

REPOSITORY = "OpenAdaptAI/openadapt-flow"
SHA = "a" * 40
RUN_ID = 12345


def _run(
    *,
    sha: str = SHA,
    event: str = "workflow_dispatch",
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, Any]:
    return {
        "id": RUN_ID,
        "head_sha": sha,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": "2026-07-23T00:00:00Z",
    }


def _matrix_jobs(*, conclusion: str = "success") -> list[dict[str, Any]]:
    return [
        {"name": name, "conclusion": conclusion}
        for name in sorted(EXPECTED_MATRIX_JOBS)
    ]


class FakeGitHub:
    def __init__(
        self,
        *,
        runs: list[dict[str, Any]] | None = None,
        jobs: list[dict[str, Any]] | None = None,
        page_size: int = 100,
        error_endpoint: str | None = None,
    ) -> None:
        self.runs = runs if runs is not None else [_run()]
        self.jobs = jobs if jobs is not None else _matrix_jobs()
        self.page_size = page_size
        self.error_endpoint = error_endpoint
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        params_copy = dict(params)
        self.calls.append((endpoint, params_copy))
        if self.error_endpoint and self.error_endpoint in endpoint:
            raise QualificationError("simulated API error")
        source = self.runs if endpoint.endswith("/runs") else self.jobs
        key = "workflow_runs" if endpoint.endswith("/runs") else "jobs"
        page = int(params_copy["page"])
        start = (page - 1) * self.page_size
        end = start + self.page_size
        return {"total_count": len(source), key: source[start:end]}


def _require(fake: FakeGitHub):
    return require_exact_full_matrix(fake, repository=REPOSITORY, sha=SHA)


def test_accepts_exact_dispatched_run_with_exact_successful_matrix() -> None:
    result = _require(FakeGitHub())

    assert result.run_id == RUN_ID
    assert result.sha == SHA
    assert result.job_names == EXPECTED_MATRIX_JOBS


def test_rejects_skipped_matrix_job() -> None:
    jobs = _matrix_jobs()
    jobs[0]["conclusion"] = "skipped"

    with pytest.raises(QualificationError, match="non-success matrix jobs"):
        _require(FakeGitHub(jobs=jobs))


def test_rejects_partial_matrix() -> None:
    with pytest.raises(QualificationError, match="job set/count mismatch"):
        _require(FakeGitHub(jobs=_matrix_jobs()[:-1]))


def test_rejects_duplicate_plus_missing_matrix_even_when_count_is_four() -> None:
    jobs = _matrix_jobs()
    jobs[-1] = dict(jobs[0])

    assert len(jobs) == 4
    with pytest.raises(QualificationError, match="job set/count mismatch"):
        _require(FakeGitHub(jobs=jobs))


def test_rejects_unexpected_matrix_job() -> None:
    jobs = _matrix_jobs() + [
        {"name": "test-matrix (ubuntu-latest, 3.13)", "conclusion": "success"}
    ]

    with pytest.raises(QualificationError, match="job set/count mismatch"):
        _require(FakeGitHub(jobs=jobs))


def test_rejects_wrong_sha() -> None:
    with pytest.raises(QualificationPending, match="no exact-SHA"):
        _require(FakeGitHub(runs=[_run(sha="b" * 40)]))


def test_rejects_wrong_event() -> None:
    with pytest.raises(QualificationPending, match="no exact-SHA"):
        _require(FakeGitHub(runs=[_run(event="push")]))


@pytest.mark.parametrize("endpoint", ["/runs", "/jobs"])
def test_rejects_api_error(endpoint: str) -> None:
    with pytest.raises(QualificationError, match="simulated API error"):
        _require(FakeGitHub(error_endpoint=endpoint))


def test_follows_job_pagination_beyond_first_hundred() -> None:
    filler = [
        {"name": f"unrelated-{index:03d}", "conclusion": "success"}
        for index in range(100)
    ]
    fake = FakeGitHub(jobs=filler + _matrix_jobs(), page_size=100)

    result = _require(fake)

    assert result.job_names == EXPECTED_MATRIX_JOBS
    job_calls = [call for call in fake.calls if call[0].endswith("/jobs")]
    assert [params["page"] for _, params in job_calls] == ["1", "2"]
    assert all(params["filter"] == "latest" for _, params in job_calls)


def test_follows_run_pagination_beyond_first_hundred() -> None:
    unrelated = [
        {
            **_run(sha="b" * 40),
            "id": index + 1,
            "created_at": f"2026-07-22T00:{index % 60:02d}:00Z",
        }
        for index in range(100)
    ]
    fake = FakeGitHub(runs=unrelated + [_run()], page_size=100)

    result = _require(fake)

    assert result.run_id == RUN_ID
    run_calls = [call for call in fake.calls if call[0].endswith("/runs")]
    assert [params["page"] for _, params in run_calls] == ["1", "2"]
    assert all(params["event"] == "workflow_dispatch" for _, params in run_calls)
    assert all(params["head_sha"] == SHA for _, params in run_calls)


def test_rejects_incomplete_pagination() -> None:
    class IncompleteGitHub(FakeGitHub):
        def __call__(
            self, endpoint: str, params: Mapping[str, str]
        ) -> Mapping[str, Any]:
            if endpoint.endswith("/jobs"):
                return {"total_count": 101, "jobs": []}
            return super().__call__(endpoint, params)

    with pytest.raises(QualificationError, match="pagination was incomplete"):
        _require(IncompleteGitHub())
