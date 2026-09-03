#!/usr/bin/env python3
"""Start missing exact-commit qualification and require complete evidence."""

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

# The hosted workflow proves the portable package lifecycle. The live workflow
# separately proves the exact candidate on a qualified Linux desktop.
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
EXPECTED_LIVE_QUALIFICATION_JOBS = frozenset(
    {
        "Select live qualification platforms",
        "Build candidate distributions",
        "Interactive qualification (Linux X64)",
        "Interactive qualification (macOS ARM64)",
        "Interactive qualification (Windows X64)",
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
    events: tuple[str, ...]
    dispatch_inputs: dict[str, str] | None = None


REQUIREMENTS = (
    WorkflowRequirement("test.yml", ("push", "workflow_dispatch")),
    WorkflowRequirement(
        "production-qualification.yml",
        ("workflow_dispatch",),
        {"candidate_sha": "{sha}"},
    ),
    WorkflowRequirement(
        "live-qualification.yml",
        ("workflow_dispatch",),
        {"candidate_sha": "{sha}", "platform": "linux"},
    ),
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


def _github_dispatch(
    repository: str,
    requirement: WorkflowRequirement,
    *,
    ref: str,
    sha: str,
    token: str,
) -> None:
    payload: dict[str, Any] = {"ref": ref}
    if requirement.dispatch_inputs:
        payload["inputs"] = {
            key: value.format(sha=sha) for key, value in requirement.dispatch_inputs.items()
        }
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/workflows/"
        f"{requirement.file_name}/dispatches",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "openadapt-capture-release-evidence/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if not 200 <= response.status < 300:
                raise ReleaseEvidenceError(
                    f"GitHub returned HTTP {response.status} while dispatching "
                    f"{requirement.file_name}"
                )
    except (urllib.error.URLError, OSError) as exc:
        raise ReleaseEvidenceError(
            f"GitHub workflow dispatch failed for {requirement.file_name}: {exc}"
        ) from exc


def select_exact_run(
    runs: list[dict[str, Any]], *, sha: str, events: tuple[str, ...]
) -> dict[str, Any]:
    exact = [run for run in runs if run.get("head_sha") == sha and run.get("event") in events]
    if not exact:
        raise EvidencePending(
            f"no {' or '.join(events)} workflow run exists for exact commit {sha}"
        )
    exact.sort(key=lambda run: (run.get("created_at") or "", int(run.get("id") or 0)))
    run = exact[-1]
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status in ACTIVE_STATES or status != "completed":
        raise EvidencePending(f"workflow run {run.get('id')} for {sha} is {status}/{conclusion}")
    if conclusion != "success":
        raise ReleaseEvidenceError(f"workflow run {run.get('id')} for {sha} concluded {conclusion}")
    return run


def validate_qualification_jobs(jobs: list[dict[str, Any]]) -> None:
    names = [str(job.get("name")) for job in jobs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ReleaseEvidenceError(f"production qualification has duplicate jobs: {duplicates}")
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


def validate_test_jobs(jobs: list[dict[str, Any]]) -> None:
    """Require the package archive contract in the exact test run."""

    package_jobs = [job for job in jobs if job.get("name") == "package-contract"]
    if len(package_jobs) != 1:
        raise ReleaseEvidenceError("exact test run must contain one package-contract job")
    package_job = package_jobs[0]
    if package_job.get("status") != "completed" or package_job.get("conclusion") != "success":
        raise ReleaseEvidenceError(
            "package-contract is incomplete: "
            f"{package_job.get('status')}/{package_job.get('conclusion')}"
        )


def validate_live_linux_jobs(jobs: list[dict[str, Any]]) -> None:
    """Require exact-candidate evidence from the qualified Linux runner."""

    names = [str(job.get("name")) for job in jobs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ReleaseEvidenceError(f"live qualification has duplicate jobs: {duplicates}")
    actual = set(names)
    if actual != EXPECTED_LIVE_QUALIFICATION_JOBS:
        missing = sorted(EXPECTED_LIVE_QUALIFICATION_JOBS - actual)
        unexpected = sorted(actual - EXPECTED_LIVE_QUALIFICATION_JOBS)
        raise ReleaseEvidenceError(
            "live qualification job set differs from the release contract: "
            f"missing={missing}, unexpected={unexpected}"
        )
    required = {
        "Select live qualification platforms",
        "Build candidate distributions",
        "Interactive qualification (Linux X64)",
    }
    incomplete = [
        f"{job.get('name')}={job.get('status')}/{job.get('conclusion')}"
        for job in jobs
        if job.get("name") in required
        and (job.get("status") != "completed" or job.get("conclusion") != "success")
    ]
    if incomplete:
        raise ReleaseEvidenceError(
            "live Linux qualification has incomplete jobs: " + ", ".join(incomplete)
        )


def _run_has_live_linux_job(
    *,
    base: str,
    run: dict[str, Any],
    token: str,
    get_json: Callable[[str, str], dict[str, Any]],
) -> bool:
    """Return true when a run selected Linux instead of another live platform."""

    payload = get_json(
        f"{base}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100",
        token,
    )
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return False
    linux = [job for job in jobs if job.get("name") == "Interactive qualification (Linux X64)"]
    return len(linux) == 1 and linux[0].get("conclusion") != "skipped"


def _workflow_runs(
    *,
    base: str,
    requirement: WorkflowRequirement,
    sha: str,
    token: str,
    get_json: Callable[[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"head_sha": sha, "per_page": "100"})
    payload = get_json(f"{base}/actions/workflows/{requirement.file_name}/runs?{query}", token)
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise EvidencePending(f"GitHub returned no workflow_runs list for {requirement.file_name}")
    return runs


def dispatch_missing_qualifications(
    *,
    repository: str,
    sha: str,
    ref: str,
    token: str,
    get_json: Callable[[str, str], dict[str, Any]] = _github_get,
    dispatch: Callable[..., None] = _github_dispatch,
) -> tuple[str, ...]:
    """Start each required workflow only when this SHA has no accepted run."""

    if ref != "main":
        raise ReleaseEvidenceError("release qualification must dispatch ref 'main'")
    base = f"https://api.github.com/repos/{repository}"
    missing: list[WorkflowRequirement] = []
    for requirement in REQUIREMENTS:
        runs = _workflow_runs(
            base=base,
            requirement=requirement,
            sha=sha,
            token=token,
            get_json=get_json,
        )
        matching = [
            run
            for run in runs
            if run.get("head_sha") == sha and run.get("event") in requirement.events
        ]
        if requirement.file_name == "live-qualification.yml":
            matching = [
                run
                for run in matching
                if _run_has_live_linux_job(
                    base=base,
                    run=run,
                    token=token,
                    get_json=get_json,
                )
            ]
        if not any(
            run.get("head_sha") == sha and run.get("event") in requirement.events
            for run in matching
        ):
            missing.append(requirement)
    for requirement in missing:
        dispatch(
            repository,
            requirement,
            ref=ref,
            sha=sha,
            token=token,
        )
    return tuple(requirement.file_name for requirement in missing)


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
        runs = _workflow_runs(
            base=base,
            requirement=requirement,
            sha=sha,
            token=token,
            get_json=get_json,
        )
        selected[requirement.file_name] = select_exact_run(runs, sha=sha, events=requirement.events)

    tests = selected["test.yml"]
    test_jobs_payload = get_json(
        f"{base}/actions/runs/{tests['id']}/jobs?filter=latest&per_page=100",
        token,
    )
    test_jobs = test_jobs_payload.get("jobs")
    if not isinstance(test_jobs, list):
        raise EvidencePending("GitHub returned no exact test jobs list")
    validate_test_jobs(test_jobs)

    qualification = selected["production-qualification.yml"]
    jobs_payload = get_json(
        f"{base}/actions/runs/{qualification['id']}/jobs?filter=latest&per_page=100",
        token,
    )
    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        raise EvidencePending("GitHub returned no production qualification jobs list")
    validate_qualification_jobs(jobs)

    live = selected["live-qualification.yml"]
    live_jobs_payload = get_json(
        f"{base}/actions/runs/{live['id']}/jobs?filter=latest&per_page=100",
        token,
    )
    live_jobs = live_jobs_payload.get("jobs")
    if not isinstance(live_jobs, list):
        raise EvidencePending("GitHub returned no live qualification jobs list")
    validate_live_linux_jobs(live_jobs)
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
    parser.add_argument("--dispatch-missing", action="store_true")
    parser.add_argument("--ref")
    args = parser.parse_args()
    if len(args.sha) != 40 or any(char not in "0123456789abcdef" for char in args.sha):
        raise SystemExit("--sha must be a lowercase 40-character Git commit SHA")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    if args.dispatch_missing != bool(args.ref):
        raise SystemExit("--dispatch-missing and --ref must be used together")
    if args.dispatch_missing:
        dispatched = dispatch_missing_qualifications(
            repository=args.repository,
            sha=args.sha,
            ref=args.ref,
            token=token,
        )
        if dispatched:
            print("started missing exact-commit qualification: " + ", ".join(dispatched))
        else:
            print("exact-commit qualification runs already exist")
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
