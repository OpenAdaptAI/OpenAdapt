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
README = Path(__file__).resolve().parents[1] / "README.md"
# An install command such as pip install "openadapt-capture>=1.3.0,<1.4".
README_PIN = re.compile(r"openadapt-capture(?:\[[^\]]*\])?(?P<spec>[<>=!~][^\s\"'`;]*)")
SPECIFIER_CLAUSE = re.compile(r"^(?P<op>~=|==|!=|<=|>=|<|>)(?P<version>\d+(?:\.\d+)*)$")
FINAL_RELEASE = re.compile(r"^\d+(?:\.\d+)*$")
TAG_PATTERN = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
COMMIT_PATTERN = re.compile(r"^(?P<kind>[A-Za-z]+)(?:\([^)]*\))?(?P<breaking>!)?:\s+.+$")
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
ACTIVE_RUN_STATES = {"queued", "in_progress", "waiting", "pending", "requested"}
# The release.yml step that asks the release GitHub App for a token. It fails
# when the App is not installed on this repository or its credentials are wrong.
# The publish-tag job has a step with the same name, but it reads the pypi
# environment, so the advice below names only the tag-creation job.
APP_TOKEN_JOB = "create-release-tag"
APP_TOKEN_STEP = "Create the exact release App token"
FAILED_CONCLUSIONS = {"failure", "timed_out"}
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


def readme_pins(text: str) -> list[str]:
    """Return each distinct version specifier the README tells users to install."""
    pins: list[str] = []
    for match in README_PIN.finditer(text):
        if match.group("spec") not in pins:
            pins.append(match.group("spec"))
    return pins


def _release(value: str, width: int) -> tuple[int, ...]:
    parts = [int(part) for part in value.split(".")]
    return tuple(parts + [0] * (width - len(parts)))


def parse_specifier(specifier: str) -> list[tuple[str, str]] | None:
    """Split a pip specifier into (operator, version) clauses.

    Returns None for syntax this dependency-free check does not model, such as
    wildcards or pre-releases, so the caller can report it instead of guessing.
    """
    clauses = []
    for clause in specifier.split(","):
        match = SPECIFIER_CLAUSE.fullmatch(clause.strip())
        if not match:
            return None
        op, bound = match.group("op"), match.group("version")
        if op == "~=" and "." not in bound:
            return None
        clauses.append((op, bound))
    return clauses


def satisfies(version: str, clauses: list[tuple[str, str]]) -> bool:
    """Check a version against parsed clauses. Pip skips pre-releases here."""
    if not FINAL_RELEASE.fullmatch(version):
        return False
    for op, bound in clauses:
        width = max(len(version.split(".")), len(bound.split(".")))
        have, want = _release(version, width), _release(bound, width)
        if op == "~=":
            prefix = len(bound.split(".")) - 1
            ok = have >= want and have[:prefix] == want[:prefix]
        else:
            ok = {
                "==": have == want,
                "!=": have != want,
                "<=": have <= want,
                ">=": have >= want,
                "<": have < want,
                ">": have > want,
            }[op]
        if not ok:
            return False
    return True


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
    releases = payload.get("releases", {})
    return {
        "reachable": True,
        "latest": payload.get("info", {}).get("version"),
        "versions": sorted(releases.keys()),
        # pip skips a version whose files are all yanked or deleted.
        "installable": sorted(
            version
            for version, files in releases.items()
            if any(not file.get("yanked") for file in files or [])
        ),
    }


def run_failed(run: dict[str, Any] | None) -> bool:
    return bool(
        run and run.get("status") == "completed" and run.get("conclusion") in FAILED_CONCLUSIONS
    )


def failed_steps(run_id: Any) -> list[dict[str, str]] | None:
    """Return the failed steps of one workflow run, or None if they can't be read."""
    try:
        payload = github_get(
            f"/repos/{REPOSITORY}/actions/runs/{run_id}/jobs",
            {"filter": "latest", "per_page": "100"},
        )
        return [
            {"job": job.get("name", ""), "step": step.get("name", "")}
            for job in payload.get("jobs", [])
            for step in job.get("steps") or []
            if step.get("conclusion") in FAILED_CONCLUSIONS
        ]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError, AttributeError):
        return None


