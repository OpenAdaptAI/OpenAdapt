"""Register the optional Chrome extension's native-messaging host for a recording.

Part of OpenAdapt#1872. The optional Chrome extension sends evidence to a local
native-messaging host (``packages/flow/chrome_extension/native_host``). Chrome
starts that host only when a manifest named ``com.openadapt.extension.json``
sits in a directory it searches and the manifest's ``allowed_origins`` names
the calling extension.

Desktop does this for the person, only while a native recording runs:

- Opt-in. Nothing is written unless ``chrome_extension_id`` is set to a real
  32-letter (``a``-``p``) extension id. The manifest allows exactly that one
  origin; there is never a wildcard.
- On recording start Desktop writes the manifest, a launcher that points the
  host at the capture folder, and an "active" marker. On stop it removes all
  three. The host writes only while the marker holds ``active`` and the
  capture folder exists, so a host Chrome keeps running after the stop writes
  nothing more and never recreates a deleted folder.
- Revocation fails closed. The marker is also a lease: Desktop touches it
  while the recording runs and the host refuses a marker older than
  ``LEASE_SECONDS``. On stop Desktop stops touching it first, then removes it;
  if removal fails it overwrites it with ``revoked`` and backdates it. So a
  host that is already running stops writing even when every cleanup step
  fails. A marker Desktop could not revoke is retried, and no new
  registration is made until it is gone.
- Desktop never overwrites or removes a registration it did not write (for
  example one made by hand with ``install_host.py``). Its launcher carries an
  ownership marker line.
- At engine start no recording is active, so any Desktop-owned registration
  left by a crash is removed.
- macOS and Linux only, and only when the host script and a real Python
  interpreter are available (a source or editable install). Windows registers
  hosts in the registry and the packaged app does not ship the host yet; both
  are skipped and recording continues without extension evidence.

No network. The files are local and owner-only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

HOST_NAME = "com.openadapt.extension"
MANIFEST_FILE = f"{HOST_NAME}.json"
LAUNCHER_FILE = f"{HOST_NAME}-host.sh"
OWNER_MARKER = "# openadapt-desktop: registered for the active recording"
ACTIVE_PREFIX = ".extension-active-"
# Matched with fullmatch: "$" alone would also accept a trailing newline.
EXTENSION_ID = re.compile(r"[a-p]{32}")
ACTIVE_TEXT = "active\n"
REVOKED_TEXT = "revoked\n"
# The host refuses a marker not touched within LEASE_SECONDS; Desktop touches it
# every HEARTBEAT_SECONDS while the recording runs.
LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 5.0
BROWSERS = ("chrome", "chromium")

_USER_DIRS = {
    ("darwin", "chrome"): ("Library", "Application Support", "Google", "Chrome"),
    ("darwin", "chromium"): ("Library", "Application Support", "Chromium"),
    ("linux", "chrome"): (".config", "google-chrome"),
    ("linux", "chromium"): (".config", "chromium"),
}


def _platform_key(platform: str) -> str | None:
    if platform == "darwin":
        return "darwin"
    if platform.startswith("linux"):
        return "linux"
    return None


def user_manifest_dir(
    browser: str,
    *,
    home: Path | None = None,
    platform: str = sys.platform,
) -> Path | None:
    """The per-user ``NativeMessagingHosts`` folder, or None where unsupported."""

    key = _platform_key(platform)
    if key is None or (key, browser) not in _USER_DIRS:
        return None
    base = Path(home) if home is not None else Path.home()
    return base.joinpath(*_USER_DIRS[(key, browser)], "NativeMessagingHosts")


def find_host_script() -> Path | None:
    """The Flow host script next to an editable/source ``openadapt_flow``."""

    try:
        import openadapt_flow
    except ImportError:
        return None
    origin = getattr(openadapt_flow, "__file__", None)
    if not origin:
        return None
    candidate = (
        Path(origin).resolve().parents[1]
        / "chrome_extension"
        / "native_host"
        / "openadapt_extension_host.py"
    )
    return candidate if candidate.is_file() else None


def build_manifest(extension_id: str, launcher: Path) -> dict[str, Any]:
    """The host manifest allowing exactly one extension origin."""

    if not isinstance(extension_id, str) or not EXTENSION_ID.fullmatch(extension_id):
        raise ValueError("extension id must be 32 letters a-p")
    if not Path(launcher).is_absolute():
        raise ValueError("launcher path must be absolute")
    return {
        "name": HOST_NAME,
        "description": "OpenAdapt optional Chrome extension evidence host",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def launcher_text(python: str, host_script: Path, recording: Path, active_file: Path) -> str:
    """A POSIX launcher that runs the host for one recording."""

    argv = [
        python,
        str(host_script),
        "--recording",
        str(recording),
        "--active-file",
        str(active_file),
        "--active-lease",
        str(LEASE_SECONDS),
    ]
    command = " ".join(shlex.quote(part) for part in argv)
    return f'#!/bin/sh\n{OWNER_MARKER}\nexec {command} "$@"\n'


def owned_by_desktop(manifest_dir: Path) -> bool:
    """Whether the registration in ``manifest_dir`` was written by Desktop."""

    launcher = Path(manifest_dir) / LAUNCHER_FILE
    try:
        return OWNER_MARKER in launcher.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _registered(manifest_dir: Path) -> bool:
    return any((Path(manifest_dir) / name).exists() for name in (MANIFEST_FILE, LAUNCHER_FILE))


def _write_private(path: Path, text: str, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def register(
    manifest_dir: Path,
    extension_id: str,
    recording: Path,
    active_file: Path,
    *,
    host_script: Path,
    python: str,
) -> Path:
    """Register the host for one recording. Returns the manifest path.

    Raises ValueError (and writes nothing) on any precondition failure,
    including a registration Desktop did not write.
    """

    manifest_dir = Path(manifest_dir)
    if not python or not Path(python).is_absolute() or not Path(python).is_file():
        raise ValueError("python must be an absolute interpreter path")
    if not Path(host_script).is_file():
        raise ValueError("native host script is missing")
    recording = Path(recording)
    if not recording.is_absolute() or not recording.is_dir():
        raise ValueError("recording folder must exist")
    build_manifest(extension_id, manifest_dir.absolute() / LAUNCHER_FILE)  # validate first
    if _registered(manifest_dir) and not owned_by_desktop(manifest_dir):
        raise ValueError("another native host registration exists; leaving it alone")
    manifest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher = manifest_dir.resolve() / LAUNCHER_FILE
    manifest = build_manifest(extension_id, launcher)
    active_file = Path(active_file)
    active_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(active_file, ACTIVE_TEXT, 0o600)
    _write_private(
        launcher,
        launcher_text(python, Path(host_script).resolve(), recording.resolve(), active_file),
        0o700,
    )
    path = manifest_dir.resolve() / MANIFEST_FILE
    _write_private(path, json.dumps(manifest, indent=2) + "\n", 0o600)
    return path


def marker_revoked(marker: Path) -> bool:
    """Whether a running host would refuse ``marker``. Unreadable counts as live.

    Mirrors the host's check: gone, not holding exactly ``active``, or not
    touched within the lease.
    """

    marker = Path(marker)
    try:
        if not marker.exists() and not marker.is_symlink():
            return True
        if not marker.is_file():
            return False  # unexpected; leave it counted as live
        if not _holds_active(marker):
            return True
        return abs(time.time() - marker.stat().st_mtime) > LEASE_SECONDS
    except OSError:
        return False


def _holds_active(marker: Path) -> bool:
    with Path(marker).open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read(len(ACTIVE_TEXT) + 1) == ACTIVE_TEXT


def revoke_marker(marker: Path) -> bool:
    """End one active marker. Tries every route; True only when verified.

    Removal first. If that fails, overwrite the marker with ``revoked`` and
    backdate it past the lease, each attempted on its own. Never raises.
    """

    marker = Path(marker)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove a Chrome extension active marker: {}", exc)
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(REVOKED_TEXT)
        except OSError as write_exc:
            logger.warning("Could not overwrite a Chrome extension active marker: {}", write_exc)
        try:
            os.utime(marker, (0, 0), follow_symlinks=False)
        except (OSError, NotImplementedError) as time_exc:
            logger.warning("Could not expire a Chrome extension active marker: {}", time_exc)
    return marker_revoked(marker)


class RevocationError(OSError):
    """Some active markers could not be revoked."""

    def __init__(self, markers: list[Path]) -> None:
        super().__init__(f"{len(markers)} active marker(s) could not be revoked")
        self.markers = markers


def unregister(manifest_dir: Path | None, *, control_dir: Path | None = None) -> list[Path]:
    """Remove Desktop's registration and active markers. Foreign files stay.

    Raises RevocationError when an active marker could not be revoked; every other
    marker and the registration files are still attempted first.
    """

    removed: list[Path] = []
    unrevoked: list[Path] = []
    if control_dir is not None and Path(control_dir).is_dir():
        for marker in Path(control_dir).glob(f"{ACTIVE_PREFIX}*"):
            if not marker.exists() and not marker.is_symlink():
                continue
            if revoke_marker(marker):
                if not marker.exists():
                    removed.append(marker)
            else:
                unrevoked.append(marker)
    error: OSError | None = None
    try:
        _unregister_files(manifest_dir, removed)
    except OSError as exc:
        error = exc
    if unrevoked:
        raise RevocationError(unrevoked)
    if error is not None:
        raise error
    return removed


def _unregister_files(manifest_dir: Path | None, removed: list[Path]) -> list[Path]:
    if manifest_dir is None or not owned_by_desktop(manifest_dir):
        return removed
    for name in (MANIFEST_FILE, LAUNCHER_FILE):
        path = Path(manifest_dir) / name
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed


class ExtensionHostRegistrar:
    """Desktop's per-recording native host registration (opt-in, fail closed)."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.active = False
        # Markers this registrar wrote and has not verified as revoked. A new
        # registration waits until this is empty.
        self.unrevoked: set[Path] = set()
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat: threading.Thread | None = None

    @property
    def control_dir(self) -> Path:
        return Path(self.config.data_dir) / "recording-control"

    def manifest_dir(self) -> Path | None:
        override = getattr(self.config, "chrome_native_hosts_dir", None)
        if override:
            if _platform_key(sys.platform) is None:
                return None
            return Path(override)
        browser = str(getattr(self.config, "chrome_extension_browser", "chrome"))
        return user_manifest_dir(browser)

    def start(self, capture_id: str, capture_dir: Path | None) -> Path | None:
        """Register for ``capture_dir``. Never raises; None when skipped."""

        extension_id = str(getattr(self.config, "chrome_extension_id", "") or "")
        if not extension_id:
            return None
        try:
            if self.unrevoked and not self._revoke_known():
                raise ValueError("an earlier recording's host is not revoked yet")
            if getattr(sys, "frozen", False):
                raise ValueError("the packaged app does not ship the native host yet")
            target = self.manifest_dir()
            if target is None:
                raise ValueError("native host registration is not automated on this system")
            if capture_dir is None:
                raise ValueError("no recording folder")
            script = getattr(self.config, "chrome_extension_host_script", None)
            host_script = Path(script) if script else find_host_script()
            if host_script is None:
                raise ValueError("native host script is not installed")
            active_file = self.control_dir / f"{ACTIVE_PREFIX}{capture_id}"
            self.unrevoked.add(active_file)
            path = register(
                target,
                extension_id,
                Path(capture_dir),
                active_file,
                host_script=host_script,
                python=sys.executable,
            )
        except Exception as exc:
            logger.warning("Chrome extension evidence is off for this recording: {}", exc)
            self.sweep()  # leave nothing half-written
            return None
        self.active = True
        self._start_heartbeat(active_file)
        logger.info("Chrome extension host registered for this recording")
        return path

    def stop(self) -> list[Path]:
        """End this recording's registration, if one was made. Never raises."""

        if not self.active and not self.unrevoked:
            return []
        return self.sweep()

    def sweep(self) -> list[Path]:
        """Remove any Desktop-owned registration and markers. Never raises.

        Called at engine start and shutdown, when no recording can be active.
        The lease heartbeat stops first, so even if every later step fails a
        running host refuses once the lease runs out.
        """

        self._stop_heartbeat()
        self.active = False
        self._revoke_known()
        try:
            manifest_dir = self.manifest_dir()
        except Exception as exc:
            logger.warning("Could not locate the Chrome native host folder: {}", exc)
            manifest_dir = None
        try:
            return unregister(manifest_dir, control_dir=self.control_dir)
        except RevocationError as exc:
            self.unrevoked.update(exc.markers)
            logger.error("Could not revoke the Chrome extension host: {}", exc)
            return []
        except Exception as exc:
            logger.error("Could not remove the Chrome extension host registration: {}", exc)
            return []
        finally:
            self._revoke_known()

    @property
    def revoked(self) -> bool:
        """Whether every marker this registrar wrote is verified revoked."""

        return not self.active and not self.unrevoked

    def _revoke_known(self) -> bool:
        for marker in sorted(self.unrevoked):
            if revoke_marker(marker):
                self.unrevoked.discard(marker)
            else:
                logger.error(
                    "Chrome extension host for an ended recording is not revoked; "
                    "it stops writing within {} seconds",
                    LEASE_SECONDS,
                )
        return not self.unrevoked

    def _start_heartbeat(self, active_file: Path) -> None:
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    # A lapsed lease (say after sleep) is renewed; a removed or
                    # overwritten marker never is.
                    if not active_file.is_file() or not _holds_active(active_file):
                        return
                    os.utime(active_file)
                except OSError as exc:
                    # Fail closed: without a touch the lease runs out.
                    logger.warning("Chrome extension host lease not renewed: {}", exc)
                    return

        thread = threading.Thread(target=beat, name="extension-host-lease", daemon=True)
        self._heartbeat_stop, self._heartbeat = stop, thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        stop, thread = self._heartbeat_stop, self._heartbeat
        self._heartbeat_stop = self._heartbeat = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=HEARTBEAT_SECONDS * 2)
