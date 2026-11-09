#!/usr/bin/env python3
"""Publish lowercase X11 SONAME aliases that Flow 1.34.0 probes.

Flow 1.34.0 calls ``ctypes.util.find_library`` with lowercase names
(``xcomposite``, ``xdamage``, ``xfixes``, ``xrandr``). Linux's ldconfig
cache is case-sensitive; Playwright's packages expose ``libXcomposite``
and friends. Flow 1.34.1 (PR #426) preserves case. Until Desktop pins
that wheel, the host must answer the published probe.

This does not skip Flow's library check and does not download Chromium.
It only aliases libraries that are already installed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

X11_ALIASES = (
    ("xcomposite", "Xcomposite"),
    ("xdamage", "Xdamage"),
    ("xfixes", "Xfixes"),
    ("xrandr", "Xrandr"),
)

_LIB_DIRS = (
    Path("/usr/lib/x86_64-linux-gnu"),
    Path("/lib/x86_64-linux-gnu"),
    Path("/usr/lib64"),
    Path("/usr/lib"),
)


def locate_real_library(soname: str, lib_dirs: tuple[Path, ...] = _LIB_DIRS) -> Path:
    """Return the installed ``lib{soname}.so*`` path."""

    for directory in lib_dirs:
        for candidate in (
            directory / f"lib{soname}.so.1",
            directory / f"lib{soname}.so",
        ):
            if candidate.is_file() or candidate.is_symlink():
                return candidate
        matches = sorted(directory.glob(f"lib{soname}.so.*"))
        if matches:
            return matches[0]
    raise FileNotFoundError(f"missing real X11 library lib{soname}.so")


def alias_path(real: Path, lower: str) -> Path:
    """Return the lowercase SONAME path beside the real library."""

    return real.with_name(f"lib{lower}.so")


def install_alias(real: Path, lower: str) -> Path:
    """Create ``lib{lower}.so`` next to ``real`` if it is not already present."""

    dest = alias_path(real, lower)
    if dest.exists() or dest.is_symlink():
        return dest
    dest.symlink_to(real)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lib-dir",
        action="append",
        type=Path,
        help="Search this directory first. Repeatable. Defaults to the host lib paths.",
    )
    args = parser.parse_args(argv)

    if sys.platform != "linux":
        return 0

    lib_dirs = tuple(args.lib_dir) if args.lib_dir else _LIB_DIRS
    created: list[Path] = []
    for lower, upper in X11_ALIASES:
        real = locate_real_library(upper, lib_dirs)
        dest = alias_path(real, lower)
        if dest.exists() or dest.is_symlink():
            continue
        try:
            install_alias(real, lower)
        except PermissionError:
            subprocess.run(["sudo", "ln", "-sfn", str(real), str(dest)], check=True)
        created.append(dest)

    if created:
        ldconfig = ["ldconfig"] if os.geteuid() == 0 else ["sudo", "ldconfig"]
        subprocess.run(ldconfig, check=False)

    for lower, upper in X11_ALIASES:
        real = locate_real_library(upper, lib_dirs)
        dest = alias_path(real, lower)
        if not dest.exists() and not dest.is_symlink():
            raise SystemExit(f"failed to publish lowercase probe alias {dest}")
        print(f"{lower} -> {real}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
