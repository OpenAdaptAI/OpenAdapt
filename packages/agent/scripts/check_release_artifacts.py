#!/usr/bin/env python3
"""License-hygiene / package-boundary gate for the built release archives.

Enforces the workspace release rule (AGENTS.md 4.1 / 4.2): a public,
MIT-licensed wheel/sdist must ship code only and must never carry a user's
compiled workflow bundle, an encrypted artifact, run outputs, secrets, or
any other data/crown-jewel material. This inspects the ACTUAL built
archives in ``dist/`` (not the source tree), because that is the only
thing that gets published.

Usage::

    python -m build
    python scripts/check_release_artifacts.py            # checks ./dist
    python scripts/check_release_artifacts.py path/to/dist

Exits nonzero (loudly) on the first violation.
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

# Substrings that must never appear in a published archive member path.
# Guards against accidentally vendoring a user's bundle, an encrypted
# artifact, run evidence, or a credential file into the wheel/sdist.
# Precise path fragments (not broad words like "secret", which would flag
# a legitimately-named workflow file such as secret-scan.yml).
FORBIDDEN_SUBSTRINGS = (
    "workflow.json",  # a compiled bundle (user's private artifact)
    ".enc",  # encrypted-at-rest bundle
    "/bundle/",  # a copied bundle directory
    "/runs/",  # run outputs (may contain live observations / PHI)
    "/.env",
    "id_rsa",
    "id_ed25519",
    ".pem",
    ".sqlite",
    ".db",
)

# The wheel must contain ONLY the import package and its metadata.
ALLOWED_WHEEL_PREFIXES = ("openadapt_agent/",)
# Extensions allowed inside the wheel's import package (code + typing only).
ALLOWED_WHEEL_SUFFIXES = (".py", ".pyi", ".typed")


def _wheel_members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return [n for n in zf.namelist() if not n.endswith("/")]


def _sdist_members(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as tf:
        return [m.name for m in tf.getmembers() if m.isfile()]


def _check_forbidden(archive: str, members: list[str]) -> list[str]:
    problems = []
    for name in members:
        lowered = name.lower()
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad in lowered:
                problems.append(f"{archive}: forbidden member {name!r} (matched {bad!r})")
    return problems


def _check_wheel_is_code_only(archive: str, members: list[str]) -> list[str]:
    problems = []
    saw_license = False
    for name in members:
        if ".dist-info/" in name:
            base = name.rsplit("/", 1)[-1]
            if base.startswith("LICENSE") or base == "METADATA":
                saw_license = saw_license or base.startswith("LICENSE")
            continue
        if not name.startswith(ALLOWED_WHEEL_PREFIXES):
            problems.append(f"{archive}: unexpected top-level member {name!r} (wheel must be code only)")
            continue
        if not name.endswith(ALLOWED_WHEEL_SUFFIXES):
            problems.append(
                f"{archive}: non-code file {name!r} in wheel "
                f"(allowed suffixes: {ALLOWED_WHEEL_SUFFIXES})"
            )
    if not saw_license:
        problems.append(f"{archive}: no LICENSE file found in .dist-info (MIT notice required)")
    return problems


def main(argv: list[str]) -> int:
    dist_dir = Path(argv[1]) if len(argv) > 1 else Path("dist")
    if not dist_dir.is_dir():
        print(f"ERROR: dist directory not found: {dist_dir}", file=sys.stderr)
        return 2

    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if not wheels or not sdists:
        print(
            f"ERROR: expected at least one wheel and one sdist in {dist_dir} "
            f"(found {len(wheels)} wheel(s), {len(sdists)} sdist(s)); run `python -m build` first.",
            file=sys.stderr,
        )
        return 2

    problems: list[str] = []
    for wheel in wheels:
        members = _wheel_members(wheel)
        problems += _check_forbidden(wheel.name, members)
        problems += _check_wheel_is_code_only(wheel.name, members)
    for sdist in sdists:
        members = _sdist_members(sdist)
        problems += _check_forbidden(sdist.name, members)

    if problems:
        print("RELEASE ARTIFACT CHECK FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        f"Release artifact check passed: {len(wheels)} wheel(s) + {len(sdists)} "
        f"sdist(s) in {dist_dir} are code-only and carry no bundle/secret/data material."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
