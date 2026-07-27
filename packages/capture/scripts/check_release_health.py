#!/usr/bin/env python3
"""Report actionable gaps between Capture main, its release, and PyPI."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPOSITORY = "OpenAdaptAI/openadapt-capture"
BRANCH = "main"
GITHUB_API = "https://api.github.com"
PYPI_API = "https://pypi.org/pypi/openadapt-capture/json"
TAG_PATTERN = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
COMMIT_PATTERN = re.compile(r"^(?P<kind>[A-Za-z]+)(?:\([^)]*\))?(?P<breaking>!)?:\s+.+$")
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
ACTIVE_RUN_STATES = {"queued", "in_progress", "waiting", "pending", "requested"}
ALLOWED_COMMIT_KINDS = frozenset("build chore ci docs feat fix perf refactor style test".split())
RELEASE_GRACE = timedelta(hours=4)
TAG_GRACE = timedelta(hours=1)
PYPI_GRACE = timedelta(minutes=30)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def age(now: datetime, value: str | None) -> timedelta:
    parsed = parse_time(value)
    return now - parsed if parsed else timedelta(0)


def humanize(value: timedelta) -> str:
    minutes = max(0, int(value.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def version_key(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def bump_level(message: str) -> str | None:
    """Mirror Capture's configured semantic-release tags without dependencies."""
    subject, _, body = message.replace("\r\n", "\n").partition("\n")
    match = COMMIT_PATTERN.match(subject.strip())
    if not match:
        return None
    kind = match.group("kind").lower()
    if kind not in ALLOWED_COMMIT_KINDS:
        return None
    if match.group("breaking") or BREAKING_FOOTER.search(body):
        return "major"
    if kind == "feat":
        return "minor"
    if kind in {"fix", "perf"}:
        return "patch"
    return None


