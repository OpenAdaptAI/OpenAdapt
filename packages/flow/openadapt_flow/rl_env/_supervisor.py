"""Run one command under a child subreaper and prove its whole tree gone.

    python -I _supervisor.py REPORT_DIR -- COMMAND [ARGS...]

Run as a file with ``-I`` (standard library only, nothing from the package
is imported). On Linux the supervisor marks itself a child subreaper
(``prctl(PR_SET_CHILD_SUBREAPER)``), so every descendant of the command that
is orphaned, including one that left with ``setsid``, is reparented to it
rather than to init. The command runs in its own process group and shares
the supervisor's stdin, stdout and stderr.

When the command exits, or the supervisor gets SIGTERM, it SIGKILLs the
command's group and each of its own remaining children, reaps with
``waitpid(-1, WNOHANG)`` and repeats until ``ECHILD``: it has no child
left, alive or zombie, so nothing the command started survives. It then
writes its verdict to ``REPORT_DIR/<its pid>`` and exits:

* ``proven``: a subreaper reached ECHILD.
* ``unproven``: no subreaper (not Linux, or prctl refused), so an orphan may
  have gone to init and ECHILD proves nothing about it.
* ``survivors``: children were still there at the bound.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
#: How long the teardown may take before it reports survivors.
TEARDOWN_S = 10.0


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
            with open(f"/proc/{name}/stat") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 1 and fields[1] == str(me):
            found.append(int(name))
    return found


def _teardown(pgid: int, subreaper: bool) -> str:
    deadline = time.monotonic() + TEARDOWN_S
    while True:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass  # empty, or only zombies left
        for pid in _own_children():
            try:
                os.kill(pid, signal.SIGKILL)
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
    if len(argv) < 3 or argv[1] != "--":
        print("usage: _supervisor.py REPORT_DIR -- COMMAND...", file=sys.stderr)
        return 2
    report_dir, command = argv[0], argv[2:]
    subreaper = _subreaper()
    stop = {"requested": False}

    def on_term(_signum: int, _frame: object) -> None:
        stop["requested"] = True

    signal.signal(signal.SIGTERM, on_term)
    child = subprocess.Popen(command, start_new_session=True)
    code = None
    while code is None and not stop["requested"]:
        try:
            code = child.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
    verdict = _teardown(child.pid, subreaper)
    path = os.path.join(report_dir, str(os.getpid()))
    with open(path + ".tmp", "w") as handle:
        json.dump({"verdict": verdict, "subreaper": subreaper}, handle)
    os.replace(path + ".tmp", path)
    return 0 if verdict == "proven" else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
