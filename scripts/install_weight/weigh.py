"""Measure and enforce what installing one OpenAdapt package alone costs.

Each package in this repository is published on its own, and each must stay
cheap to install: someone who installs openadapt-privacy must not get Flow's
vision stack, and openadapt-types must stay a Pydantic-only leaf.

``check`` builds the wheel of every in-tree package listed in budgets.json,
installs each one alone into a fresh virtual environment, and fails if the
install pulls a distribution outside that package's allow list, pulls a
forbidden one, or exceeds its size or distribution-count budget::

    python scripts/install_weight/weigh.py check
    python scripts/install_weight/weigh.py check --only types --only privacy

``measure`` records the same numbers for arbitrary requirements, such as the
published releases::

    python scripts/install_weight/weigh.py measure --out before.json \\
        "types=openadapt-types==0.23.0:openadapt_types"

In-tree wheels are built with a ``+weight`` local version label. It satisfies
every ``>=``, ``<`` and ``==`` specifier on the release version and sorts above
the release on PyPI, so a package that depends on another in-tree package
installs the in-tree wheel. Each environment is deleted after it is measured.
Needs ``uv`` on PATH.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUDGETS = Path(__file__).resolve().with_name("budgets.json")
LOCAL_LABEL = "weight"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _site_packages(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Lib" / "site-packages"
    return next((venv / "lib").glob("python*/site-packages"))


def _dir_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath, name)
            if not path.is_symlink():
                total += path.stat().st_size
    return total


class ImportFailed(RuntimeError):
    """The installed package does not import."""


def _import_seconds(python: Path, module: str) -> float:
    code = f"import time; t = time.perf_counter(); import {module}; print(time.perf_counter() - t)"
    done = subprocess.run([str(python), "-I", "-c", code], capture_output=True, text=True)
    if done.returncode != 0:
        raise ImportFailed(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "")
    return float(done.stdout.strip().splitlines()[-1])


def measure_one(
    label: str,
    requirement: str,
    module: str,
    *,
    python: str,
    find_links: list[str],
    repeats: int = 3,
) -> dict:
    """Install ``requirement`` alone into a fresh environment and measure it."""
    work = Path(tempfile.mkdtemp(prefix=f"weight-{label}-"))
    venv = work / "venv"
    try:
        subprocess.run(["uv", "venv", "-q", "--python", python, str(venv)], check=True)
        install = ["uv", "pip", "install", "-q", "--python", str(_python(venv))]
        for links in find_links:
            install += ["--find-links", links]
        started = time.perf_counter()
        subprocess.run([*install, requirement], check=True)
        install_seconds = time.perf_counter() - started
        listing = json.loads(
            subprocess.run(
                ["uv", "pip", "list", "--format", "json", "--python", str(_python(venv))],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        dists = sorted((_normalize(item["name"]), item["version"]) for item in listing)
        size = _dir_bytes(_site_packages(venv))
        import_error = None
        try:
            first = _import_seconds(_python(venv), module)
            warm = [_import_seconds(_python(venv), module) for _ in range(repeats)]
        except ImportFailed as exc:
            import_error = str(exc) or f"import {module} failed"
            first, warm = float("nan"), [float("nan")]
        return {
            "label": label,
            "requirement": requirement,
            "module": module,
            "python": python,
            "platform": sys.platform,
            "dist_count": len(dists),
            "dists": [f"{name}=={version}" for name, version in dists],
            "site_packages_bytes": size,
            "install_seconds": round(install_seconds, 2),
            "import_first_seconds": round(first, 3),
            "import_warm_median_seconds": round(statistics.median(warm), 3),
            "import_error": import_error,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _project_field(pyproject: str, field: str) -> str:
    project = pyproject[pyproject.index("[project]") :]
    match = re.search(rf'(?m)^{field} = "([^"]+)"', project)
    if match is None:
        raise ValueError(f"no static {field} in [project]")
    return match.group(1)


def build_local_wheel(path: Path, out_dir: Path) -> tuple[str, str]:
    """Build ``path`` as ``<version>+weight`` and return (dist name, version)."""
    pyproject_path = path / "pyproject.toml"
    original = pyproject_path.read_text(encoding="utf-8")
    name = _project_field(original, "name")
    version = f"{_project_field(original, 'version')}+{LOCAL_LABEL}"
    head, tail = original.split("[project]", 1)
    patched = head + "[project]" + re.sub(
        r'(?m)^version = "[^"]+"', f'version = "{version}"', tail, count=1
    )
    try:
        pyproject_path.write_text(patched, encoding="utf-8")
        subprocess.run(
            ["uv", "build", "-q", "--wheel", "--out-dir", str(out_dir), str(path)],
            check=True,
        )
    finally:
        pyproject_path.write_text(original, encoding="utf-8")
    return name, version


def violations(result: dict, budget: dict, defaults: dict) -> list[str]:
    """Every way ``result`` breaks ``budget``. Empty means it passes."""
    names = [entry.split("==", 1)[0] for entry in result["dists"]]
    forbid = [_normalize(n) for n in defaults.get("forbid", []) + budget.get("forbid", [])]
    allow = [_normalize(n) for n in budget["allow"]]
    problems = []
    if result.get("import_error"):
        problems.append(f"does not import alone: {result['import_error']}")
    for name in names:
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in forbid):
            problems.append(f"pulls forbidden distribution {name}")
        elif not any(fnmatch.fnmatchcase(name, pattern) for pattern in allow):
            problems.append(f"pulls {name}, which is not in its allow list")
    megabytes = result["site_packages_bytes"] / 1e6
    if megabytes > budget["max_mb"]:
        problems.append(f"installs {megabytes:.1f} MB, over its {budget['max_mb']} MB budget")
    if result["dist_count"] > budget["max_dists"]:
        problems.append(
            f"installs {result['dist_count']} distributions, over its budget of {budget['max_dists']}"
        )
    return problems


def _markdown(rows: list[tuple[dict, list[str]]]) -> str:
    lines = [
        "| Package | Source | Dists | Size | Import (first / warm) | Result |",
        "|---|---|--:|--:|--:|---|",
    ]
    for result, problems in rows:
        lines.append(
            f"| {result['label']} | `{result['requirement']}` | {result['dist_count']} "
            f"/ {result['budget']['max_dists']} | "
            f"{result['site_packages_bytes'] / 1e6:.1f} / {result['budget']['max_mb']} MB | "
            f"{result['import_first_seconds']:.2f} s / {result['import_warm_median_seconds']:.2f} s | "
            f"{'OK' if not problems else '<br>'.join(problems)} |"
        )
    return "\n".join(lines) + "\n"


def check(args: argparse.Namespace) -> int:
    config = json.loads(Path(args.budgets).read_text(encoding="utf-8"))
    defaults = config.get("defaults", {})
    selected = {
        label: budget
        for label, budget in config["packages"].items()
        if not args.only or label in args.only
    }
    unknown = set(args.only or []) - set(config["packages"])
    if unknown:
        raise SystemExit(f"unknown package label(s): {sorted(unknown)}")
    dist_dir = Path(args.dist_dir or tempfile.mkdtemp(prefix="weight-dist-"))
    dist_dir.mkdir(parents=True, exist_ok=True)
    # Build every in-tree package, not only the selected ones, so a selected
    # package resolves its in-tree dependencies to the in-tree wheels.
    requirements = {}
    for label, budget in config["packages"].items():
        path = ROOT / budget["path"] if "path" in budget else None
        if path is not None and (path / "pyproject.toml").is_file():
            name, version = build_local_wheel(path, dist_dir)
            requirements[label] = f"{name}=={version}"
        elif "fallback_requirement" in budget:
            requirements[label] = budget["fallback_requirement"]
        else:
            raise SystemExit(f"{label}: {budget.get('path')} has no pyproject.toml")
    rows = []
    for label, budget in selected.items():
        print(f"weighing {label}: {requirements[label]}", flush=True)
        result = measure_one(
            label,
            requirements[label],
            budget["module"],
            python=config.get("python", "3.12"),
            find_links=[str(dist_dir)],
        )
        result["budget"] = {"max_mb": budget["max_mb"], "max_dists": budget["max_dists"]}
        problems = violations(result, budget, defaults)
        for problem in problems:
            print(f"  FAIL {label} {problem}", flush=True)
        print(
            f"  {result['dist_count']} dists, {result['site_packages_bytes'] / 1e6:.1f} MB, "
            f"import {result['import_first_seconds']} s first / "
            f"{result['import_warm_median_seconds']} s warm",
            flush=True,
        )
        rows.append((result, problems))
    report = _markdown(rows)
    print(report)
    if args.json:
        Path(args.json).write_text(
            json.dumps([dict(r, violations=p) for r, p in rows], indent=2) + "\n",
            encoding="utf-8",
        )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("## Install weight\n\n" + report)
    if not args.dist_dir:
        shutil.rmtree(dist_dir, ignore_errors=True)
    failed = [result["label"] for result, problems in rows if problems]
    if failed:
        print(
            f"Install weight check failed for: {', '.join(failed)}.\n"
            "If a new dependency is intended, add it to that package's allow list in "
            "scripts/install_weight/budgets.json with the reason, or move it into an "
            "extra. See docs/monorepo/INSTALL_WEIGHT.md."
        )
        return 1
    return 0


def measure(args: argparse.Namespace) -> int:
    results = []
    for spec in args.specs:
        label, rest = spec.split("=", 1)
        requirement, module = rest.rsplit(":", 1)
        print(f"measuring {label}: {requirement}", flush=True)
        result = measure_one(
            label,
            requirement,
            module,
            python=args.python,
            find_links=args.find_links,
            repeats=args.repeats,
        )
        print(
            f"  {result['dist_count']} dists, {result['site_packages_bytes'] / 1e6:.1f} MB, "
            f"import {result['import_first_seconds']} s first / "
            f"{result['import_warm_median_seconds']} s warm",
            flush=True,
        )
        results.append(result)
        Path(args.out).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="build, install and enforce budgets")
    check_parser.add_argument("--budgets", default=str(BUDGETS))
    check_parser.add_argument("--only", action="append", help="package label; repeatable")
    check_parser.add_argument("--dist-dir", help="keep the built wheels here")
    check_parser.add_argument("--json", help="write the measurements here")
    check_parser.set_defaults(func=check)
    measure_parser = commands.add_parser("measure", help="measure arbitrary requirements")
    measure_parser.add_argument("specs", nargs="+", help="label=requirement:module")
    measure_parser.add_argument("--out", required=True)
    measure_parser.add_argument("--python", default="3.12")
    measure_parser.add_argument("--find-links", action="append", default=[])
    measure_parser.add_argument("--repeats", type=int, default=3)
    measure_parser.set_defaults(func=measure)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
