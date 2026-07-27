"""Verify that a built MCPB contains code/config only, never workflow data."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

_ROOT_FILES = {
    ".mcpbignore",
    "LICENSE",
    "README.md",
    "manifest.json",
    "pyproject.toml",
    "uv.lock",
}
_SOURCE_PREFIX = "src/openadapt_agent/"
_SOURCE_SUFFIXES = (".py", ".pyi", ".typed")
_FORBIDDEN = (
    "workflow.json",
    ".enc",
    "/runs/",
    "/bundles/",
    "/.env",
    ".pem",
    ".sqlite",
    "openimis",
    "real_emr",
    "oracle_recipe",
    "held_out_corpus",
)


def check(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        members = {
            item.filename: archive.read(item) for item in archive.infolist() if not item.is_dir()
        }

    if "manifest.json" not in members:
        raise ValueError(f"{path}: manifest.json is missing")
    manifest = json.loads(members["manifest.json"])
    if manifest.get("name") != "openadapt-agent":
        raise ValueError(f"{path}: unexpected MCPB identity")

    problems: list[str] = []
    for name in members:
        lowered = f"/{name.lower()}"
        if any(marker in lowered for marker in _FORBIDDEN):
            problems.append(f"forbidden private/data path: {name}")
        elif name in _ROOT_FILES or (
            name.startswith(_SOURCE_PREFIX) and name.endswith(_SOURCE_SUFFIXES)
        ):
            continue
        else:
            problems.append(f"unexpected MCPB member: {name}")

    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    print(f"PASS {path}: {len(members)} code/config files, no workflow or evidence payload")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_mcpb.py <bundle.mcpb> [...]", file=sys.stderr)
        return 2
    try:
        for value in argv[1:]:
            check(Path(value))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"MCPB CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
