"""Act 2's runs, in parallel: one worker process per run.

Each worker starts while Act 1 is still demonstrating and compiling: it loads
the engine, starts its own browser, and serves its own copy of the clinic app
with its own database. When the program is ready, each worker runs it once
for one note, through the same governed path as any run (the Standard run
gate, identity checks, postconditions, the database read-back), and sends
back the facts. Nothing is shared between runs but the program on disk, which
every run checks against its sealed manifest before it starts.

Processes, not threads: the browser driver's objects belong to the thread that
made them, and each run needs its own database because every run of the
tutorial program writes the same logical record.

Workers drive Playwright's own Chromium. Several installed-Chrome instances
started at once stalled about one run in ten on a loaded Mac (2 of 20 runs),
while the bundled build ran 30 of 30. So a worker starts only when the bundled
build is already on disk (or the person chose a browser with
``OPENADAPT_FLOW_BROWSER``); otherwise the quickstart downloads nothing extra
and runs the five runs one after another in its own process.
"""

from __future__ import annotations

import multiprocessing
import os
import time
from dataclasses import asdict
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

#: Act 2 runs five notes; more workers than runs would only cost memory.
MAX_WORKERS = 5

#: How long workers may take to get ready before their runs are done in the
#: main process instead.
READY_TIMEOUT_S = 120.0

#: A run takes about 3 to 6 seconds. One that hasn't reported after this long
#: has stalled: its worker is stopped (its browser and its database go with
#: it) and the run is repeated in the main process, in a new run folder.
STALL_S = 45.0

#: How much lower than the main process a worker's CPU priority is.
WORKER_NICENESS = 10

#: The OCR engine's thread count (read once, when the engine loads).
OCR_THREADS_ENV = "OPENADAPT_FLOW_OCR_THREADS"

#: One job: run number, note (None replays the demonstrated note), and the
#: name of its run folder under the quickstart's output directory.
Job = tuple[int, Optional[str], str]


