"""Verify that a built MCPB contains code/config only, never workflow data."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 release check
    import tomli as tomllib

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
_SKILL_FILE = "SKILL.md"
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


def _is_skill_markdown(name: str) -> bool:
    """Allow the portable SKILL.md files, never a compiled bundle beside them."""
    parts = name.split("/")
    return (
        len(parts) == 3
        and parts[0] == "skills"
        and parts[2] == _SKILL_FILE
        and parts[1] not in {".", "..", ""}
        and "\\" not in parts[1]
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

    if "uv.lock" not in members:
        raise ValueError(f"{path}: uv.lock is missing")
    try:
        lock = tomllib.loads(members["uv.lock"].decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: uv.lock is not valid UTF-8 TOML") from exc
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError(f"{path}: uv.lock has no package inventory")
    package_names = {
        package.get("name")
        for package in packages
        if isinstance(package, dict) and isinstance(package.get("name"), str)
    }
    flow_packages = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == "openadapt-flow"
    ]
    if len(flow_packages) != 1 or not isinstance(flow_packages[0].get("version"), str):
        raise ValueError(f"{path}: uv.lock must contain one exact openadapt-flow release")
    opencv_providers = package_names & {"opencv-python", "opencv-python-headless"}
    if len(opencv_providers) != 1:
        raise ValueError(
            f"{path}: uv.lock must contain one OpenCV provider, found "
            f"{sorted(opencv_providers)}"
        )

    problems: list[str] = []
    for name in members:
        lowered = f"/{name.lower()}"
        if any(marker in lowered for marker in _FORBIDDEN):
            problems.append(f"forbidden private/data path: {name}")
        elif name in _ROOT_FILES or (
            name.startswith(_SOURCE_PREFIX) and name.endswith(_SOURCE_SUFFIXES)
        ) or _is_skill_markdown(name):
            continue
        else:
            problems.append(f"unexpected MCPB member: {name}")

    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    print(
        f"PASS {path}: {len(members)} code/config files, "
        f"Flow {flow_packages[0]['version']}, one OpenCV provider, "
        "no workflow or evidence payload"
    )


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
