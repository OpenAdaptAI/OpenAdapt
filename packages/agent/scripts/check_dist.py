#!/usr/bin/env python3
"""Fail a release if its archives violate the OpenAdapt package boundary."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

_FORBIDDEN_PATH_MARKERS = (
    "openimis",
    "benchmark/openimis",
    "benchmarks/openimis",
    "adversary_corpus",
    "identity_roc",
    "pixel_verify_cert",
    "oracle_recipe",
    "real_emr",
    "held_out_corpus",
    "/private/",
)
_COPYLEFT_MARKERS = (
    b"GNU " + b"AFFERO GENERAL PUBLIC LICENSE",
    b"GNU " + b"GENERAL PUBLIC LICENSE",
    b"SPDX-License-Identifier: " + b"AGPL-",
    b"SPDX-License-Identifier: " + b"GPL-",
)


class ArtifactError(RuntimeError):
    """Distribution archive failed the release boundary."""


def _members(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {
                info.filename: archive.read(info)
                for info in archive.infolist()
                if not info.is_dir()
            }
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return {
                member.name: extracted.read()
                for member in archive.getmembers()
                if member.isfile() and (extracted := archive.extractfile(member)) is not None
            }
    raise ArtifactError(f"unsupported artifact type: {path}")


def check(path: Path) -> None:
    members = _members(path)
    if not members:
        raise ArtifactError(f"{path}: empty archive")

    lowered_names = {name: name.lower() for name in members}
    forbidden = sorted(
        name
        for name, lowered in lowered_names.items()
        if any(marker in lowered for marker in _FORBIDDEN_PATH_MARKERS)
    )
    if forbidden:
        raise ArtifactError(f"{path}: repository-only benchmark material in artifact: {forbidden}")

    metadata = [
        body
        for name, body in members.items()
        if name.endswith(".dist-info/METADATA") or name.endswith("/PKG-INFO")
    ]
    if not metadata or not any(
        b"License-Expression: MIT" in body or b"License: MIT" in body for body in metadata
    ):
        raise ArtifactError(f"{path}: package metadata does not declare MIT")

    license_files = [
        body
        for name, body in members.items()
        if Path(name).name.upper() in {"LICENSE", "LICENSE.TXT", "LICENSE.MD"}
    ]
    if not license_files or not any(
        b"Permission is hereby granted, free of charge" in body for body in license_files
    ):
        raise ArtifactError(f"{path}: MIT LICENSE is missing")

    copied_copyleft = []
    for name, body in members.items():
        if name.endswith((".dist-info/METADATA", "/PKG-INFO")):
            continue
        body_upper = body.upper()
        if any(marker.upper() in body_upper for marker in _COPYLEFT_MARKERS):
            copied_copyleft.append(name)
    copied_copyleft.sort()
    if copied_copyleft:
        raise ArtifactError(f"{path}: copied copyleft material in MIT artifact: {copied_copyleft}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()
    for artifact in args.artifacts:
        check(artifact)
        print(f"PASS {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
