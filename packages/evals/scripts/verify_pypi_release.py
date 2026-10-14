#!/usr/bin/env python3
"""Verify one immutable PyPI release against the exact local build."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

PYPI_PROJECT = "openadapt-evals"
STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
BUILD_TOOL_IGNORE_FILE = ".gitignore"
PUBLISH_ATTESTATION_SUFFIX = ".publish.attestation"


class ReleaseVerificationError(RuntimeError):
    """The public release does not equal the reviewed local build."""


class PublicationPending(ReleaseVerificationError):
    """The exact immutable PyPI publication is not visible yet."""


class PublicationAbsent(PublicationPending):
    """The exact version or one of its inventoried files is absent."""


@dataclass(frozen=True)
class Artifact:
    """One local immutable distribution file."""

    name: str
    package_type: str
    body: bytes
    sha256: str

    @property
    def size(self) -> int:
        return len(self.body)


Fetch = Callable[[str, int], bytes]


def _canonical_project(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _fetch_url(url: str, limit: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "openadapt-evals-release-verifier/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise PublicationAbsent(f"PyPI has not published {url}") from exc
        raise ReleaseVerificationError(f"PyPI request failed with HTTP {exc.code}: {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublicationPending(f"PyPI request is not ready: {url}: {exc}") from exc
    if len(body) > limit:
        raise ReleaseVerificationError(f"PyPI response exceeds the size limit: {url}")
    return body


def _local_artifacts(directory: Path) -> dict[str, Artifact]:
    if not directory.is_dir() or directory.is_symlink():
        raise ReleaseVerificationError(f"distribution directory is missing or invalid: {directory}")

    artifacts: dict[str, Artifact] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.is_symlink():
            raise ReleaseVerificationError(f"unexpected local distribution: {path.name}")
        if path.name == BUILD_TOOL_IGNORE_FILE and path.read_bytes().strip() == b"*":
            # `uv build` writes a one-line `*` .gitignore into a dist/ directory
            # it creates, so the build output never gets committed. It is not a
            # distribution and PyPI never sees it. Anything else hidden in
            # dist/ is still refused below.
            continue
        if path.name.endswith(PUBLISH_ATTESTATION_SUFFIX):
            # pypa/gh-action-pypi-publish writes `<distribution>.publish.attestation`
            # beside each file it uploads. The post-publication verification
            # step runs after that, so the sidecar is expected there; it is
            # not a distribution and PyPI serves it separately, if at all.
            continue
        if path.name.endswith(".whl"):
            package_type = "bdist_wheel"
        elif path.name.endswith(".tar.gz"):
            package_type = "sdist"
        else:
            raise ReleaseVerificationError(f"unexpected local distribution: {path.name}")
        body = path.read_bytes()
        if not body:
            raise ReleaseVerificationError(f"local distribution is empty: {path.name}")
        if len(body) > MAX_ARTIFACT_BYTES:
            raise ReleaseVerificationError(
                f"local distribution exceeds the size limit: {path.name}"
            )
        artifacts[path.name] = Artifact(
            name=path.name,
            package_type=package_type,
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
        )

    if len(artifacts) != 2 or {artifact.package_type for artifact in artifacts.values()} != {
        "bdist_wheel",
        "sdist",
    }:
        raise ReleaseVerificationError("local release must contain exactly one wheel and one sdist")
    return artifacts


def _metadata_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReleaseVerificationError("PyPI returned invalid JSON metadata") from exc
    if not isinstance(value, dict):
        raise ReleaseVerificationError("PyPI metadata is not an object")
    return value


def _published_files(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    urls = document.get("urls")
    if not isinstance(urls, list):
        raise ReleaseVerificationError("PyPI metadata has no release file inventory")

    published: dict[str, dict[str, Any]] = {}
    for entry in urls:
        if not isinstance(entry, dict):
            raise ReleaseVerificationError("PyPI release file metadata is invalid")
        filename = entry.get("filename")
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or filename in published
        ):
            raise ReleaseVerificationError(
                f"PyPI release filename is invalid or duplicated: {filename!r}"
            )
        published[filename] = entry
    return published


def _artifact_url(entry: dict[str, Any], filename: str) -> str:
    value = entry.get("url")
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        port = parsed.port if parsed is not None else None
    except ValueError as exc:
        raise ReleaseVerificationError(f"PyPI artifact URL is invalid: {filename}") from exc
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.netloc != "files.pythonhosted.org"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or Path(parsed.path).name != filename
    ):
        raise ReleaseVerificationError(f"PyPI artifact URL is invalid: {filename}")
    return value


def verify_pypi_release(
    directory: Path,
    version: str,
    *,
    fetch: Fetch = _fetch_url,
    allow_matching_subset: bool = False,
) -> None:
    """Verify all public files; optionally allow an absent or matching subset."""

    if STABLE_VERSION.fullmatch(version) is None:
        raise ReleaseVerificationError(f"version is not an exact stable X.Y.Z value: {version!r}")
    local = _local_artifacts(directory)
    wheel = next(name for name in local if name.endswith(".whl"))
    sdist = next(name for name in local if name.endswith(".tar.gz"))
    normalized_release = f"openadapt_evals-{version}"
    if not wheel.startswith(f"{normalized_release}-"):
        raise ReleaseVerificationError("local wheel does not identify the exact release")
    if sdist != f"{normalized_release}.tar.gz":
        raise ReleaseVerificationError("local sdist does not identify the exact release")
    metadata_url = f"https://pypi.org/pypi/{PYPI_PROJECT}/{quote(version, safe='')}/json"
    try:
        metadata_body = fetch(metadata_url, MAX_METADATA_BYTES)
    except PublicationAbsent:
        if allow_matching_subset:
            return
        raise
    document = _metadata_object(metadata_body)
    info = document.get("info")
    if (
        not isinstance(info, dict)
        or _canonical_project(str(info.get("name") or "")) != PYPI_PROJECT
        or info.get("version") != version
    ):
        raise ReleaseVerificationError(
            "PyPI metadata does not identify the requested package version"
        )

    published = _published_files(document)
    expected_names = set(local)
    published_names = set(published)
    extras = sorted(published_names - expected_names)
    if extras:
        raise ReleaseVerificationError(f"PyPI has unexpected immutable release files: {extras}")
    missing = sorted(expected_names - published_names)
    if missing and not allow_matching_subset:
        raise PublicationPending(f"PyPI release files are not visible yet: {missing}")

    for filename, artifact in local.items():
        if filename not in published:
            continue
        entry = published[filename]
        digests = entry.get("digests")
        remote_digest = digests.get("sha256") if isinstance(digests, dict) else None
        remote_size = entry.get("size")
        if (
            not isinstance(remote_digest, str)
            or SHA256.fullmatch(remote_digest) is None
            or isinstance(remote_size, bool)
            or not isinstance(remote_size, int)
            or remote_size <= 0
            or entry.get("packagetype") != artifact.package_type
            or entry.get("yanked") is not False
        ):
            raise ReleaseVerificationError(f"PyPI release file metadata is invalid: {filename}")
        if remote_digest != artifact.sha256 or remote_size != artifact.size:
            raise ReleaseVerificationError(
                f"PyPI release file metadata differs from the build: {filename}"
            )
        remote_url = _artifact_url(entry, filename)
        public_body = fetch(remote_url, min(MAX_ARTIFACT_BYTES, artifact.size + 1))
        if public_body != artifact.body:
            raise ReleaseVerificationError(f"PyPI release bytes differ from the build: {filename}")


def _verify_with_wait(
    directory: Path,
    version: str,
    *,
    wait_seconds: int,
    poll_seconds: int,
    allow_matching_subset: bool,
) -> None:
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            verify_pypi_release(
                directory,
                version,
                allow_matching_subset=allow_matching_subset,
            )
            return
        except PublicationPending:
            if time.monotonic() >= deadline:
                raise
            time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--wait-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument(
        "--allow-matching-subset",
        action="store_true",
        help="permit an absent release or byte-identical subset before publication",
    )
    args = parser.parse_args()
    if args.wait_seconds < 0 or args.poll_seconds <= 0:
        parser.error("wait seconds must be nonnegative and poll seconds must be positive")
    try:
        _verify_with_wait(
            args.directory,
            args.version,
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
            allow_matching_subset=args.allow_matching_subset,
        )
    except (OSError, ReleaseVerificationError) as exc:
        parser.exit(1, f"{exc}\n")
    scope = (
        "existing immutable PyPI bytes" if args.allow_matching_subset else "immutable PyPI bytes"
    )
    print(f"Verified {scope} for {PYPI_PROJECT} {args.version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
