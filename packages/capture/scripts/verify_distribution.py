#!/usr/bin/env python3
"""Verify release archives preserve the MIT package boundary."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

if __package__:
    from .check_changelog import validate_documents
else:  # Direct execution: python scripts/verify_distribution.py
    from check_changelog import validate_documents

FORBIDDEN_DEPENDENCIES = ("oa-atomacos", "pynput", "websockets")
FORBIDDEN_SOURCE_TOKENS = ("oa_atomacos", "pynput", "EXECUTE_ACTION")
FORBIDDEN_ARCHIVE_PATHS = (
    ".env.example",
    ".github/",
    "CLAUDE.md",
    "chrome_extension/",
    "docs/images/demo.gif",
    "docs/whisper-integration-plan.md",
    "examples/",
    "scripts/",
    "tests/",
    "uv.lock",
    "openadapt_capture/browser_bridge.py",
)
REQUIRED_OBSERVER_PATHS = {
    "openadapt_capture/structural.py",
    "openadapt_capture/structural_observer/__init__.py",
    "openadapt_capture/structural_observer/linux.py",
    "openadapt_capture/structural_observer/macos.py",
    "openadapt_capture/structural_observer/windows.py",
}


def _archive_files(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name) for name in archive.namelist() if not name.endswith("/")
            }
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return {
                member.name: extracted.read()
                for member in archive.getmembers()
                if member.isfile() and (extracted := archive.extractfile(member)) is not None
            }
    raise ValueError(f"Unsupported distribution archive: {path}")


def _relative_archive_name(name: str) -> str:
    """Remove the versioned root directory used by source distributions."""
    parts = Path(name).parts
    if len(parts) > 1 and parts[0].startswith("openadapt_capture-"):
        return Path(*parts[1:]).as_posix()
    return Path(name).as_posix()


def verify_distribution(path: Path) -> None:
    files = _archive_files(path)
    relative_files = {_relative_archive_name(name): content for name, content in files.items()}
    relative_names = set(relative_files)
    assert any(Path(name).name == "LICENSE" for name in files), (
        f"{path}: MIT LICENSE file is missing"
    )

    for name in relative_names:
        assert not any(
            name == forbidden.rstrip("/") or name.startswith(forbidden)
            for forbidden in FORBIDDEN_ARCHIVE_PATHS
        ), f"{path}: repository-only path is in the release archive: {name}"

    missing_observers = REQUIRED_OBSERVER_PATHS - relative_names
    assert not missing_observers, (
        f"{path}: native structural observer files are missing: "
        f"{sorted(missing_observers)}"
    )

    if path.name.endswith(".tar.gz"):
        required_source_files = {
            "CHANGELOG.md",
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "openadapt_capture/__init__.py",
        }
        missing = required_source_files - relative_names
        assert not missing, f"{path}: required source files are missing: {sorted(missing)}"
        try:
            validate_documents(
                relative_files["CHANGELOG.md"].decode("utf-8"),
                relative_files["pyproject.toml"].decode("utf-8"),
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise AssertionError(f"{path}: invalid changelog contract: {exc}") from exc

    metadata_files = [
        content.decode("utf-8")
        for name, content in files.items()
        if name.endswith((".dist-info/METADATA", "/PKG-INFO"))
    ]
    assert metadata_files, f"{path}: package metadata is missing"
    metadata = "\n".join(metadata_files).lower()
    metadata_lines = metadata.splitlines()
    assert "provides-extra: linux" in metadata_lines, (
        f"{path}: the Linux AT-SPI package extra is missing"
    )
    assert any(
        line.startswith("requires-dist: pygobject<3.50,>=3.46;")
        and "extra == 'linux'" in line
        for line in metadata_lines
    ), f"{path}: the Linux extra does not carry the reviewed PyGObject range"
    for dependency in FORBIDDEN_DEPENDENCIES:
        assert f"requires-dist: {dependency}" not in metadata, (
            f"{path}: forbidden dependency {dependency!r} is in package metadata"
        )
    assert "requires-dist: av" not in metadata, (
        f"{path}: PyAV must not be in the package dependency closure"
    )

    forbidden_binary_names = (
        "ffmpeg",
        "ffprobe",
        "avcodec",
        "avformat",
        "x264",
        "x265",
    )
    for name in files:
        leaf = Path(name).name.lower()
        assert not any(token in leaf for token in forbidden_binary_names), (
            f"{path}: bundled video binary violates the external-process boundary: {name}"
        )

    python_sources = "\n".join(
        content.decode("utf-8")
        for name, content in files.items()
        if name.endswith(".py") and "/openadapt_capture/" in f"/{name}"
    )
    for token in FORBIDDEN_SOURCE_TOKENS:
        assert token not in python_sources, (
            f"{path}: forbidden source token {token!r} is in the archive"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args()
    for archive in args.archives:
        verify_distribution(archive)
        print(f"verified {archive}")


if __name__ == "__main__":
    main()
