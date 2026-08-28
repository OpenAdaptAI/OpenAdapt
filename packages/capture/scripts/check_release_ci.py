#!/usr/bin/env python3
"""Require exact-commit test and production-qualification evidence."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

# The exact job set production-qualification.yml produces. Every job runs on a
# GitHub-hosted runner. The three self-hosted interactive lanes that used to be
# in this set moved to live-qualification.yml on 2026-08-28: no runner carrying
# their label was ever registered, so they were cancelled in every dispatch and
# this gate could never pass. The header of production-qualification.yml names
# what the release gate no longer proves as a result.
EXPECTED_QUALIFICATION_JOBS = frozenset(
    {
        "Build candidate distributions",
        "Clean candidate wheel (ubuntu-latest)",
        "Clean candidate wheel (macos-latest)",
        "Clean candidate wheel (windows-latest)",
        "Hosted live recorder qualification (macos-latest)",
        "Hosted live recorder qualification (windows-latest)",
    }
)
ACTIVE_STATES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})


class ReleaseEvidenceError(RuntimeError):
    """The exact release candidate does not have complete successful evidence."""


class EvidencePending(RuntimeError):
    """The exact release candidate can still obtain the required evidence."""


@dataclass(frozen=True)
class WorkflowRequirement:
    file_name: str
    event: str


REQUIREMENTS = (
    WorkflowRequirement("test.yml", "push"),
    WorkflowRequirement("production-qualification.yml", "workflow_dispatch"),
)


def _github_get(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "openadapt-capture-release-evidence/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(4 * 1024 * 1024))
    except (urllib.error.URLError, OSError, ValueError, TypeError) as exc:
        raise EvidencePending(f"GitHub evidence query failed: {exc}") from exc


def select_exact_run(
    runs: list[dict[str, Any]], *, sha: str, event: str
) -> dict[str, Any]:
    exact = [
        run
        for run in runs
        if run.get("head_sha") == sha and run.get("event") == event
    ]
    if not exact:
        raise EvidencePending(f"no {event} workflow run exists for exact commit {sha}")
    exact.sort(key=lambda run: (run.get("created_at") or "", int(run.get("id") or 0)))
    run = exact[-1]
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status in ACTIVE_STATES or status != "completed":
        raise EvidencePending(
            f"workflow run {run.get('id')} for {sha} is {status}/{conclusion}"
        )
    if conclusion != "success":
        raise ReleaseEvidenceError(
            f"workflow run {run.get('id')} for {sha} concluded {conclusion}"
        )
    return run


def validate_qualification_jobs(jobs: list[dict[str, Any]]) -> None:
    names = [str(job.get("name")) for job in jobs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ReleaseEvidenceError(
            f"production qualification has duplicate jobs: {duplicates}"
        )
    actual = set(names)
    if actual != EXPECTED_QUALIFICATION_JOBS:
        missing = sorted(EXPECTED_QUALIFICATION_JOBS - actual)
        unexpected = sorted(actual - EXPECTED_QUALIFICATION_JOBS)
        raise ReleaseEvidenceError(
            "production qualification job set differs from the release contract: "
            f"missing={missing}, unexpected={unexpected}"
        )
    incomplete = [
        f"{job.get('name')}={job.get('status')}/{job.get('conclusion')}"
        for job in jobs
        if job.get("status") != "completed" or job.get("conclusion") != "success"
    ]
    if incomplete:
        raise ReleaseEvidenceError(
            "production qualification has incomplete jobs: " + ", ".join(incomplete)
        )


def check_once(
    *,
    repository: str,
    sha: str,
    token: str,
    get_json: Callable[[str, str], dict[str, Any]] = _github_get,
) -> dict[str, int]:
    base = f"https://api.github.com/repos/{repository}"
    selected: dict[str, dict[str, Any]] = {}
    for requirement in REQUIREMENTS:
        query = urllib.parse.urlencode(
            {"head_sha": sha, "event": requirement.event, "per_page": "100"}
        )
        payload = get_json(
            f"{base}/actions/workflows/{requirement.file_name}/runs?{query}", token
        )
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list):
            raise EvidencePending(
                f"GitHub returned no workflow_runs list for {requirement.file_name}"
            )
        selected[requirement.file_name] = select_exact_run(
            runs, sha=sha, event=requirement.event
        )

    qualification = selected["production-qualification.yml"]
    jobs_payload = get_json(
        f"{base}/actions/runs/{qualification['id']}/jobs?filter=latest&per_page=100",
        token,
    )
    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        raise EvidencePending("GitHub returned no production qualification jobs list")
    validate_qualification_jobs(jobs)
    return {name: int(run["id"]) for name, run in selected.items()}


def wait_for_evidence(
    *, repository: str, sha: str, token: str, timeout_seconds: int, interval_seconds: int
) -> dict[str, int]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return check_once(repository=repository, sha=sha, token=token)
        except EvidencePending as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReleaseEvidenceError(
                    f"exact-commit release evidence did not complete within {timeout_seconds}s: {exc}"
                ) from exc
            print(f"waiting for exact-commit evidence: {exc}", flush=True)
            time.sleep(min(interval_seconds, remaining))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=2700)
    parser.add_argument("--interval-seconds", type=int, default=10)
    args = parser.parse_args()
    if len(args.sha) != 40 or any(char not in "0123456789abcdef" for char in args.sha):
        raise SystemExit("--sha must be a lowercase 40-character Git commit SHA")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    evidence = wait_for_evidence(
        repository=args.repository,
        sha=args.sha,
        token=token,
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds,
    )
    print(f"exact-commit release evidence is complete: {json.dumps(evidence, sort_keys=True)}")


if __name__ == "__main__":
    main()
