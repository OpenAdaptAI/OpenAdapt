#!/usr/bin/env python3
"""Smoke-test every package from its own lock: it installs, imports and starts.

The `ci` check runs this on every pull request, whatever changed, so a change
to one package that breaks another's install or import fails before merge even
when the other package's suite does not run. Each package syncs into its own
environment with `uv sync --locked` (no extras, no dev tools), imports its
top-level module, and runs its command-line entry point with `--help`.

    python scripts/ci/smoke.py            # every package
    python scripts/ci/smoke.py flow tray  # some
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# package -> (project directory, import, command and arguments)
SMOKES: dict[str, tuple[str, str, list[str]]] = {
    "launcher": (".", "openadapt", ["openadapt", "--help"]),
    "types": ("packages/types", "openadapt_types", ["openadapt-vocab-lint", "--help"]),
    "flow": ("packages/flow", "openadapt_flow", ["openadapt-flow", "--help"]),
    "agent": ("packages/agent", "openadapt_agent", ["openadapt-agent", "--help"]),
    "capture": ("packages/capture", "openadapt_capture", ["capture", "--help"]),
    "privacy": ("packages/privacy", "openadapt_privacy", ["openadapt-privacy-scan", "--help"]),
    # The tray's entry point starts a GUI; its IPC module is the contract
    # Desktop and Flow talk to.
    "tray": ("packages/tray", "openadapt_tray.ipc", []),
    "desktop": ("packages/desktop", "openadapt_desktop", ["openadapt-desktop", "--help"]),
}


def smoke(name: str) -> tuple[bool, str]:
    directory, module, command = SMOKES[name]
    project = ["--project", str(ROOT / directory)]
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(ROOT / ".smoke" / name)}
    steps = [
        ["uv", "sync", "--locked", "--no-dev", "--quiet", *project],
        ["uv", "run", "--locked", "--no-dev", *project, "python", "-c", f"import {module}"],
    ]
    if command:
        steps.append(["uv", "run", "--locked", "--no-dev", *project, *command])
    for step in steps:
        result = subprocess.run(step, env=env, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            tail = (result.stdout + result.stderr)[-2000:]
            return False, f"{' '.join(step[:4])} ... exited {result.returncode}\n{tail}"
    return True, ""


def main(argv: list[str]) -> int:
    names = argv or list(SMOKES)
    unknown = sorted(set(names) - set(SMOKES))
    if unknown:
        raise SystemExit(f"unknown package(s): {', '.join(unknown)}")
    failed = []
    for name in names:
        start = time.monotonic()
        ok, detail = smoke(name)
        print(f"{'ok  ' if ok else 'FAIL'} {name} ({time.monotonic() - start:.0f}s)", flush=True)
        if not ok:
            print(detail, file=sys.stderr)
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
