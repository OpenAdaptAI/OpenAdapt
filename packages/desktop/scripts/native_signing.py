#!/usr/bin/env python3
"""Fail-closed signing credential validation for native release jobs.

The release workflow reads two facts from this script:

* ``mode`` -- the honest signing label staged into every artifact filename
  (``adhoc``/``unsigned`` when no credentials are configured, or the signed
  label once a complete credential set is present).
* ``method`` -- *how* a signed artifact is produced. Windows reaches the
  ``authenticode`` mode through SignPath (the SignPath Foundation signs open
  source projects free of charge): the workflow uploads the unsigned MSI and
  NSIS installers, submits a signing request, and waits for the signed files.

Every credential set is complete-or-absent: a partial set fails the build
instead of silently falling back to an unsigned artifact. With no set, the
release ships ``adhoc`` (macOS) or ``unsigned`` (Windows) artifacts, labelled
as such in every file name, the website manifest, and the release notes.
``--require-mode`` still refuses a plan below a required trust mode.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# SignPath: an API token for a submitter, the organization, and the project and
# signing policy the request is submitted to. The four are configured together
# in the ``native-release`` environment, or not at all.
WINDOWS_SIGNPATH_CREDENTIALS = (
    "SIGNPATH_API_TOKEN",
    "SIGNPATH_ORGANIZATION_ID",
    "SIGNPATH_PROJECT_SLUG",
    "SIGNPATH_SIGNING_POLICY_SLUG",
)

CREDENTIALS = {
    "macos": (
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
        "APPLE_SIGNING_IDENTITY",
        "APPLE_ID",
        "APPLE_PASSWORD",
        "APPLE_TEAM_ID",
    ),
    "windows": WINDOWS_SIGNPATH_CREDENTIALS,
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


def _complete_or_partial(
    names: tuple[str, ...], environment: dict[str, str], *, label: str
) -> None:
    """Raise if a credential set is present but incomplete."""

    missing = [name for name in names if not environment.get(name)]
    if missing:
        raise ValueError(f"partial {label} signing credentials; missing: {', '.join(missing)}")


def windows_plan(environ: dict[str, str] | None = None) -> tuple[str, str]:
    """Return ``(mode, method)`` for Windows from complete-or-absent secrets."""

    environment = os.environ if environ is None else environ
    if any(environment.get(name) for name in WINDOWS_SIGNPATH_CREDENTIALS):
        _complete_or_partial(WINDOWS_SIGNPATH_CREDENTIALS, environment, label="SignPath")
        return "authenticode", "signpath"
    return "unsigned", "unsigned"


def signing_mode(platform: str, environ: dict[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    if platform == "windows":
        return windows_plan(environment)[0]
    names = CREDENTIALS[platform]
    present = [name for name in names if environment.get(name)]
    if not present:
        return UNSIGNED_MODES[platform]
    _complete_or_partial(names, environment, label=platform)
    if platform == "linux":
        raise ValueError(
            "Linux AppImage signing is disabled until a pinned external signature validator "
            "and authenticated public-key channel are configured"
        )
    return SIGNED_MODES[platform]


def signing_method(platform: str, environ: dict[str, str] | None = None) -> str:
    """How the signed artifact is produced, for workflow step dispatch."""

    environment = os.environ if environ is None else environ
    if platform == "windows":
        return windows_plan(environment)[1]
    mode = signing_mode(platform, environment)
    if platform == "macos":
        return "developer-id" if mode == SIGNED_MODES["macos"] else "adhoc"
    return "unsigned"


def require_signing_mode(
    platform: str,
    required_mode: str,
    environ: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return the signing plan or refuse a release without the required trust."""

    mode = signing_mode(platform, environ)
    method = signing_method(platform, environ)
    if mode != required_mode:
        raise ValueError(
            f"{platform} release requires signing mode {required_mode!r}; "
            f"configured mode is {mode!r}"
        )
    return mode, method


def write_github_output(path: Path, mode: str, method: str) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"mode={mode}\n")
        output.write(f"method={method}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--platform", choices=sorted(CREDENTIALS), required=True)
    preflight.add_argument("--require-mode")
    preflight.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "preflight":
            if args.require_mode:
                mode, method = require_signing_mode(args.platform, args.require_mode)
            else:
                mode = signing_mode(args.platform)
                method = signing_method(args.platform)
            if args.github_output:
                write_github_output(args.github_output, mode, method)
            print(f"{mode} ({method})")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
