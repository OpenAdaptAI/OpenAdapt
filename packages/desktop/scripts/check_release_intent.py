#!/usr/bin/env python3
"""Fail a Desktop pull request whose changes should ship but would not.

desktop-release.yml publishes openadapt-desktop when a merged version is not on
PyPI yet, so a feat, fix or perf change only ships if its pull request bumps
the version. This check runs on every pull request that touches
packages/desktop:

* With no version change, it fails when the pull request has a releasable
  commit, and prints the bump and the command that makes it.
* With a version change, it requires a strict advance at least as large as the
  commits need, every engine and native version source on the new version, and
  a matching dated section at the top of CHANGELOG.md.

    python scripts/check_release_intent.py --base <base-sha> --head <head-sha>
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_release_consistency  # noqa: E402
import native_release  # noqa: E402
import prepare_release  # noqa: E402

PYPROJECT_VERSION = re.compile(r'\[project\]\s+name = "openadapt-desktop"\s+version = "([^"]+)"')
CHANGELOG_HEADER = re.compile(r"^## v([^ ]+) \(\d{4}-\d{2}-\d{2}\)$", re.MULTILINE)


def version_at(root: Path, ref: str) -> str:
    text = subprocess.run(
        ["git", "show", f"{ref}:./pyproject.toml"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = PYPROJECT_VERSION.search(text)
    if match is None:
        raise ValueError(f"could not read the version at {ref}")
    return match.group(1)


def check(root: Path, base: str, head: str) -> list[str]:
    """Return the problems with this change; empty means it may merge."""

    base_version = version_at(root, base)
    head_version = version_at(root, head)
    commits = prepare_release.commits_between(root, base, head)
    needed = prepare_release.required_level(commits, base_version)
    releasable = [
        commit.subject
        for commit in commits
        if commit.ships
        and (
            commit.breaking
            or (commit.parsed and commit.parsed.group("type") in {"feat", "fix", "perf"})
        )
    ]

    # Every engine and native version source always carries the one version.
    # This also keeps the native installer version from moving on its own.
    problems = []
    versions = check_release_consistency.release_versions(root)
    versions["native installer"] = native_release.native_version(root)
    stale = {source: value for source, value in versions.items() if value != head_version}
    if stale:
        problems.append(
            f"version sources disagree with {head_version}: {stale}; "
            "run scripts/prepare_release.py --version " + head_version
        )

    if head_version == base_version:
        if needed is None:
            return problems
        suggested = prepare_release.bump(base_version, needed)
        listed = "\n".join(f"  - {subject}" for subject in releasable)
        return problems + [
            f"This pull request changes Desktop in a way that should ship:\n{listed}\n"
            f"but keeps version {base_version}, so nothing would be released. Bump to "
            f"{suggested} ({needed}) with:\n"
            f"  cd packages/desktop && python scripts/prepare_release.py --bump {needed}"
        ]

    if prepare_release.parse_version(head_version) <= prepare_release.parse_version(base_version):
        problems.append(f"version {head_version} does not advance {base_version}")
    elif needed is not None:
        minimum = prepare_release.bump(base_version, needed)
        if prepare_release.parse_version(head_version) < prepare_release.parse_version(minimum):
            problems.append(
                f"version {head_version} is smaller than the {needed} bump these commits "
                f"need ({minimum})"
            )
    headers = CHANGELOG_HEADER.findall((root / "CHANGELOG.md").read_text(encoding="utf-8"))
    if not headers or headers[0] != head_version:
        top = headers[0] if headers else "missing"
        problems.append(f"CHANGELOG.md must start with v{head_version}; found v{top}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the pull request's base commit")
    parser.add_argument("--head", required=True, help="the pull request's head commit")
    args = parser.parse_args(argv)
    problems = check(ROOT, args.base, args.head)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if not problems:
        print("Desktop release intent is consistent.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
