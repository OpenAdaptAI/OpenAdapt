#!/usr/bin/env python3
"""Fetch a release's files from PyPI, or check local files against PyPI.

    python scripts/ci/pypi_dist.py fetch openadapt-flow 2.4.0 dist/
    python scripts/ci/pypi_dist.py compare openadapt-flow 2.4.0 dist/

`fetch` downloads the version's sdist and wheels into the directory and checks
each against the SHA-256 digest PyPI lists, so a resumed release carries the
exact bytes PyPI serves. `compare` requires PyPI to serve exactly the files in
the directory, with the same digests: it proves an upload finished before the
release is tagged. Both retry for a while, since PyPI's JSON can lag an
upload.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def pypi_files(project: str, version: str) -> tuple[dict[str, str], list[dict]]:
    """({filename: sha256}, PyPI's file entries), or ({}, []) for no such version."""
    try:
        url = f"https://pypi.org/pypi/{project}/{version}/json"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}, []
        raise
    return {item["filename"]: item["digests"]["sha256"] for item in data["urls"]}, data[
        "urls"
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(local: dict[str, str], remote: dict[str, str]) -> list[str]:
    problems = [f"PyPI lacks {name}" for name in sorted(set(local) - set(remote))]
    problems += [f"PyPI has extra {name}" for name in sorted(set(remote) - set(local))]
    problems += [
        f"{name} differs from PyPI's"
        for name in sorted(set(local) & set(remote))
        if local[name] != remote[name]
    ]
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[0] not in ("fetch", "compare"):
        raise SystemExit(__doc__)
    command, project, version, directory = argv
    out = Path(directory)
    for attempt in range(12):
        digests, urls = pypi_files(project, version)
        if command == "fetch" and digests:
            kinds = {item["packagetype"] for item in urls}
            if not {"sdist", "bdist_wheel"} <= kinds:
                print(
                    f"PyPI {project} {version} has only {sorted(kinds)}.",
                    file=sys.stderr,
                )
                return 1
            out.mkdir(parents=True, exist_ok=True)
            for item in urls:
                target = out / item["filename"]
                with urllib.request.urlopen(item["url"], timeout=120) as response:
                    target.write_bytes(response.read())
                if _sha256(target) != item["digests"]["sha256"]:
                    print(
                        f"{target.name} does not match PyPI's digest.", file=sys.stderr
                    )
                    return 1
                print(f"fetched {target.name}")
            return 0
        if command == "compare" and digests:
            local = {
                path.name: _sha256(path)
                for path in sorted(out.iterdir())
                if path.is_file()
            }
            problems = compare(local, digests)
            if not problems:
                print(f"PyPI serves exactly {', '.join(sorted(local))}")
                return 0
            if attempt == 11:
                print("; ".join(problems), file=sys.stderr)
                return 1
        time.sleep(10)
    print(f"PyPI has no {project} {version}.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
