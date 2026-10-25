"""Start and stop ``openadapt-flow record`` when OpenAdapt Desktop is absent.

The tray still records nothing itself. It runs the ``openadapt-flow record``
command a person would type, in its own process group, and stops it with the
same Ctrl-C (SIGINT) the command documents. The capture watcher reports when
the recorder is really ready and how it ended.

Only macOS is supported for now. On Windows, ``openadapt-flow`` stops on
Ctrl-C, which a tray cannot deliver to a separate console process reliably.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

_BACKENDS = {"darwin": "macos"}


class LocalRecordError(RuntimeError):
    """``openadapt-flow record`` could not be started or stopped."""


@dataclass
class LocalRun:
    """One tray-started ``openadapt-flow record`` process and its log."""

    process: Any
    output_dir: Path
    log_path: Path
    log: IO[bytes] | None = field(default=None, repr=False)
    # Set by the tray when the watcher reports this run's capture as saved.
    capture_saved: bool = False
    interrupted: bool = False

    def interrupt(self) -> bool:
        """Send SIGINT once. A second Ctrl-C could cut finalization short."""
        if self.interrupted or not self.is_running():
            return False
        try:
            self.process.send_signal(signal.SIGINT)
        except OSError:
            return False
        self.interrupted = True
        return True

    def is_running(self) -> bool:
        """Return True while the process has not exited."""
        return self.process.poll() is None

    def owns(self, capture_dir: str) -> bool:
        """Return True when capture_dir is inside this run's output folder."""
        try:
            Path(capture_dir).resolve().relative_to(self.output_dir.resolve())
        except ValueError:
            return False
        return True

    def wait(self) -> int:
        """Wait for the process to exit, close its log, return the exit code."""
        try:
            return self.process.wait()
        finally:
            if self.log is not None:
                self.log.close()
                self.log = None


def safe_capture_name(name: str) -> str:
    """Return name as one folder name: no path separators, no leading dots.

    The name comes from a dialog. ``captures_dir / "/abs"`` is ``/abs``, and
    ``..`` climbs out of the captures folder, so neither may reach a path.
    """
    cleaned = re.sub(r"[/\\:\x00]+", "-", name).strip(" .-\t\n")
    if not cleaned:
        raise LocalRecordError(f"{name!r} cannot be used as a capture name.")
    return cleaned


def unique_output_dir(captures_dir: Path, name: str) -> Path:
    """Return ``captures_dir/name``, with a numeric suffix if it exists."""
    candidate = captures_dir / name
    index = 2
    while candidate.exists():
        candidate = captures_dir / f"{name}-{index}"
        index += 1
    return candidate


class LocalRecorder:
    """At most one running tray-owned ``openadapt-flow record`` at a time."""

    def __init__(
        self,
        *,
        platform: str = sys.platform,
        which: Callable[[str], str | None] = shutil.which,
        popen: Callable[..., Any] = subprocess.Popen,
    ):
        """Create a launcher.

        Args:
            platform: ``sys.platform`` value (injectable for tests).
            which: Executable lookup (injectable for tests).
            popen: Process factory (injectable for tests).
        """
        self._backend = _BACKENDS.get(platform)
        self._which = which
        self._popen = popen
        # The latest run. It stays set after the process exits so a late
        # watcher event can still be matched to it.
        self.current: LocalRun | None = None

    def executable(self) -> str | None:
        """Return the ``openadapt-flow`` path, or None when it cannot be used."""
        if self._backend is None:
            return None
        return self._which("openadapt-flow")

    def available(self) -> bool:
        """Return True when this platform and PATH allow a local recording."""
        return self.executable() is not None

    def is_running(self) -> bool:
        """Return True while the latest run's process has not exited."""
        return self.current is not None and self.current.is_running()

    def owns(self, capture_dir: str) -> bool:
        """Return True when capture_dir belongs to the latest run."""
        return self.current is not None and self.current.owns(capture_dir)

    def start(self, name: str, captures_dir: Path) -> LocalRun:
        """Start ``openadapt-flow record`` into a new folder under captures_dir.

        Raises:
            LocalRecordError: A recording is already running, the name is
                unusable, or the command could not be started.
        """
        if self.is_running():
            raise LocalRecordError("A tray recording is already running.")
        executable = self.executable()
        if executable is None:
            raise LocalRecordError("openadapt-flow is not on PATH.")
        name = safe_capture_name(name)
        captures_dir.mkdir(parents=True, exist_ok=True)
        output_dir = unique_output_dir(captures_dir, name)
        log_path = captures_dir / f"{output_dir.name}.record.log"
        command = [
            executable,
            "record",
            "--backend",
            self._backend,
            "--out",
            str(output_dir),
            "--task",
            name,
        ]
        # The log holds recorder output, so only this user may read it.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        log = os.fdopen(os.open(log_path, flags, 0o600), "ab")
        try:
            process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            log.close()
            raise LocalRecordError(f"Could not start openadapt-flow: {e}") from e
        self.current = LocalRun(
            process=process, output_dir=output_dir, log_path=log_path, log=log
        )
        return self.current

    def stop(self) -> None:
        """Send SIGINT, the Ctrl-C that ``openadapt-flow record`` stops on."""
        if not self.is_running():
            raise LocalRecordError("No tray recording is running.")
        self.current.interrupt()

    def interrupt_if_running(self) -> bool:
        """Send SIGINT if a run is alive, whatever the tray state is.

        Used when the tray quits: the process is in its own session, so it
        would otherwise keep recording with no menu-bar cue.
        """
        return self.current is not None and self.current.interrupt()
