"""Native desktop GnuCash launcher (macOS and Linux/Xvfb)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def resolve_gnucash_binary() -> str | None:
    """Locate the native GnuCash executable on macOS or Linux."""
    found = shutil.which("gnucash")
    if found:
        return found
    mac_app = Path("/Applications/GnuCash.app/Contents/MacOS/Gnucash")
    if sys.platform == "darwin" and mac_app.is_file():
        return str(mac_app)
    return None


def build_launch_command(ledger_path: Path, *, headless_linux: bool = True) -> list[str]:
    """Construct the command list to open ``ledger_path`` in GnuCash."""
    binary = resolve_gnucash_binary()
    if binary is None:
        raise FileNotFoundError(
            "GnuCash binary not found on PATH or /Applications/GnuCash.app"
        )
    cmd = [binary, str(ledger_path)]
    if sys.platform.startswith("linux") and headless_linux:
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            return [xvfb, "-a", *cmd]
    return cmd


def launch_gnucash(
    ledger_path: Path, *, headless_linux: bool = True
) -> subprocess.Popen[bytes]:
    """Launch GnuCash against ``ledger_path`` (caller owns termination)."""
    cmd = build_launch_command(ledger_path, headless_linux=headless_linux)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
