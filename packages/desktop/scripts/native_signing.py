#!/usr/bin/env python3
"""Fail-closed signing credential validation for native release jobs.

The release workflow reads two facts from this script:

* ``mode`` -- the honest signing label staged into every artifact filename
  (``adhoc``/``unsigned`` when no credentials are configured, or the signed
  label once a complete credential set is present).
* ``method`` -- *how* a signed artifact is produced. Windows supports two
  interchangeable ways to reach the ``authenticode`` mode: a traditional
  importable ``pfx`` certificate, or Azure ``trusted-signing`` (short-lived
  certificates minted per signature, the cheapest publicly trusted option for
  a startup and the only one that avoids a hardware token). Both yield an
  ordinary, publicly trusted, timestamped Authenticode signature, so staging
  and signature verification are identical.

Every credential set is complete-or-absent: a partial set fails the build
instead of silently falling back to an unsigned artifact. When no set is
configured the build falls back to exactly today's ad-hoc/unsigned behaviour.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Traditional importable PKCS#12 certificate (self-hosted / enterprise-internal
# certificates). Publicly trusted OV/EV keys have been hardware-bound since the
# June 2023 CA/Browser Forum mandate and generally cannot be exported to a .pfx;
# for public trust prefer the Azure Trusted Signing method below.
WINDOWS_PFX_CREDENTIALS = (
    "WINDOWS_CERTIFICATE",
    "WINDOWS_CERTIFICATE_PASSWORD",
    "WINDOWS_CERTIFICATE_THUMBPRINT",
)
# Azure Trusted Signing (formerly Azure Code Signing / now Azure Artifact
# Signing). Authentication uses a service principal (DefaultAzureCredential);
# the signing account and certificate profile identify the publicly trusted
# certificate authority Microsoft operates on your behalf.
WINDOWS_TRUSTED_SIGNING_CREDENTIALS = (
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "TRUSTED_SIGNING_ENDPOINT",
    "TRUSTED_SIGNING_ACCOUNT",
    "TRUSTED_SIGNING_CERTIFICATE_PROFILE",
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
    # Windows credential names are documented on the two constants above; the
    # tuple here keeps the historical single-set shape used by callers/tests
    # that only ask about the traditional pfx path.
    "windows": WINDOWS_PFX_CREDENTIALS,
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
    pfx_present = [name for name in WINDOWS_PFX_CREDENTIALS if environment.get(name)]
    azure_present = [name for name in WINDOWS_TRUSTED_SIGNING_CREDENTIALS if environment.get(name)]

    if pfx_present and azure_present:
        raise ValueError(
            "ambiguous Windows signing credentials: configure either the "
            "importable pfx set or the Azure Trusted Signing set, never both"
        )
    if azure_present:
        _complete_or_partial(
            WINDOWS_TRUSTED_SIGNING_CREDENTIALS, environment, label="Azure Trusted Signing"
        )
        return "authenticode", "trusted-signing"
    if pfx_present:
        _complete_or_partial(WINDOWS_PFX_CREDENTIALS, environment, label="windows")
        return "authenticode", "pfx"
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


def write_github_output(path: Path, mode: str, method: str) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"mode={mode}\n")
        output.write(f"method={method}\n")


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


def write_trusted_signing_config(
    path: Path, *, endpoint: str, account: str, certificate_profile: str
) -> None:
    """Write a Tauri config overlay that signs Windows bundles via Trusted Signing.

    ``trusted-signing-cli`` reads the Azure service-principal credentials from
    the environment (``AZURE_TENANT_ID``/``AZURE_CLIENT_ID``/
    ``AZURE_CLIENT_SECRET``) and applies an RFC 3161 timestamp itself, so no
    thumbprint, digest, or timestamp URL is pinned here. ``%1`` is replaced by
    Tauri with the path of each binary to sign.
    """

    for label, value in (
        ("endpoint", endpoint),
        ("account", account),
        ("certificate profile", certificate_profile),
    ):
        if not value or not value.strip():
            raise ValueError(f"Azure Trusted Signing {label} must not be empty")
    config = {
        "bundle": {
            "windows": {
                "signCommand": {
                    "cmd": "trusted-signing-cli",
                    "args": [
                        "-e",
                        endpoint,
                        "-a",
                        account,
                        "-c",
                        certificate_profile,
                        "%1",
                    ],
                }
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
    trusted_signing_config = subparsers.add_parser("windows-trusted-signing-config")
    trusted_signing_config.add_argument("--output", type=Path, required=True)
    trusted_signing_config.add_argument("--endpoint", required=True)
    trusted_signing_config.add_argument("--account", required=True)
    trusted_signing_config.add_argument("--certificate-profile", required=True)
    args = parser.parse_args()

    try:
        if args.command == "preflight":
            mode = signing_mode(args.platform)
            method = signing_method(args.platform)
            if args.github_output:
                write_github_output(args.github_output, mode, method)
            print(f"{mode} ({method})")
        elif args.command == "windows-config":
            write_windows_config(args.output, args.thumbprint)
            print(args.output)
        else:
            write_trusted_signing_config(
                args.output,
                endpoint=args.endpoint,
                account=args.account,
                certificate_profile=args.certificate_profile,
            )
            print(args.output)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
