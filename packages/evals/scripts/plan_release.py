#!/usr/bin/env python3
"""Derive the next release version and changelog section from merged commits.

The release workflow refuses to guess a version.  It requires `pyproject.toml`
and `CHANGELOG.md` to already carry the exact version being published, so some
step has to compute that version and write those files.  This module is that
step, and it is deliberately a pure function of the git history plus the
release policy already declared in `pyproject.toml`.

It replaces the version arithmetic that `python-semantic-release` used to do.
That tool cannot run here: GitPython 3.1.60 removed `Actor.name_email_regex`,
which every released version reads while loading its config, and the upstream
fix is unmerged.  The arithmetic is small enough to own.

Nothing here publishes, tags, pushes, or merges.  It writes two files in the
working tree and prints what it decided.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_URL = "https://github.com/OpenAdaptAI/openadapt-evals"

_SEMVER_TAG = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
_SUBJECT = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[^)]*)\))?"
    r"(?P<breaking>!)?"
    r": (?P<summary>.+)$"
)
_HEADING = re.compile(r"(?m)^## v\d+\.\d+\.\d+ \(\d{4}-\d{2}-\d{2}\)$")

MINOR_TYPES = frozenset({"feat"})
PATCH_TYPES = frozenset({"fix", "perf"})
SECTIONS = (
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("perf", "Performance Improvements"),
    ("refactor", "Refactoring"),
    ("docs", "Documentation"),
    ("build", "Build System"),
    ("ci", "Continuous Integration"),
    ("test", "Testing"),
    ("style", "Styles"),
    ("chore", "Chores"),
)


class ReleasePlanError(Exception):
    """Raised when the history or the policy cannot produce an exact version."""


@dataclass(frozen=True)
class Change:
    """One parsed conventional commit."""

    commit: str
    type: str
    scope: str
    summary: str
    breaking: bool


@dataclass(frozen=True)
class ReleasePlan:
    """The exact next version and the section that documents it."""

    previous_version: str
    next_version: str
    changes: tuple[Change, ...]
    staged: bool = False

    @property
    def released(self) -> bool:
        return self.next_version != self.previous_version


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _policy(root: Path) -> tuple[bool, bool]:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    return (
        "major_on_zero = true" in text,
        "allow_zero_version = true" in text,
    )


def project_version(root: Path = ROOT) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version = "(\d+\.\d+\.\d+)"$', text)
    if match is None:
        raise ReleasePlanError("pyproject.toml has no exact project version")
    return match.group(1)


def latest_tag(root: Path = ROOT) -> str | None:
    tags = [tag for tag in _git(root, "tag", "--list", "v*").splitlines() if _SEMVER_TAG.match(tag)]
    if not tags:
        return None
    return max(tags, key=lambda tag: tuple(int(part) for part in tag[1:].split(".")))


def changes_since(reference: str | None, root: Path = ROOT) -> tuple[Change, ...]:
    span = f"{reference}..HEAD" if reference else "HEAD"
    raw = _git(root, "log", span, "--no-merges", "--format=%H%x1f%s")
    changes: list[Change] = []
    for line in raw.splitlines():
        if not line:
            continue
        commit, _, subject = line.partition("\x1f")
        match = _SUBJECT.match(subject)
        if match is None:
            continue
        changes.append(
            Change(
                commit=commit,
                type=match.group("type"),
                scope=match.group("scope") or "",
                summary=match.group("summary").strip(),
                breaking=bool(match.group("breaking")),
            )
        )
    return tuple(changes)


def next_version(current: str, changes: tuple[Change, ...], *, major_on_zero: bool) -> str:
    major, minor, patch = (int(part) for part in current.split("."))
    breaking = any(change.breaking for change in changes)
    feature = any(change.type in MINOR_TYPES for change in changes)
    fix = any(change.type in PATCH_TYPES for change in changes)
    if breaking and (major > 0 or major_on_zero):
        return f"{major + 1}.0.0"
    if breaking or feature:
        return f"{major}.{minor + 1}.0"
    if fix:
        return f"{major}.{minor}.{patch + 1}"
    return current


def plan(root: Path = ROOT) -> ReleasePlan:
    major_on_zero, allow_zero_version = _policy(root)
    current = project_version(root)
    tag = latest_tag(root)
    if tag is not None and tag[1:] != current:
        return ReleasePlan(
            previous_version=current,
            next_version=current,
            changes=(),
            staged=True,
        )
    if current.startswith("0.") and not allow_zero_version:
        raise ReleasePlanError("pyproject.toml is on 0.x but allow_zero_version is not set")
    changes = changes_since(tag, root)
    return ReleasePlan(
        previous_version=current,
        next_version=next_version(current, changes, major_on_zero=major_on_zero),
        changes=changes,
    )


def render_section(release: ReleasePlan, *, released_on: date) -> str:
    lines = [f"## v{release.next_version} ({released_on.isoformat()})", ""]
    for kind, heading in SECTIONS:
        entries = [change for change in release.changes if change.type == kind]
        if not entries:
            continue
        lines.extend([f"### {heading}", ""])
        for change in entries:
            summary = change.summary[:1].upper() + change.summary[1:]
            scope = f"**{change.scope}**: " if change.scope else ""
            short = change.commit[:7]
            link = f"[`{short}`]({REPOSITORY_URL}/commit/{change.commit})"
            lines.append(f"- {scope}{summary} ({link})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write(release: ReleasePlan, *, released_on: date, root: Path = ROOT) -> None:
    if not release.released:
        raise ReleasePlanError("no releasable change since the last tag")
    metadata_path = root / "pyproject.toml"
    metadata = metadata_path.read_text(encoding="utf-8")
    stamped = metadata.replace(
        f'version = "{release.previous_version}"',
        f'version = "{release.next_version}"',
        1,
    )
    if stamped == metadata:
        raise ReleasePlanError("pyproject.toml version could not be stamped")
    metadata_path.write_text(stamped, encoding="utf-8")

    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    heading = _HEADING.search(changelog)
    insertion = heading.start() if heading else len(changelog)
    section = render_section(release, released_on=released_on)
    changelog_path.write_text(
        changelog[:insertion] + section + "\n\n" + changelog[insertion:],
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--released-on", default=None)
    arguments = parser.parse_args(argv)
    try:
        release = plan()
    except (ReleasePlanError, subprocess.CalledProcessError) as error:
        print(f"release plan refused: {error}", file=sys.stderr)
        return 2
    print(f"previous={release.previous_version}")
    print(f"next={release.next_version}")
    print(f"released={'true' if release.released else 'false'}")
    print(f"staged={'true' if release.staged else 'false'}")
    print(f"changes={len(release.changes)}")
    if arguments.write and release.released:
        released_on = (
            date.fromisoformat(arguments.released_on) if arguments.released_on else date.today()
        )
        write(release, released_on=released_on)
        print("wrote pyproject.toml and CHANGELOG.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
