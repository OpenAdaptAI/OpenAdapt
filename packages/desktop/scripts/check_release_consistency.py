#!/usr/bin/env python3
"""Check that release version sources and built distributions agree."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PACKAGE_PATTERN = re.compile(
    r'(?P<prefix>\[\[package\]\]\nname = "openadapt-desktop"\nversion = ")'
    r'[^\"]+(?P<suffix>"\nsource = \{ editable = "\." \})',
    flags=re.MULTILINE,
)


def _match(pattern: str, text: str, source: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"could not read version from {source}")
    return match.group(1)


def release_versions(root: Path = ROOT) -> dict[str, str]:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    package_init = (root / "engine/__init__.py").read_text(encoding="utf-8")
    lock = (root / "uv.lock").read_text(encoding="utf-8")

    return {
        "pyproject.toml": _match(
            r'\[project\]\s+name = "openadapt-desktop"\s+version = "([^"]+)"',
            pyproject,
            "pyproject.toml",
        ),
        "engine/__init__.py": _match(
            r'^__version__ = "([^"]+)"$', package_init, "engine/__init__.py"
        ),
        "uv.lock": _match(
            r'\[\[package\]\]\s+name = "openadapt-desktop"\s+version = "([^"]+)"'
            r'\s+source = \{ editable = "\." \}',
            lock,
            "uv.lock",
        ),
    }


def sync_lock_version(root: Path = ROOT) -> str:
    versions = release_versions(root)
    source_versions = {
        versions["pyproject.toml"],
        versions["engine/__init__.py"],
    }
    if len(source_versions) != 1:
        raise ValueError(f"release source versions differ: {versions}")
    version = source_versions.pop()

    lock_path = root / "uv.lock"
    lock = lock_path.read_text(encoding="utf-8")
    updated, replacements = LOCK_PACKAGE_PATTERN.subn(
        rf"\g<prefix>{version}\g<suffix>", lock
    )
    if replacements != 1:
        raise ValueError(
            "expected exactly one editable openadapt-desktop package in uv.lock, "
            f"found {replacements}"
        )
    lock_path.write_text(updated, encoding="utf-8")

    synchronized = release_versions(root)
    if len(set(synchronized.values())) != 1:
        raise ValueError(f"release versions differ after lock sync: {synchronized}")
    return version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--require-dist", action="store_true")
    args = parser.parse_args()

    if args.sync:
        sync_lock_version()

    versions = release_versions()
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        parser.error(f"release versions differ: {versions}")
    version = unique_versions.pop()

    if args.require_dist:
        distributions = list((ROOT / "dist").glob(f"openadapt_desktop-{version}*"))
        names = [path.name for path in distributions]
        if not any(name.endswith(".whl") for name in names) or not any(
            name.endswith(".tar.gz") for name in names
        ):
            parser.error(
                f"missing wheel or source distribution for {version}: {distributions}"
            )

    print(f"Release version {version} is synchronized across project, module, and lock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
