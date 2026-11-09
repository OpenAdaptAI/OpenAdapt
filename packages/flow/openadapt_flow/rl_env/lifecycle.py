"""Start and stop environment server processes, and confirm they are gone.

A server runs in its own session (its own process group), so stopping it
reaches everything it started. Stopping sends SIGTERM to the group, waits a
bounded time with ``Popen.wait`` (which also reaps the child), escalates to
SIGKILL and waits again. A process that is still there after both waits
raises :class:`ProcessNotReaped`; nothing reports it stopped until it has
been reaped.

Platform limit. An empty process group does not prove a tree gone: a
descendant that called ``setsid`` has left the group, and once its parent
dies it is reparented to init (or launchd), where no parent-pid walk finds
it. Only Linux can prove the whole tree gone: a command run under
:func:`supervised_command` has a child subreaper above it
(``PR_SET_CHILD_SUBREAPER``), every orphan comes back to that supervisor,
and its ``waitpid(-1)`` reaching ``ECHILD`` is the proof
(:func:`stop_supervised` returns a :class:`Teardown` with ``proven=True``).
On macOS and Windows, and for anything not started under the supervisor,
teardown kills what it can find and reports ``proven=False``; callers must
treat that as unknown, never as gone. :func:`stop` covers a server's own
process group only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

#: How long a server gets to exit after SIGTERM, then after SIGKILL.
TERM_TIMEOUT_S = 10.0
KILL_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Teardown:
    """How a process tree was stopped. ``proven`` only when a subreaper saw
    every descendant gone (Linux, under :func:`supervised_command`)."""

    how: Literal["killed", "exited"]
    proven: bool


class ProcessNotReaped(RuntimeError):
    """A process could not be confirmed dead within the bound."""


class DescendantsUnknown(ProcessNotReaped):
    """The process table could not be read, so what a process left behind is
    unknown. Never treated as an empty tree."""


#: How many times one process-table read is tried before it counts as failed.
CENSUS_ATTEMPTS = 2


@dataclass(frozen=True)
class Stopped:
    """How a server process ended: it had ``exited`` already, was
    ``terminated`` by SIGTERM, or was ``killed`` after ignoring it."""

    pid: int
    how: Literal["exited", "terminated", "killed"]
    returncode: int


def spawn(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    """``Popen`` in a new session, so the whole group can be stopped."""
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)  # type: ignore[attr-defined]
    else:
        kwargs.setdefault("start_new_session", True)
    return subprocess.Popen(list(args), **kwargs)


def _signal_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    if os.name == "nt":
        if sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass  # the group is already empty


def _group_alive(pgid: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # members are there (macOS also says so for zombies)
    except OSError as exc:
        raise DescendantsUnknown(
            f"process group {pgid} could not be inspected: {exc}"
        ) from exc
    return True


def stop(
    proc: subprocess.Popen[bytes],
    *,
    term_timeout: float = TERM_TIMEOUT_S,
    kill_timeout: float = KILL_TIMEOUT_S,
) -> Stopped:
    """Stop ``proc`` and its process group; return once it is reaped.

    Raises :class:`ProcessNotReaped` if the process, or anything left in its
    group, is still there after SIGKILL and ``kill_timeout``.
    """
    how: Literal["exited", "terminated", "killed"]
    if proc.poll() is not None:
        how = "exited"
    else:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=term_timeout)
            how = "terminated"
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=kill_timeout)
            except subprocess.TimeoutExpired:
                raise ProcessNotReaped(
                    f"process {proc.pid} is still running {kill_timeout:.0f} s "
                    "after SIGKILL"
                ) from None
            how = "killed"
    # The leader is reaped; anything it left in its group goes too.
    if os.name != "nt" and _group_alive(proc.pid):
        _signal_group(proc, signal.SIGKILL)
        deadline = time.monotonic() + kill_timeout
        while _group_alive(proc.pid):
            if time.monotonic() > deadline:
                raise ProcessNotReaped(
                    f"process group {proc.pid} still has members "
                    f"{kill_timeout:.0f} s after SIGKILL"
                )
            time.sleep(0.05)
    assert proc.returncode is not None
    return Stopped(pid=proc.pid, how=how, returncode=proc.returncode)


# -- processes this one did not start with Popen -----------------------------
#
# The proof that a tree is gone is its process group: the target must lead
# its own group (started with ``start_new_session``), the whole group is
# SIGKILLed, and ``killpg(pgid, 0)`` must then fail with ESRCH. A ``ps`` walk
# by parent pid only adds processes that left the group (``setsid``) to the
# kill list. It is never the proof, and a census that looks incomplete
# refuses the teardown instead of being read as "nothing else there".

Row = tuple[int, int, int, str]  # pid, ppid, pgid, stat

#: The header ``ps -A -o pid,ppid,pgid,stat`` prints first.
PS_HEADER = ["PID", "PPID", "PGID", "STAT"]


def _ps_rows(must_include: Sequence[int] = ()) -> Optional[list[Row]]:
    """One read of the process table, or None when it could not be read or
    looks incomplete: no header, an unparseable row, or a missing process
    that is known to exist (this one, and each of ``must_include``)."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid,ppid,pgid,stat"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.split() for line in out.splitlines() if line.strip()]
    if not lines or [t.upper() for t in lines[0]] != PS_HEADER:
        return None
    rows: list[Row] = []
    for parts in lines[1:]:
        if len(parts) < 4:
            return None
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        except ValueError:
            return None
    listed = {row[0] for row in rows}
    if os.getpid() not in listed or any(p not in listed for p in must_include):
        return None
    return rows


