#!/usr/bin/env python3
"""Make an unreleased fix and a silently skipped publish impossible to miss.

Two failures happened here and neither produced a signal.

1. Unreleased work. ``openadapt-capture`` PR #51 removed a hosted-recognizer
   path that uploaded raw audio waveforms to a third party. It merged to main
   and sat there while the only installable version on PyPI, 1.2.0, still
   contained the upload path, and two downstream packages resolve
   ``openadapt-capture>=1.1.0`` unpinned. It shipped as 1.2.1 only because a
   human happened to look.

2. Silently skipped publishes. Four ``openadapt-desktop`` tags
   (``desktop-v0.12.0``, ``0.8.0``, ``0.7.0``, ``0.1.0``) exist with no release
   object: a failed macOS x86_64 build, an ungranted ``native-release``
   environment approval, a wholly cancelled run, and a ``tomllib`` import error
   on a pre-3.11 runner. The launcher's ``Release and PyPI Publish`` runs
   ``30275153205`` and ``30226357239`` were cancelled seconds in. Existing
   ``if: failure()`` notifiers see none of this, because ``cancelled`` and
   ``skipped`` are not ``failure``.

The check is deliberately state-based rather than event-based. "A release run
was cancelled" is not the alarm; "a release-worthy commit or a tag is still
unpublished" is. That distinction matters because the launcher's release
workflow fires on every push to main under a ``concurrency: release`` group, so
routine supersession cancellations are constant and benign. A guard that fired
on them would go the way of the platform-manifest drift check, which was
correct but had been benignly red after every single release until nobody could
tell its real signal from its noise.

Detectors (per configured release lane in ``.github/release-health.json``):

  unreleased-work        main carries commits newer than the newest release tag
                         that WOULD bump the version under this repository's own
                         ``[tool.semantic_release.commit_parser_options]``, main
                         is green, no release run is in flight, and the oldest
                         such commit is older than the grace window.
  tag-without-release    a release-shaped tag has no GitHub release object, no
                         run for it is queued/waiting/running, and it is older
                         than the grace window. Reports whether the workflow
                         concluded ``failure``/``cancelled``/``skipped`` or
                         never started at all.
  unpublished-release    the newest release tag's version is absent from PyPI
                         after the CDN grace window.

Everything that can be transient warns or stays silent instead:

  * the semantic-release commit and the launcher's ``chore(release): reconcile
    platform manifest`` commit land 0-90s after the tag and are ``chore``,
    which is in ``allowed_tags`` but in neither ``minor_tags`` nor
    ``patch_tags`` -- no bump, no alert. The launcher's release commit subject
    is a bare ``1.10.0``, which is not a Conventional Commit at all -- no bump,
    no alert;
  * PyPI CDN propagation is absorbed by ``pypi_lag_grace_minutes``, and an
    unreachable PyPI warns rather than alarms;
  * a queued, waiting (human approval gate), or running release job suppresses
    both gap detectors for its lane;
  * a red or still-running main suppresses unreleased-work, because a red main
    is its own signal and must not be released anyway;
  * an unreachable GitHub API warns and exits 0.

Standard library only: no dependency install, no lockfile, no cache.
``tomllib`` is imported defensively -- a missing ``tomllib`` is exactly what
broke ``openadapt-desktop`` run ``29514474536``, and this script degrades to
documented defaults with a warning rather than crashing.

Usage:
    python scripts/check_release_health.py                    # live check
    python scripts/check_release_health.py --self-test        # offline, no network
    python scripts/check_release_health.py --state-file s.json --offline
    python scripts/check_release_health.py --dump-state s.json
"""

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
from typing import Any, Iterable

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on old runners
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / ".github" / "release-health.json"
GITHUB_API = "https://api.github.com"
PYPI_URL_TEMPLATE = "https://pypi.org/pypi/{package}/json"
HTTP_TIMEOUT_SECONDS = 30

# python-semantic-release's defaults, used only when pyproject.toml cannot be
# read. Kept explicit so a degraded run states what it assumed.
FALLBACK_PARSER_OPTIONS = {
    "allowed_tags": [
        "build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor",
        "style", "test",
    ],
    "minor_tags": ["feat"],
    "patch_tags": ["fix", "perf"],
}

# `type(scope)!: subject`. The trailing `!` is semantic-release's breaking
# marker; `BREAKING CHANGE:` in the body is the other one.
COMMIT_SUBJECT = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s+(?P<subject>.+)$"
)
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)

ACTIVE_RUN_STATES = {"queued", "in_progress", "waiting", "pending", "requested"}
NON_SUCCESS_CONCLUSIONS = {
    "failure", "cancelled", "skipped", "timed_out", "action_required", "stale",
}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class Report:
    """Alerts are worth waking someone; warnings never are."""

    def __init__(self) -> None:
        self.alerts: list[dict[str, str]] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def alert(self, detector: str, lane: str, headline: str, detail: str) -> None:
        self.alerts.append(
            {"detector": detector, "lane": lane, "headline": headline, "detail": detail}
        )

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def ok(self) -> bool:
        return not self.alerts


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    for required in ("repository", "branch", "lanes"):
        if required not in config:
            raise SystemExit(f"{path}: missing required key {required!r}")
    return config


