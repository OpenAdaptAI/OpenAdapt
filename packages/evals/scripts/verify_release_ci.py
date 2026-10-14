#!/usr/bin/env python3
"""Require a successful main-branch test workflow for an exact release SHA."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

GITHUB_API_VERSION = "2022-11-28"
TEST_WORKFLOW_NAME = "test"
TEST_WORKFLOW_PATH = ".github/workflows/test.yml"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

JSONFetcher = Callable[[str, Mapping[str, str]], Mapping[str, Any]]


class ReleaseCIError(RuntimeError):
    """The exact release commit does not have successful test evidence."""


class GitHubJSONFetcher:
    """Read one bounded GitHub API response and reject every query error."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ReleaseCIError("GH_TOKEN is required")
        self._token = token

    def __call__(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"https://api.github.com{endpoint}?{query}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "openadapt-evals-release-ci-gate",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status < 200 or response.status >= 300:
                    raise ReleaseCIError(f"GitHub API returned HTTP {response.status}")
                payload = json.load(response)
        except ReleaseCIError:
            raise
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            raise ReleaseCIError(f"GitHub API request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ReleaseCIError("GitHub API response is not an object")
        return payload


def require_successful_test_run(
    fetch_json: JSONFetcher,
    *,
    repository: str,
    sha: str,
) -> int:
    """Return the latest exact test run ID only when that run succeeded."""

    if not _REPOSITORY_RE.fullmatch(repository):
        raise ReleaseCIError(f"invalid GitHub repository: {repository!r}")
    if not _SHA_RE.fullmatch(sha):
        raise ReleaseCIError(f"invalid Git commit SHA: {sha!r}")

    payload = fetch_json(
        f"/repos/{repository}/actions/workflows/test.yml/runs",
        {
            "branch": "main",
            "event": "push",
            "head_sha": sha,
            "per_page": "100",
        },
    )
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise ReleaseCIError("GitHub API response has an invalid workflow_runs list")

    exact_runs = [
        run
        for run in runs
        if run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("name") == TEST_WORKFLOW_NAME
        and run.get("path") == TEST_WORKFLOW_PATH
    ]
    if not exact_runs:
        raise ReleaseCIError(f"no exact-SHA test workflow run exists for {sha}")

    try:
        latest = max(
            exact_runs,
            key=lambda run: (str(run.get("created_at", "")), int(run.get("id", 0))),
        )
    except (TypeError, ValueError) as exc:
        raise ReleaseCIError("the exact-SHA test workflow run has an invalid id") from exc

    run_id = latest.get("id")
    if not isinstance(run_id, int) or run_id <= 0:
        raise ReleaseCIError("the exact-SHA test workflow run has an invalid id")
    status = latest.get("status")
    conclusion = latest.get("conclusion")
    if status != "completed":
        raise ReleaseCIError(f"exact-SHA test workflow run {run_id} is {status!r}/{conclusion!r}")
    if conclusion != "success":
        raise ReleaseCIError(f"exact-SHA test workflow run {run_id} concluded {conclusion!r}")
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    try:
        run_id = require_successful_test_run(
            GitHubJSONFetcher(token),
            repository=args.repository,
            sha=args.sha,
        )
    except ReleaseCIError as exc:
        raise SystemExit(f"REFUSED: {exc}") from exc
    print(f"The exact-SHA test workflow succeeded in run {run_id}.")


if __name__ == "__main__":
    main()