def _census(must_include: Sequence[int] = ()) -> Optional[list[Row]]:
    for _ in range(CENSUS_ATTEMPTS):
        rows = _ps_rows(must_include)
        if rows is not None:
            return rows
    return None


def _tree(pid: int, rows: list[Row]) -> set[int]:
    """``pid``'s live descendants in one census (not ``pid`` itself)."""
    children: dict[int, list[int]] = {}
    for child, parent, _pgid, stat in rows:
        if not stat.startswith("Z"):
            children.setdefault(parent, []).append(child)
    found: set[int] = set()
    todo = list(children.get(pid, []))
    while todo:
        current = todo.pop()
        if current not in found:
            found.add(current)
            todo.extend(children.get(current, []))
    return found


def _reap_child(pid: int) -> bool:
    """Reap ``pid`` if it is this process's exited child. True once it is
    reaped (by this call or by someone else, such as asyncio's watcher)."""
    try:
        done, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True  # not an unreaped child of ours any more
    except OSError as exc:
        raise ProcessNotReaped(f"process {pid} could not be waited on: {exc}") from exc
    return done == pid


def _signal(pid: int, sig: int) -> bool:
    """Send ``sig``; False only when ``pid`` is already gone."""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise ProcessNotReaped(f"process {pid} could not be signalled: {exc}") from exc
    return True


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # already empty
    except PermissionError:
        pass  # only zombies left (macOS); the emptiness check decides
    except OSError as exc:
        raise ProcessNotReaped(
            f"process group {pgid} could not be signalled: {exc}"
        ) from exc


