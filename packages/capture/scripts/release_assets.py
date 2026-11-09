#!/usr/bin/env python3
"""Prepare the GitHub Release body and prove its assets are the PyPI artifacts.

Both release paths in ``release.yml`` attach assets with
``gh release create "$tag" dist/* --verify-tag``. Before they do, this script
proves ``dist`` holds exactly one wheel and one sdist, and that PyPI serves
those exact files for that version (SHA-256 digests). The tag path built
``dist`` and uploaded it to PyPI. The existing-tag path downloads the files
from PyPI, checks them against PyPI's own digests, and requires them to equal
a rebuild of the tagged source, so no asset can differ from PyPI.

Subcommands:

``notes``      write the ``## vX.Y.Z (date)`` section of CHANGELOG.md.
``check-pypi`` require ``dist`` to be exactly the PyPI artifacts.
``fetch-pypi`` download the PyPI artifacts into ``dist`` and require them to
               equal a rebuilt ``--rebuilt`` directory.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_release_publication import (  # noqa: E402
    PublicationError,
    _json_document,
    _request,
    artifact_digests,
)

RELEASE_HEADING = re.compile(r"^## v(?P<version>\d+\.\d+\.\d+) \(\d{4}-\d{2}-\d{2}\)$")
STABLE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
PYPI_FILES_HOST = "files.pythonhosted.org"


def release_notes(changelog: str, version: str) -> str:
    """Return the exact CHANGELOG section for one version, heading included."""
    if not STABLE_VERSION.fullmatch(version):
        raise PublicationError(f"not an exact stable version: {version!r}")
    lines = changelog.splitlines()
    starts = [index for index, line in enumerate(lines) if RELEASE_HEADING.match(line)]
    matching = [
        index for index in starts if RELEASE_HEADING.match(lines[index])["version"] == version
    ]
    if len(matching) != 1:
        raise PublicationError(
            f"CHANGELOG.md must contain exactly one v{version} section, found {len(matching)}"
        )
    start = matching[0]
    later = [index for index in starts if index > start]
    end = later[0] if later else len(lines)
    section = "\n".join(lines[start:end]).strip()
    if section == lines[start].strip():
        raise PublicationError(f"the v{version} CHANGELOG section is empty")
    return section + "\n"


def exact_dist(dist: Path) -> dict[str, str]:
    """Require ``dist/*`` to expand to one wheel, one sdist, and nothing else.

    A shell glob skips hidden names, so ``uv build``'s own ``dist/.gitignore``
    is not an asset; every other entry, such as a ``*.publish.attestation``
    file the PyPI action writes, must not be there.
    """
    if not dist.is_dir():
        raise PublicationError(f"{dist} is not a directory")
    entries = sorted(path.name for path in dist.iterdir() if not path.name.startswith("."))
    digests = artifact_digests(dist)
    if entries != sorted(digests):
        extra = sorted(set(entries) - set(digests))
        raise PublicationError(f"dist holds files that are not release artifacts: {extra}")
    return digests


def pypi_files(package: str, version: str) -> list[dict[str, Any]]:
    """Return PyPI's file entries for one version; fail if the version is absent."""
    url = (
        "https://pypi.org/pypi/"
        f"{urllib.parse.quote(package, safe='')}/{urllib.parse.quote(version, safe='')}/json"
    )
    document = _json_document(_request(url), surface="PyPI")
    if document is None:
        raise PublicationError(f"{package} {version} is not on PyPI")
    urls = document.get("urls")
    if not isinstance(urls, list) or not urls:
        raise PublicationError("PyPI response has no artifact list")
    files = []
    for entry in urls:
        if not isinstance(entry, dict):
            raise PublicationError("PyPI response has an invalid artifact entry")
        filename = entry.get("filename")
        file_url = entry.get("url")
        digests = entry.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not all(isinstance(value, str) for value in (filename, file_url, digest)):
            raise PublicationError("PyPI response has an incomplete artifact entry")
        if Path(filename).name != filename or filename.startswith("."):
            raise PublicationError(f"PyPI returned an unsafe file name: {filename!r}")
        parsed = urllib.parse.urlsplit(file_url)
        if parsed.scheme != "https" or parsed.hostname != PYPI_FILES_HOST:
            raise PublicationError(f"PyPI artifact is not served from {PYPI_FILES_HOST}")
        files.append({"filename": filename, "url": file_url, "sha256": digest})
    return files


def pypi_digests(package: str, version: str) -> dict[str, str]:
    return {entry["filename"]: entry["sha256"] for entry in pypi_files(package, version)}


def check_pypi(dist: Path, package: str, version: str) -> dict[str, str]:
    """Require ``dist`` to be exactly the artifacts PyPI serves for ``version``."""
    local = exact_dist(dist)
    remote = pypi_digests(package, version)
    if local != remote:
        raise PublicationError(f"dist differs from PyPI: dist={local}, PyPI={remote}")
    return local


def fetch_pypi(dist: Path, rebuilt: Path, package: str, version: str) -> dict[str, str]:
    """Download the PyPI artifacts into an empty ``dist``; they must equal the rebuild."""
    expected = exact_dist(rebuilt)
    files = pypi_files(package, version)
    remote = {entry["filename"]: entry["sha256"] for entry in files}
    if remote != expected:
        raise PublicationError(
            f"PyPI artifacts differ from the tagged rebuild: PyPI={remote}, rebuild={expected}"
        )
    dist.mkdir(parents=True, exist_ok=True)
    if any(not path.name.startswith(".") for path in dist.iterdir()):
        raise PublicationError(f"{dist} must be empty before PyPI artifacts are fetched")
    for entry in files:
        content = _request(entry["url"], accept="application/octet-stream")
        if content is None:
            raise PublicationError(f"PyPI artifact disappeared: {entry['filename']}")
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise PublicationError(f"downloaded {entry['filename']} does not match PyPI's digest")
        (dist / entry["filename"]).write_bytes(content)
    return check_pypi(dist, package, version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    notes = commands.add_parser("notes")
    notes.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    notes.add_argument("--version", required=True)
    notes.add_argument("--output", type=Path, required=True)
    for name in ("check-pypi", "fetch-pypi"):
        command = commands.add_parser(name)
        command.add_argument("--dist", type=Path, required=True)
        command.add_argument("--package", required=True)
        command.add_argument("--version", required=True)
        if name == "fetch-pypi":
            command.add_argument("--rebuilt", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "notes":
        text = release_notes(args.changelog.read_text(encoding="utf-8"), args.version)
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote the v{args.version} CHANGELOG section to {args.output}.")
        return 0
    if args.command == "check-pypi":
        digests = check_pypi(args.dist, args.package, args.version)
    else:
        digests = fetch_pypi(args.dist, args.rebuilt, args.package, args.version)
    for name, digest in sorted(digests.items()):
        print(f"{digest}  {name}")
    print(f"dist is exactly the PyPI artifacts of {args.package} {args.version}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicationError as exc:
        raise SystemExit(f"error: {exc}") from None
