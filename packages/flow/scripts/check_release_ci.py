"""Fail-closed GitHub Actions qualification gate for Flow publication."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

EXPECTED_MATRIX_JOBS = frozenset(
    {
        "test-matrix (ubuntu-latest, 3.10)",
        "test-matrix (ubuntu-latest, 3.11)",
        "test-matrix (ubuntu-latest, 3.12)",
        "test-matrix (macos-latest, 3.12)",
    }
)
PER_PAGE = 100
MAX_PAGES = 100
GITHUB_API_VERSION = "2022-11-28"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

JSONFetcher = Callable[[str, Mapping[str, str]], Mapping[str, Any]]


class QualificationError(RuntimeError):
    """The qualification cannot safely authorize publication."""


class QualificationPending(QualificationError):
    """The exact-target qualification is absent or still running."""


@dataclass(frozen=True)
class Qualification:
    run_id: int
    sha: str
    job_names: frozenset[str]


class GitHubJSONFetcher:
    """Small authenticated GitHub REST reader with fail-closed decoding."""

    def __init__(self, token: str) -> None:
        if not token:
            raise QualificationError("GH_TOKEN is required")
        self._token = token

    def __call__(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"https://api.github.com{endpoint}?{query}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "openadapt-flow-release-gate",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status < 200 or response.status >= 300:
                    raise QualificationError(
                        f"GitHub API returned HTTP {response.status}"
                    )
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise QualificationError(f"GitHub API request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise QualificationError("GitHub API response is not an object")
        return payload


def _paginate(
    fetch_json: JSONFetcher,
    endpoint: str,
    item_key: str,
    params: Mapping[str, str],
) -> list[Mapping[str, Any]]:
    """Read every API page and reject incomplete or inconsistent pagination."""

    items: list[Mapping[str, Any]] = []
    expected_total: int | None = None
    for page in range(1, MAX_PAGES + 1):
        page_params = {
            **params,
            "per_page": str(PER_PAGE),
            "page": str(page),
        }
        payload = fetch_json(endpoint, page_params)
        total = payload.get("total_count")
        page_items = payload.get(item_key)
        if not isinstance(total, int) or total < 0:
            raise QualificationError("GitHub API response has invalid total_count")
        if not isinstance(page_items, list) or not all(
            isinstance(item, dict) for item in page_items
        ):
            raise QualificationError(
                f"GitHub API response has invalid {item_key!r} collection"
            )
        if expected_total is None:
            expected_total = total
            if expected_total > PER_PAGE * MAX_PAGES:
                raise QualificationError(
                    "GitHub API result exceeds the bounded pagination limit"
                )
        elif total != expected_total:
            raise QualificationError("GitHub API total_count changed during pagination")
        items.extend(page_items)
        if len(items) == expected_total:
            return items
        if len(items) > expected_total or not page_items:
            raise QualificationError("GitHub API pagination was incomplete")
    raise QualificationError("GitHub API pagination exceeded its page limit")


def require_exact_full_matrix(
    fetch_json: JSONFetcher,
    *,
    repository: str,
    sha: str,
) -> Qualification:
    """Require the latest exact-SHA dispatched CI run and its exact matrix."""

    if not _REPOSITORY_RE.fullmatch(repository):
        raise QualificationError(f"invalid GitHub repository: {repository!r}")
    if not _SHA_RE.fullmatch(sha):
        raise QualificationError(f"invalid Git commit SHA: {sha!r}")

    runs = _paginate(
        fetch_json,
        f"/repos/{repository}/actions/workflows/ci.yml/runs",
        "workflow_runs",
        {"head_sha": sha, "event": "workflow_dispatch"},
    )
    exact_runs = [
        run
        for run in runs
        if run.get("head_sha") == sha and run.get("event") == "workflow_dispatch"
    ]
    if not exact_runs:
        raise QualificationPending(
            f"no exact-SHA workflow_dispatch CI run exists for {sha}"
        )
    latest = max(
        exact_runs,
        key=lambda run: (str(run.get("created_at", "")), int(run.get("id", 0))),
    )
    run_id = latest.get("id")
    if not isinstance(run_id, int) or run_id <= 0:
        raise QualificationError("qualification run has an invalid id")
    status = latest.get("status")
    conclusion = latest.get("conclusion")
    if status != "completed":
        raise QualificationPending(
            f"exact-SHA qualification run {run_id} is {status!r}"
        )
    if conclusion != "success":
        raise QualificationError(
            f"exact-SHA qualification run {run_id} concluded {conclusion!r}"
        )

    jobs = _paginate(
        fetch_json,
        f"/repos/{repository}/actions/runs/{run_id}/jobs",
        "jobs",
        {"filter": "latest"},
    )
    matrix_jobs = [
        job
        for job in jobs
        if isinstance(job.get("name"), str)
        and str(job["name"]).startswith("test-matrix")
    ]
    counts = Counter(str(job.get("name")) for job in matrix_jobs)
    expected_counts = Counter({name: 1 for name in EXPECTED_MATRIX_JOBS})
    if counts != expected_counts:
        raise QualificationError(
            "exact-SHA qualification matrix job set/count mismatch: "
            f"expected={dict(sorted(expected_counts.items()))}, "
            f"observed={dict(sorted(counts.items()))}"
        )
    non_success = {
        str(job["name"]): job.get("conclusion")
        for job in matrix_jobs
        if job.get("conclusion") != "success"
    }
    if non_success:
        raise QualificationError(
            "exact-SHA qualification has non-success matrix jobs: "
            f"{dict(sorted(non_success.items()))}"
        )
    return Qualification(
        run_id=run_id,
        sha=sha,
        job_names=frozenset(counts),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Require an exact-SHA dispatched CI full-matrix qualification."
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--poll-seconds", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.wait_seconds < 0 or args.poll_seconds <= 0:
        print("wait-seconds must be >= 0 and poll-seconds must be > 0", file=sys.stderr)
        return 2
    try:
        fetch_json = GitHubJSONFetcher(os.environ.get("GH_TOKEN", ""))
    except QualificationError as exc:
        print(f"Refusing to publish: {exc}", file=sys.stderr)
        return 1

    deadline = time.monotonic() + args.wait_seconds
    while True:
        try:
            qualification = require_exact_full_matrix(
                fetch_json,
                repository=args.repository,
                sha=args.sha,
            )
        except QualificationPending as exc:
            if time.monotonic() >= deadline:
                print(f"Refusing to publish: {exc}", file=sys.stderr)
                return 1
            print(f"Waiting for release qualification: {exc}")
            time.sleep(min(args.poll_seconds, max(0.0, deadline - time.monotonic())))
            continue
        except QualificationError as exc:
            print(f"Refusing to publish: {exc}", file=sys.stderr)
            return 1
        print(
            "Release qualification passed: "
            f"sha={qualification.sha} run_id={qualification.run_id} "
            f"matrix_jobs={len(qualification.job_names)}"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
