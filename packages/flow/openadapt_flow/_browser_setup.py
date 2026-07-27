"""Lazy, on-first-use provisioning of the Chromium browser Playwright needs.

The base runtime deliberately has no browser dependency. Native desktop, RDP,
and Citrix users therefore install neither Playwright's ~34-47 MB platform
wheel nor its separate Chromium runtime. Browser users select the ``browser``
extra once; the matching Chromium build is then provisioned lazily on the first
real browser launch.

Design constraints:

* **No import-time side effects.** Importing this module (or the package) never
  touches the network or the filesystem beyond normal Python import -- the
  provisioning only happens when a browser launch is actually attempted.
* **At most once per process.** A module-level guard means the (cheap) probe
  runs a single time; a second launch in the same process is a no-op.
* **Idempotent across processes.** ``playwright install chromium`` is itself
  idempotent, and the probe skips it entirely once the binary is present, so a
  second *run* finds it installed and pays nothing.
* **Opt-out for air-gapped / pre-provisioned environments.** Set
  ``OPENADAPT_FLOW_NO_AUTO_INSTALL=1`` to skip the auto-install; the original
  clear Playwright "Executable doesn't exist ... run playwright install" error
  is then allowed to surface.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading

#: Environment variable that disables the auto-install (air-gapped / CI that
#: pre-provisions the browser itself). Any non-empty value opts out.
NO_AUTO_INSTALL_ENV = "OPENADAPT_FLOW_NO_AUTO_INSTALL"

_NOTICE = "Downloading the Chromium browser OpenAdapt needs (first run only)…"


class BrowserSupportMissing(RuntimeError):
    """The optional Playwright driver is absent for a browser operation."""


def browser_support_installed() -> bool:
    """Return whether the optional driver is importable, without importing it."""
    try:
        return importlib.util.find_spec("playwright.sync_api") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def require_browser_support() -> None:
    """Refuse a web operation with the canonical one-command install path."""
    if not browser_support_installed():
        raise BrowserSupportMissing(
            "Browser recording and replay are an optional capability. Install "
            "them once with:\n\n"
            "    python -m pip install 'openadapt[browser]'\n\n"
            "Native desktop, RDP, and Citrix workflows do not need this extra."
        )


# Guards so the probe runs at most once per process even under concurrent
# first-launch attempts from multiple threads.
_ensured = False
_lock = threading.Lock()


def _opted_out() -> bool:
    """True when the user asked us not to auto-install (env var set)."""
    return bool(os.environ.get(NO_AUTO_INSTALL_ENV))


def _chromium_present() -> bool:
    """Return whether Playwright's Chromium browser binary is installed.

    Playwright always reports the *expected* executable path for the pinned
    browser revision (even when it has never been downloaded), so the presence
    of the file on disk is the reliable signal -- we do not rely on catching a
    launch error. Any failure to determine the path is treated as "not present"
    so the (idempotent) install is attempted rather than wrongly skipped.
    """
    require_browser_support()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        path = p.chromium.executable_path
    return bool(path) and os.path.exists(path)


def _install_chromium() -> None:
    """Run ``python -m playwright install chromium`` once, with a notice.

    Raises:
        RuntimeError: if the install subprocess fails (e.g. offline), with an
            actionable message pointing at the manual command and the opt-out.
    """
    require_browser_support()
    print(_NOTICE, file=sys.stderr, flush=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError(
            "openadapt-flow could not automatically download the Chromium "
            "browser it needs. Run\n\n"
            "    playwright install chromium\n\n"
            "manually (you may be offline or behind a proxy), or set "
            f"{NO_AUTO_INSTALL_ENV}=1 to disable auto-install if the browser "
            "is provisioned another way."
        ) from exc


def ensure_chromium_installed() -> None:
    """Ensure Playwright's Chromium browser is available before a launch.

    Call this immediately before launching Chromium. It is safe to call from
    every browser-launch chokepoint: the work happens at most once per process
    (subsequent calls return immediately) and is a cheap no-op when the browser
    is already installed.

    When the browser is missing it downloads it once via
    ``playwright install chromium`` and prints a one-time notice. When
    :data:`NO_AUTO_INSTALL_ENV` is set it does nothing, leaving Playwright's own
    "browser not installed" error to surface at launch.
    """
    global _ensured
    require_browser_support()
    if _ensured:
        return
    with _lock:
        if _ensured:
            return
        if _opted_out():
            _ensured = True
            return
        try:
            present = _chromium_present()
        except Exception:
            # Could not determine presence (unexpected Playwright state); fall
            # back to the idempotent install rather than block the launch.
            present = False
        if not present:
            _install_chromium()
        _ensured = True
