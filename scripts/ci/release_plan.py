#!/usr/bin/env python3
"""Decide which packages release-package.yml publishes in this run.

Merging a reviewed version bump is the authorization to publish. Each run
(after a Packages run on main, every three hours, or on a dispatch) checks
every package and publishes a package when all of these hold:

- the newest commit on main's first-parent history that changed the package
  has a successful push run with the package's own jobs run and passed
  (scripts/ci/newest_green.py, the rule desktop-release.yml set);
- the version in its pyproject.toml is not on PyPI, is higher than every
  version PyPI has, and has no tag yet;
- its CHANGELOG.md starts with an entry for exactly that version.

A version is never republished. A half-published one is resumed, not
rebuilt: re-run the failed jobs of the run that published it, which reuses
its built bytes. When PyPI serves the version and its tag exists but the
GitHub release was never published, a dispatch with `resume` publishes the
release from the bytes PyPI serves. A version on PyPI with no package tag
(published before package tags, as capture 1.3.0 was) counts as done. A
scheduled or Packages-started run only warns about a version it may not
publish; a dispatch fails on it.

Writes `matrix` (a JSON list of {package, project, version, tag, action,
commit}, where action is `release` or `resume`), `release` (the same, only
releases) and `any` to GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = {
    "types": "openadapt-types",
    "flow": "openadapt-flow",
    "agent": "openadapt-agent",
    "capture": "openadapt-capture",
    "privacy": "openadapt-privacy",
    "tray": "openadapt-tray",
}
_HEADER = re.compile(r"^## v([^ ]+) \(\d{4}-\d{2}-\d{2}\)$", re.MULTILINE)


def _newest_green():
    spec = importlib.util.spec_from_file_location(
        "newest_green", ROOT / "scripts/ci/newest_green.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _toml(text: str) -> dict:
    # Imported here: the release workflow runs Python 3.12, but the launcher's
    # tests import this module on 3.10 too, which has no tomllib.
    import tomllib

    return tomllib.loads(text)


def version_key(version: str) -> tuple:
    """Order release versions (X.Y.Z with an optional pre-release suffix)."""
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(.*)", version)
    if not match:
        raise ValueError(f"cannot order version {version!r}")
    numbers = tuple(int(part) for part in match.group(1).split("."))
    # A final release sorts after its own pre-releases (1.0.0rc1 < 1.0.0).
    return numbers, match.group(2) == "", match.group(2)


def changelog_top(text: str) -> str | None:
    match = _HEADER.search(text)
    return match.group(1) if match else None


def decide(
    *,
    version: str,
    changelog: str | None,
    pypi_versions: list[str],
    pypi_files: list[dict] | None,
    tag_commit: str | None,
    release_published: bool,
    green: bool,
    resume: bool,
) -> tuple[str, str]:
    """Return (action, reason). Action: release, resume, done, wait or refuse.

    `pypi_files` is None when PyPI has no such version. `tag_commit` is the
    commit the package tag names, or None. `wait` means nothing to do yet;
    `refuse` means a person must act.
    """
    if pypi_files is not None:
        if tag_commit and release_published:
            return "done", f"{version} is released"
        kinds = {item.get("packagetype") for item in pypi_files}
        if tag_commit and {"sdist", "bdist_wheel"} <= kinds:
            if resume:
                return (
                    "resume",
                    f"{version} is on PyPI and tagged; publishing its release",
                )
            return "refuse", (
                f"{version} is on PyPI and tagged, but its GitHub release is not "
                "published. Re-run the failed jobs of the run that published it, "
                "or dispatch this workflow with resume."
            )
        if tag_commit:
            return "refuse", (
                f"{version} is tagged but PyPI has only {sorted(kinds)}. Re-run the "
                "failed jobs of the run that published it; it is never rebuilt."
            )
        # Published before package tags existed, or by a run that failed
        # after the upload. That run is red, and re-running its failed jobs
        # tags it from the same bytes. Never rebuilt.
        return "done", (
            f"{version} is on PyPI without a package tag; if a release run failed "
            "after its upload, re-run that run's failed jobs"
        )
    if tag_commit:
        return "refuse", f"tag for {version} exists but PyPI has no {version}"
    if changelog != version:
        return "wait", f"CHANGELOG.md starts with v{changelog}, not v{version}"
    newer = [v for v in pypi_versions if version_key(v) >= version_key(version)]
    if newer:
        return (
            "refuse",
            f"{version} is not higher than PyPI's {max(newer, key=version_key)}",
        )
    if not green:
        return "wait", "the newest commit that changed it has no green run on main yet"
    return "release", f"{version} is new"


def _pypi(project: str, version: str) -> tuple[list[str], list[dict] | None]:
    def get(url: str) -> dict | None:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    index = get(f"https://pypi.org/pypi/{project}/json")
    versions = sorted(index["releases"]) if index else []
    exact = get(f"https://pypi.org/pypi/{project}/{version}/json")
    return versions, (exact["urls"] if exact else None)


def _tag_commit(tag: str) -> str | None:
    out = subprocess.run(
        [
            "git",
            "ls-remote",
            "--tags",
            "origin",
            f"refs/tags/{tag}^{{}}",
            f"refs/tags/{tag}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    # An annotated tag lists its object and then the peeled commit (^{}).
    pairs = dict(zip(out[1::2], out[0::2]))
    return pairs.get(f"refs/tags/{tag}^{{}}") or pairs.get(f"refs/tags/{tag}")


def _release_published(repo: str, tag: str) -> bool:
    # GET /releases/tags/{tag} never returns a draft; a draft counts as not
    # published.
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/tags/{tag}", "--jq", ".draft"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "false"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--package", default="all", choices=["all", *PACKAGES])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dispatch", action="store_true", help="fail instead of warn")
    parser.add_argument("--tip", default="HEAD")
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)
    repo = os.environ["GITHUB_REPOSITORY"]
    ng = _newest_green()
    tip = subprocess.run(
        ["git", "rev-parse", args.tip], check=True, capture_output=True, text=True
    ).stdout.strip()

    matrix, refused = [], []
    for package in PACKAGES if args.package == "all" else [args.package]:
        project = PACKAGES[package]
        directory = ROOT / "packages" / package
        meta = _toml((directory / "pyproject.toml").read_text(encoding="utf-8"))
        if meta["project"]["name"] != project:
            raise SystemExit(
                f"packages/{package} is {meta['project']['name']}, not {project}"
            )
        version = meta["project"]["version"]
        tag = f"{project}-v{version}"
        versions, files = _pypi(project, version)
        tag_commit = _tag_commit(tag)
        published = bool(tag_commit) and _release_published(repo, tag)
        green = ng.main([package, "--tip", tip]) == 0 if files is None else False
        action, reason = decide(
            version=version,
            changelog=changelog_top(
                (directory / "CHANGELOG.md").read_text(encoding="utf-8")
            ),
            pypi_versions=versions,
            pypi_files=files,
            tag_commit=tag_commit,
            release_published=published,
            green=green,
            resume=args.resume,
        )
        print(f"{project} {version}: {action}: {reason}", flush=True)
        if action in ("release", "resume"):
            matrix.append(
                {
                    "package": package,
                    "project": project,
                    "version": version,
                    "tag": tag,
                    "action": action,
                    # A release tags the tip it builds; a resume keeps its tag.
                    "commit": tip if action == "release" else tag_commit,
                }
            )
        elif action == "refuse" or (
            args.dispatch and action == "wait" and args.package != "all"
        ):
            refused.append(f"{project} {version}: {reason}")
            if not args.dispatch:
                print(f"::warning::{project} {version}: {reason}")

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={json.dumps(matrix)}\n")
            releases = [item for item in matrix if item["action"] == "release"]
            handle.write(f"release={json.dumps(releases)}\n")
            handle.write(f"any={'true' if matrix else 'false'}\n")
    if args.dispatch and refused:
        print("\n".join(["Refusing:", *refused]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
