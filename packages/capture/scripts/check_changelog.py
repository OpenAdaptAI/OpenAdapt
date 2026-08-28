#!/usr/bin/env python3
"""Fail closed when release metadata and the maintained changelog disagree."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 test matrix.
    import tomli as tomllib

REPOSITORY = "OpenAdaptAI/openadapt-capture"
INSERTION_FLAG = "<!-- version list -->"
RELEASE_HEADING = re.compile(
    r"^## v(?P<version>\d+\.\d+\.\d+) \((?P<date>\d{4}-\d{2}-\d{2})\)$",
    re.MULTILINE,
)
STABLE_TAG = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")


class ChangelogContractError(ValueError):
    """The changelog cannot prove the package's release history."""


@dataclass(frozen=True)
class Release:
    version: str
    version_key: tuple[int, int, int]
    released_on: date
    body: str


def _version_key(version: str) -> tuple[int, int, int]:
    try:
        major, minor, patch = version.split(".")
        return int(major), int(minor), int(patch)
    except (TypeError, ValueError) as exc:
        raise ChangelogContractError(f"invalid stable version: {version!r}") from exc


def parse_releases(changelog: str) -> list[Release]:
    """Parse all stable release sections and reject malformed release headings."""
    heading_lines = [line for line in changelog.splitlines() if line.startswith("## v")]
    matches = list(RELEASE_HEADING.finditer(changelog))
    if len(matches) != len(heading_lines):
        valid_lines = {match.group(0) for match in matches}
        malformed = [line for line in heading_lines if line not in valid_lines]
        raise ChangelogContractError(
            f"malformed release heading(s): {', '.join(repr(line) for line in malformed)}"
        )
    if not matches:
        raise ChangelogContractError("the changelog has no stable release sections")

    releases: list[Release] = []
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(changelog)
        version = match.group("version")
        try:
            released_on = date.fromisoformat(match.group("date"))
        except ValueError as exc:
            raise ChangelogContractError(
                f"v{version} has an invalid release date: {match.group('date')!r}"
            ) from exc
        releases.append(
            Release(
                version=version,
                version_key=_version_key(version),
                released_on=released_on,
                body=changelog[match.end() : body_end],
            )
        )
    return releases


def _semantic_release_config(pyproject: str) -> tuple[str, dict[str, object]]:
    try:
        document = tomllib.loads(pyproject)
        project = document["project"]
        semantic_release = document["tool"]["semantic_release"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ChangelogContractError(
            "pyproject.toml has no valid project and semantic-release configuration"
        ) from exc
    version = project.get("version")
    if not isinstance(version, str):
        raise ChangelogContractError("project.version must be a string")
    return version, semantic_release


def validate_documents(changelog: str, pyproject: str) -> list[Release]:
    """Validate the files that are available both in Git and in the source archive."""
    project_version, semantic_release = _semantic_release_config(pyproject)
    releases = parse_releases(changelog)
    versions = [release.version for release in releases]
    if len(set(versions)) != len(versions):
        raise ChangelogContractError("the changelog contains a duplicate release version")
    if [release.version_key for release in releases] != sorted(
        (release.version_key for release in releases), reverse=True
    ):
        raise ChangelogContractError("release sections are not in descending version order")
    if releases[0].version != project_version:
        raise ChangelogContractError(
            "the newest changelog release does not match project.version: "
            f"v{releases[0].version} != v{project_version}"
        )

    if changelog.count(INSERTION_FLAG) != 1:
        raise ChangelogContractError(
            f"the changelog must contain exactly one {INSERTION_FLAG!r} insertion flag"
        )
    if changelog.index(INSERTION_FLAG) > changelog.index("## v"):
        raise ChangelogContractError("the changelog insertion flag must precede every release")

    version_toml = semantic_release.get("version_toml")
    if not isinstance(version_toml, list) or "pyproject.toml:project.version" not in version_toml:
        raise ChangelogContractError(
            "semantic-release must update pyproject.toml:project.version"
        )
    changelog_config = semantic_release.get("changelog")
    if not isinstance(changelog_config, dict):
        raise ChangelogContractError("semantic-release changelog configuration is missing")
    if changelog_config.get("mode") != "update":
        raise ChangelogContractError("semantic-release changelog mode must be 'update'")
    if changelog_config.get("insertion_flag") != INSERTION_FLAG:
        raise ChangelogContractError("semantic-release must use the maintained insertion flag")
    templates = changelog_config.get("default_templates")
    if not isinstance(templates, dict):
        raise ChangelogContractError("semantic-release default changelog templates are missing")
    if templates.get("changelog_file") != "CHANGELOG.md":
        raise ChangelogContractError("semantic-release must update CHANGELOG.md")
    if templates.get("output_format") != "md":
        raise ChangelogContractError("semantic-release changelog output must be Markdown")

    # v1.0.0 is the first release that was absent from the maintained file.
    # Require generated release content and an exact adjacent-tag comparison
    # for every backfilled and future 1.x-or-later release.
    for index, release in enumerate(releases):
        if release.version_key < (1, 0, 0):
            continue
        if "\n### " not in release.body or not re.search(r"(?m)^- ", release.body):
            raise ChangelogContractError(f"v{release.version} has no categorized release notes")
        if index + 1 >= len(releases):
            raise ChangelogContractError(
                f"v{release.version} has no prior release for its comparison link"
            )
        previous = releases[index + 1].version
        comparison = (
            f"https://github.com/{REPOSITORY}/compare/v{previous}...v{release.version}"
        )
        if comparison not in release.body:
            raise ChangelogContractError(
                f"v{release.version} is missing its exact adjacent-tag comparison link"
            )
    return releases


def validate_git_tags(releases: list[Release], repository: Path) -> None:
    """Require every stable Git tag to have exactly one changelog section.

    The newest documented release may be untagged, and only that one. It is the
    reviewed release candidate that ``validate_documents`` already pinned to
    ``project.version``: the release workflow requires that section to exist on
    protected main *before* it creates the tag, so demanding a tag for it makes
    preparing a release impossible. Every older section must still name a real
    tag, and every tag must still be documented, so a published release can
    neither lose its notes nor be invented after the fact.
    """
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ChangelogContractError("cannot read the repository's release tags") from exc
    tags = {
        match.group("version")
        for line in result.stdout.splitlines()
        if (match := STABLE_TAG.fullmatch(line)) is not None
    }
    if not tags:
        raise ChangelogContractError(
            "no stable Git tags are available; fetch the complete tag history"
        )
    documented = {release.version for release in releases}
    missing = sorted(tags - documented, key=_version_key)
    if missing:
        raise ChangelogContractError(
            "stable Git tag(s) are absent from CHANGELOG.md: "
            + ", ".join(f"v{version}" for version in missing)
        )
    pending = releases[0].version
    untagged = sorted(documented - tags - {pending}, key=_version_key)
    if untagged:
        raise ChangelogContractError(
            "CHANGELOG.md contains untagged stable release(s): "
            + ", ".join(f"v{version}" for version in untagged)
        )


def check_repository(repository: Path) -> list[Release]:
    changelog = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
    pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
    releases = validate_documents(changelog, pyproject)
    validate_git_tags(releases, repository)
    return releases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    releases = check_repository(args.repository.resolve())
    print(f"verified CHANGELOG.md through v{releases[0].version}")


if __name__ == "__main__":
    main()
