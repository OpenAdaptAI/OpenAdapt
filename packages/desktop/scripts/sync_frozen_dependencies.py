#!/usr/bin/env python3
"""Install the locked frozen-runtime dependencies for the current platform."""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Mapping
from pathlib import Path


def frozen_dependency_environment(
    *,
    system: str | None = None,
    machine: str | None = None,
    source: Mapping[str, str] | None = None,
    run=subprocess.run,
) -> dict[str, str]:
    """Return the build environment, including the safe Intel macOS boundary."""

    environment = dict(os.environ if source is None else source)
    system = system or platform.system()
    machine = machine or platform.machine()
    if system != "Darwin" or machine != "x86_64":
        return environment

    openssl_dir = environment.get("OPENSSL_DIR", "").strip()
    if not openssl_dir:
        result = run(
            ["brew", "--prefix", "openssl@3"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(
                "Intel macOS requires Homebrew openssl@3 to build the locked "
                f"cryptography source archive: {result.stderr[-1000:]}"
            )
        openssl_dir = result.stdout.strip()

    prefix = Path(openssl_dir)
    missing = [
        str(prefix / "lib" / name)
        for name in ("libssl.a", "libcrypto.a")
        if not (prefix / "lib" / name).is_file()
    ]
    if missing:
        raise RuntimeError(
            "Intel macOS cryptography requires static OpenSSL libraries: " + ", ".join(missing)
        )

    environment["OPENSSL_DIR"] = str(prefix)
    environment["OPENSSL_STATIC"] = "1"
    return environment


def main() -> int:
    environment = frozen_dependency_environment()
    command = ["uv", "sync", "--locked", "--extra", "build"]
    if platform.system() == "Darwin" and platform.machine() == "x86_64":
        command.extend(
            ("--reinstall-package", "cryptography", "--no-binary-package", "cryptography")
        )
    subprocess.run(command, check=True, env=environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
