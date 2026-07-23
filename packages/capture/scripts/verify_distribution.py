#!/usr/bin/env python3
"""Verify release archives preserve the MIT package boundary."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN_DEPENDENCIES = ("oa-atomacos", "pynput")
FORBIDDEN_SOURCE_TOKENS = ("oa_atomacos", "pynput")


def _archive_files(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name)
                for name in archive.namelist()
                if not name.endswith("/")
            }
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return {
                member.name: extracted.read()
                for member in archive.getmembers()
                if member.isfile()
                and (extracted := archive.extractfile(member)) is not None
            }
    raise ValueError(f"Unsupported distribution archive: {path}")


def verify_distribution(path: Path) -> None:
    files = _archive_files(path)
    assert any(Path(name).name == "LICENSE" for name in files), (
        f"{path}: MIT LICENSE file is missing"
    )

    metadata_files = [
        content.decode("utf-8")
        for name, content in files.items()
        if name.endswith((".dist-info/METADATA", "/PKG-INFO"))
    ]
    assert metadata_files, f"{path}: package metadata is missing"
    metadata = "\n".join(metadata_files).lower()
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
        if name.endswith(".py")
        and "/openadapt_capture/" in f"/{name}"
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