def load_parser_options(pyproject: Path, report: Report) -> dict[str, list[str]]:
    """Read this repository's ACTUAL semantic-release commit parser options."""
    if tomllib is None:
        report.warn(
            "tomllib is unavailable (Python < 3.11); assuming python-semantic-release "
            "default commit parser options. Run this check on Python 3.11+ for an "
            "exact answer."
        )
        return dict(FALLBACK_PARSER_OPTIONS)
    if not pyproject.is_file():
        report.warn(
            f"{pyproject.name} not found; assuming python-semantic-release default "
            "commit parser options."
        )
        return dict(FALLBACK_PARSER_OPTIONS)
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    options = (
        document.get("tool", {})
        .get("semantic_release", {})
        .get("commit_parser_options", {})
    )
    if not options:
        report.warn(
            "pyproject.toml declares no [tool.semantic_release.commit_parser_options]; "
            "assuming python-semantic-release defaults."
        )
        return dict(FALLBACK_PARSER_OPTIONS)
    resolved = dict(FALLBACK_PARSER_OPTIONS)
    for key in ("allowed_tags", "minor_tags", "patch_tags"):
        if key in options:
            resolved[key] = list(options[key])
    return resolved


# ---------------------------------------------------------------------------
# Commit classification
# ---------------------------------------------------------------------------


