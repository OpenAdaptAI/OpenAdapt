"""Run one command under a child subreaper and prove its whole tree gone.

    python -I _supervisor.py REPORT_DIR NONCE HANDOFF_FD -- COMMAND [ARGS...]

First, on Linux, it opens a pidfd for its own process and sends it, with
``{nonce, pid, start}``, over the inherited socket ``HANDOFF_FD`` (``-`` for
none) to the parent: a handle the process made for itself, so it can never
refer to a process that later reused the pid. The parent signals it only
through that handle.

Run as a file with ``-I`` (standard library only, nothing from the package
is imported). On Linux the supervisor marks itself a child subreaper
(``prctl(PR_SET_CHILD_SUBREAPER)``), so every descendant of the command that
is orphaned, including one that left with ``setsid``, is reparented to it
rather than to init. The command runs in its own process group and shares
the supervisor's stdin, stdout and stderr.

When the command exits, or the supervisor gets SIGTERM, it tears down: it
SIGKILLs the command's group and each of its own remaining children, reaps
with ``waitpid(-1, WNOHANG)`` and repeats until ``ECHILD``: it has no child
left, alive or zombie, so nothing the command started survives. It then
writes ``REPORT_DIR/NONCE.json`` holding the nonce, its own pid and start
identity, and the verdict, and exits:

* ``proven``: a subreaper reached ECHILD.
* ``unproven``: no subreaper (not Linux, or prctl refused), so an orphan may
  have gone to init and ECHILD proves nothing about it.
* ``survivors``: children were still there at the bound.

Reuse conditions. A process id, or a group id, can be reused only once
nothing holds it: a group id stays reserved while any member exists, a
zombie included. The supervisor therefore signals the command's group only
while the command is unreaped (alive, or a zombie it has not collected), and
signals other processes only while they are its own unreaped children
(listed under it, and only it reaps them). On Linux it watches for the
command's exit with ``waitid(WNOWAIT)``, which leaves the zombie in place.
Where that is unavailable (macOS) it reaps the command to notice the exit
and then no longer signals that group; that platform is ``unproven`` anyway.
The command itself is signalled through a pidfd where Linux provides one.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Optional

PR_SET_CHILD_SUBREAPER = 36
#: How long the teardown may take before it reports survivors.
TEARDOWN_S = 10.0


def start_identity(pid: int) -> Optional[str]:
    """When ``pid`` started, as the kernel records it, or None if unknown.
    With the pid, it tells this process apart from a later one that reuses
    the id."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
            return f"linux:{fields[19]}"  # field 22, starttime
        except (OSError, IndexError):
            return None
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"ps:{out}" if out else None


def handoff(fd: str, nonce: str) -> None:
    """Send this process's own pidfd and identity to the parent over the
    inherited socket ``fd``; nothing when there is none or it fails (the
    parent then treats the launch as unbound and never signals it)."""
    if fd == "-":
        return
    try:
        sock = socket.socket(fileno=int(fd))
    except (OSError, ValueError):
        return
    try:
        own = getattr(os, "pidfd_open")(os.getpid())
        try:
            message = json.dumps(
                {
                    "nonce": nonce,
                    "pid": os.getpid(),
                    "start": start_identity(os.getpid()),
                }
            ).encode()
            getattr(socket, "send_fds")(sock, [message], [own])
        finally:
            os.close(own)
    except (OSError, AttributeError):
        pass
    finally:
        sock.close()


def _subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _own_children() -> list[int]:
    """This process's children, from /proc (Linux); [] where unreadable.
    Only a kill list: the proof is ECHILD."""
    me, found = os.getpid(), []
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 1 and fields[1] == str(me):
            found.append(int(name))
    return found


def _pidfd(pid: int) -> Optional[int]:
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        return None
    try:
        return int(opener(pid))
    except OSError:
        return None


def _exited_unreaped(pid: int, pidfd: Optional[int]) -> Optional[bool]:
    """Whether the command (our child) has exited, leaving it unreaped; None
    where the platform cannot look without reaping."""
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        return None
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    try:
        if pidfd is not None:
            return waitid(getattr(os, "P_PIDFD"), pidfd, flags) is not None
        return waitid(os.P_PID, pid, flags) is not None
    except ChildProcessError:
        return True


def _teardown(
    command: int, pidfd: Optional[int], group_owned: bool, subreaper: bool
) -> str:
    deadline = time.monotonic() + TEARDOWN_S
    if pidfd is not None:
        try:
            getattr(signal, "pidfd_send_signal")(pidfd, signal.SIGKILL)
        except OSError:
            pass  # already exited
    if group_owned:
        # The command is unreaped, so its group id is still ours.
        try:
            os.killpg(command, signal.SIGKILL)
        except OSError:
            pass  # empty, or only zombies left
    while True:
        for pid in _own_children():
            try:
                os.kill(pid, signal.SIGKILL)  # our unreaped child: still ours
            except OSError:
                pass
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return "proven" if subreaper else "unproven"
            if pid == 0:
                break  # children remain; kill again
        if time.monotonic() > deadline:
            return "survivors"
        time.sleep(0.02)


def main(argv: list[str]) -> int:
    if len(argv) < 5 or argv[3] != "--":
        print(
            "usage: _supervisor.py REPORT_DIR NONCE HANDOFF_FD -- COMMAND...",
            file=sys.stderr,
        )
        return 2
    report_dir, nonce, fd, command = argv[0], argv[1], argv[2], argv[4:]
    handoff(fd, nonce)
    identity = start_identity(os.getpid())
    subreaper = _subreaper()
    stop = {"requested": False}

    def on_term(_signum: int, _frame: object) -> None:
        stop["requested"] = True

    signal.signal(signal.SIGTERM, on_term)
    child = subprocess.Popen(command, start_new_session=True)
    # Only this process reaps its children, so the pid is the command's until
    # the teardown below; the pidfd makes that hold for signals too.
    pidfd = _pidfd(child.pid)
    group_owned = True
    while not stop["requested"]:
        exited = _exited_unreaped(child.pid, pidfd)
        if exited is None:
            # No way to look without reaping: reap, and stop owning the group.
            if child.poll() is not None:
                group_owned = False
                break
        elif exited:
            break
        time.sleep(0.1)
    verdict = _teardown(child.pid, pidfd, group_owned, subreaper)
    if pidfd is not None:
        os.close(pidfd)
    if child.returncode is None:
        child.returncode = -9  # reaped by the teardown; keep Popen from waiting
    path = os.path.join(report_dir, f"{nonce}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(
            {
                "nonce": nonce,
                "pid": os.getpid(),
                "start": identity,
                "verdict": verdict,
                "subreaper": subreaper,
            },
            handle,
        )
    os.replace(path + ".tmp", path)
    return 0 if verdict == "proven" else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
