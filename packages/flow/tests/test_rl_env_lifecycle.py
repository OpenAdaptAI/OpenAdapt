"""Environment server processes are confirmed dead and reaped, never assumed."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openadapt_flow.rl_env import lifecycle

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")

#: A "server" that ignores SIGTERM and starts a child that ignores it too.
STUBBORN = """
import signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print(flush=True); time.sleep(600)"], stdout=subprocess.PIPE)
child.stdout.readline()
print(child.pid, flush=True)
time.sleep(600)
"""

COOPERATIVE = "import time; print(flush=True); time.sleep(600)"


def _start(code: str) -> tuple[subprocess.Popen[bytes], str]:
    proc = lifecycle.spawn(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    assert proc.stdout is not None
    return proc, proc.stdout.readline().decode().strip()


def _running(pid: int) -> bool:
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(out) and not out.startswith("Z")


def test_a_server_that_ignores_sigterm_is_killed_and_reaped() -> None:
    proc, grandchild = _start(STUBBORN)
    grandchild_pid = int(grandchild)
    assert os.getpgid(proc.pid) == proc.pid  # its own process group
    started = time.monotonic()
    stopped = lifecycle.stop(proc, term_timeout=1, kill_timeout=10)
    assert time.monotonic() - started < 10
    assert stopped.how == "killed"
    assert stopped.returncode == -signal.SIGKILL
    # Checked on the reaped Popen object, not with os.kill(pid, 0).
    assert proc.poll() is not None and proc.returncode == -signal.SIGKILL
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)
    # Its child, which also ignored SIGTERM, went with the group.
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)
    assert not _running(grandchild_pid)


def test_a_cooperative_server_is_reported_terminated() -> None:
    proc, _ = _start(COOPERATIVE)
    stopped = lifecycle.stop(proc, term_timeout=10)
    assert (stopped.how, stopped.returncode) == ("terminated", -signal.SIGTERM)
    assert proc.poll() is not None


def test_an_already_exited_server_is_reaped_and_not_signalled() -> None:
    proc = lifecycle.spawn([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    stopped = lifecycle.stop(proc)
    assert (stopped.how, stopped.returncode) == ("exited", 0)


def test_a_server_that_cannot_be_reaped_raises_instead_of_reporting_stopped(
    monkeypatch,
) -> None:
    proc, _ = _start(STUBBORN)
    real_wait = proc.wait

    def never(timeout=None):
        raise subprocess.TimeoutExpired(proc.args, timeout)

    monkeypatch.setattr(proc, "wait", never)
    try:
        with pytest.raises(lifecycle.ProcessNotReaped, match="SIGKILL"):
            lifecycle.stop(proc, term_timeout=0.1, kill_timeout=0.1)
    finally:
        monkeypatch.undo()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:  # already empty (macOS says EPERM for a zombie group)
            pass
        real_wait(timeout=30)


def test_kill_child_tree_reaps_a_child_that_ignores_sigterm_and_its_children() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", STUBBORN],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline())
    assert lifecycle.kill_child_tree(proc.pid, timeout=10) == "killed"
    # Reaped by kill_child_tree itself: nothing left for waitpid to find.
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)
    assert not _running(grandchild)
    proc.returncode = -signal.SIGKILL  # already reaped; keep Popen from waiting


def test_kill_child_tree_does_not_signal_an_already_reaped_pid(monkeypatch) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    sent: list[int] = []
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: sent.append(pid))
    assert lifecycle.kill_child_tree(proc.pid) == "exited"
    assert sent == []  # the pid may already belong to someone else


def test_kill_child_tree_raises_when_the_child_cannot_be_reaped(monkeypatch) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: None)
        with pytest.raises(lifecycle.ProcessNotReaped, match=str(proc.pid)):
            lifecycle.kill_child_tree(proc.pid, timeout=0.3)
    finally:
        monkeypatch.undo()
        proc.kill()
        proc.wait(timeout=30)


# -- the browser environment's stuck-worker teardown --------------------------

MOCKMED = Path(__file__).resolve().parents[1] / "environments" / "openenv" / "mockmed"


def _environment():
    pytest.importorskip("openenv")
    from openadapt_flow.rl_env.environment import OpenAdaptWebEnvironment

    return OpenAdaptWebEnvironment(MOCKMED)


def test_an_abandoned_browser_that_cannot_be_reaped_is_not_reported_closed(
    monkeypatch,
) -> None:
    env = _environment()
    from openadapt_flow.rl_env import environment as E

    def stuck(pid: int, **_: object) -> str:
        raise lifecycle.ProcessNotReaped(f"process {pid} still running")

    monkeypatch.setattr(E, "kill_child_tree", stuck)
    env._pw, env._driver_pid = object(), 424242
    with pytest.raises(lifecycle.ProcessNotReaped):
        env._abandon_worker()
    # The episode is torn down, but nothing claims the browser stopped.
    assert env._teardown is None and env._driver_pid is None
    assert env.state.done is True and env._pw is None


def test_an_abandoned_browser_with_an_unknown_driver_pid_raises() -> None:
    env = _environment()
    env._pw, env._driver_pid = object(), None
    with pytest.raises(lifecycle.ProcessNotReaped, match="unknown"):
        env._abandon_worker()


def test_an_abandoned_worker_without_a_browser_has_nothing_to_kill() -> None:
    env = _environment()
    env._abandon_worker()
    assert env._teardown is None and env.state.done is True


def test_an_abandoned_browser_reports_how_its_driver_was_stopped(monkeypatch) -> None:
    env = _environment()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    env._pw, env._driver_pid = object(), child.pid
    env._abandon_worker()
    assert env._teardown == {"driver_pid": child.pid, "driver": "killed"}
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
    child.returncode = -signal.SIGKILL
