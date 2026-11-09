"""Start and stop environment server processes, and confirm they are gone.

A server runs in its own session (its own process group), so stopping it
reaches everything it started. Stopping sends SIGTERM to the group, waits a
bounded time with ``Popen.wait`` (which also reaps the child), escalates to
SIGKILL and waits again. A process that is still there after both waits
raises :class:`ProcessNotReaped`; nothing reports it stopped until it has
been reaped.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

#: How long a server gets to exit after SIGTERM, then after SIGKILL.
TERM_TIMEOUT_S = 10.0
KILL_TIMEOUT_S = 10.0


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


def _ps_rows() -> Optional[list[tuple[int, int, str]]]:
    """One read of the process table as (pid, ppid, stat), or None when it
    could not be read or parsed. None is never an empty table."""
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | ForEach-Object "
            '{ "$($_.ProcessId) $($_.ParentProcessId) R" }',
        ]
    else:
        command = ["ps", "-A", "-o", "pid=,ppid=,stat="]
    try:
        out = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 3:
            return None
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            return None
    # The table always holds at least this process; an empty one was not read.
    return rows if any(row[0] == os.getpid() for row in rows) else None


def _census() -> Optional[list[tuple[int, int, str]]]:
    for _ in range(CENSUS_ATTEMPTS):
        rows = _ps_rows()
        if rows is not None:
            return rows
    return None


def _tree(pid: int, rows: list[tuple[int, int, str]]) -> set[int]:
    """``pid``'s live descendants in one census (not ``pid`` itself)."""
    children: dict[int, list[int]] = {}
    for child, parent, stat in rows:
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


def kill_child_tree(
    pid: int, *, timeout: float = KILL_TIMEOUT_S
) -> Literal["killed", "exited"]:
    """SIGKILL ``pid`` (a child of this process) and every process below it.

    The tree is frozen with SIGSTOP while it is listed, so no member can
    start another one unseen, then killed. Returns ``"killed"`` only once
    ``pid`` is reaped and a successful read of the process table shows none
    of its descendants still running. A process table that cannot be read,
    before or after the kill, raises :class:`DescendantsUnknown`; anything
    still running at the bound raises :class:`ProcessNotReaped`.
    ``"exited"`` means ``pid`` had already exited and been reaped, and was
    not signalled, so a reused process id is never hit.
    """
    deadline = time.monotonic() + timeout
    windows = os.name == "nt"
    if not windows and _reap_child(pid):
        return "exited"
    known: set[int] = {pid}
    if not windows:
        _signal(pid, signal.SIGSTOP)
    unknown: Optional[str] = None
    while True:
        rows = _census()
        if rows is None:
            unknown = "the process table could not be read before the kill"
            break
        new = _tree(pid, rows) - known
        if not new:
            break
        known |= new
        if not windows:
            for target in new:
                _signal(target, signal.SIGSTOP)
        if time.monotonic() > deadline:
            unknown = f"process {pid} kept starting processes while being stopped"
            break
    if windows:
        for target in sorted(known):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(target)], capture_output=True
            )
    else:
        for target in [pid, *sorted(known - {pid})]:
            _signal(target, signal.SIGKILL)
    descendants = known - {pid}
    remaining: Optional[set[int]] = None
    while True:
        rows = _census()
        if windows:
            reaped = rows is not None and pid not in {r[0] for r in rows}
        else:
            reaped = _reap_child(pid)
        if reaped and unknown is not None:
            raise DescendantsUnknown(
                f"process {pid} was killed and reaped, but {unknown}, so its "
                "descendants are unknown"
            )
        if reaped and rows is not None:
            running = {p for p, _pp, stat in rows if not stat.startswith("Z")}
            remaining = descendants & running
            if not remaining:
                return "killed"
        if time.monotonic() > deadline:
            if not reaped:
                raise ProcessNotReaped(
                    f"process {pid} still running {timeout:.0f} s after SIGKILL"
                )
            if rows is None:
                raise DescendantsUnknown(
                    f"process {pid} was reaped, but the process table could not "
                    "be read after the kill"
                )
            raise ProcessNotReaped(
                f"descendants {sorted(remaining or ())} of {pid} still running "
                f"{timeout:.0f} s after SIGKILL"
            )
        time.sleep(0.05)