def worker_count(cpus: Optional[int] = None) -> int:
    """Workers for this computer: one per two CPUs, at most one per run."""

    cpus = cpus if cpus is not None else (os.cpu_count() or 2)
    return max(1, min(MAX_WORKERS, cpus // 2))


def worker_browser(visible: bool) -> Optional[str]:
    """The browser setting workers should use, or None when they shouldn't run.

    Checked once, in the main process: ``""`` keeps the person's own
    ``OPENADAPT_FLOW_BROWSER`` (or ``auto``, which means the bundled build on
    a computer without Chrome or Edge); ``"bundled"`` points workers at
    Playwright's own Chromium, already on disk. None means only an installed
    Chrome or Edge is available, so the runs go one after another.
    """

    from openadapt_flow import _browser_setup as setup

    if os.environ.get(setup.BROWSER_ENV):
        return ""
    if setup.installed_browser_channel() is None:
        return ""
    try:
        present = setup._bundled_present(only_shell=not visible)
    except Exception:  # noqa: BLE001 - unknown means run one after another
        present = False
    return "bundled" if present else None


def _worker_main(conn: Connection, visible: bool, browser: str = "") -> None:
    """One worker: get ready, then run the program for each job it is sent."""

    # Five engines at once: two OCR threads each, as Flow's parallel workers
    # use, so they don't crowd out the main process's compile.
    os.environ.setdefault(OCR_THREADS_ENV, "2")
    if browser:
        os.environ["OPENADAPT_FLOW_BROWSER"] = browser
    # Workers get ready while the main process demonstrates and compiles.
    # Lower priority keeps them on spare cores then; in Act 2 the main
    # process only waits, so the workers have the computer to themselves.
    if hasattr(os, "nice"):
        try:
            os.nice(WORKER_NICENESS)
        except OSError:
            pass
    try:
        from openadapt_flow import _prewarm
        from openadapt_flow.backends._browser_process import shared_browser
        from openadapt_flow.ir import Workflow
        from openadapt_flow.mockmed.fault_server import serve

        from openadapt.quickstart.engine import run_program

        _prewarm.start()
        with shared_browser(headless=not visible):
            base_url, _db, stop = serve()
            try:
                conn.send(("ready",))
                while True:
                    message = conn.recv()
                    if message[0] != "run":
                        return
                    _, index, note, bundle_dir, run_dir = message
                    try:
                        facts = run_program(
                            base_url=base_url,
                            workflow=Workflow.load(Path(bundle_dir)),
                            bundle_dir=Path(bundle_dir),
                            run_dir=Path(run_dir),
                            index=index,
                            note=note,
                            visible=visible,
                        )
                    except Exception as exc:  # noqa: BLE001 - reported to the parent
                        conn.send(("failed", f"{type(exc).__name__}: {exc}"))
                        continue
                    payload = asdict(facts)
                    payload["run_dir"] = str(facts.run_dir)
                    conn.send(("done", payload))
            finally:
                stop()
    except (EOFError, KeyboardInterrupt, BrokenPipeError):
        return
    except Exception as exc:  # noqa: BLE001 - the parent runs the job itself
        try:
            conn.send(("broken", f"{type(exc).__name__}: {exc}"))
        except (BrokenPipeError, OSError):
            pass
    finally:
        conn.close()


def _facts(fields: dict[str, Any]) -> Any:
    from openadapt.quickstart.engine import RunFacts

    fields = dict(fields)
    fields["run_dir"] = Path(fields["run_dir"])
    return RunFacts(**fields)


def _unfinished(root: Path, job: Job, reason: str) -> Any:
    """A run that raised in its worker: not checked, with the reason."""

    from openadapt.quickstart.engine import RunFacts

    index, note, folder = job
    return RunFacts(
        index=index,
        note=note or "",
        run_dir=root / folder,
        execution_outcome="ERROR",
        transaction_outcome=None,
        checked=False,
        records=0,
        model_calls=0,
        cost_usd=0.0,
        effects_required=0,
        effects_confirmed=0,
        seconds=0.0,
        plain_label=f"Didn't finish: {reason}"[:80],
    )


class WorkerPool:
    """Worker processes, started early, each taking one run at a time."""

    def __init__(
        self,
        size: int,
        *,
        visible: bool,
        browser: str = "",
        target: Callable[..., None] = _worker_main,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        self._workers: dict[Connection, Any] = {}
        for _ in range(size):
            parent, child = context.Pipe()
            process = context.Process(
                target=target, args=(child, visible, browser), daemon=True
            )
            process.start()
            child.close()
            self._workers[parent] = process
        self._ready: list[Connection] = []
        self._gone: set[Connection] = set()

    @property
    def size(self) -> int:
        return len(self._workers)

    def usable(self) -> int:
        """Workers that are ready or still starting (not ones that gave up)."""

        for conn in wait(list(self._starting()), timeout=0):
            self._note_start(conn)
        return self.size - len(self._gone)

    def _starting(self) -> set[Connection]:
        return set(self._workers) - set(self._ready) - self._gone

    def _note_start(self, conn: Connection) -> None:
        """Read a starting worker's first message: ready, or gave up."""

        try:
            message = conn.recv()
        except (EOFError, OSError):
            message = ("broken", "the worker process exited")
        if message[0] == "ready":
            self._ready.append(conn)
        else:
            self._drop(conn)

    def _drop(self, conn: Connection) -> None:
        self._gone.add(conn)
        process = self._workers.get(conn)
        if process is not None and process.is_alive():
            process.terminate()

    def run(
        self,
        jobs: Sequence[Job],
        *,
        bundle_dir: Path,
        root: Path,
        on_finish: Callable[[Any], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> list[Job]:
        """Run ``jobs`` across the workers; return the jobs to run here instead.

        Returned jobs are those no worker started, and those whose worker
        stalled. A stalled job comes back with a new run folder, since the
        engine never reuses one; its worker, with its browser and its own
        database, is stopped first. A run that raised in its worker is
        reported as not finished and not repeated.
        """

        pending = list(jobs)
        given: dict[Connection, tuple[Job, float]] = {}
        back: list[Job] = []
        started = clock()

        def give(conn: Connection) -> None:
            if pending:
                job = pending.pop(0)
                index, note, folder = job
                conn.send(("run", index, note, str(bundle_dir), str(root / folder)))
                given[conn] = (job, clock())

        for conn in list(self._ready):
            give(conn)
        while (self._starting() and pending) or given:
            now = clock()
            deadlines = [since + STALL_S for _job, since in given.values()]
            starting = self._starting() if pending else set()
            if starting:
                deadlines.append(started + READY_TIMEOUT_S)
            timeout = max(0.0, min(deadlines) - now) if deadlines else None
            for conn in wait(list(starting | set(given)), timeout=timeout):
                if conn not in given:
                    self._note_start(conn)
                    if conn in self._ready:
                        give(conn)
                    continue
                try:
                    message = conn.recv()
                except (EOFError, OSError):
                    message = ("broken", "the worker process exited")
                kind = message[0]
                if kind == "done":
                    given.pop(conn, None)
                    on_finish(_facts(message[1]))
                    give(conn)
                elif kind == "failed":
                    job, _since = given.pop(conn)
                    on_finish(_unfinished(root, job, message[1]))
                    give(conn)
                else:  # broken: this worker exited mid-run
                    job, _since = given.pop(conn)
                    self._drop(conn)
                    back.append(_again(job))
            now = clock()
            for conn, (job, since) in list(given.items()):
                if now - since >= STALL_S:
                    given.pop(conn)
                    self._drop(conn)
                    back.append(_again(job))
            if pending and now - started >= READY_TIMEOUT_S:
                for conn in self._starting():
                    self._drop(conn)
        return sorted(back + pending)

    def release(self) -> None:
        """Tell every worker to shut down, without waiting for it."""

        for conn in self._workers:
            try:
                conn.send(("close",))
            except (BrokenPipeError, OSError):
                pass

    def close(self) -> None:
        self.release()
        for conn, process in self._workers.items():
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            conn.close()


def _again(job: Job) -> Job:
    index, note, folder = job
    return (index, note, f"{folder}-again")


__all__ = [
    "MAX_WORKERS",
    "STALL_S",
    "Job",
    "WorkerPool",
    "worker_browser",
    "worker_count",
]
