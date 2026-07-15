#!/usr/bin/env python3
"""Verify that CI produced a usable platform-native desktop artifact."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def artifact_path(kind: str) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    if kind == "sidecar":
        return ROOT / "dist" / f"openadapt-engine{suffix}"
    return ROOT / "src-tauri" / "target" / "release" / f"openadapt-desktop{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("sidecar", "tauri"))
    args = parser.parse_args()

    artifact = artifact_path(args.kind)
    if not artifact.is_file() or artifact.stat().st_size == 0:
        parser.error(f"missing or empty {args.kind} artifact: {artifact}")

    if args.kind == "sidecar":
        result = subprocess.run(
            [str(artifact), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0 or "OpenAdapt Desktop" not in output:
            parser.error(
                f"sidecar smoke test failed with exit {result.returncode}: {output[-1000:]}"
            )

    print(f"Verified {args.kind} artifact: {artifact} ({artifact.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
