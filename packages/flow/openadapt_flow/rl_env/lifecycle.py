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
        return True
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


def _ps_rows() -> list[tuple[int, int, str]]:
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,stat="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _reap_child(pid: int) -> bool:
    """Reap ``pid`` if it is this process's exited child. True once it is
    reaped (by this call or by someone else, such as asyncio's watcher)."""
    try:
        done, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True  # not an unreaped child of ours any more
    return done == pid


def kill_child_tree(
    pid: int, *, timeout: float = KILL_TIMEOUT_S
) -> Literal["killed", "exited"]:
    """SIGKILL ``pid`` (a child of this process) and every process below it.

    Returns once ``pid`` is reaped and its descendants are gone (exited or
    zombies awaiting their new parent). Raises :class:`ProcessNotReaped`
    otherwise. ``"exited"`` means ``pid`` had already exited and was not
    signalled, so a reused process id is never hit.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        deadline = time.monotonic() + timeout
        while True:
            listed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
            ).stdout
            if str(pid) not in listed:
                return "killed"
            if time.monotonic() > deadline:
                raise ProcessNotReaped(f"process {pid} is still running")
            time.sleep(0.05)
    if _reap_child(pid):
        return "exited"
    rows = _ps_rows()
    children: dict[int, list[int]] = {}
    for child, parent, _stat in rows:
        children.setdefault(parent, []).append(child)
    descendants: list[int] = []
    todo = list(children.get(pid, []))
    while todo:
        current = todo.pop()
        descendants.append(current)
        todo.extend(children.get(current, []))
    for target in [pid, *descendants]:
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + timeout
    reaped = False
    remaining: Optional[list[int]] = None
    while True:
        reaped = reaped or _reap_child(pid)
        if reaped:
            running = {p for p, _pp, stat in _ps_rows() if not stat.startswith("Z")}
            remaining = [p for p in descendants if p in running]
            if not remaining:
                return "killed"
        if time.monotonic() > deadline:
            what = f"descendants {remaining}" if reaped else f"process {pid}"
            raise ProcessNotReaped(
                f"{what} still running {timeout:.0f} s after SIGKILL"
            )
        time.sleep(0.05)