def github_get(path: str, params: dict[str, str] | None = None) -> Any:
    url = f"{GITHUB_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "openadapt-capture-release-health/1",
        },
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def fetch_pypi_versions() -> dict[str, Any]:
    request = urllib.request.Request(
        PYPI_API,
        headers={"User-Agent": "openadapt-capture-release-health/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read(2 * 1024 * 1024))
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
    ) as error:
        return {"reachable": False, "error": str(error)}
    return {
        "reachable": True,
        "latest": payload.get("info", {}).get("version"),
        "versions": sorted(payload.get("releases", {}).keys()),
    }


def collect_live_state() -> dict[str, Any]:
    main = github_get(f"/repos/{REPOSITORY}/commits/{BRANCH}")
    tags = github_get(f"/repos/{REPOSITORY}/tags", {"per_page": "100"})
    releases = github_get(f"/repos/{REPOSITORY}/releases", {"per_page": "100"})

    versions = []
    for tag in tags:
        match = TAG_PATTERN.fullmatch(tag.get("name", ""))
        if match:
            versions.append(
                (version_key(match.group("version")), tag["name"], match.group("version"))
            )
    versions.sort()
    latest_tag = versions[-1] if versions else None

    tests = github_get(
        f"/repos/{REPOSITORY}/actions/workflows/test.yml/runs",
        {"branch": BRANCH, "event": "push", "per_page": "20"},
    ).get("workflow_runs", [])
    exact_tests = [run for run in tests if run.get("head_sha") == main["sha"]]
    exact_tests.sort(key=lambda run: run.get("created_at") or "", reverse=True)
    main_test = exact_tests[0] if exact_tests else None

    release_runs = github_get(
        f"/repos/{REPOSITORY}/actions/workflows/release.yml/runs",
        {"branch": BRANCH, "per_page": "20"},
    ).get("workflow_runs", [])
    release_runs.sort(key=lambda run: run.get("created_at") or "", reverse=True)

    state: dict[str, Any] = {
        "now": datetime.now(timezone.utc).isoformat(),
        "main_sha": main["sha"],
        "main_test": main_test,
        "release_runs": release_runs[:10],
        "release_tags": [release.get("tag_name") for release in releases],
        "pypi": fetch_pypi_versions(),
        "latest_tag": None,
        "latest_version": None,
        "latest_tag_date": None,
        "commits_since_tag": [],
    }
    if latest_tag:
        _, tag_name, version = latest_tag
        tag_commit = github_get(f"/repos/{REPOSITORY}/commits/{tag_name}")
        comparison = github_get(f"/repos/{REPOSITORY}/compare/{tag_name}...{BRANCH}")
        state.update(
            {
                "latest_tag": tag_name,
                "latest_version": version,
                "latest_tag_date": (tag_commit.get("commit", {}).get("committer", {}).get("date")),
                "commits_since_tag": [
                    {
                        "sha": commit["sha"],
                        "message": commit.get("commit", {}).get("message", ""),
                        "date": commit.get("commit", {}).get("committer", {}).get("date"),
                    }
                    for commit in comparison.get("commits", [])
                ],
            }
        )
    return state


def evaluate(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    now = parse_time(state["now"])
    assert now is not None
    alerts: list[str] = []
    notes: list[str] = []

    current_runs = [
        run
        for run in state.get("release_runs", [])
        if run.get("head_branch") == BRANCH and run.get("head_sha") == state.get("main_sha")
    ]
    active_runs = [run for run in current_runs if run.get("status") in ACTIVE_RUN_STATES]
    # Runs arrive newest first. A successful retry resolves an older failure on
    # the same exact main commit; only the latest completed attempt is relevant.
    latest_current_run = current_runs[0] if current_runs else None
    failed_run = (
        latest_current_run
        if latest_current_run
        and latest_current_run.get("status") == "completed"
        and latest_current_run.get("conclusion") != "success"
        else None
    )
    latest_run = (state.get("release_runs") or [None])[0]
    if active_runs:
        notes.append("A manual release run is active; publication-gap alerts are deferred.")

    releasable = []
    for commit in state.get("commits_since_tag", []):
        level = bump_level(commit.get("message", ""))
        if level:
            releasable.append({**commit, "level": level})
    main_test = state.get("main_test") or {}
    main_green = main_test.get("status") == "completed" and main_test.get("conclusion") == "success"
    oldest_age = max(
        (age(now, commit.get("date")) for commit in releasable),
        default=timedelta(0),
    )
    release_failed = failed_run is not None
    if (
        releasable
        and not active_runs
        and main_green
        and (release_failed or oldest_age >= RELEASE_GRACE)
    ):
        rows = ", ".join(
            f"{commit['sha'][:8]} ({commit['level']}: {commit['message'].splitlines()[0]})"
            for commit in releasable
        )
        dispatch = (
            f" Release run {failed_run.get('id')} concluded " f"{failed_run.get('conclusion')}."
            if failed_run
            else ""
        )
        alerts.append(
            f"**Unreleased main:** {len(releasable)} release-worthy commit(s) have "
            f"remained after {state.get('latest_tag')} for {humanize(oldest_age)}."
            f"{dispatch} {rows}."
        )
    elif releasable:
        reason = (
            "release active"
            if active_runs
            else f"exact-main Tests are {main_test.get('status', 'missing')}/{main_test.get('conclusion')}"
        )
        if main_green and oldest_age < RELEASE_GRACE:
            reason = f"inside the {humanize(RELEASE_GRACE)} grace window"
        notes.append(f"{len(releasable)} release-worthy commit(s) are pending; {reason}.")
    else:
        notes.append("No release-worthy commit remains after the latest version tag.")

    tag = state.get("latest_tag")
    version = state.get("latest_version")
    tag_age = age(now, state.get("latest_tag_date"))
    release_exists = tag in set(state.get("release_tags") or [])
    pypi = state.get("pypi") or {}
    pypi_versions = set(pypi.get("versions") or []) if pypi.get("reachable") else None
    pypi_exists = version in pypi_versions if pypi_versions is not None else None

    if (
        tag
        and not active_runs
        and tag_age >= TAG_GRACE
        and not release_exists
        and pypi_exists is False
    ):
        alerts.append(
            f"**Incomplete publish:** {tag} is {humanize(tag_age)} old, but neither a "
            "GitHub release nor its PyPI artifact exists."
        )
    elif tag and not release_exists and pypi_exists is True:
        notes.append(f"{tag} is installable from PyPI; its missing release page is cosmetic.")

    if (
        tag
        and release_exists
        and pypi_exists is False
        and not active_runs
        and tag_age >= PYPI_GRACE
    ):
        alerts.append(
            f"**PyPI missing:** GitHub release {tag} exists, but openadapt-capture "
            f"{version} is still absent from PyPI after {humanize(tag_age)}."
        )
    elif pypi_exists is None:
        notes.append("PyPI was unreachable, so no PyPI absence was inferred.")

    if latest_run:
        notes.append(
            "Latest manual release run (context only, not attributed to a tag): "
            f"{latest_run.get('id')} on {latest_run.get('head_branch')} at "
            f"{str(latest_run.get('head_sha') or '')[:8]} — "
            f"{latest_run.get('status')}/{latest_run.get('conclusion')}."
        )
    return alerts, notes


def render(alerts: list[str], notes: list[str], now: str) -> str:
    lines = [
        "Automated check of Capture main, GitHub Releases, and PyPI.",
        "",
        f"Last evaluated: `{now}`",
        "",
    ]
    for alert in alerts:
        lines.extend([alert, ""])
    lines.extend(["<details><summary>Cleared and deferred state</summary>", ""])
    lines.extend(f"- {note}" for note in notes)
    lines.extend(["", "</details>", ""])
    return "\n".join(lines)


def can_close_issue(state: dict[str, Any], alerts: list[str]) -> bool:
    """Close only after a complete healthy check, never on unknown index state."""
    return not alerts and (state.get("pypi") or {}).get("reachable") is True


def write_outputs(path: Path | None, *, alert: bool, evaluated: bool, close: bool) -> None:
    if not path:
        return
    with path.open("a", encoding="utf-8") as output:
        output.write(f"alert={'true' if alert else 'false'}\n")
        output.write(f"evaluated={'true' if evaluated else 'false'}\n")
        output.write(f"close={'true' if close else 'false'}\n")


def self_test() -> int:
    now = "2026-07-27T20:00:00+00:00"

    def commit(message: str, date: str) -> dict[str, str]:
        return {"sha": "b" * 40, "message": message, "date": date}

    def release_run(
        run_id: int,
        *,
        sha: str = "a" * 40,
        status: str = "completed",
        conclusion: str | None = "success",
    ) -> dict[str, Any]:
        return {
            "id": run_id,
            "head_branch": "main",
            "head_sha": sha,
            "status": status,
            "conclusion": conclusion,
        }

    base = {
        "now": now,
        "main_sha": "a" * 40,
        "main_test": {"status": "completed", "conclusion": "success"},
        "latest_tag": "v1.2.0",
        "latest_version": "1.2.0",
        "latest_tag_date": "2026-07-26T20:00:00+00:00",
        "release_tags": ["v1.2.0"],
        "pypi": {"reachable": True, "versions": ["1.2.0"]},
        "release_runs": [],
        "commits_since_tag": [],
    }
    cases = [
        (
            "unreleased fix alerts",
            {"commits_since_tag": [commit("fix: privacy boundary", "2026-07-27T12:00:00+00:00")]},
            1,
        ),
        (
            "fresh fix waits",
            {"commits_since_tag": [commit("fix: fresh", "2026-07-27T19:00:00+00:00")]},
            0,
        ),
        (
            "docs commit is quiet",
            {"commits_since_tag": [commit("docs: clarify", "2026-07-20T00:00:00+00:00")]},
            0,
        ),
        (
            "active main dispatch suppresses",
            {
                "commits_since_tag": [commit("fix: pending", "2026-07-20T00:00:00+00:00")],
                "release_runs": [release_run(10, status="in_progress", conclusion=None)],
            },
            0,
        ),
        (
            "failed exact-main dispatch with unpublished work alerts",
            {
                "release_runs": [release_run(11, conclusion="cancelled")],
                "commits_since_tag": [commit("fix: pending", "2026-07-27T19:30:00+00:00")],
            },
            1,
        ),
        (
            "cancelled no-op dispatch is context only",
            {"release_runs": [release_run(12, conclusion="cancelled")]},
            0,
        ),
        (
            "superseded dispatch is context only",
            {"release_runs": [release_run(13, sha="c" * 40, conclusion="cancelled")]},
            0,
        ),
        (
            "successful retry clears older exact-main failure",
            {
                "release_runs": [
                    release_run(14, conclusion="success"),
                    release_run(13, conclusion="failure"),
                ]
            },
            0,
        ),
        (
            "missing release and PyPI alerts",
            {"release_tags": [], "pypi": {"reachable": True, "versions": []}},
            1,
        ),
        ("PyPI artifact makes missing page cosmetic", {"release_tags": []}, 0),
        ("release without PyPI alerts", {"pypi": {"reachable": True, "versions": []}}, 1),
    ]
    failures = []
    for name, override, expected in cases:
        state = {**base, **override}
        alerts, notes = evaluate(state)
        passed = len(alerts) == expected
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        if not passed:
            failures.append(f"{name}: expected {expected}, got {len(alerts)}")
        if name == "active main dispatch suppresses" and not any(
            "context only" in note for note in notes
        ):
            failures.append("manual workflow_dispatch context was not rendered safely")
    if can_close_issue({**base, "pypi": {"reachable": False}}, []):
        failures.append("PyPI outage was treated as evidence that an alert resolved")
    if not can_close_issue(base, []):
        failures.append("complete healthy state did not resolve an alert")
    if bump_level("revert!: undo an accidental release") is not None:
        failures.append("unconfigured commit kind incorrectly triggered a major release")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    try:
        state = (
            json.loads(args.state_file.read_text(encoding="utf-8"))
            if args.state_file
            else collect_live_state()
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        print(f"Release state is temporarily unavailable: {error}", file=sys.stderr)
        write_outputs(args.github_output, alert=False, evaluated=False, close=False)
        return 0
    alerts, notes = evaluate(state)
    body = render(alerts, notes, state["now"])
    if args.markdown:
        args.markdown.write_text(body, encoding="utf-8")
    else:
        print(body)
    write_outputs(
        args.github_output,
        alert=bool(alerts),
        evaluated=True,
        close=can_close_issue(state, alerts),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
