"""Verify that project metadata and the editable uv lock entry agree."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _quoted_table_value(text: str, table: str, key: str) -> str:
    current_table = ""
    assignment = re.compile(rf'^{re.escape(key)}\s*=\s*"([^"]+)"\s*$')

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            current_table = line
            continue
        if current_table != f"[{table}]":
            continue
        match = assignment.match(line)
        if match:
            return match.group(1)

    raise ValueError(f"missing quoted {key!r} in [{table}]")


def _editable_lock_entry(lock_text: str, package_name: str) -> tuple[int, int, str]:
    starts = list(re.finditer(r"(?m)^\[\[package\]\]\s*$", lock_text))
    editable_versions: list[tuple[int, int, str]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(lock_text)
        block = lock_text[start.start() : end]
        if not re.search(rf'(?m)^name\s*=\s*"{re.escape(package_name)}"\s*$', block):
            continue
        if not re.search(r'(?m)^source\s*=\s*\{\s*editable\s*=\s*"\."\s*\}\s*$', block):
            continue
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', block)
        if not match:
            raise ValueError(f"editable {package_name!r} lock entry has no version")
        editable_versions.append(
            (
                start.start() + match.start(1),
                start.start() + match.end(1),
                match.group(1),
            )
        )

    if len(editable_versions) != 1:
        raise ValueError(
            f"expected one editable {package_name!r} lock entry, "
            f"found {len(editable_versions)}"
        )
    return editable_versions[0]


def _project_identity(root: Path) -> tuple[str, str]:
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    return (
        _quoted_table_value(project_text, "project", "name"),
        _quoted_table_value(project_text, "project", "version"),
    )


def release_versions(root: Path = ROOT) -> tuple[str, str]:
    """Return ``(project_version, editable_lock_version)``."""
    package_name, project_version = _project_identity(root)
    lock_text = (root / "uv.lock").read_text(encoding="utf-8")
    _, _, lock_version = _editable_lock_entry(lock_text, package_name)
    return project_version, lock_version


def synchronize_release_lock(root: Path = ROOT) -> bool:
    """Stamp only the editable root version, preserving reviewed resolution."""
    package_name, project_version = _project_identity(root)
    lock_path = root / "uv.lock"
    lock_text = lock_path.read_text(encoding="utf-8")
    version_start, version_end, lock_version = _editable_lock_entry(
        lock_text, package_name
    )
    if lock_version == project_version:
        return False

    lock_path.write_text(
        lock_text[:version_start] + project_version + lock_text[version_end:],
        encoding="utf-8",
    )
    verify_release_lock(root)
    return True


def verify_release_lock(root: Path = ROOT) -> None:
    project_version, lock_version = release_versions(root)
    if project_version != lock_version:
        raise ValueError(
            "release version drift: "
            f"pyproject.toml={project_version}, uv.lock={lock_version}; "
            "run `python scripts/verify_release_lock.py --write`"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="stamp the project version into the editable lock entry before checking",
    )
    args = parser.parse_args()

    try:
        if args.write:
            synchronize_release_lock()
        verify_release_lock()
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
