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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

_BACKENDS = {"darwin": "macos"}

# Outcomes of LocalRun.stop_within.
STOP_RECORDER_STOPPED = "recorder_stopped"  # the recorder stopped capturing
STOP_EXITED = "exited"
STOP_TERMINATED = "terminated"  # ignored Ctrl-C; terminate() ended it
STOP_KILLED = "killed"  # ignored terminate() too; kill() ended it
STOP_STILL_RUNNING = "still_running"
STOP_NOT_RUNNING = "not_running"


class LocalRecordError(RuntimeError):
    """``openadapt-flow record`` could not be started or stopped."""


def interrupt_signal(platform: str = sys.platform) -> int:
    """Return the Ctrl-C equivalent for a child in its own process group.

    POSIX: SIGINT. Windows has no SIGINT delivery to another console process
    group; CTRL_BREAK_EVENT is the documented one, and it needs
    ``CREATE_NEW_PROCESS_GROUP`` (see :func:`spawn_options`).
    """
    if platform == "win32":
        return getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
    return signal.SIGINT


def spawn_options(platform: str = sys.platform) -> dict[str, Any]:
    """Return Popen options that detach the child from the tray's own group.

    The child must not receive the tray's own Ctrl-C, and the tray must be
    able to signal it on its own.
    """
    if platform == "win32":
        # subprocess.CREATE_NEW_PROCESS_GROUP is 0x200 on Windows.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


@dataclass
class LocalRun:
    """One tray-started ``openadapt-flow record`` process and its log."""

    process: Any
    output_dir: Path
    log_path: Path
    log: IO[bytes] | None = field(default=None, repr=False)
    # Set by the tray when the watcher reports this run's capture as saved.
    capture_saved: bool = False
    # Sent by interrupt(); see interrupt_signal().
    interrupt_with: int = signal.SIGINT
    interrupted: bool = False
    _interrupt_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def interrupt(self) -> bool:
        """Send Ctrl-C once. A second one could cut finalization short."""
        with self._interrupt_lock:
            if self.interrupted or not self.is_running():
                return False
            try:
                self.process.send_signal(self.interrupt_with)
            except OSError:
                return False
            self.interrupted = True
            return True

    # The latest watcher stage for this run's recorder (capture_watch kinds).
    recorder_stage: str | None = None

    def recorder_stopped(self) -> bool:
        """Return True once the watcher saw this run's recorder stop capturing."""
        return self.recorder_stage in ("stopping", "ended")

    def stop_within(
        self,
        interrupt_timeout_s: float = 30.0,
        terminate_timeout_s: float = 10.0,
        *,
        poll_s: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> str:
        """Stop the recording, escalating if Ctrl-C is not obeyed in time.

        Ctrl-C (SIGINT) first. That succeeds once the recorder reports it
        stopped capturing, or the process exits. Finalizing and converting may
        then take as long as they need. Past the deadline, terminate, then
        kill (SIGTERM and SIGKILL on POSIX, TerminateProcess on Windows): a
        recorder that ignores Ctrl-C must not keep capturing.

        Returns:
            One of the ``STOP_*`` outcomes.
        """
        if not self.is_running():
            return STOP_NOT_RUNNING
        self.interrupt()
        if self._wait_until(
            lambda: self.recorder_stopped() or not self.is_running(),
            interrupt_timeout_s,
            poll_s,
            clock,
            sleep,
        ):
            return STOP_RECORDER_STOPPED if self.is_running() else STOP_EXITED
        # Popen.terminate()/kill() are SIGTERM/SIGKILL on POSIX and
        # TerminateProcess on Windows, which has no signal.SIGKILL.
        for force, outcome in (
            (self.process.terminate, STOP_TERMINATED),
            (self.process.kill, STOP_KILLED),
        ):
            try:
                force()
            except OSError:
                pass
            if self._wait_until(
                lambda: not self.is_running(),
                terminate_timeout_s,
                poll_s,
                clock,
                sleep,
            ):
                return outcome
        return STOP_STILL_RUNNING

    @staticmethod
    def _wait_until(
        condition: Callable[[], bool],
        timeout_s: float,
        poll_s: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> bool:
        deadline = clock() + timeout_s
        while True:
            if condition():
                return True
            if clock() >= deadline:
                return False
            sleep(poll_s)

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
        self._platform = platform
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
        # The log holds recorder output, so only this user may read it. On
        # Windows the mode sets only the read-only bit; the file inherits the
        # captures folder's ACL, which is in the user's own profile.
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
                **spawn_options(self._platform),
            )
        except OSError as e:
            log.close()
            raise LocalRecordError(f"Could not start openadapt-flow: {e}") from e
        self.current = LocalRun(
            process=process,
            output_dir=output_dir,
            log_path=log_path,
            log=log,
            interrupt_with=interrupt_signal(self._platform),
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
