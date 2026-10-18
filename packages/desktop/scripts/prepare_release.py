#!/usr/bin/env python3
"""Prepare an openadapt-desktop release in the pull request that ships it.

In the OpenAdapt monorepo, Desktop has no bot version commit. The pull request
that should ship sets the version and the changelog itself, and
.github/workflows/desktop-release.yml publishes that version once it merges.
This script makes that one step:

    python scripts/prepare_release.py            # bump from the commits
    python scripts/prepare_release.py --bump minor
    python scripts/prepare_release.py --version 0.21.0

It reads the conventional commits under packages/desktop since the version
last changed (feat -> minor, fix or perf -> patch; a breaking change is minor
while the version is 0.x), then writes the new version to pyproject.toml,
openadapt_desktop/__init__.py, uv.lock and the five native installer version
files, and adds a dated section at the top of CHANGELOG.md.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_release_consistency  # noqa: E402
import native_release  # noqa: E402

VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?: (?P<text>.+)$")
LEVELS = ("patch", "minor", "major")
SECTIONS = (("feat", "Features"), ("fix", "Bug Fixes"), ("perf", "Performance Improvements"))
# Paths (relative to packages/desktop) that never reach the wheel, the sdist, or
# an installer. A feat, fix, or perf commit confined to them needs no release.
# Every other path counts as shipped, so an unlisted one fails closed.
NOT_SHIPPED_PREFIXES = ("tests/", "docs/", ".github/")
NOT_SHIPPED_SUFFIXES = (".md",)


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str
    paths: tuple[str, ...] = ()

    @property
    def ships(self) -> bool:
        """Whether the commit changes anything that reaches a release."""

        return not self.paths or any(
            not path.startswith(NOT_SHIPPED_PREFIXES) and not path.endswith(NOT_SHIPPED_SUFFIXES)
            for path in self.paths
        )

    @property
    def parsed(self) -> re.Match[str] | None:
        return SUBJECT.match(self.subject)

    @property
    def breaking(self) -> bool:
        match = self.parsed
        return bool(match and match.group("bang")) or "BREAKING CHANGE:" in self.body


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def parse_version(version: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"version must be X.Y.Z, got {version!r}")
    return int(match[1]), int(match[2]), int(match[3])


def bump(version: str, level: str) -> str:
    major, minor, patch = parse_version(version)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump level {level!r}")


def commits_between(root: Path, base: str, head: str = "HEAD") -> list[Commit]:
    """Return non-merge commits in ``base..head`` that touch this package."""

    raw = _git(
        root,
        "log",
        "--no-merges",
        "--relative",
        "--name-only",
        "--format=%x1e%H%x00%s%x00%b%x00",
        f"{base}..{head}",
        "--",
        ".",
    )
    commits = []
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        sha, subject, body, names = (record.split("\x00") + ["", "", ""])[:4]
        paths = tuple(line for line in names.splitlines() if line.strip())
        commits.append(Commit(sha=sha.strip(), subject=subject.strip(), body=body, paths=paths))
    return commits


def required_level(commits: list[Commit], version: str) -> str | None:
    """Return the bump these commits need, or None when nothing releasable."""

    level = None
    for commit in commits:
        if not commit.ships:
            continue
        match = commit.parsed
        kind = match.group("type") if match else None
        if commit.breaking:
            candidate = "major" if parse_version(version)[0] > 0 else "minor"
        elif kind == "feat":
            candidate = "minor"
        elif kind in {"fix", "perf"}:
            candidate = "patch"
        else:
            continue
        if level is None or LEVELS.index(candidate) > LEVELS.index(level):
            level = candidate
    return level


def last_version_change(root: Path) -> str:
    """Return the newest commit that changed the project version."""

    sha = _git(root, "log", "-1", "--format=%H", "-G", r'^version = "', "--", "pyproject.toml")
    sha = sha.strip()
    if not sha:
        raise ValueError("no commit changed the version in pyproject.toml")
    return sha


def changelog_section(version: str, commits: list[Commit], today: datetime.date) -> str:
    lines = [f"## v{version} ({today.isoformat()})", ""]
    breaking = [commit for commit in commits if commit.breaking]
    if breaking:
        lines += ["### Breaking Changes", ""]
        lines += [f"- {commit.subject}" for commit in breaking]
        lines.append("")
    for kind, title in SECTIONS:
        entries = []
        for commit in commits:
            match = commit.parsed
            if match and match.group("type") == kind:
                scope = match.group("scope")
                text = match.group("text")
                entries.append(f"- **{scope}**: {text}" if scope else f"- {text}")
        if entries:
            lines += [f"### {title}", ""] + entries + [""]
    if len(lines) == 2:
        lines += ["- Maintenance release.", ""]
    return "\n".join(lines) + "\n"


def write_changelog(root: Path, section: str) -> None:
    path = root / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    header = "# CHANGELOG\n"
    if not text.startswith(header):
        raise ValueError("CHANGELOG.md must start with '# CHANGELOG'")
    rest = text[len(header) :].lstrip("\n")
    path.write_text(f"{header}\n\n{section}\n\n{rest}", encoding="utf-8")


def set_engine_version(root: Path, version: str) -> None:
    pyproject = root / "pyproject.toml"
    text, count = re.subn(
        r'(\[project\]\s+name = "openadapt-desktop"\s+version = ")[^"]+(")',
        rf"\g<1>{version}\g<2>",
        pyproject.read_text(encoding="utf-8"),
        count=1,
    )
    if count != 1:
        raise ValueError("could not set the version in pyproject.toml")
    pyproject.write_text(text, encoding="utf-8")
    module = root / "openadapt_desktop" / "__init__.py"
    text, count = re.subn(
        r'(?m)^__version__ = "[^"]+"$',
        f'__version__ = "{version}"',
        module.read_text(encoding="utf-8"),
        count=1,
    )
    if count != 1:
        raise ValueError("could not set __version__ in openadapt_desktop/__init__.py")
    module.write_text(text, encoding="utf-8")
    check_release_consistency.sync_lock_version(root)
    native_release.set_native_version(version, root)


def prepare(
    root: Path,
    *,
    level: str | None = None,
    version: str | None = None,
    today: datetime.date | None = None,
) -> str:
    current = check_release_consistency.release_versions(root)["pyproject.toml"]
    commits = commits_between(root, last_version_change(root))
    if version is None:
        level = level or required_level(commits, current)
        if level is None:
            raise ValueError(
                "no feat, fix, perf or breaking commit since the last version change; "
                "pass --bump or --version to release anyway"
            )
        version = bump(current, level)
    if parse_version(version) <= parse_version(current):
        raise ValueError(f"version {version} does not advance {current}")
    set_engine_version(root, version)
    write_changelog(root, changelog_section(version, commits, today or datetime.date.today()))
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bump", choices=LEVELS)
    group.add_argument("--version")
    args = parser.parse_args(argv)
    try:
        version = prepare(ROOT, level=args.bump, version=args.version)
    except ValueError as exc:
        parser.exit(1, f"error: {exc}\n")
    print(f"Prepared openadapt-desktop {version}. Review CHANGELOG.md, then commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
