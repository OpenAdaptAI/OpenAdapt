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
resolves at install time: `uv pip check` fails the job when an in-tree package
needs something no lock provides, and the fix is to update a lock.
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
    with tempfile.TemporaryDirectory() as temp:
        for index, spec in enumerate(args.lock):
            requirements = Path(temp) / f"layer-{index}.txt"
            _run(_export_command(spec, skip, requirements))
            _run(["uv", "pip", "install", *target, "--require-hashes", "--no-deps",
                  "--requirement", str(requirements)])  # fmt: skip
    if editables:
        command = ["uv", "pip", "install", *target, "--no-deps"]
        for path in editables:
            command += ["--editable", str(path)]
        _run(command)
    _run(["uv", "pip", "check", *target])
    return 0


if __name__ == "__main__":
    sys.exit(main())
