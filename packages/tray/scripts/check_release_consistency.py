#!/usr/bin/env python3
"""Check and synchronize the versioned inputs used by releases."""

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


def _project_identity(root: Path) -> tuple[str, str]:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    project_table = _match(
        r"(?ms)^\[project\]\s*$\n(.*?)(?=^\[|\Z)",
        pyproject,
        "pyproject.toml [project] table",
    )
    return (
        _match(
            r'(?m)^name\s*=\s*"([^"]+)"\s*$',
            project_table,
            "pyproject.toml",
        ),
        _match(
            r'(?m)^version\s*=\s*"([^"]+)"\s*$',
            project_table,
            "pyproject.toml",
        ),
    )


def _editable_lock_version_span(
    lock_text: str, package_name: str
) -> tuple[int, int, str]:
    package_starts = list(re.finditer(r"(?m)^\[\[package\]\]\s*$", lock_text))
    matches: list[tuple[int, int, str]] = []

    for index, package_start in enumerate(package_starts):
        package_end = (
            package_starts[index + 1].start()
            if index + 1 < len(package_starts)
            else len(lock_text)
        )
        block = lock_text[package_start.start() : package_end]
        if not re.search(
            rf'(?m)^name\s*=\s*"{re.escape(package_name)}"\s*$', block
        ):
            continue
        if not re.search(
            r'(?m)^source\s*=\s*\{\s*editable\s*=\s*"\."\s*\}\s*$', block
        ):
            continue

        version = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', block)
        if not version:
            raise ValueError(
                f"editable {package_name!r} lock entry has no version"
            )
        matches.append(
            (
                package_start.start() + version.start(1),
                package_start.start() + version.end(1),
                version.group(1),
            )
        )

    if len(matches) != 1:
        raise ValueError(
            f"expected one editable {package_name!r} lock entry, "
            f"found {len(matches)}"
        )
    return matches[0]


def release_versions(root: Path = ROOT) -> dict[str, str]:
    package_name, project_version = _project_identity(root)
    if package_name != "openadapt-tray":
        raise ValueError(f"unexpected project name: {package_name!r}")

    package_init = (root / "src/openadapt_tray/__init__.py").read_text(
        encoding="utf-8"
    )
    lock = (root / "uv.lock").read_text(encoding="utf-8")
    _, _, lock_version = _editable_lock_version_span(lock, package_name)

    return {
        "pyproject.toml": project_version,
        "src/openadapt_tray/__init__.py": _match(
            r'^__version__ = "([^"]+)"$', package_init, "package __init__"
        ),
        "uv.lock": lock_version,
    }


def synchronize_release_lock(root: Path = ROOT) -> bool:
    """Stamp only the editable root version, preserving reviewed resolution."""
    package_name, project_version = _project_identity(root)
    if package_name != "openadapt-tray":
        raise ValueError(f"unexpected project name: {package_name!r}")

    lock_path = root / "uv.lock"
    lock_text = lock_path.read_text(encoding="utf-8")
    version_start, version_end, lock_version = _editable_lock_version_span(
        lock_text, package_name
    )
    if lock_version == project_version:
        return False

    lock_path.write_text(
        lock_text[:version_start] + project_version + lock_text[version_end:],
        encoding="utf-8",
    )
    if release_versions(root)["uv.lock"] != project_version:
        raise ValueError("failed to synchronize the editable lock version")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write-lock",
        action="store_true",
        help="stamp the project version into the editable root lock entry",
    )
    parser.add_argument("--require-dist", action="store_true")
    args = parser.parse_args()

    if args.write_lock:
        synchronize_release_lock()

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
