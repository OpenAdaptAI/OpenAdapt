#!/usr/bin/env python3
"""Verify that a GitHub Release is the exact immutable release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_files(directory: Path, *, allow_empty: bool = False) -> dict[str, Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"artifact directory does not exist: {directory}")

    entries = list(directory.iterdir())
    invalid = sorted(
        path.name for path in entries if path.is_symlink() or not path.is_file()
    )
    if invalid:
        raise ValueError(
            f"artifact directory contains invalid entries: {', '.join(invalid)}"
        )
    files = {
        path.name: path
        for path in entries
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    }
    if not files and not allow_empty:
        raise ValueError(f"artifact directory has no wheel or sdist: {directory}")

    unexpected = sorted(path.name for path in entries if path.name not in files)
    if unexpected:
        raise ValueError(
            f"artifact directory contains unexpected files: {', '.join(unexpected)}"
        )
    package_types = {"wheel" if name.endswith(".whl") else "sdist" for name in files}
    if (
        not allow_empty
        and files
        and (len(files) != 2 or package_types != {"wheel", "sdist"})
    ):
        raise ValueError("artifact directory must contain one wheel and one sdist")
    return files


def verify_release(
    document: dict[str, Any],
    *,
    expected_tag: str,
    expected_author: str,
    expected_dir: Path,
    downloaded_dir: Path | None = None,
    allow_missing: bool = False,
    expected_state: str = "published",
) -> list[str]:
    if document.get("tag_name") != expected_tag:
        raise ValueError(
            f"release tag mismatch: expected {expected_tag!r}, "
            f"found {document.get('tag_name')!r}"
        )
    if expected_state not in {"draft", "published"}:
        raise ValueError(f"unknown GitHub Release state: {expected_state}")
    expected_draft = expected_state == "draft"
    if document.get("draft") is not expected_draft:
        raise ValueError(f"the GitHub Release is not {expected_state}")
    if document.get("prerelease") is not False:
        raise ValueError("the GitHub Release is a prerelease")
    if expected_draft:
        if document.get("immutable") is not False:
            raise ValueError("the draft GitHub Release is immutable")
        if document.get("published_at") is not None:
            raise ValueError("the draft GitHub Release has a publication time")
    else:
        if document.get("immutable") is not True:
            raise ValueError("the published GitHub Release is mutable")
        if not isinstance(document.get("published_at"), str):
            raise ValueError("the published GitHub Release has no publication time")

    author = document.get("author")
    actual_author = author.get("login") if isinstance(author, dict) else None
    if actual_author != expected_author:
        raise ValueError(
            f"release author mismatch: expected {expected_author!r}, "
            f"found {actual_author!r}"
        )

    expected = _artifact_files(expected_dir)
    raw_assets = document.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError("release assets are missing or invalid")

    assets: dict[str, dict[str, Any]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict) or not isinstance(
            raw_asset.get("name"), str
        ):
            raise ValueError("release asset metadata is invalid")
        name = raw_asset["name"]
        if name in assets:
            raise ValueError(f"release has duplicate asset name: {name}")
        assets[name] = raw_asset

    expected_names = set(expected)
    actual_names = set(assets)
    missing = sorted(expected_names - actual_names)
    unexpected = sorted(actual_names - expected_names)
    if unexpected:
        raise ValueError(
            f"release asset set mismatch (unexpected: {', '.join(unexpected)})"
        )
    if missing and not allow_missing:
        raise ValueError(f"release asset set mismatch (missing: {', '.join(missing)})")

    expected_digests: dict[str, str] = {}
    for name in sorted(actual_names):
        path = expected[name]
        digest = _sha256(path)
        expected_digests[name] = digest
        asset = assets[name]
        uploader = asset.get("uploader")
        actual_uploader = uploader.get("login") if isinstance(uploader, dict) else None
        if actual_uploader != expected_author:
            raise ValueError(
                f"release asset uploader mismatch: expected {expected_author!r}, "
                f"found {actual_uploader!r}: {name}"
            )
        if asset.get("size") != path.stat().st_size:
            raise ValueError(f"release asset size mismatch: {name}")
        if asset.get("digest") != f"sha256:{digest}":
            raise ValueError(f"release asset digest mismatch: {name}")

    if downloaded_dir is None:
        return missing

    downloaded = _artifact_files(downloaded_dir, allow_empty=allow_missing)
    required_downloads = actual_names if allow_missing else expected_names
    if set(downloaded) != required_downloads:
        raise ValueError("downloaded release asset set does not match the candidate")
    for name, path in downloaded.items():
        if path.stat().st_size != expected[name].stat().st_size:
            raise ValueError(f"downloaded release asset size mismatch: {name}")
        if _sha256(path) != expected_digests[name]:
            raise ValueError(f"downloaded release asset bytes mismatch: {name}")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--expected-dir", type=Path, required=True)
    parser.add_argument("--downloaded-dir", type=Path)
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--missing-output", type=Path)
    parser.add_argument("--state", choices=("draft", "published"), default="published")
    args = parser.parse_args()

    try:
        document = json.loads(args.metadata.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("release metadata must be a JSON object")
        missing = verify_release(
            document,
            expected_tag=args.tag,
            expected_author=args.author,
            expected_dir=args.expected_dir,
            downloaded_dir=args.downloaded_dir,
            allow_missing=args.allow_missing,
            expected_state=args.state,
        )
        if args.missing_output is not None:
            args.missing_output.write_text(
                "".join(f"{name}\n" for name in missing), encoding="utf-8"
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(1, f"GitHub Release verification failed: {exc}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
