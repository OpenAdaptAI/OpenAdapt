#!/usr/bin/env python3
"""Compare one package version on PyPI with the exact files built for it.

Stdlib only. Used by the draft per-package release workflow, which publishes
any workspace package, so the project name is an argument (the launcher's
scripts/verify_pypi_release.py is fixed to `openadapt`).

  --mode preflight   Before upload. Refuse when PyPI already holds a file of
                     this version that is not byte-identical to the local
                     build, or holds a file the local build does not have.
                     A version that is absent, or a matching subset left by an
                     interrupted publish, passes.
  --mode verify      After upload. Wait until PyPI serves every local file
                     with the same SHA-256, and nothing else, for the version.

Exit 0 on success, 1 on a mismatch or timeout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STABLE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def local_files(directory: Path) -> dict[str, str]:
    files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir()
        if p.is_file() and (p.name.endswith(".whl") or p.name.endswith(".tar.gz"))
    }
    if not files:
        raise SystemExit(f"no wheel or sdist in {directory}")
    return files


def remote_files(project: str, version: str) -> dict[str, str] | None:
    url = f"https://pypi.org/pypi/{project}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            document = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    return {item["filename"]: item["digests"]["sha256"] for item in document["urls"]}


def compare(local: dict[str, str], remote: dict[str, str]) -> list[str]:
    problems = []
    for name, digest in sorted(remote.items()):
        if name not in local:
            problems.append(f"PyPI holds {name}, which this build did not produce")
        elif local[name] != digest:
            problems.append(f"{name}: PyPI {digest} != local {local[name]}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--mode", choices=("preflight", "verify"), required=True)
    parser.add_argument("--wait-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args()
    if not PROJECT.match(args.project) or not STABLE.match(args.version):
        raise SystemExit("project or version is malformed")

    local = local_files(args.directory)
    deadline = time.monotonic() + (args.wait_seconds if args.mode == "verify" else 0)
    while True:
        remote = remote_files(args.project, args.version)
        if args.mode == "preflight":
            problems = compare(local, remote or {})
            for line in problems:
                print(f"REFUSED: {line}")
            if not problems:
                state = (
                    "absent"
                    if not remote
                    else f"{len(remote)} matching file(s) already present"
                )
                print(f"OK: {args.project} {args.version} on PyPI is {state}")
            return 1 if problems else 0
        problems = compare(local, remote or {})
        missing = sorted(set(local) - set(remote or {}))
        if not problems and not missing:
            print(
                f"OK: PyPI serves exactly the {len(local)} built file(s) for {args.project} {args.version}"
            )
            return 0
        if problems or time.monotonic() >= deadline:
            for line in problems:
                print(f"MISMATCH: {line}")
            for name in missing:
                print(f"MISSING: {name}")
            return 1
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
