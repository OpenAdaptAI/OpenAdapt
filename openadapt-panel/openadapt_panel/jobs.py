"""Background job manager for long-running panel operations.

Two job flavours share one registry and status model:

* **Process jobs** (``start_process``) — capture recording and training run as
  subprocesses of the ``openadapt`` CLI itself. Reusing the CLI as the execution
  seam means the panel can't drift from the tested CLI seam. stdout/stderr is
  tee'd into an in-memory ring buffer that the SSE endpoint streams.
* **Thread jobs** (``start_thread``) — evaluation calls sibling functions
  directly (it needs structured metrics back), so it runs in a worker thread and
  stashes its result/exception on the job.

Stopping:
* training → write the ``STOP_TRAINING`` sentinel the CLI/openadapt-ml honor
  (clean stop). Configured via ``stop_sentinel``.
* capture → send an interrupt to the child. On POSIX that's ``SIGINT``; on
  Windows the child is spawned in a new process group and sent
  ``CTRL_BREAK_EVENT`` so it raises KeyboardInterrupt and the Recorder context
  manager exits cleanly (mirroring the CLI's Ctrl+C stop path).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_MAX_LOG_LINES = 2000


@dataclass
class Job:
    id: str
    kind: str  # "capture" | "train" | "eval"
    title: str
    status: str = "running"  # running | finished | failed | stopped
    lines: deque = field(default_factory=lambda: deque(maxlen=_MAX_LOG_LINES))
    result: Optional[dict] = None
    error: Optional[str] = None
    returncode: Optional[int] = None
    # internals
    _process: Optional[subprocess.Popen] = None
    _stop_sentinel: Optional[Path] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def to_dict(self, *, tail: int = 200) -> dict:
        with self._lock:
            lines = list(self.lines)[-tail:]
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "returncode": self.returncode,
            "result": self.result,
            "error": self.error,
            "lines": lines,
        }

    def append(self, line: str) -> None:
        with self._lock:
            self.lines.append(line.rstrip("\n"))


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def _new_id(self, kind: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{kind}-{self._counter}"

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        return [job.to_dict(tail=1) for job in self._jobs.values()]

    # -- process jobs -------------------------------------------------------

    def start_process(
        self,
        *,
        kind: str,
        title: str,
        argv: list[str],
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stop_sentinel: Optional[Path] = None,
    ) -> Job:
        job = Job(id=self._new_id(kind), kind=kind, title=title)
        job._stop_sentinel = stop_sentinel

        popen_kwargs: dict = dict(
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )
        # New process group so we can signal it for a clean stop.
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(argv, **popen_kwargs)
        job._process = proc
        self._jobs[job.id] = job

        threading.Thread(
            target=self._pump_process, args=(job, proc), daemon=True
        ).start()
        return job

    def _pump_process(self, job: Job, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            job.append(line)
        proc.wait()
        job.returncode = proc.returncode
        if job.status == "running":
            job.status = "finished" if proc.returncode == 0 else "failed"

    # -- thread jobs --------------------------------------------------------

    def start_thread(
        self,
        *,
        kind: str,
        title: str,
        target: Callable[[Job], dict],
    ) -> Job:
        job = Job(id=self._new_id(kind), kind=kind, title=title)
        self._jobs[job.id] = job

        def runner() -> None:
            try:
                job.result = target(job)
                if job.status == "running":
                    job.status = "finished"
            except Exception as exc:  # noqa: BLE001 - surface any failure to UI
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "failed"

        threading.Thread(target=runner, daemon=True).start()
        return job

    # -- stopping -----------------------------------------------------------

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != "running":
            return False

        # Training: clean stop via the sentinel file openadapt-ml honors.
        if job._stop_sentinel is not None:
            job._stop_sentinel.parent.mkdir(parents=True, exist_ok=True)
            job._stop_sentinel.touch()
            job.status = "stopped"
            job.append("[panel] stop requested (STOP_TRAINING sentinel written)")
            return True

        # Process job: interrupt the child's process group.
        if job._process is not None and job._process.poll() is None:
            try:
                if os.name == "nt":
                    job._process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(os.getpgid(job._process.pid), signal.SIGINT)
            except (ProcessLookupError, OSError):
                job._process.terminate()
            job.status = "stopped"
            job.append("[panel] stop signal sent")
            return True

        return False


def cli_argv(*args: str) -> list[str]:
    """argv to invoke the installed openadapt CLI as a subprocess."""
    return [sys.executable, "-m", "openadapt.cli", *args]
