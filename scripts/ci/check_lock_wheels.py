#!/usr/bin/env python3
"""Check that every lock installs on its oldest Python, on each CI platform.

A universal uv.lock can pin a version that has no wheel, and no sdist, for
an old Python: onnxruntime 1.24 dropped cp310, and the Agent and launcher
locks pinned it for 3.10, which failed only on main's 3.10 jobs, because the
pull-request lane runs 3.12. This resolves each lock's exported pins (every
extra) with `uv pip compile --no-deps` for its requires-python floor on
Linux x86_64, macOS arm64 and Windows x86_64, which fails when a pinned
version has no file for that interpreter. It downloads metadata only and
installs nothing.

    python scripts/ci/check_lock_wheels.py            # every lock
    python scripts/ci/check_lock_wheels.py packages/agent
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The runners' own tags: ubuntu-latest is Ubuntu 24.04 (glibc 2.39), the macOS
# runners are 14 or newer (MACOSX_DEPLOYMENT_TARGET below), windows-latest is
# x86_64. uv's bare targets default to older tags, which rejects wheels the
# runners take (faiss-cpu, torch and mlx ship only macOS 14 and
# manylinux_2_28 or newer).
PLATFORMS = ("x86_64-manylinux_2_39", "aarch64-apple-darwin", "x86_64-pc-windows-msvc")


def locks() -> list[Path]:
    found = [ROOT] if (ROOT / "uv.lock").is_file() else []
    found += sorted(path.parent for path in (ROOT / "packages").glob("*/uv.lock"))
    found += sorted(path.parent for path in (ROOT / ".github").glob("**/uv.lock"))
    return found


def floor(directory: Path) -> str:
    text = (directory / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^requires-python\s*=\s*"[^"]*>=\s*(3\.\d+)', text)
    if not match:
        raise SystemExit(f"{directory}/pyproject.toml has no requires-python floor")
    return match.group(1)


def check(directory: Path) -> list[str]:
    python = floor(directory)
    problems = []
    with tempfile.TemporaryDirectory() as temp:
        pins = Path(temp) / "pins.txt"
        subprocess.run(
            [
                "uv", "export", "--locked", "--quiet", "--all-extras", "--all-groups",
                "--no-emit-project", "--no-emit-local", "--no-hashes", "--no-header",
                "--project", str(directory), "--output-file", str(pins),
            ],
            check=True,
        )  # fmt: skip
        for platform in PLATFORMS:
            result = subprocess.run(
                [
                    "uv", "pip", "compile", "--quiet", "--no-deps", "--no-header",
                    "--python-version", python, "--python-platform", platform,
                    "--output-file", str(Path(temp) / "out.txt"), str(pins),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "MACOSX_DEPLOYMENT_TARGET": "14.0"},
            )  # fmt: skip
            if result.returncode != 0:
                detail = (result.stderr.strip().splitlines() or ["(no output)"])[-3:]
                problems.append(f"{directory.relative_to(ROOT)} on Python {python} "
                                f"{platform}: {' '.join(detail)}")  # fmt: skip
    return problems


def main(argv: list[str]) -> int:
    targets = [ROOT / arg for arg in argv] if argv else locks()
    problems = []
    for directory in targets:
        found = check(directory)
        print(
            f"{'ok  ' if not found else 'FAIL'} {directory.relative_to(ROOT) or '.'}",
            flush=True,
        )
        problems += found
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