def release_blocker(run: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Explain why the latest release run failed, as (alert, note)."""
    if not run_failed(run):
        return None, None
    assert run is not None
    when = f" on {run['created_at'][:10]}" if run.get("created_at") else ""
    steps = run.get("failed_steps")
    if steps is None:
        if "failed_steps" not in run:
            return None, None
        return None, (
            f"The check couldn't read the steps of release run {run.get('id')}, "
            "so it doesn't say why that run failed."
        )
    if any(
        step.get("job") == APP_TOKEN_JOB and step.get("step") == APP_TOKEN_STEP for step in steps
    ):
        return (
            f"**Release blocked:** The latest release run, {run.get('id')}{when}, "
            f'failed at "{APP_TOKEN_STEP}". That step fails when the release '
            "GitHub App can't get a token for this repository. Check that the App "
            f"is installed on {REPOSITORY} with read and write access to contents, "
            "and that the release-identity environment has its current App ID and "
            "private key. Then dispatch release.yml again."
        ), None
    if steps:
        where = ", ".join(f'"{step["step"]}" in job {step["job"]}' for step in steps)
        return (
            f"**Last release run failed:** Release run {run.get('id')}{when} failed "
            f"at {where}. Fix that step, then dispatch release.yml again."
        ), None
    return (
        f"**Last release run failed:** Release run {run.get('id')}{when} concluded "
        f"{run.get('conclusion')}, and GitHub listed no failed step. Open that run, "
        "fix the cause, then dispatch release.yml again."
    ), None


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
    if release_runs and run_failed(release_runs[0]):
        release_runs[0] = {**release_runs[0], "failed_steps": failed_steps(release_runs[0]["id"])}

    state: dict[str, Any] = {
        "now": datetime.now(timezone.utc).isoformat(),
        "main_sha": main["sha"],
        "main_test": main_test,
        "release_runs": release_runs[:10],
        "release_tags": [release.get("tag_name") for release in releases],
        "pypi": fetch_pypi_versions(),
        "readme_pins": readme_pins(README.read_text(encoding="utf-8")),
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
        # Say why the last attempt failed, whichever main commit it ran on.
        blocker_alert, blocker_note = release_blocker(latest_run)
        if blocker_alert:
            alerts.append(blocker_alert)
        if blocker_note:
            notes.append(blocker_note)
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

    pins = state.get("readme_pins") or []
    if pins and pypi_versions is not None and not active_runs:
        installable = pypi.get("installable", pypi.get("versions")) or []
        for pin in pins:
            clauses = parse_specifier(pin)
            matches = [v for v in installable if clauses and satisfies(v, clauses)]
            if clauses is None:
                alerts.append(
                    f"**README pin unchecked:** README.md tells users to install "
                    f"`openadapt-capture{pin}`, and this check cannot evaluate that "
                    "specifier. Use plain version comparisons."
                )
            elif not matches:
                alerts.append(
                    f"**README ahead of PyPI:** README.md tells users to install "
                    f"`openadapt-capture{pin}`, but no published version satisfies it "
                    f"(PyPI latest: {pypi.get('latest')}). Publish the release, or pin "
                    "the README to a published version."
                )
            else:
                newest = max(matches, key=lambda v: _release(v, 4))
                notes.append(f"README pin `openadapt-capture{pin}` resolves on PyPI to {newest}.")

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
        "readme_pins": [],
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
        ("README pin ahead of PyPI alerts", {"readme_pins": [">=1.3.0,<1.4"]}, 1),
        ("README pin on a published version is quiet", {"readme_pins": [">=1.2.0,<1.3"]}, 0),
        (
            "README pin on a yanked version alerts",
            {
                "readme_pins": [">=1.2.0,<1.3"],
                "pypi": {"reachable": True, "versions": ["1.2.0"], "installable": []},
            },
            1,
        ),
        (
            "README pin waits for an active release",
            {
                "readme_pins": [">=1.3.0,<1.4"],
                "release_runs": [release_run(15, status="in_progress", conclusion=None)],
            },
            0,
        ),
        (
            "README pin is not judged while PyPI is unreachable",
            {"readme_pins": [">=1.3.0,<1.4"], "pypi": {"reachable": False}},
            0,
        ),
        ("README pin the check cannot read alerts", {"readme_pins": ["==1.2.*"]}, 1),
        (
            "release App token failure is named next to unreleased work",
            {
                "release_runs": [
                    {
                        **release_run(16, sha="c" * 40, conclusion="failure"),
                        "failed_steps": [{"job": "create-release-tag", "step": APP_TOKEN_STEP}],
                    }
                ],
                "commits_since_tag": [commit("fix: pending", "2026-07-20T00:00:00+00:00")],
            },
            2,
        ),
        (
            "release failure with nothing to release is quiet",
            {
                "release_runs": [
                    {
                        **release_run(17, sha="c" * 40, conclusion="failure"),
                        "failed_steps": [{"job": "create-release-tag", "step": APP_TOKEN_STEP}],
                    }
                ]
            },
            0,
        ),
        (
            "release failure with no failed step is still named",
            {
                "release_runs": [
                    {**release_run(19, sha="c" * 40, conclusion="failure"), "failed_steps": []}
                ],
                "commits_since_tag": [commit("fix: pending", "2026-07-20T00:00:00+00:00")],
            },
            2,
        ),
        (
            "unreadable release steps add no alert",
            {
                "release_runs": [
                    {**release_run(18, sha="c" * 40, conclusion="failure"), "failed_steps": None}
                ],
                "commits_since_tag": [commit("fix: pending", "2026-07-20T00:00:00+00:00")],
            },
            1,
        ),
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
    if readme_pins('pip install "openadapt-capture[linux]>=1.3.0,<1.4"\n'
                   'pip install "openadapt-capture[linux]"\n'
                   "github.com/OpenAdaptAI/openadapt-capture/blob/main") != [">=1.3.0,<1.4"]:
        failures.append("README install pins were not extracted exactly")
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
