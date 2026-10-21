"""Start and stop ``openadapt-flow record`` when OpenAdapt Desktop is absent.

The tray still records nothing itself. It runs the ``openadapt-flow record``
command a person would type, in its own process group, and stops it with the
same Ctrl-C (SIGINT) the command documents. The capture watcher reports when
the recorder is really ready and how it ended.

Only macOS is supported for now. On Windows, ``openadapt-flow`` stops on
Ctrl-C, which a tray cannot deliver to a separate console process reliably.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

_BACKENDS = {"darwin": "macos"}


class LocalRecordError(RuntimeError):
    """``openadapt-flow record`` could not be started or stopped."""


def unique_output_dir(captures_dir: Path, name: str) -> Path:
    """Return ``captures_dir/name``, with a numeric suffix if it exists."""
    candidate = captures_dir / name
    index = 2
    while candidate.exists():
        candidate = captures_dir / f"{name}-{index}"
        index += 1
    return candidate


class LocalRecorder:
    """One tray-owned ``openadapt-flow record`` process at a time."""

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
        self._process: Any | None = None
        self._log: IO[bytes] | None = None
        self.output_dir: Path | None = None
        self.log_path: Path | None = None

    def executable(self) -> str | None:
        """Return the ``openadapt-flow`` path, or None when it cannot be used."""
        if self._backend is None:
            return None
        return self._which("openadapt-flow")

    def available(self) -> bool:
        """Return True when this platform and PATH allow a local recording."""
        return self.executable() is not None

    def is_running(self) -> bool:
        """Return True while the tray-owned process has not exited."""
        return self._process is not None and self._process.poll() is None

    def start(self, name: str, captures_dir: Path) -> Path:
        """Start ``openadapt-flow record`` into a new folder under captures_dir.

        Returns:
            The recording output directory.

        Raises:
            LocalRecordError: A recording is already running, or the command
                could not be started.
        """
        if self.is_running():
            raise LocalRecordError("A tray recording is already running.")
        executable = self.executable()
        if executable is None:
            raise LocalRecordError("openadapt-flow is not on PATH.")
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
        log = log_path.open("ab")
        try:
            self._process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            log.close()
            raise LocalRecordError(f"Could not start openadapt-flow: {e}") from e
        self._log = log
        self.output_dir = output_dir
        self.log_path = log_path
        return output_dir

    def owns(self, capture_dir: str) -> bool:
        """Return True when capture_dir belongs to the latest tray recording."""
        if self.output_dir is None:
            return False
        try:
            Path(capture_dir).resolve().relative_to(self.output_dir.resolve())
        except ValueError:
            return False
        return True

    def stop(self) -> None:
        """Send SIGINT, the Ctrl-C that ``openadapt-flow record`` stops on."""
        if not self.is_running():
            raise LocalRecordError("No tray recording is running.")
        self._process.send_signal(signal.SIGINT)

    def wait(self) -> int:
        """Wait for the process to exit and return its exit code."""
        if self._process is None:
            raise LocalRecordError("No tray recording was started.")
        try:
            return self._process.wait()
        finally:
            if self._log is not None:
                self._log.close()
                self._log = None