def bump_level(message: str, parser_options: dict[str, list[str]]) -> str | None:
    """Return 'major' | 'minor' | 'patch', or None when this commit does not release.

    This is the single most important false-positive control in the file. The
    commits a release itself pushes -- ``chore: release 1.2.1``,
    ``chore(release): reconcile platform manifest``, and the launcher's bare
    ``1.10.0`` -- must all return None, or every repository would report itself
    as having unreleased work for the ~90 seconds after every release, forever.
    """
    lines = message.replace("\r\n", "\n").split("\n")
    subject = lines[0].strip()
    body = "\n".join(lines[1:])
    match = COMMIT_SUBJECT.match(subject)
    if match is None:
        # Not a Conventional Commit: semantic-release ignores it entirely.
        return None
    commit_type = match.group("type").lower()
    if commit_type not in {tag.lower() for tag in parser_options["allowed_tags"]}:
        return None
    if match.group("breaking") or BREAKING_FOOTER.search(body):
        return "major"
    if commit_type in {tag.lower() for tag in parser_options["minor_tags"]}:
        return "minor"
    if commit_type in {tag.lower() for tag in parser_options["patch_tags"]}:
        return "patch"
    return None


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def humanize(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "0 minutes"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def version_key(version: str) -> tuple[int, ...]:
    parts = version.split(".")
    return tuple(int(part) if part.isdigit() else 0 for part in parts)


# ---------------------------------------------------------------------------
# Evaluation (pure: operates only on `state`, so it is testable offline)
# ---------------------------------------------------------------------------


def evaluate(state: dict[str, Any], report: Report) -> Report:
    now = parse_time(state["now"])
    assert now is not None, "state['now'] must be an ISO-8601 timestamp"
    repository = state["repository"]
    parser_options = state["parser_options"]

    for lane in state["lanes"]:
        lane_id = lane["id"]
        grace_unreleased = timedelta(hours=float(lane.get("unreleased_grace_hours", 4)))
        grace_tag = timedelta(hours=float(lane.get("tag_without_release_grace_hours", 3)))
        grace_pypi = timedelta(minutes=float(lane.get("pypi_lag_grace_minutes", 30)))
        remediation = lane.get("remediation", "")

        active_runs = [
            run
            for run in lane.get("runs", [])
            if run.get("status") in ACTIVE_RUN_STATES
        ]
        if active_runs:
            report.note(
                f"[{lane_id}] a release run is in flight "
                f"({', '.join(str(run['id']) for run in active_runs)}); gap detectors "
                "are suppressed for this lane."
            )

        # -- unreleased-work ------------------------------------------------
        if lane.get("tracks_main"):
            _check_unreleased_work(
                report, lane, repository, parser_options, now,
                grace_unreleased, bool(active_runs), remediation,
            )

        # -- tag-without-release --------------------------------------------
        _check_tags_without_release(
            report, lane, repository, now, grace_tag, bool(active_runs), remediation,
        )

        # -- unpublished-release --------------------------------------------
        _check_pypi(report, lane, now, grace_pypi, remediation)

    return report


def _check_unreleased_work(
    report: Report,
    lane: dict[str, Any],
    repository: str,
    parser_options: dict[str, list[str]],
    now: datetime,
    grace: timedelta,
    runs_in_flight: bool,
    remediation: str,
) -> None:
    lane_id = lane["id"]
    commits = lane.get("commits_since_tag") or []
    releasable = []
    for commit in commits:
        level = bump_level(commit["message"], parser_options)
        if level is not None:
            releasable.append({**commit, "level": level})
    if not releasable:
        report.note(
            f"[{lane_id}] {len(commits)} commit(s) since {lane.get('latest_tag')}, "
            "none of which trigger a version bump."
        )
        return

    if runs_in_flight:
        return

    dates = [parse_time(commit.get("date")) for commit in releasable]
    dates = [date for date in dates if date is not None]
    oldest = min(dates) if dates else now
    age = now - oldest

    ci = lane.get("main_ci_state", "unknown")
    if ci != "success":
        # Never tell anyone to release a branch that is not green. But say so
        # loudly once the work is past its grace window, because this is the
        # one path that can silence the detector indefinitely: a main whose CI
        # run was cancelled stays not-green until someone re-runs it.
        message = (
            f"[{lane_id}] {len(releasable)} releasable commit(s) since "
            f"{lane.get('latest_tag')}, oldest {humanize(age)}, but main CI is "
            f"{ci!r}; not reporting an unreleased-work gap for a branch that is "
            "not green."
        )
        if age >= grace:
            report.warn(message)
        else:
            report.note(message)
        return

    if age < grace:
        report.note(
            f"[{lane_id}] {len(releasable)} releasable commit(s) since "
            f"{lane.get('latest_tag')}, oldest {humanize(age)} old; inside the "
            f"{humanize(grace)} grace window."
        )
        return

    highest = "major" if any(c["level"] == "major" for c in releasable) else (
        "minor" if any(c["level"] == "minor" for c in releasable) else "patch"
    )
    lines = [
        f"`main` has been carrying **{len(releasable)} releasable commit(s)** for "
        f"**{humanize(age)}** past `{lane.get('latest_tag')}`. main CI is green and "
        "no release run is in flight, so nothing is going to publish these on its own.",
        "",
        f"Next version would be a **{highest}** bump.",
        "",
        "| commit | bump | age | subject |",
        "| --- | --- | --- | --- |",
    ]
    for commit in releasable:
        date = parse_time(commit.get("date"))
        commit_age = humanize(now - date) if date else "unknown"
        subject = commit["message"].split("\n")[0].replace("|", "\\|")
        lines.append(
            f"| [`{commit['sha'][:8]}`](https://github.com/{repository}/commit/"
            f"{commit['sha']}) | {commit['level']} | {commit_age} | {subject} |"
        )
    seen_runs = lane.get("runs") or []
    if not seen_runs:
        lines += [
            "",
            "**No release workflow run has been recorded for this lane at all.** "
            "The publish did not fail; it never started.",
        ]
    else:
        latest = seen_runs[0]
        lines += [
            "",
            f"Most recent release run: [`{latest['id']}`](https://github.com/"
            f"{repository}/actions/runs/{latest['id']}) — "
            f"{latest.get('status')}/{latest.get('conclusion')}.",
        ]
    if remediation:
        lines += ["", "Publish with:", "", "```sh", remediation, "```"]
    report.alert(
        "unreleased-work",
        lane_id,
        f"{len(releasable)} releasable commit(s) unpublished for {humanize(age)}",
        "\n".join(lines),
    )


def _check_tags_without_release(
    report: Report,
    lane: dict[str, Any],
    repository: str,
    now: datetime,
    grace: timedelta,
    runs_in_flight: bool,
    remediation: str,
) -> None:
    lane_id = lane["id"]
    orphans = lane.get("tags_without_release") or []
    if not orphans:
        return
    acknowledged = lane.get("acknowledged_tags") or {}
    pypi = lane.get("pypi") or {}
    published_versions = set(pypi.get("versions") or []) if pypi.get("reachable") else None
    pattern = re.compile(lane.get("tag_pattern", "")) if lane.get("tag_pattern") else None

    reportable = []
    for tag in orphans:
        if tag["name"] in acknowledged:
            report.note(
                f"[{lane_id}] tag {tag['name']} has no release object; acknowledged: "
                f"{acknowledged[tag['name']]}"
            )
            continue
        # What matters is whether the artifact is installable, not whether a
        # GitHub release object exists. A tag whose version is already on PyPI
        # shipped; a missing release page is cosmetic and permanently
        # unactionable, and alerting on it forever is how a guard stops being
        # read. Say it once as a warning instead.
        if published_versions is not None and pattern is not None:
            match = pattern.match(tag["name"])
            if match and match.group(1) in published_versions:
                report.warn(
                    f"[{lane_id}] tag {tag['name']} has no GitHub release object, but "
                    f"{lane.get('pypi_package')} {match.group(1)} is installable from "
                    "PyPI; the artifact shipped and only the release page is absent."
                )
                continue
        created = parse_time(tag.get("date"))
        if created is not None and now - created < grace:
            report.note(
                f"[{lane_id}] tag {tag['name']} has no release yet but is only "
                f"{humanize(now - created)} old; inside the {humanize(grace)} "
                "publish window."
            )
            continue
        if tag.get("run_status") in ACTIVE_RUN_STATES:
            report.note(
                f"[{lane_id}] tag {tag['name']} has no release yet; its run is "
                f"{tag['run_status']} (a waiting run is a human approval gate, "
                "not a failure)."
            )
            continue
        if tag.get("run_id") is None and runs_in_flight:
            # No run is attributed to this tag yet AND the lane has one in
            # flight: most likely it IS this tag's run, not a publish that
            # never started. Wait for it to conclude before saying anything.
            report.note(
                f"[{lane_id}] tag {tag['name']} has no attributed run yet, but a "
                "run is in flight for this lane; deferring."
            )
            continue
        reportable.append(tag)
    if not reportable:
        return

    lines = [
        f"**{len(reportable)} tag(s) in the `{lane_id}` lane exist with no GitHub "
        "release object.** The tag is immutable and public; the artifacts it "
        "promises were never published.",
        "",
        "| tag | age | release run | outcome |",
        "| --- | --- | --- | --- |",
    ]
    for tag in reportable:
        created = parse_time(tag.get("date"))
        age = humanize(now - created) if created else "unknown"
        run_id = tag.get("run_id")
        if run_id:
            run_cell = (
                f"[`{run_id}`](https://github.com/{repository}/actions/runs/{run_id})"
            )
            outcome = f"`{tag.get('run_conclusion') or tag.get('run_status')}`"
        else:
            run_cell = "—"
            outcome = "**workflow never started**"
        lines.append(f"| `{tag['name']}` | {age} | {run_cell} | {outcome} |")
    lines += [
        "",
        "`cancelled` and `skipped` are the outcomes no `if: failure()` step can "
        "see. A run cancelled while waiting on an environment approval gate lands "
        "here.",
    ]
    if remediation:
        lines += ["", "Re-run the publish with:", "", "```sh", remediation, "```"]
    report.alert(
        "tag-without-release",
        lane_id,
        f"{len(reportable)} tag(s) published no release",
        "\n".join(lines),
    )


def _check_pypi(
    report: Report,
    lane: dict[str, Any],
    now: datetime,
    grace: timedelta,
    remediation: str,
) -> None:
    lane_id = lane["id"]
    package = lane.get("pypi_package")
    if not package:
        return
    pypi = lane.get("pypi") or {}
    if not pypi.get("reachable", False):
        report.warn(
            f"[{lane_id}] could not query PyPI for {package} "
            f"({pypi.get('error', 'unknown error')}); skipping the publication "
            "comparison. An index outage is not evidence of a missing release."
        )
        return
    tag_version = lane.get("latest_tag_version")
    if not tag_version:
        return
    published = set(pypi.get("versions") or [])
    if tag_version in published:
        report.note(f"[{lane_id}] {package} {tag_version} is on PyPI.")
        return
    tagged_at = parse_time(lane.get("latest_tag_date"))
    if tagged_at is not None and now - tagged_at < grace:
        report.note(
            f"[{lane_id}] {package} {tag_version} is not on PyPI yet but the tag is "
            f"only {humanize(now - tagged_at)} old; inside the {humanize(grace)} "
            "CDN/propagation window."
        )
        return
    age = humanize(now - tagged_at) if tagged_at else "unknown"
    latest_published = pypi.get("latest")
    lines = [
        f"Tag `{lane.get('latest_tag')}` was created **{age}** ago, but "
        f"`{package} {tag_version}` is **not installable from PyPI**. The newest "
        f"version PyPI serves is `{latest_published}`.",
        "",
        "Anyone running `pip install` gets the older code. If the tag was created "
        "for a security-relevant fix, that fix is not shipped.",
    ]
    if remediation:
        lines += ["", "```sh", remediation, "```"]
    report.alert(
        "unpublished-release",
        lane_id,
        f"{package} {tag_version} is tagged but not on PyPI ({age})",
        "\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Live state collection
# ---------------------------------------------------------------------------


class GitHubUnavailable(Exception):
    pass


def github_get(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{GITHUB_API}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GitHubUnavailable(f"GET {path}: {exc}") from exc


def github_paginate(path: str, params: dict[str, Any] | None = None) -> list[Any]:
    collected: list[Any] = []
    previous: list[Any] | None = None
    page = 1
    while page <= 10:
        payload = github_get(path, {**(params or {}), "per_page": 100, "page": page})
        if not isinstance(payload, list) or not payload:
            break
        # /git/matching-refs/ ignores page and returns the complete set every
        # time. Following the naive loop there yields ten identical copies and
        # reports the same orphaned tag ten times -- a guard that inflates its
        # own findings is the first step to being ignored.
        if payload == previous:
            break
        collected.extend(payload)
        if len(payload) < 100:
            break
        previous = payload
        page += 1
    return collected


def fetch_pypi(package: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(
            PYPI_URL_TEMPLATE.format(package=package), timeout=HTTP_TIMEOUT_SECONDS
        ) as response:
            document = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"reachable": False, "error": str(exc)}
    return {
        "reachable": True,
        "latest": document["info"]["version"],
        "versions": sorted(document.get("releases", {}).keys()),
    }


def collect_state(config: dict[str, Any], report: Report) -> dict[str, Any]:
    repository = config["repository"]
    branch = config["branch"]
    parser_options = load_parser_options(ROOT / "pyproject.toml", report)

    releases = github_paginate(f"/repos/{repository}/releases")
    released_tags = {release["tag_name"] for release in releases}
    refs = github_paginate(f"/repos/{repository}/git/matching-refs/tags/")
    tag_names = sorted({ref["ref"].removeprefix("refs/tags/") for ref in refs})

    ci_state = _main_ci_state(repository, branch, config.get("ci_workflows") or [])

    lanes: list[dict[str, Any]] = []
    for lane_config in config["lanes"]:
        pattern = re.compile(lane_config["tag_pattern"])
        matched = []
        for name in tag_names:
            match = pattern.match(name)
            if match:
                matched.append((version_key(match.group(1)), name, match.group(1)))
        matched.sort()

        runs = _lane_runs(repository, lane_config.get("release_workflows") or [])

        orphans = []
        for _key, name, _version in matched:
            if name in released_tags:
                continue
            orphans.append(_describe_orphan_tag(repository, name, runs))

        lane_state: dict[str, Any] = {
            "id": lane_config["id"],
            "name": lane_config.get("name", lane_config["id"]),
            "tag_pattern": lane_config["tag_pattern"],
            "acknowledged_tags": lane_config.get("acknowledged_tags", {}),
            "tracks_main": bool(lane_config.get("tracks_main")),
            "unreleased_grace_hours": lane_config.get("unreleased_grace_hours", 4),
            "tag_without_release_grace_hours": lane_config.get(
                "tag_without_release_grace_hours", 3
            ),
            "pypi_lag_grace_minutes": lane_config.get("pypi_lag_grace_minutes", 30),
            "remediation": lane_config.get("remediation", ""),
            "pypi_package": lane_config.get("pypi_package"),
            "tags_without_release": orphans,
            "runs": runs[:10],
            "main_ci_state": ci_state,
        }

        if matched:
            _key, latest_name, latest_version = matched[-1]
            lane_state["latest_tag"] = latest_name
            lane_state["latest_tag_version"] = latest_version
            lane_state["latest_tag_date"] = _tag_date(repository, latest_name)
            if lane_config.get("tracks_main"):
                lane_state["commits_since_tag"] = _commits_since(
                    repository, latest_name, branch
                )
        else:
            lane_state["commits_since_tag"] = []

        if lane_config.get("pypi_package"):
            lane_state["pypi"] = fetch_pypi(lane_config["pypi_package"])
        lanes.append(lane_state)

    return {
        "repository": repository,
        "branch": branch,
        "now": datetime.now(timezone.utc).isoformat(),
        "parser_options": parser_options,
        "lanes": lanes,
    }


def _main_ci_state(repository: str, branch: str, workflows: Iterable[str]) -> str:
    head = github_get(f"/repos/{repository}/commits/{branch}")
    head_sha = head["sha"]
    states: list[str] = []
    for workflow in workflows:
        payload = github_get(
            f"/repos/{repository}/actions/workflows/{workflow}/runs",
            {"head_sha": head_sha, "per_page": 20},
        )
        runs = payload.get("workflow_runs") or []
        if not runs:
            states.append("unknown")
            continue
        runs.sort(key=lambda run: run.get("created_at") or "")
        latest = runs[-1]
        if latest.get("status") != "completed":
            states.append("pending")
        elif latest.get("conclusion") == "success":
            states.append("success")
        else:
            states.append(str(latest.get("conclusion")))
    if not states:
        return "unknown"
    if all(state == "success" for state in states):
        return "success"
    if "pending" in states:
        return "pending"
    if "unknown" in states:
        return "unknown"
    return ",".join(sorted(set(states)))


def _lane_runs(repository: str, workflows: Iterable[str]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for workflow in workflows:
        payload = github_get(
            f"/repos/{repository}/actions/workflows/{workflow}/runs", {"per_page": 50}
        )
        for run in payload.get("workflow_runs") or []:
            runs.append(
                {
                    "id": run["id"],
                    "workflow": workflow,
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "head_branch": run.get("head_branch"),
                    "head_sha": run.get("head_sha"),
                    "created_at": run.get("created_at"),
                }
            )
    runs.sort(key=lambda run: run.get("created_at") or "", reverse=True)
    return runs


def _describe_orphan_tag(
    repository: str, name: str, runs: list[dict[str, Any]]
) -> dict[str, Any]:
    tag = {"name": name, "date": _tag_date(repository, name)}
    for run in runs:
        if run.get("head_branch") == name:
            tag["run_id"] = run["id"]
            tag["run_status"] = run.get("status")
            tag["run_conclusion"] = run.get("conclusion")
            break
    return tag


def _tag_date(repository: str, name: str) -> str | None:
    try:
        commit = github_get(f"/repos/{repository}/commits/{name}")
    except GitHubUnavailable:
        return None
    committer = (commit.get("commit") or {}).get("committer") or {}
    return committer.get("date")


def _commits_since(repository: str, base: str, head: str) -> list[dict[str, Any]]:
    payload = github_get(f"/repos/{repository}/compare/{base}...{head}")
    commits = []
    for commit in payload.get("commits") or []:
        message = (commit.get("commit") or {}).get("message") or ""
        committer = (commit.get("commit") or {}).get("committer") or {}
        commits.append(
            {
                "sha": commit["sha"],
                "message": message,
                "date": committer.get("date"),
            }
        )
    return commits


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_markdown(state: dict[str, Any], report: Report, run_url: str | None) -> str:
    lines = [
        "Automated release-health check. This issue is **rewritten in place** on "
        "every run and **closed automatically** once every gap below is closed; "
        "it is never duplicated.",
        "",
        f"Last evaluated: `{state['now']}`" + (f" ([run]({run_url}))" if run_url else ""),
        "",
    ]
    for alert in report.alerts:
        lines += [
            f"## `{alert['detector']}` — {alert['headline']}",
            "",
            alert["detail"],
            "",
        ]
    if report.warnings:
        lines += ["## Warnings (not failures)", ""]
        lines += [f"- {warning}" for warning in report.warnings]
        lines += [""]
    lines += [
        "<details><summary>Everything the check considered and cleared</summary>",
        "",
    ]
    lines += [f"- {note}" for note in report.notes] or ["- (nothing)"]
    lines += [
        "",
        "</details>",
        "",
        "Reproduce locally with `python scripts/check_release_health.py`.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test: prove the detectors fire, and prove they stay quiet
# ---------------------------------------------------------------------------


def _state(**overrides: Any) -> dict[str, Any]:
    now = "2026-07-27T20:00:00+00:00"
    lane = {
        "id": "python",
        "tag_pattern": r"^v(\d+\.\d+\.\d+)$",
        "acknowledged_tags": {},
        "tracks_main": True,
        "unreleased_grace_hours": 4,
        "tag_without_release_grace_hours": 3,
        "pypi_lag_grace_minutes": 30,
        "remediation": "gh workflow run release.yml",
        "pypi_package": "openadapt-capture",
        "latest_tag": "v1.2.0",
        "latest_tag_version": "1.2.0",
        "latest_tag_date": "2026-07-26T23:34:00+00:00",
        "commits_since_tag": [],
        "tags_without_release": [],
        "runs": [],
        "main_ci_state": "success",
        "pypi": {"reachable": True, "latest": "1.2.0", "versions": ["1.1.0", "1.2.0"]},
    }
    lane.update(overrides.pop("lane", {}))
    state = {
        "repository": "OpenAdaptAI/openadapt-capture",
        "branch": "main",
        "now": now,
        "parser_options": dict(FALLBACK_PARSER_OPTIONS),
        "lanes": [lane],
    }
    state.update(overrides)
    return state


def _detectors(state: dict[str, Any]) -> set[str]:
    return {alert["detector"] for alert in evaluate(state, Report()).alerts}


def self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {name}")
        if not condition:
            failures.append(name)

    print("A. It FIRES on the states it exists to catch")

    # The exact openadapt-capture miss: a `fix:` merged, green, unreleased.
    capture_miss = _state(
        lane={
            "commits_since_tag": [
                {
                    "sha": "27ccd2625ddb6d76f81d4d53c52793366aafdddb",
                    "message": "fix(audio): make narration capture on-device only "
                    "and fail closed (#51)",
                    "date": "2026-07-27T14:35:42+00:00",
                }
            ],
        }
    )
    # Five hours later, still unreleased.
    capture_miss["now"] = "2026-07-27T19:35:42+00:00"
    check("unreleased fix: commit, green main, past grace -> alert",
          "unreleased-work" in _detectors(capture_miss))

    breaking = _state(
        lane={
            "commits_since_tag": [
                {"sha": "a" * 40, "message": "refactor!: drop the hosted recognizer",
                 "date": "2026-07-20T00:00:00+00:00"}
            ]
        }
    )
    check("`type!:` breaking change -> alert",
          "unreleased-work" in _detectors(breaking))

    footer = _state(
        lane={
            "commits_since_tag": [
                {"sha": "b" * 40,
                 "message": "refactor: rework storage\n\nBREAKING CHANGE: paths moved",
                 "date": "2026-07-20T00:00:00+00:00"}
            ]
        }
    )
    check("`BREAKING CHANGE:` footer -> alert",
          "unreleased-work" in _detectors(footer))

    # The exact openadapt-desktop misses: cancelled at the approval gate, a
    # failed matrix leg, and a workflow that never started.
    desktop_miss = _state(
        lane={
            "tracks_main": False,
            "tag_pattern": r"^desktop-v(\d+\.\d+\.\d+)$",
            "pypi_package": None,
            "pypi": {},
            "tags_without_release": [
                {"name": "desktop-v0.8.0", "date": "2026-07-21T03:43:31+00:00",
                 "run_id": 29799291231, "run_status": "completed",
                 "run_conclusion": "cancelled"},
                {"name": "desktop-v0.12.0", "date": "2026-07-25T18:35:18+00:00",
                 "run_id": 30169968004, "run_status": "completed",
                 "run_conclusion": "failure"},
                {"name": "desktop-v0.1.0", "date": "2026-07-10T00:00:00+00:00"},
            ],
        }
    )
    check("tag with a cancelled/failed/never-started publish -> alert",
          "tag-without-release" in _detectors(desktop_miss))

    skipped = _state(
        lane={
            "tracks_main": False,
            "tag_pattern": r"^desktop-v(\d+\.\d+\.\d+)$",
            "pypi_package": None,
            "pypi": {},
            "tags_without_release": [
                {"name": "desktop-v0.7.0", "date": "2026-07-20T04:43:42+00:00",
                 "run_id": 29756479649, "run_status": "completed",
                 "run_conclusion": "skipped"},
            ],
        }
    )
    check("conclusion `skipped` (invisible to `if: failure()`) -> alert",
          "tag-without-release" in _detectors(skipped))

    unpublished = _state(
        lane={
            "latest_tag": "v1.2.1",
            "latest_tag_version": "1.2.1",
            "latest_tag_date": "2026-07-27T15:17:40+00:00",
            "pypi": {"reachable": True, "latest": "1.2.0",
                     "versions": ["1.1.0", "1.2.0"]},
        }
    )
    unpublished["now"] = "2026-07-27T23:17:40+00:00"
    check("tag exists but PyPI never got the version -> alert",
          "unpublished-release" in _detectors(unpublished))

    print("B. It stays QUIET on every transient this repository actually produces")

    # ~90s window: semantic-release pushes the release commit, then the
    # launcher's reconcile job pushes a second one.
    release_window = _state(
        lane={
            "latest_tag": "v1.2.1",
            "latest_tag_version": "1.2.1",
            "commits_since_tag": [
                {"sha": "c" * 40, "message": "chore: release 1.2.1",
                 "date": "2026-07-27T15:17:00+00:00"},
                {"sha": "d" * 40,
                 "message": "chore(release): reconcile platform manifest",
                 "date": "2026-07-27T15:18:30+00:00"},
            ],
            "pypi": {"reachable": True, "latest": "1.2.1",
                     "versions": ["1.2.0", "1.2.1"]},
        }
    )
    check("semantic-release + reconcile commits are `chore` -> quiet",
          _detectors(release_window) == set())

    # The launcher's own release commit subject is a bare version string.
    bare_version = _state(
        lane={
            "latest_tag": "v1.10.0",
            "latest_tag_version": "1.10.0",
            "commits_since_tag": [
                {"sha": "e" * 40,
                 "message": "1.10.0\n\nAutomatically generated by python-semantic-release",
                 "date": "2026-07-20T00:00:00+00:00"}
            ],
            "pypi": {"reachable": True, "latest": "1.10.0", "versions": ["1.10.0"]},
        }
    )
    check("launcher's bare `1.10.0` release commit is not a bump -> quiet",
          _detectors(bare_version) == set())

    # Documentation and CI merges are allowed_tags but bump nothing.
    non_bumping = _state(
        lane={
            "commits_since_tag": [
                {"sha": "f" * 40, "message": "docs: clarify the capture boundary",
                 "date": "2026-07-20T00:00:00+00:00"},
                {"sha": "0" * 40, "message": "ci: reduce launcher PR matrix cost (#1061)",
                 "date": "2026-07-20T00:00:00+00:00"},
                {"sha": "1" * 40, "message": "chore(deps): update release actions (#56)",
                 "date": "2026-07-20T00:00:00+00:00"},
            ]
        }
    )
    check("docs/ci/chore(deps) merges -> quiet",
          _detectors(non_bumping) == set())

    fresh = _state(
        lane={
            "commits_since_tag": [
                {"sha": "2" * 40, "message": "fix: correct a boundary check",
                 "date": "2026-07-27T19:00:00+00:00"}
            ]
        }
    )
    check("a fix merged 1h ago is inside the 4h grace -> quiet",
          _detectors(fresh) == set())

    in_flight = _state(
        lane={
            "commits_since_tag": [
                {"sha": "3" * 40, "message": "fix: correct a boundary check",
                 "date": "2026-07-20T00:00:00+00:00"}
            ],
            "runs": [{"id": 1, "status": "in_progress", "conclusion": None}],
        }
    )
    check("a release run is in flight -> quiet",
          _detectors(in_flight) == set())

    red_main = _state(
        lane={
            "commits_since_tag": [
                {"sha": "4" * 40, "message": "fix: correct a boundary check",
                 "date": "2026-07-20T00:00:00+00:00"}
            ],
            "main_ci_state": "failure",
        }
    )
    check("main is red -> quiet (a red main is its own signal)",
          _detectors(red_main) == set())

    pending_main = _state(
        lane={
            "commits_since_tag": [
                {"sha": "5" * 40, "message": "fix: correct a boundary check",
                 "date": "2026-07-20T00:00:00+00:00"}
            ],
            "main_ci_state": "pending",
        }
    )
    check("main CI still running -> quiet",
          _detectors(pending_main) == set())

    cdn_lag = _state(
        lane={
            "latest_tag": "v1.2.1",
            "latest_tag_version": "1.2.1",
            "latest_tag_date": "2026-07-27T19:50:00+00:00",
            "pypi": {"reachable": True, "latest": "1.2.0",
                     "versions": ["1.1.0", "1.2.0"]},
        }
    )
    check("PyPI CDN has not propagated yet (10m) -> quiet",
          _detectors(cdn_lag) == set())

    pypi_down = _state(
        lane={
            "latest_tag": "v1.2.1",
            "latest_tag_version": "1.2.1",
            "latest_tag_date": "2026-07-01T00:00:00+00:00",
            "pypi": {"reachable": False, "error": "timed out"},
        }
    )
    down_report = evaluate(pypi_down, Report())
    check("PyPI unreachable -> warning, never an alert",
          down_report.ok and len(down_report.warnings) == 1)

    approval_gate = _state(
        lane={
            "tracks_main": False,
            "tag_pattern": r"^desktop-v(\d+\.\d+\.\d+)$",
            "pypi_package": None,
            "pypi": {},
            "tags_without_release": [
                {"name": "desktop-v0.15.0", "date": "2026-07-20T00:00:00+00:00",
                 "run_id": 999, "run_status": "waiting", "run_conclusion": None},
            ],
        }
    )
    check("tag awaiting a human `native-release` approval -> quiet",
          _detectors(approval_gate) == set())

    building = _state(
        lane={
            "tracks_main": False,
            "tag_pattern": r"^desktop-v(\d+\.\d+\.\d+)$",
            "pypi_package": None,
            "pypi": {},
            "tags_without_release": [
                {"name": "desktop-v0.15.0", "date": "2026-07-27T18:30:00+00:00"},
            ],
        }
    )
    check("tag pushed 90m ago, installers still building -> quiet",
          _detectors(building) == set())

    # openadapt-capture v0.1.0: the very first, hand-made release. The wheel is
    # on PyPI; only the GitHub release page was never created. Permanently
    # unactionable, so it must never be an alert.
    cosmetic = _state(
        lane={
            "tags_without_release": [
                {"name": "v0.1.0", "date": "2025-12-13T02:42:01+00:00"}
            ],
            "pypi": {"reachable": True, "latest": "1.2.0",
                     "versions": ["0.1.0", "1.1.0", "1.2.0"]},
        }
    )
    cosmetic_report = evaluate(cosmetic, Report())
    check("tag whose artifact IS on PyPI -> warning, never an alert",
          cosmetic_report.ok and len(cosmetic_report.warnings) == 1)

    ack = _state(
        lane={
            "acknowledged_tags": {"v0.1.0": "hand-made first release"},
            "tags_without_release": [
                {"name": "v0.1.0", "date": "2025-12-13T02:42:01+00:00"}
            ],
        }
    )
    check("explicitly acknowledged historical tag -> quiet",
          _detectors(ack) == set())

    superseded_cancel = _state(
        lane={
            "latest_tag": "v1.10.0",
            "latest_tag_version": "1.10.0",
            "commits_since_tag": [],
            "runs": [
                {"id": 30277764097, "status": "completed", "conclusion": "success"},
                {"id": 30276970325, "status": "completed", "conclusion": "cancelled"},
                {"id": 30275153205, "status": "completed", "conclusion": "cancelled"},
            ],
            "pypi": {"reachable": True, "latest": "1.10.0", "versions": ["1.10.0"]},
        }
    )
    check(
        "launcher concurrency cancellations that left no gap -> quiet "
        "(the exact runs 30275153205/30276970325)",
        _detectors(superseded_cancel) == set(),
    )

    print("C. Commit parsing honours THIS repository's configured tags")
    options = {"allowed_tags": ["feat", "fix", "chore"], "minor_tags": ["feat"],
               "patch_tags": ["fix"]}
    check("`perf:` bumps only when patch_tags says so",
          bump_level("perf: speed up capture", options) is None
          and bump_level("perf: speed up capture", FALLBACK_PARSER_OPTIONS) == "patch")
    check("`feat(scope): ...` -> minor",
          bump_level("feat(cli): add a flag", options) == "minor")
    check("Merge commits and free text -> no bump",
          bump_level("Merge pull request #1 from x", options) is None)

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} scenario(s): {failures}")
        return 1
    print("SELF-TEST PASSED: every detector fires on its evidence case and stays "
          "quiet on every enumerated transient.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--self-test", action="store_true",
                        help="Run the offline detector scenarios and exit.")
    parser.add_argument("--state-file", type=Path,
                        help="Evaluate a recorded state instead of querying GitHub.")
    parser.add_argument("--dump-state", type=Path,
                        help="Write the collected live state to this path.")
    parser.add_argument("--markdown", type=Path,
                        help="Write the issue body to this path.")
    parser.add_argument("--github-output", type=Path,
                        help="Append alert=true|false for the workflow to read.")
    parser.add_argument("--run-url", help="URL of the run producing this report.")
    parser.add_argument("--fail-on-alert", action="store_true",
                        help="Exit non-zero when an alert fires (default: exit 0 and "
                             "let the workflow file the issue).")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    report = Report()
    if args.state_file:
        state = json.loads(args.state_file.read_text(encoding="utf-8"))
        state.setdefault("parser_options", dict(FALLBACK_PARSER_OPTIONS))
        state.setdefault("now", datetime.now(timezone.utc).isoformat())
    else:
        config = load_config(args.config)
        try:
            state = collect_state(config, report)
        except GitHubUnavailable as exc:
            # An unreachable API is not evidence of a missing release.
            print(f"WARNING: {exc}; skipping this release-health evaluation.",
                  file=sys.stderr)
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8") as handle:
                    handle.write("alert=false\n")
            return 0

    if args.dump_state:
        args.dump_state.write_text(json.dumps(state, indent=2), encoding="utf-8")

    evaluate(state, report)

    for warning in report.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    for note in report.notes:
        print(f"ok: {note}")
    for alert in report.alerts:
        print(f"ALERT [{alert['detector']}/{alert['lane']}] {alert['headline']}",
              file=sys.stderr)

    if args.markdown:
        args.markdown.write_text(
            render_markdown(state, report, args.run_url), encoding="utf-8"
        )
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"alert={'true' if report.alerts else 'false'}\n")

    if report.alerts:
        print(f"\n{len(report.alerts)} release-health alert(s).")
        return 1 if args.fail_on_alert else 0
    print("\nNo release-health alerts: every release lane is published and current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
