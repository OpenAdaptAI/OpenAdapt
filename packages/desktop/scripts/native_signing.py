#!/usr/bin/env python3
"""Fail-closed signing credential validation for native release jobs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CREDENTIALS = {
    "macos": (
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
        "APPLE_SIGNING_IDENTITY",
        "APPLE_ID",
        "APPLE_PASSWORD",
        "APPLE_TEAM_ID",
    ),
    "windows": (
        "WINDOWS_CERTIFICATE",
        "WINDOWS_CERTIFICATE_PASSWORD",
        "WINDOWS_CERTIFICATE_THUMBPRINT",
    ),
    "linux": (
        "LINUX_GPG_PRIVATE_KEY",
        "LINUX_GPG_KEY_ID",
        "LINUX_GPG_PASSPHRASE",
        "LINUX_GPG_FINGERPRINT",
    ),
}
UNSIGNED_MODES = {"macos": "adhoc", "windows": "unsigned", "linux": "unsigned"}
SIGNED_MODES = {
    "macos": "developer-id-notarized",
    "windows": "authenticode",
}


def signing_mode(platform: str, environ: dict[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    names = CREDENTIALS[platform]
    present = [name for name in names if environment.get(name)]
    if not present:
        return UNSIGNED_MODES[platform]
    missing = [name for name in names if not environment.get(name)]
    if missing:
        raise ValueError(f"partial {platform} signing credentials; missing: {', '.join(missing)}")
    if platform == "linux":
        raise ValueError(
            "Linux AppImage signing is disabled until a pinned external signature validator "
            "and authenticated public-key channel are configured"
        )
    return SIGNED_MODES[platform]


def write_github_output(path: Path, mode: str) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"mode={mode}\n")


def write_windows_config(path: Path, thumbprint: str) -> None:
    normalized = re.sub(r"\s+", "", thumbprint).upper()
    if not re.fullmatch(r"[0-9A-F]{40}", normalized):
        raise ValueError("Windows certificate thumbprint must be exactly 40 hexadecimal characters")
    config = {
        "bundle": {
            "windows": {
                "certificateThumbprint": normalized,
                "digestAlgorithm": "sha256",
                "timestampUrl": "http://timestamp.digicert.com",
                "tsp": True,
            }
        }
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--platform", choices=sorted(CREDENTIALS), required=True)
    preflight.add_argument("--github-output", type=Path)
    windows_config = subparsers.add_parser("windows-config")
    windows_config.add_argument("--output", type=Path, required=True)
    windows_config.add_argument("--thumbprint", required=True)
    args = parser.parse_args()

    try:
        if args.command == "preflight":
            mode = signing_mode(args.platform)
            if args.github_output:
                write_github_output(args.github_output, mode)
            print(mode)
        else:
            write_windows_config(args.output, args.thumbprint)
            print(args.output)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
