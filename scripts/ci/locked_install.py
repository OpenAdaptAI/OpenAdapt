#!/usr/bin/env python3
"""Install a CI job's Python dependencies from uv lockfiles, with hashes.

Each package keeps a uv.lock, and .github/ci-tools/uv.lock holds the tools for
jobs that test no package of their own. A job names the locks it installs from
and the in-tree packages it installs over them, with paths relative to the
repository root:

    python scripts/ci/locked_install.py \\
        --lock 'packages/flow[dev,capture,agent]' \\
        --editable packages/types --editable packages/capture \\
        --editable packages/flow

    python scripts/ci/locked_install.py --lock .github/ci-tools:test

`DIR[extra,...]` installs the project's dependencies with those extras.
`DIR:group,...` installs only those dependency groups.

Each lock is exported with `uv export --locked`, which fails when the lock no
longer matches its pyproject.toml, and installed with `--require-hashes
--no-deps` into the interpreter that runs this script. Where two locks pin the
same package, the later one wins. The in-tree packages then install editable
with `--no-deps`, in place of any published copy a lock names. Nothing
resolves at install time: `uv pip check` fails the job when a package this
script installed needs something no lock provides, and the fix is to update a
lock. Packages it did not install, such as pipx on the Windows runner's Python
or a lock's deliberate override in a uv venv, are reported but not fatal.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_LOCK = re.compile(r"^(?P<dir>[^\[:]+)(?:\[(?P<extras>[^\]]*)\])?(?::(?P<groups>.+))?$")


def _project_name(directory: Path) -> str:
    # tomllib is 3.11+, and the oldest CI Python is 3.10.
    text = (directory / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r"(?ms)^\[project\]\n(.*?)(?=^\[|\Z)", text)
    name = project and re.search(r'(?m)^name\s*=\s*"([^"]+)"', project.group(1))
    if not name:
        raise SystemExit(f"{directory}/pyproject.toml has no [project] name")
    return name.group(1)


def _export_command(spec: str, skip: list[str], output: Path) -> list[str]:
    match = _LOCK.match(spec)
    if not match:
        raise SystemExit(f"cannot read lock spec {spec!r}")
    directory = ROOT / match.group("dir")
    if not (directory / "uv.lock").is_file():
        raise SystemExit(f"{directory} has no uv.lock")
    command = [
        "uv", "export", "--locked", "--quiet", "--format", "requirements.txt",
        "--no-header", "--no-emit-project", "--no-emit-local", "--no-default-groups",
        "--project", str(directory), "--output-file", str(output),
    ]  # fmt: skip
    for extra in filter(None, (match.group("extras") or "").split(",")):
        command += ["--extra", extra.strip()]
    for group in filter(None, (match.group("groups") or "").split(",")):
        command += ["--only-group", group.strip()]
    for name in skip:
        command += ["--no-emit-package", name]
    return command


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match:
            names.add(_normalize(match.group(1)))
    return names


_CONFLICT = re.compile(r"^The package `([^`]+)` ")


def problems_with(output: str, installed: set[str]) -> tuple[list[str], list[str]]:
    """Split `uv pip check` findings into ours and the environment's own.

    A finding names the package whose requirement is unmet. It is ours when
    this script installed that package. A line in an unknown format counts as
    ours, so a new kind of finding fails the job.
    """
    ours, others = [], []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(
            ("Using Python", "Checked ", "Found ", "All installed")
        ):
            continue
        match = _CONFLICT.match(line)
        if match and _normalize(match.group(1)) not in installed:
            others.append(line)
        else:
            ours.append(line)
    return ours, others


def _run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lock", action="append", required=True, metavar="SPEC")
    parser.add_argument("--editable", action="append", default=[], metavar="DIR")
    args = parser.parse_args(argv)

    editables = [ROOT / path for path in args.editable]
    skip = [_project_name(path) for path in editables]
    target = ["--python", sys.executable]
    installed = {_normalize(name) for name in skip}
    with tempfile.TemporaryDirectory() as temp:
        for index, spec in enumerate(args.lock):
            requirements = Path(temp) / f"layer-{index}.txt"
            _run(_export_command(spec, skip, requirements))
            installed |= _requirement_names(requirements)
            _run(["uv", "pip", "install", *target, "--require-hashes", "--no-deps",
                  "--requirement", str(requirements)])  # fmt: skip
    if editables:
        command = ["uv", "pip", "install", *target, "--no-deps"]
        for path in editables:
            command += ["--editable", str(path)]
        _run(command)
    check = subprocess.run(
        ["uv", "pip", "check", *target], capture_output=True, text=True, check=False
    )
    ours, others = problems_with(check.stdout + check.stderr, installed)
    for line in others:
        print(f"not installed here, ignored: {line}")
    if ours:
        print("\n".join(["Locked packages conflict:", *ours]), file=sys.stderr)
        return 1
    print(f"{len(installed)} locked packages are compatible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
