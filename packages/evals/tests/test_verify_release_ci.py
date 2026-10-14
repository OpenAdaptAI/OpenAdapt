"""Tests for the exact-SHA release CI gate."""

from __future__ import annotations

import urllib.error
from collections.abc import Mapping
from typing import Any

import pytest

from scripts.verify_release_ci import (
    GitHubJSONFetcher,
    ReleaseCIError,
    require_successful_test_run,
)

REPOSITORY = "OpenAdaptAI/openadapt-evals"
SHA = "a" * 40
RUN_ID = 12345


def _run(
    *,
    sha: str = SHA,
    status: str = "completed",
    conclusion: str | None = "success",
    run_id: int = RUN_ID,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": "test",
        "path": ".github/workflows/test.yml",
        "head_branch": "main",
        "head_sha": sha,
        "event": "push",
        "status": status,
        "conclusion": conclusion,
        "created_at": "2026-09-03T00:00:00Z",
    }


class FakeGitHub:
    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.runs = runs
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        self.calls.append((endpoint, dict(params)))
        return {"workflow_runs": self.runs}


def _require(fake: FakeGitHub) -> int:
    return require_successful_test_run(fake, repository=REPOSITORY, sha=SHA)


def test_accepts_latest_successful_exact_sha_test_run() -> None:
    older = {**_run(run_id=100), "created_at": "2026-09-02T00:00:00Z"}
    fake = FakeGitHub([older, _run()])

    assert _require(fake) == RUN_ID
    assert fake.calls == [
        (
            "/repos/OpenAdaptAI/openadapt-evals/actions/workflows/test.yml/runs",
            {
                "branch": "main",
                "event": "push",
                "head_sha": SHA,
                "per_page": "100",
            },
        )
    ]


def test_rejects_missing_test_run() -> None:
    with pytest.raises(ReleaseCIError, match="no exact-SHA"):
        _require(FakeGitHub([]))


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending", "requested"])
def test_rejects_pending_test_run(status: str) -> None:
    with pytest.raises(ReleaseCIError, match=f"is '{status}'/None"):
        _require(FakeGitHub([_run(status=status, conclusion=None)]))


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_rejects_failed_test_run(conclusion: str) -> None:
    with pytest.raises(ReleaseCIError, match=f"concluded '{conclusion}'"):
        _require(FakeGitHub([_run(conclusion=conclusion)]))


def test_rejects_sha_mismatched_run_even_if_successful() -> None:
    with pytest.raises(ReleaseCIError, match="no exact-SHA"):
        _require(FakeGitHub([_run(sha="b" * 40)]))


def test_rejects_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_request(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError("simulated API error")

    monkeypatch.setattr("scripts.verify_release_ci.urllib.request.urlopen", fail_request)

    with pytest.raises(ReleaseCIError, match="GitHub API request failed"):
        require_successful_test_run(GitHubJSONFetcher("token"), repository=REPOSITORY, sha=SHA)
