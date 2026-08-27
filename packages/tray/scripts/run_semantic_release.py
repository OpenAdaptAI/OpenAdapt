#!/usr/bin/env python3
"""Run the exact semantic-release CLI from the reviewed uv environment."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from importlib import metadata
from pathlib import Path

REQUIRED_RUNTIME = {
    "python-semantic-release": "10.6.1",
    "GitPython": "3.1.59",
}
REQUIRED_ENVIRONMENT = ("GH_TOKEN", "GITHUB_OUTPUT")


class ReleaseRuntimeError(RuntimeError):
    """The installed release runtime differs from the reviewed lock."""


def verify_runtime(
    version_reader: Callable[[str], str] = metadata.version,
) -> None:
    """Refuse a release when either load-bearing package has drifted."""

    for distribution, expected in REQUIRED_RUNTIME.items():
        try:
            actual = version_reader(distribution)
        except metadata.PackageNotFoundError as exc:
            raise ReleaseRuntimeError(
                f"required release package is not installed: {distribution}"
            ) from exc
        if actual != expected:
            raise ReleaseRuntimeError(
                f"{distribution} version differs: expected {expected}; got {actual}"
            )


def release_command(python_executable: str = sys.executable) -> list[str]:
    """Return the console entry point installed beside the active Python."""

    # Do not resolve the Python symlink. uv places console scripts beside the
    # virtual-environment link, not beside the managed interpreter target.
    cli = Path(python_executable).absolute().with_name("semantic-release")
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise ReleaseRuntimeError(
            f"semantic-release is not executable beside the active Python: {cli}"
        )
    return [str(cli), "-v", "version"]


def run_release(
    *,
    environment: Mapping[str, str] = os.environ,
    version_reader: Callable[[str], str] = metadata.version,
    python_executable: str = sys.executable,
    runner: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> int:
    """Run PSR unchanged so it writes its native GitHub Action outputs."""

    missing = [name for name in REQUIRED_ENVIRONMENT if not environment.get(name)]
    if missing:
        raise ReleaseRuntimeError(
            "release environment is missing: " + ", ".join(missing)
        )
    verify_runtime(version_reader)
    result = runner(
        release_command(python_executable),
        env=dict(environment),
        check=False,
    )
    return result.returncode


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("run_semantic_release.py accepts no arguments", file=sys.stderr)
        return 2
    try:
        return run_release()
    except (OSError, ReleaseRuntimeError) as exc:
        print(f"RELEASE RUNTIME REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
