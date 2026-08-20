from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.check_changelog import (
    ChangelogContractError,
    validate_documents,
    validate_git_tags,
)
from scripts.verify_distribution import verify_distribution

REPOSITORY = Path(__file__).resolve().parents[1]


def _documents() -> tuple[str, str]:
    return (
        (REPOSITORY / "CHANGELOG.md").read_text(encoding="utf-8"),
        (REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"),
    )


def _add_tar_file(archive: tarfile.TarFile, name: str, content: str) -> None:
    data = content.encode("utf-8")
    member = tarfile.TarInfo(name)
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))


def _project_with_next_patch(changelog: str, pyproject: str) -> tuple[str, str]:
    current = validate_documents(changelog, pyproject)[0].version
    major, minor, patch = (int(part) for part in current.split("."))
    next_version = f"{major}.{minor}.{patch + 1}"
    updated = pyproject.replace(
        f'version = "{current}"',
        f'version = "{next_version}"',
        1,
    )
    return updated, next_version


def test_changelog_matches_project_version_and_release_config() -> None:
    changelog, pyproject = _documents()
    releases = validate_documents(changelog, pyproject)

    assert {
        "1.0.0",
        "1.0.1",
        "1.0.2",
        "1.0.3",
        "1.0.4",
        "1.1.0",
        "1.1.1",
        "1.2.0",
        "1.2.1",
        "1.2.2",
    } <= {release.version for release in releases}
    assert releases[-1].version == "0.1.0"


def test_changelog_refuses_a_project_version_without_release_notes() -> None:
    changelog, pyproject = _documents()
    pyproject, _ = _project_with_next_patch(changelog, pyproject)

    with pytest.raises(
        ChangelogContractError,
        match="newest changelog release does not match project.version",
    ):
        validate_documents(changelog, pyproject)


def test_changelog_refuses_a_missing_semantic_release_insertion_flag() -> None:
    changelog, pyproject = _documents()
    changelog = changelog.replace("<!-- version list -->", "", 1)

    with pytest.raises(ChangelogContractError, match="exactly one"):
        validate_documents(changelog, pyproject)


def test_changelog_refuses_a_missing_adjacent_tag_comparison() -> None:
    changelog, pyproject = _documents()
    changelog = changelog.replace("compare/v1.2.1...v1.2.2", "compare/v1.2.0...v1.2.2", 1)

    with pytest.raises(ChangelogContractError, match="exact adjacent-tag comparison"):
        validate_documents(changelog, pyproject)


def test_git_tag_contract_refuses_an_incomplete_tag_inventory(monkeypatch) -> None:
    changelog, pyproject = _documents()
    releases = validate_documents(changelog, pyproject)
    incomplete_tags = "\n".join(f"v{release.version}" for release in releases[1:])
    monkeypatch.setattr(
        "scripts.check_changelog.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=incomplete_tags),
    )

    with pytest.raises(ChangelogContractError, match="untagged stable release"):
        validate_git_tags(releases, REPOSITORY)


def test_source_distribution_refuses_a_version_without_release_notes(tmp_path: Path) -> None:
    changelog, pyproject = _documents()
    pyproject, next_version = _project_with_next_patch(changelog, pyproject)
    archive_path = tmp_path / f"openadapt_capture-{next_version}.tar.gz"
    root = f"openadapt_capture-{next_version}"
    files = {
        "CHANGELOG.md": changelog,
        "LICENSE": "MIT License\n",
        "README.md": "# OpenAdapt Capture\n",
        "pyproject.toml": pyproject,
        "openadapt_capture/__init__.py": "",
        f"openadapt_capture-{next_version}.dist-info/PKG-INFO": (
            "Metadata-Version: 2.4\n"
            "Name: openadapt-capture\n"
            f"Version: {next_version}\n"
        ),
    }
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, content in files.items():
            _add_tar_file(archive, f"{root}/{name}", content)

    with pytest.raises(AssertionError, match="invalid changelog contract"):
        verify_distribution(archive_path)
