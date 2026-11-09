"""Verify that project metadata and the editable uv lock entry agree."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _quoted_table_value(text: str, table: str, key: str) -> str:
    table_match = re.search(
        rf"(?ms)^\[{re.escape(table)}\]\s*$\n(?P<body>.*?)(?=^\[|\Z)",
        text,
    )
    if table_match is None:
        raise ValueError(f"missing [{table}] table")
    value_match = re.search(
        rf'(?m)^{re.escape(key)}\s*=\s*"(?P<value>[^"]+)"\s*$',
        table_match.group("body"),
    )
    if value_match is None:
        raise ValueError(f"missing quoted [{table}].{key}")
    return value_match.group("value")


def _editable_lock_entry(lock_text: str, package_name: str) -> tuple[int, int, str]:
    matches: list[tuple[int, int, str]] = []
    for block_match in re.finditer(
        r"(?ms)^\[\[package\]\]\s*$\n.*?(?=^\[\[package\]\]\s*$|\Z)",
        lock_text,
    ):
        block = block_match.group(0)
        name_match = re.search(r'(?m)^name\s*=\s*"(?P<value>[^"]+)"\s*$', block)
        if name_match is None or name_match.group("value") != package_name:
            continue
        if not re.search(
            r'(?m)^source\s*=\s*\{\s*editable\s*=\s*"\."\s*\}\s*$',
            block,
        ):
            continue
        version_match = re.search(
            r'(?m)^version\s*=\s*"(?P<value>[^"]+)"\s*$', block
        )
        if version_match is None:
            raise ValueError(f"editable {package_name!r} lock entry has no version")
        matches.append(
            (
                block_match.start() + version_match.start("value"),
                block_match.start() + version_match.end("value"),
                version_match.group("value"),
            )
        )
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one editable {package_name!r} lock entry; "
            f"found {len(matches)}"
        )
    return matches[0]


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
