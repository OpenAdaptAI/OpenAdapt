"""Automatic update helper for the OpenAdapt CLI.

Goal: keep users on the latest `openadapt` release with minimal friction.

This module:
- checks PyPI for the latest version (rate-limited via a small cache file)
- upgrades `openadapt` via pip when a newer version exists
- re-execs the CLI entrypoint to continue with the same arguments

Safety:
- can be disabled via `OPENADAPT_DISABLE_AUTO_UPDATE=1`
- skips in CI environments
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .version import __version__


PYPI_URL = "https://pypi.org/pypi/openadapt/json"
DEFAULT_CHECK_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class AutoUpdateResult:
    checked: bool
    updated: bool
    message: str


def _cache_path() -> Path:
    # Keep this simple to avoid adding a dependency (platformdirs).
    root = Path.home() / ".openadapt"
    root.mkdir(parents=True, exist_ok=True)
    return root / "autoupdate.json"


def _read_last_check_ts(path: Path) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data.get("last_check_ts")
        return float(ts) if ts is not None else None
    except Exception:
        return None


def _write_last_check_ts(path: Path, ts: float) -> None:
    path.write_text(json.dumps({"last_check_ts": ts}, indent=2), encoding="utf-8")


def _is_ci() -> bool:
    return os.getenv("CI", "").lower() in ("1", "true", "yes")


def _fetch_latest_version(timeout_seconds: float = 2.5) -> str | None:
    try:
        req = urllib.request.Request(
            PYPI_URL,
            headers={"User-Agent": f"openadapt/{__version__} (autoupdate)"},
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return str(payload.get("info", {}).get("version") or "").strip() or None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def _parse_version_tuple(v: str) -> tuple[int, ...] | None:
    """Parse a simple dotted version string into a numeric tuple.

    This avoids adding a hard dependency on `packaging` while still working for
    standard release versions like `1.2.3`. For non-standard versions, return
    None (caller should treat as non-comparable).
    """
    if not v:
        return None
    # Accept things like "1.2.3" or "1.2.3rc1" by stopping at the first
    # non-numeric segment.
    parts = re.split(r"[.+-]", v.strip())
    nums: list[int] = []
    for part in parts:
        m = re.match(r"^(\\d+)", part)
        if not m:
            break
        nums.append(int(m.group(1)))
    return tuple(nums) if nums else None


def maybe_auto_update(
    *,
    interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS,
    force: bool = False,
) -> AutoUpdateResult:
    """Check and (optionally) upgrade openadapt, then re-exec the CLI.

    This is intended to be called from the top-level CLI group callback.
    """
    if os.getenv("OPENADAPT_DISABLE_AUTO_UPDATE", "").lower() in ("1", "true", "yes"):
        return AutoUpdateResult(checked=False, updated=False, message="auto-update disabled")

    if _is_ci():
        return AutoUpdateResult(checked=False, updated=False, message="skipped in CI")

    cache = _cache_path()
    now = time.time()
    last = _read_last_check_ts(cache)
    if not force and last is not None and now - last < interval_seconds:
        return AutoUpdateResult(checked=False, updated=False, message="recently checked")

    _write_last_check_ts(cache, now)

    latest_str = _fetch_latest_version()
    if not latest_str:
        return AutoUpdateResult(checked=True, updated=False, message="failed to fetch latest version")

    installed = _parse_version_tuple(__version__)
    latest = _parse_version_tuple(latest_str)
    if not installed or not latest:
        return AutoUpdateResult(checked=True, updated=False, message="invalid version format")

    if latest <= installed:
        return AutoUpdateResult(checked=True, updated=False, message="already up to date")

    # Upgrade package.
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "openadapt",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Don't hard-fail the user's command; just continue.
        return AutoUpdateResult(
            checked=True,
            updated=False,
            message="pip upgrade failed",
        )

    # Re-exec through python -m openadapt.cli to avoid console-script wrappers.
    os.execv(sys.executable, [sys.executable, "-m", "openadapt.cli", *sys.argv[1:]])

    # Unreachable (execv replaces process), but keeps type-checkers happy.
    return AutoUpdateResult(checked=True, updated=True, message="updated")
