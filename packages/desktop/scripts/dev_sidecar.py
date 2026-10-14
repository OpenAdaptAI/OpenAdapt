#!/usr/bin/env python3
"""Put an engine sidecar where ``npm run tauri dev`` looks for it.

Tauri refuses to build the shell unless
``src-tauri/binaries/openadapt-engine-<target-triple>`` exists. CI fills that
path with the PyInstaller freeze of the engine. In a source checkout this
script fills it instead, so the development shell starts with a live engine.

By default it writes a small launcher that runs ``python -m engine`` from this
checkout's ``.venv``. Engine edits then take effect on the next app start, and
no freeze is needed. Windows can't run a script as the sidecar, so there (or
anywhere, to test the packaged engine) ``--frozen`` runs the same PyInstaller
build that CI runs and copies the result into place.

The launcher is for development only. ``src-tauri/binaries/`` is ignored by
git, and ``src-tauri/build.rs`` stops a release build that would package the
launcher.

Usage::

    uv sync --extra dev --extra build
    uv run python scripts/dev_sidecar.py            # launcher (macOS, Linux)
    uv run python scripts/dev_sidecar.py --frozen   # PyInstaller build (any OS)
    npm run tauri dev
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARIES = ROOT / "src-tauri" / "binaries"
SIDECAR_NAME = "openadapt-engine"
LAUNCHER_MARKER = "# Development engine launcher written by scripts/dev_sidecar.py."


def host_target_triple(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """Return the Rust host triple that Tauri appends to the sidecar name."""

    try:
        result = run(["rustc", "-vV"], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise SystemExit(
            "rustc was not found. The desktop shell needs Rust: https://rustup.rs"
        ) from exc
    for line in result.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit(f"rustc -vV did not report a host triple:\n{result.stdout}{result.stderr}")


def sidecar_path(triple: str, binaries: Path = BINARIES) -> Path:
    """Where Tauri expects the sidecar for ``triple``."""

    suffix = ".exe" if "windows" in triple else ""
    return binaries / f"{SIDECAR_NAME}-{triple}{suffix}"


def launcher_text(venv: Path) -> str:
    """A POSIX launcher that runs the engine from ``venv``.

    The venv's ``bin`` goes first on ``PATH`` so the engine finds this
    checkout's locked ``openadapt-flow``, not another one installed elsewhere.
    """

    bin_dir = venv / "bin"
    python = bin_dir / "python"
    return (
        "#!/bin/sh\n"
        f"{LAUNCHER_MARKER}\n"
        "# Runs the engine from a source checkout. Never package this file.\n"
        f'PATH={shlex.quote(str(bin_dir))}:"$PATH"\n'
        "export PATH\n"
        f'exec {shlex.quote(str(python))} -m engine "$@"\n'
    )


def write_launcher(target: Path, venv: Path) -> Path:
    """Write the development launcher to ``target`` and make it executable."""

    python = venv / "bin" / "python"
    if not python.exists():
        raise SystemExit(f"{python} does not exist. Run `uv sync --extra dev --extra build` first.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(launcher_text(venv), encoding="utf-8")
    mode = target.stat().st_mode
    target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def build_frozen(target: Path, root: Path = ROOT, run=subprocess.run) -> Path:
    """Run CI's PyInstaller build and copy the engine to ``target``."""

    run([sys.executable, str(root / "scripts" / "build_frozen_engine.py")], check=True, cwd=root)
    suffix = ".exe" if target.suffix == ".exe" else ""
    built = root / "dist" / f"{SIDECAR_NAME}{suffix}"
    if not built.is_file():
        raise SystemExit(f"The frozen build did not produce {built}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--frozen",
        action="store_true",
        help="Build the PyInstaller engine that CI ships instead of writing a launcher.",
    )
    parser.add_argument(
        "--venv",
        type=Path,
        default=ROOT / ".venv",
        help="Virtual environment the launcher runs (default: this checkout's .venv).",
    )
    args = parser.parse_args(argv)

    target = sidecar_path(host_target_triple())
    if args.frozen:
        build_frozen(target)
    elif os.name == "nt" or target.suffix == ".exe":
        raise SystemExit("Windows can't run a script as the sidecar. Run this again with --frozen.")
    else:
        write_launcher(target, args.venv.resolve())
    shown = target.relative_to(ROOT) if target.is_relative_to(ROOT) else target
    print(f"Engine sidecar ready: {shown}")
    print("Start the app with: npm run tauri dev")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
