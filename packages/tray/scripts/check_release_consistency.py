#!/usr/bin/env python3
"""Check that release version sources and built distributions agree."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _match(pattern: str, text: str, source: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"could not read version from {source}")
    return match.group(1)


def release_versions() -> dict[str, str]:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package_init = (ROOT / "src/openadapt_tray/__init__.py").read_text(
        encoding="utf-8"
    )
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    return {
        "pyproject.toml": _match(
            r"\[project\]\s+name = \"openadapt-tray\"\s+version = \"([^\"]+)\"",
            pyproject,
            "pyproject.toml",
        ),
        "src/openadapt_tray/__init__.py": _match(
            r'^__version__ = "([^"]+)"$', package_init, "package __init__"
        ),
        "uv.lock": _match(
            r"\[\[package\]\]\s+name = \"openadapt-tray\"\s+version = \"([^\"]+)\""
            r"\s+source = \{ editable = \"\.\" \}",
            lock,
            "uv.lock",
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-dist", action="store_true")
    args = parser.parse_args()

    versions = release_versions()
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        parser.error(f"release versions differ: {versions}")
    version = unique_versions.pop()

    if args.require_dist:
        distributions = list((ROOT / "dist").glob(f"openadapt_tray-{version}*"))
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
