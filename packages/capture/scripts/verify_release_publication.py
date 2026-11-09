#!/usr/bin/env python3
"""Verify that one release has the exact built artifacts on PyPI and GitHub."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class PublicationError(RuntimeError):
    """A publication surface is missing, unreachable, or inconsistent."""


def artifact_digests(dist: Path) -> dict[str, str]:
    """Return SHA-256 digests for exactly one wheel and one sdist."""
    files = sorted(path for path in dist.iterdir() if path.is_file())
    artifacts = [
        path
        for path in files
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    ]
    if len(artifacts) != 2:
        raise PublicationError("dist must contain exactly one wheel and one sdist")
    if sum(path.suffix == ".whl" for path in artifacts) != 1:
        raise PublicationError("dist must contain exactly one wheel")
    if sum(path.name.endswith(".tar.gz") for path in artifacts) != 1:
        raise PublicationError("dist must contain exactly one sdist")
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts
    }


def _request(
    url: str,
    *,
    token: str | None = None,
    accept: str = "application/vnd.github+json",
) -> bytes | None:
    headers = {
        "Accept": accept,
        "User-Agent": "openadapt-release-verifier/1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(32 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise PublicationError(f"publication query failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublicationError(f"publication query failed: {exc}") from exc


def _json_document(data: bytes | None, *, surface: str) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        document = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PublicationError(f"{surface} returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise PublicationError(f"{surface} returned a non-object JSON document")
    return document


def verify_pypi(
    package: str,
    version: str,
    expected: dict[str, str],
) -> bool:
    """Return false when the version is absent; reject any artifact mismatch."""
    url = (
        "https://pypi.org/pypi/"
        f"{urllib.parse.quote(package, safe='')}/{urllib.parse.quote(version, safe='')}/json"
    )
    document = _json_document(_request(url), surface="PyPI")
    if document is None:
        return False
    urls = document.get("urls")
    if not isinstance(urls, list):
        raise PublicationError("PyPI response has no artifact list")
    actual: dict[str, str] = {}
    for entry in urls:
        if not isinstance(entry, dict):
            raise PublicationError("PyPI response has an invalid artifact entry")
        filename = entry.get("filename")
        digests = entry.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(filename, str) or not isinstance(digest, str):
            raise PublicationError("PyPI response has an artifact without a SHA-256")
        actual[filename] = digest
    if actual != expected:
        raise PublicationError(
            f"PyPI artifacts differ: expected {expected}, received {actual}"
        )
    return True


def verify_github_release(
    repository: str,
    tag: str,
    expected: dict[str, str],
    *,
    token: str,
) -> bool:
    """Return false when the release is absent; reject any release mismatch."""
    encoded_tag = urllib.parse.quote(tag, safe="")
    url = f"https://api.github.com/repos/{repository}/releases/tags/{encoded_tag}"
    document = _json_document(_request(url, token=token), surface="GitHub")
    if document is None:
        return False
    if document.get("tag_name") != tag:
        raise PublicationError("GitHub returned a release for a different tag")
    if document.get("draft") is not False or document.get("prerelease") is not False:
        raise PublicationError("GitHub release is a draft or prerelease")
    assets = document.get("assets")
    if not isinstance(assets, list):
        raise PublicationError("GitHub release has no artifact list")
    actual: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise PublicationError("GitHub release has an invalid artifact entry")
        digest = asset.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:"):
            actual[asset["name"]] = digest.removeprefix("sha256:")
            continue
        asset_url = asset.get("url")
        if not isinstance(asset_url, str):
            raise PublicationError("GitHub release artifact has no download URL")
        content = _request(
            asset_url,
            token=token,
            accept="application/octet-stream",
        )
        if content is None:
            raise PublicationError("GitHub release artifact disappeared during download")
        actual[asset["name"]] = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise PublicationError(
            f"GitHub release artifacts differ: expected {expected}, received {actual}"
        )
    return True


def _write_outputs(path: Path, *, pypi_exists: bool, release_exists: bool) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"publish_pypi={'false' if pypi_exists else 'true'}\n")
        output.write(
            f"create_github_release={'false' if release_exists else 'true'}\n"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args()
    if args.wait_seconds < 0 or args.poll_seconds <= 0:
        parser.error("wait and poll intervals must be positive")
    if args.tag != f"v{args.version}":
        parser.error("tag must equal v plus the exact version")
    token = os.environ.get("GH_TOKEN")
    if not token:
        parser.error("GH_TOKEN must contain the exact release App token")

    expected = artifact_digests(args.dist)
    deadline = time.monotonic() + args.wait_seconds
    while True:
        pypi_exists = verify_pypi(args.package, args.version, expected)
        release_exists = verify_github_release(
            args.repository,
            args.tag,
            expected,
            token=token,
        )
        if pypi_exists and release_exists:
            print(f"Verified {args.tag} on PyPI and GitHub with exact SHA-256 digests.")
            return 0
        if args.allow_missing:
            if args.github_output is None:
                parser.error("--allow-missing requires --github-output")
            _write_outputs(
                args.github_output,
                pypi_exists=pypi_exists,
                release_exists=release_exists,
            )
            return 0
        if time.monotonic() >= deadline:
            missing = [
                name
                for name, exists in (
                    ("PyPI", pypi_exists),
                    ("GitHub Release", release_exists),
                )
                if not exists
            ]
            raise PublicationError(f"publication is missing from {', '.join(missing)}")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
