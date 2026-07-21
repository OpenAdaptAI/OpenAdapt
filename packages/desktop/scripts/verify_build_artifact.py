#!/usr/bin/env python3
"""Verify that CI produced a usable platform-native desktop artifact."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_FROZEN_MEMBERS = re.compile(
    r"(?:openimis|adversary_corpus|identity_roc|reliability_corpus|"
    r"grown[_-]corpus|oracle[_-]recipes)",
    re.IGNORECASE,
)

REQUIRED_FROZEN_NOTICES = (
    "third_party/onnxruntime/LICENSE",
    "third_party/onnxruntime/ThirdPartyNotices.txt",
    "third_party/rapidocr/LICENSE",
    "third_party/rapidocr/NOTICE",
)


def normalized_inventory(value: str) -> str:
    """Use one member separator for PyInstaller inventories on every OS."""

    # archive_viewer prints member names with ``repr``. A Windows path therefore
    # contains two literal backslashes in stdout; collapse the whole separator
    # run rather than converting each one into a separate slash.
    return re.sub(r"[\\/]+", "/", value)


def artifact_path(kind: str) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    if kind == "sidecar":
        return ROOT / "dist" / f"openadapt-engine{suffix}"
    return ROOT / "src-tauri" / "target" / "release" / f"openadapt-desktop{suffix}"


def verify_python_distributions(parser: argparse.ArgumentParser) -> None:
    """Inspect the archives users actually install, not only the source tree."""

    archives = sorted((ROOT / "dist").glob("openadapt_desktop-*.whl"))
    archives += sorted((ROOT / "dist").glob("openadapt_desktop-*.tar.gz"))
    if len(archives) != 2:
        parser.error(f"expected one wheel and one sdist, found: {archives}")

    for archive in archives:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as package:
                members = package.namelist()
        else:
            with tarfile.open(archive, "r:gz") as package:
                members = package.getnames()
        forbidden = sorted(member for member in members if FORBIDDEN_FROZEN_MEMBERS.search(member))
        if forbidden:
            parser.error(
                f"{archive.name} crossed the AGPL/private-corpus boundary: "
                + "; ".join(forbidden[:20])
            )
        print(f"Verified Python distribution: {archive} ({archive.stat().st_size} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("python-distribution", "sidecar", "tauri"))
    args = parser.parse_args()

    if args.kind == "python-distribution":
        verify_python_distributions(parser)
        return 0

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

        flow = subprocess.run(
            [str(artifact), "__openadapt_flow__", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        flow_output = flow.stdout + flow.stderr
        if flow.returncode != 0 or "openadapt-flow 1.19.0" not in flow_output:
            parser.error(
                "bundled Flow runtime smoke test failed with exit "
                f"{flow.returncode}: {flow_output[-1000:]}"
            )

        playwright = subprocess.run(
            [str(artifact), "-m", "playwright", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if playwright.returncode != 0 or "Version 1.61.0" not in (
            playwright.stdout + playwright.stderr
        ):
            parser.error("bundled Playwright bootstrap is absent or version-drifted")

        inventory = subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller.utils.cliutils.archive_viewer",
                "-r",
                "-l",
                str(artifact),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if inventory.returncode != 0:
            parser.error(f"could not inventory frozen sidecar: {inventory.stderr[-1000:]}")
        inventory_text = normalized_inventory(inventory.stdout)
        forbidden = sorted(
            line.strip()
            for line in inventory_text.splitlines()
            if FORBIDDEN_FROZEN_MEMBERS.search(line)
        )
        if forbidden:
            parser.error(
                "frozen sidecar crossed the AGPL/private-corpus boundary: "
                + "; ".join(forbidden[:20])
            )
        missing_notices = [
            member for member in REQUIRED_FROZEN_NOTICES if member not in inventory_text
        ]
        if missing_notices:
            parser.error(
                "frozen sidecar omitted required third-party notices: "
                + "; ".join(missing_notices)
            )

    print(f"Verified {args.kind} artifact: {artifact} ({artifact.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