def kill_child_tree(pid: int, *, timeout: float = KILL_TIMEOUT_S) -> Teardown:
    """SIGKILL ``pid``, a child of this process that leads its own process
    group, and everything it started.

    Returns ``how="killed"`` only once ``pid`` is reaped, its group (and the
    group of any descendant that left it) is confirmed empty with
    ``killpg(pgid, 0)``, and a complete census found every process it listed
    gone. Raises :class:`DescendantsUnknown` when that cannot be shown: the
    target does not lead its own group, or the process table looks
    incomplete. Anything still there at the bound raises
    :class:`ProcessNotReaped`. Either way everything known is killed first.
    ``how="exited"`` means ``pid`` had already exited and been reaped and its
    group is empty; nothing was signalled, so a reused id is never hit.
    ``proven`` is always False: an orphaned ``setsid`` descendant cannot be
    found from here (see the module docstring); use :func:`stop_supervised`.
    """
    deadline = time.monotonic() + timeout
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        raise DescendantsUnknown(
            f"process {pid} was sent taskkill /T, but Windows has no process "
            "group to prove its descendants gone"
        )
    try:
        pgid: Optional[int] = os.getpgid(pid)
    except ProcessLookupError:
        pgid = None
    if _reap_child(pid):
        if _group_alive(pid):
            raise DescendantsUnknown(
                f"process {pid} had already exited, but a process group with "
                "its id is still there and may not be its own"
            )
        return Teardown("exited", proven=False)
    unknown: Optional[str] = None
    groups: set[int] = set()
    if pgid == pid and pgid != os.getpgrp():
        groups.add(pgid)
        _killpg(pgid, signal.SIGSTOP)  # nothing in it can start anything now
    else:
        unknown = f"process {pid} does not lead its own process group"
        _signal(pid, signal.SIGSTOP)
    # Processes that left the group, to kill as well; never the proof.
    extras: set[int] = set()
    while True:
        rows = _census(must_include=[pid, *sorted(extras)])
        if rows is None:
            unknown = unknown or "the process table looks incomplete before the kill"
            break
        pgids = {row[0]: row[2] for row in rows}
        found = _tree(pid, rows)
        new = {p for p in found - extras if pgids.get(p) not in groups}
        for p in new:
            if pgids.get(p) == p and p != os.getpgrp():
                groups.add(p)  # a descendant that leads a group of its own
                _killpg(p, signal.SIGSTOP)
            else:
                _signal(p, signal.SIGSTOP)
        extras |= new
        if not new:
            break
        if time.monotonic() > deadline:
            unknown = unknown or f"process {pid} kept starting processes"
            break
    for group in sorted(groups):
        _killpg(group, signal.SIGKILL)
    for target in [pid, *sorted(extras)]:
        _signal(target, signal.SIGKILL)
    reaped = False
    while True:
        reaped = reaped or _reap_child(pid)
        occupied = [g for g in sorted(groups) if _group_alive(g)]
        if reaped and not occupied:
            if unknown is not None:
                raise DescendantsUnknown(
                    f"process {pid} was killed and reaped, but {unknown}, so "
                    "what it started is unknown"
                )
            rows = _census()
            if rows is None:
                raise DescendantsUnknown(
                    f"process {pid} and its group are gone, but the process "
                    "table looks incomplete after the kill"
                )
            running = {p for p, _pp, _pg, stat in rows if not stat.startswith("Z")}
            survivors = extras & running
            if not survivors:
                # Empty groups and a complete census, but a descendant that
                # left with setsid and was orphaned is out of reach here.
                return Teardown("killed", proven=False)
        else:
            survivors = set()
        if time.monotonic() > deadline:
            if not reaped:
                what = f"process {pid}"
            elif occupied:
                what = f"process group(s) {occupied} of {pid}"
            else:
                what = f"descendants {sorted(survivors)} of {pid}"
            raise ProcessNotReaped(
                f"{what} still running {timeout:.0f} s after SIGKILL"
            )
        time.sleep(0.05)


# -- Linux: a child subreaper proves the whole tree gone ----------------------

SUPERVISOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_supervisor.py")
_REPORT_DIR: Optional[str] = None


def _report_dir() -> str:
    global _REPORT_DIR
    if _REPORT_DIR is None:
        import tempfile

        _REPORT_DIR = tempfile.mkdtemp(prefix="openadapt-supervisor-")
    return _REPORT_DIR


def supervised_command(args: Sequence[str]) -> list[str]:
    """``args`` run under the subreaper supervisor (``_supervisor.py``).

    Start the result in a new session; stop it with :func:`stop_supervised`.
    """
    import sys

    return [sys.executable, "-I", SUPERVISOR, _report_dir(), "--", *args]


def spawn_supervised(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    """:func:`spawn` of ``args`` under the supervisor."""
    return spawn(supervised_command(args), **kwargs)


def stop_supervised(pid: int, *, timeout: float = KILL_TIMEOUT_S * 2) -> Teardown:
    """Stop a supervisor started from :func:`supervised_command`.

    SIGTERM asks it to kill and reap the command's whole tree; it reports
    the verdict in a file before it exits. ``proven`` only for a supervisor
    that is gone (reaped, its group empty) and reported ``proven``. A
    supervisor that does not finish in time is SIGKILLed with its group and
    :class:`ProcessNotReaped` is raised; one that reports survivors or no
    report raises :class:`DescendantsUnknown`.
    """
    report = os.path.join(_report_dir(), str(pid))
    deadline = time.monotonic() + timeout
    _signal(pid, signal.SIGTERM)
    while not _reap_child(pid) or _group_alive(pid):
        if time.monotonic() > deadline:
            _killpg(pid, signal.SIGKILL)
            _signal(pid, signal.SIGKILL)
            raise ProcessNotReaped(
                f"supervisor {pid} did not finish its teardown in {timeout:.0f} s"
            )
        time.sleep(0.02)
    try:
        with open(report) as handle:
            verdict = json.load(handle).get("verdict")
        os.unlink(report)
    except (OSError, ValueError):
        verdict = None
    if verdict == "proven":
        return Teardown("killed", proven=True)
    if verdict == "unproven":
        return Teardown("killed", proven=False)
    raise DescendantsUnknown(
        f"supervisor {pid} reported {verdict or 'nothing'}: its command's "
        "descendants may still be running"
    )
