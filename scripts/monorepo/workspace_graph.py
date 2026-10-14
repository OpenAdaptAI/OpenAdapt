#!/usr/bin/env python3
"""Workspace graph helper for the monorepo CI and release drafts.

Stdlib only on Python 3.11 and later (Python 3.10 needs `tomli`, which pytest
already installs there), except `check-pins`, which needs `packaging`.

Subcommands:

  list
      Print every workspace package with its directory, version, and the
      workspace packages it depends on.

  plan --changed-files FILE [--github-output PATH]
      Read changed paths (one per line, relative to the repository root),
      find the packages they belong to, add every package that depends on
      them (directly or transitively), and print a GitHub Actions matrix of
      test lanes for that set.

  check-pins
      For every dependency one workspace package declares on another, check
      that the version in this tree satisfies the declared range. uv ignores
      the declared range when it resolves a workspace source, so without this
      check a package could be tested against a sibling version its published
      metadata excludes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the launcher's CI still tests it.
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
ROOT_PACKAGE = "openadapt"

# Paths that can change the behaviour of every package. A change here runs
# every lane.
GLOBAL_PATHS = (
    "uv.lock",
    "pyproject.toml",
    ".python-version",
    "scripts/monorepo/",
    ".github/workflows/monorepo-ci.yml",
)

# Paths at the repository root that belong to the launcher package.
ROOT_PACKAGE_PATHS = ("openadapt/", "tests/", "scripts/", "README.md", "CHANGELOG.md")

# Inputs a package's tests read from another package's tree, beyond its
# declared dependencies. The viewer reads the recordings the capture package
# commits under examples/captures (its CI used to check that repository out).
TEST_DATA_INPUTS = {
    "openadapt-viewer": ("packages/capture/examples/captures/",),
}

# One test lane per package, copied from each standalone repository's CI.
# `sync` is passed to `uv sync --locked --package <name>`; `pytest` runs from
# the package directory. Lanes the standalone repositories run in addition
# (AT-SPI on Linux, Presidio models, the agent's dependency floors and sandbox
# end-to-end test, the evals Prime environment) are listed in
# .github/workflow-drafts/README.md and are not yet ported.
LANES: dict[str, dict] = {
    "openadapt": {
        "sync": "--extra dev --extra ml --extra capture --extra evals --extra viewer"
        " --extra grounding --extra retrieval",
        "lint": "ruff check openadapt/",
        "pytest": "tests/ -q",
        "python": ["3.10", "3.11", "3.12"],
        "os": ["ubuntu-latest"],
        "extra_os": [["macos-latest", "3.12"]],
    },
    "openadapt-types": {
        "sync": "--extra dev",
        "lint": "",
        "pytest": "-q",
        "python": ["3.12"],
        "os": ["ubuntu-latest"],
    },
    "openadapt-privacy": {
        "sync": "--extra dev",
        "lint": "",
        "pytest": "tests/ -q --ignore=tests/test_presidio.py",
        "python": ["3.10", "3.11", "3.12"],
        "os": ["ubuntu-latest"],
    },
    "openadapt-capture": {
        "sync": "--extra dev",
        "lint": "ruff check openadapt_capture/",
        "pytest": "tests/ -q -m 'not slow' --timeout=120",
        "python": ["3.10", "3.11", "3.12"],
        "os": ["ubuntu-latest"],
        "extra_os": [["macos-latest", "3.12"]],
    },
    "openadapt-agent": {
        "sync": "--extra dev",
        "lint": "ruff check src tests scripts",
        "pytest": "-q",
        "python": ["3.10", "3.11", "3.12"],
        "os": ["ubuntu-latest"],
    },
    "openadapt-evals": {
        "sync": "--extra dev",
        "lint": "ruff check .",
        "pytest": "tests/ -q -m 'not heavy and not gpu and not vm'"
        " -k 'not (test_demo_format_and_persistence or test_synthetic_demos)'",
        "python": ["3.12"],
        "os": ["ubuntu-latest"],
    },
    "openadapt-viewer": {
        "sync": "--all-extras",
        "lint": "ruff check .",
        "pytest": "tests/ -q",
        "python": ["3.10", "3.11"],
        "os": ["ubuntu-latest", "macos-latest"],
        "env": {
            "OPENADAPT_CAPTURE_EXAMPLES": "packages/capture/examples/captures",
            "OPENADAPT_CAPTURE_DIR": "packages/capture",
        },
    },
}


def _name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else ""


def load_packages() -> dict[str, dict]:
    """Return {name: {dir, version, requirements}} for the root and members."""
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = root["tool"]["uv"]["workspace"]["members"]
    dirs = [ROOT]
    for pattern in members:
        dirs.extend(
            sorted(p for p in ROOT.glob(pattern) if (p / "pyproject.toml").is_file())
        )
    packages: dict[str, dict] = {}
    for directory in dirs:
        doc = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
        project = doc["project"]
        requirements = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            requirements.extend(extra)
        for group in doc.get("dependency-groups", {}).values():
            requirements.extend(r for r in group if isinstance(r, str))
        packages[_name(project["name"])] = {
            "dir": directory.relative_to(ROOT).as_posix() or ".",
            "version": project["version"],
            "requirements": requirements,
        }
    for name, info in packages.items():
        declared = {_name(r) for r in info["requirements"]}
        info["depends_on"] = sorted((declared & set(packages)) - {name})
    return packages


def dependents(packages: dict[str, dict]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {name: set() for name in packages}
    for name, info in packages.items():
        for dep in info["depends_on"]:
            reverse[dep].add(name)
    return reverse


def owners(path: str, packages: dict[str, dict]) -> set[str]:
    if any(path == g or (g.endswith("/") and path.startswith(g)) for g in GLOBAL_PATHS):
        return set(packages)
    hits = {
        n
        for n, i in packages.items()
        if i["dir"] != "." and path.startswith(i["dir"] + "/")
    }
    for name, inputs in TEST_DATA_INPUTS.items():
        if any(path.startswith(p) for p in inputs):
            hits.add(name)
    if not hits and any(
        path == p or (p.endswith("/") and path.startswith(p))
        for p in ROOT_PACKAGE_PATHS
    ):
        hits.add(ROOT_PACKAGE)
    return hits


def affected(changed: list[str], packages: dict[str, dict]) -> list[str]:
    reverse = dependents(packages)
    selected: set[str] = set()
    for path in changed:
        selected |= owners(path.strip(), packages)
    queue = list(selected)
    while queue:
        for parent in reverse[queue.pop()]:
            if parent not in selected:
                selected.add(parent)
                queue.append(parent)
    return sorted(selected)


def matrix(names: list[str], packages: dict[str, dict]) -> dict:
    include = []
    for name in names:
        lane = LANES.get(name)
        if lane is None:
            raise SystemExit(
                f"{name} has no CI lane in scripts/monorepo/workspace_graph.py"
            )
        combos = [(os_, py) for os_ in lane["os"] for py in lane["python"]]
        combos += [tuple(x) for x in lane.get("extra_os", []) if tuple(x) not in combos]
        env = " ".join(
            f"{k}=$GITHUB_WORKSPACE/{v}" for k, v in lane.get("env", {}).items()
        )
        for os_, py in combos:
            include.append(
                {
                    "package": name,
                    "dir": packages[name]["dir"],
                    "os": os_,
                    "python": py,
                    "sync": lane["sync"],
                    "lint": lane["lint"],
                    "pytest": lane["pytest"],
                    "env": env,
                }
            )
    return {"include": include}


def check_pins(packages: dict[str, dict]) -> int:
    from packaging.requirements import Requirement
    from packaging.version import Version

    failures = 0
    for name, info in sorted(packages.items()):
        for raw in dict.fromkeys(info["requirements"]):
            req = Requirement(raw)
            target = _name(req.name)
            if target not in packages or target == name:
                continue
            version = Version(packages[target]["version"])
            ok = req.specifier.contains(version, prereleases=True)
            status = "ok  " if ok else "FAIL"
            print(
                f"{status} {name} requires {req.name}{req.specifier or ''}; tree has {version}"
            )
            failures += not ok
    if failures:
        print(f"{failures} internal requirement(s) exclude the version in this tree.")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    plan = sub.add_parser("plan")
    plan.add_argument("--changed-files", required=True, type=Path)
    plan.add_argument("--github-output", type=Path)
    sub.add_parser("check-pins")
    args = parser.parse_args()
    packages = load_packages()

    if args.command == "list":
        for name, info in sorted(packages.items()):
            print(
                f"{name:20s} {info['version']:9s} {info['dir']:20s} -> {', '.join(info['depends_on']) or '-'}"
            )
        return 0
    if args.command == "check-pins":
        return check_pins(packages)

    changed = [
        line for line in args.changed_files.read_text().splitlines() if line.strip()
    ]
    names = affected(changed, packages)
    result = matrix(names, packages)
    print(json.dumps({"packages": names, "lanes": len(result["include"])}, indent=2))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as out:
            out.write(f"packages={json.dumps(names)}\n")
            out.write(f"matrix={json.dumps(result)}\n")
            out.write(f"any={'true' if names else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
