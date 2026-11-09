"""Environment server processes are confirmed dead and reaped, never assumed."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.rl_env import lifecycle

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")

LINUX = sys.platform.startswith("linux")
#: Without the subreaper supervisor, no teardown is proven complete.
UNPROVEN_KILL = lifecycle.Teardown("killed", proven=False)

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
    proc = lifecycle.spawn(
        [sys.executable, "-c", STUBBORN],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline())
    assert lifecycle.kill_child_tree(proc.pid, timeout=10) == UNPROVEN_KILL
    # Reaped by kill_child_tree itself: nothing left for waitpid to find.
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)
    assert not _running(grandchild)
    proc.returncode = -signal.SIGKILL  # already reaped; keep Popen from waiting


def test_kill_child_tree_does_not_signal_an_already_reaped_pid(monkeypatch) -> None:
    proc = lifecycle.spawn([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    sent: list[int] = []
    real_killpg = os.killpg

    def killpg(pgid: int, sig: int) -> None:
        if sig != 0:
            sent.append(pgid)
        else:
            real_killpg(pgid, sig)

    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(lifecycle.os, "killpg", killpg)
    assert lifecycle.kill_child_tree(proc.pid) == lifecycle.Teardown(
        "exited", proven=False
    )
    assert sent == []  # the pid may already belong to someone else


def test_kill_child_tree_raises_when_the_child_cannot_be_reaped(monkeypatch) -> None:
    proc = lifecycle.spawn([sys.executable, "-c", "import time; time.sleep(600)"])
    real_killpg = os.killpg
    try:
        monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(
            lifecycle.os,
            "killpg",
            lambda pgid, sig: real_killpg(pgid, sig) if sig == 0 else None,
        )
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
    child = lifecycle.spawn([sys.executable, "-c", "import time; time.sleep(600)"])
    env._pw, env._driver_pid = object(), child.pid
    env._abandon_worker()
    assert env._teardown == {
        "driver_pid": child.pid,
        "driver": "killed",
        "proven": False,
    }
    assert env._unproven is not None  # later episodes are INDETERMINATE
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
    child.returncode = -signal.SIGKILL


# -- an unreadable process table is unknown, never an empty tree -------------


def _failing_census(monkeypatch, fail: "set[int]") -> list[int]:
    """Make the listed (1-based) ``ps`` census calls fail; count them all."""
    calls: list[int] = []
    real_run = lifecycle.subprocess.run

    def run(args, *a, **k):
        if list(args[:1]) == ["ps"]:
            calls.append(len(calls) + 1)
            if calls[-1] in fail:
                raise subprocess.CalledProcessError(1, args)
        return real_run(args, *a, **k)

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    return calls


def _live_child() -> subprocess.Popen[bytes]:
    proc = lifecycle.spawn(
        [sys.executable, "-c", "import time; print(flush=True); time.sleep(600)"],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    proc.stdout.readline()
    assert proc.poll() is None
    return proc


def _cleanup(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.kill(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=30)
    except ChildProcessError:
        pass
    proc.returncode = proc.returncode or -signal.SIGKILL


def test_a_census_that_fails_twice_refuses_to_report_killed(monkeypatch) -> None:
    proc = _live_child()
    calls = _failing_census(monkeypatch, fail={1, 2})
    try:
        with pytest.raises(lifecycle.DescendantsUnknown, match="unknown"):
            lifecycle.kill_child_tree(proc.pid, timeout=5)
        assert calls[:2] == [1, 2]
        # The child itself was still killed and reaped; only the verdict is
        # withheld.
        with pytest.raises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)
    finally:
        monkeypatch.undo()
        _cleanup(proc)


def test_a_census_that_fails_once_then_confirms_empty_reports_killed(
    monkeypatch,
) -> None:
    proc = _live_child()
    calls = _failing_census(monkeypatch, fail={1})
    try:
        assert lifecycle.kill_child_tree(proc.pid, timeout=10) == UNPROVEN_KILL
        assert len(calls) >= 3  # the failure, its retry and the confirmation
    finally:
        monkeypatch.undo()
        _cleanup(proc)


def test_a_census_that_fails_after_the_kill_refuses_to_report_killed(
    monkeypatch,
) -> None:
    proc = _live_child()
    # Call 1 lists the frozen (childless) tree; every later one fails.
    calls = _failing_census(monkeypatch, fail=set(range(2, 10_000)))
    try:
        with pytest.raises(lifecycle.DescendantsUnknown, match="after the kill"):
            lifecycle.kill_child_tree(proc.pid, timeout=0.5)
        assert len(calls) > 1
    finally:
        monkeypatch.undo()
        _cleanup(proc)


def test_unparseable_or_empty_census_output_is_unknown(monkeypatch) -> None:
    for out in ("", "garbage\n", "12 x R\n", "1 0 S\n"):
        monkeypatch.setattr(
            lifecycle.subprocess,
            "run",
            lambda *a, _o=out, **k: SimpleNamespace(stdout=_o),
        )
        assert lifecycle._ps_rows() is None, out


def test_a_stopped_group_that_cannot_be_inspected_is_not_reported_stopped(
    monkeypatch,
) -> None:
    proc, _ = _start(COOPERATIVE)
    real_killpg = os.killpg

    def killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            raise OSError(5, "I/O error")
        real_killpg(pgid, sig)

    monkeypatch.setattr(lifecycle.os, "killpg", killpg)
    try:
        with pytest.raises(lifecycle.DescendantsUnknown, match="inspected"):
            lifecycle.stop(proc, term_timeout=10)
        assert proc.poll() is not None  # the leader itself was reaped
    finally:
        monkeypatch.undo()


# -- the process group is the proof; a ps walk only adds to the kill list ----


def _truncated_census(monkeypatch, keep) -> None:
    """Serve a real, successful ``ps`` table with only the rows ``keep``
    accepts: what a truncated census looks like."""
    real_run = lifecycle.subprocess.run

    def run(args, *a, **k):
        result = real_run(args, *a, **k)
        if list(args[:1]) == ["ps"]:
            lines = result.stdout.splitlines()
            kept = [lines[0]] + [
                line
                for line in lines[1:]
                if line.split() and keep(int(line.split()[0]))
            ]
            result = SimpleNamespace(stdout="\n".join(kept) + "\n")
        return result

    monkeypatch.setattr(lifecycle.subprocess, "run", run)


def _stubborn_with_grandchild() -> tuple[subprocess.Popen[bytes], int]:
    proc = lifecycle.spawn(
        [sys.executable, "-c", STUBBORN],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline())
    assert _running(grandchild) and os.getpgid(grandchild) == proc.pid
    return proc, grandchild


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _running(pid):
            return True
        time.sleep(0.05)
    return False


def test_a_truncated_census_without_the_target_refuses(monkeypatch) -> None:
    proc, grandchild = _stubborn_with_grandchild()
    # Codex's control: a successful table that holds the caller only.
    _truncated_census(monkeypatch, keep=lambda pid: pid == os.getpid())
    try:
        with pytest.raises(lifecycle.DescendantsUnknown, match="incomplete"):
            lifecycle.kill_child_tree(proc.pid, timeout=5)
    finally:
        monkeypatch.undo()
    # The group was killed anyway, so the grandchild is gone too.
    assert _gone(grandchild)
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)
    proc.returncode = -signal.SIGKILL


def test_a_census_missing_a_grandchild_still_kills_it_through_the_group(
    monkeypatch,
) -> None:
    proc, grandchild = _stubborn_with_grandchild()
    _truncated_census(monkeypatch, keep=lambda pid: pid != grandchild)
    try:
        assert lifecycle.kill_child_tree(proc.pid, timeout=10) == UNPROVEN_KILL
    finally:
        monkeypatch.undo()
    # "killed" rests on the empty group, not on the census that missed it.
    assert not _running(grandchild)
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)
    proc.returncode = -signal.SIGKILL


def test_a_target_outside_its_own_group_is_killed_but_not_reported_killed() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", STUBBORN],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline())
    try:
        with pytest.raises(lifecycle.DescendantsUnknown, match="own process group"):
            lifecycle.kill_child_tree(proc.pid, timeout=10)
        with pytest.raises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)
        assert _gone(grandchild)  # the ps walk still added it to the kill list
    finally:
        proc.returncode = -signal.SIGKILL
        try:
            os.kill(grandchild, signal.SIGKILL)
        except ProcessLookupError:
            pass


#: A child whose own child leaves the group with setsid.
ESCAPING = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c",
    "import time; print(flush=True); time.sleep(600)"],
    stdout=subprocess.PIPE, start_new_session=True)
child.stdout.readline()
print(child.pid, flush=True)
time.sleep(600)
"""


def test_a_descendant_that_left_the_group_is_killed_and_its_group_checked() -> None:
    proc = lifecycle.spawn(
        [sys.executable, "-c", ESCAPING],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    escaped = int(proc.stdout.readline())
    assert os.getpgid(escaped) == escaped != proc.pid
    try:
        assert lifecycle.kill_child_tree(proc.pid, timeout=10) == UNPROVEN_KILL
        assert not _running(escaped)
        with pytest.raises(ProcessLookupError):
            os.killpg(escaped, 0)
    finally:
        proc.returncode = -signal.SIGKILL
        try:
            os.kill(escaped, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_census_without_its_header_is_incomplete(monkeypatch) -> None:
    real = lifecycle.subprocess.run(
        ["ps", "-A", "-o", "pid,ppid,pgid,stat"], capture_output=True, text=True
    ).stdout
    assert lifecycle._ps_rows() is not None
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="\n".join(real.splitlines()[1:])),
    )
    assert lifecycle._ps_rows() is None


# -- escaped (setsid) descendants: only a Linux subreaper proves them gone ----

#: A child that starts a setsid grandchild, reports its pid, then exits so the
#: grandchild is orphaned out of every group the caller knows about.
ORPHANING = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c",
    "import time; print(flush=True); time.sleep(600)"],
    stdout=subprocess.PIPE, start_new_session=True)
child.stdout.readline()
print(child.pid, flush=True)
"""


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_an_omitted_escaped_descendant_is_never_reported_proven(monkeypatch) -> None:
    # Codex's control: the target leads its own group, its grandchild left
    # with setsid, and a complete-looking census omits the grandchild.
    proc = lifecycle.spawn(
        [sys.executable, "-c", ESCAPING],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    escaped = int(proc.stdout.readline())
    _truncated_census(monkeypatch, keep=lambda pid: pid != escaped)
    try:
        result = lifecycle.kill_child_tree(proc.pid, timeout=10)
        # The root group is empty, but that proves nothing about the escapee.
        assert result == UNPROVEN_KILL
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)
    finally:
        monkeypatch.undo()
        proc.returncode = -signal.SIGKILL
        _kill_quietly(escaped)


@pytest.mark.skipif(not LINUX, reason="PR_SET_CHILD_SUBREAPER is Linux-only")
def test_the_subreaper_supervisor_reaps_an_orphaned_setsid_descendant() -> None:
    proc = lifecycle.spawn_supervised(
        [sys.executable, "-c", ORPHANING + "import time; time.sleep(600)\n"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    escaped = int(proc.stdout.readline())
    try:
        assert os.getpgid(escaped) == escaped
        result = lifecycle.stop_supervised(proc.pid, timeout=20)
        assert result == lifecycle.Teardown("killed", proven=True), (
            "PR_SET_CHILD_SUBREAPER could not be set or did not reap the escapee"
        )
        assert not _running(escaped)
    finally:
        proc.returncode = proc.returncode or -signal.SIGTERM
        _kill_quietly(escaped)


@pytest.mark.skipif(not LINUX, reason="PR_SET_CHILD_SUBREAPER is Linux-only")
def test_the_subreaper_supervisor_reaps_an_already_orphaned_escapee() -> None:
    # The intermediate exits first: the escapee is orphaned and, under the
    # subreaper, comes back to the supervisor instead of init, which kills
    # and reaps it as soon as the command has exited.
    proc = lifecycle.spawn_supervised(
        [sys.executable, "-c", ORPHANING],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    escaped = int(proc.stdout.readline())
    try:
        # The command has exited, so the supervisor tears down on its own.
        assert lifecycle.stop_supervised(proc.pid, timeout=20).proven is True
        assert not _running(escaped)
    finally:
        proc.returncode = proc.returncode or 0
        _kill_quietly(escaped)


@pytest.mark.skipif(LINUX, reason="the unproven path is the non-Linux one")
def test_without_a_subreaper_the_supervisor_reports_unproven() -> None:
    proc = lifecycle.spawn_supervised(
        [sys.executable, "-c", ORPHANING + "import time; time.sleep(600)\n"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    escaped = int(proc.stdout.readline())
    try:
        result = lifecycle.stop_supervised(proc.pid, timeout=20)
        # Killed what it could reach, but never claims the escapee is gone.
        assert result == UNPROVEN_KILL
    finally:
        proc.returncode = proc.returncode or -signal.SIGTERM
        _kill_quietly(escaped)


def test_a_supervisor_that_leaves_no_report_is_unknown() -> None:
    # Not a supervisor at all: SIGTERM ends it without a verdict.
    proc = lifecycle.spawn([sys.executable, "-c", "import time; time.sleep(600)"])
    with pytest.raises(lifecycle.DescendantsUnknown, match="reported nothing"):
        lifecycle.stop_supervised(proc.pid, timeout=10)
    proc.returncode = -signal.SIGTERM


def test_an_unproven_teardown_makes_later_episodes_indeterminate(monkeypatch) -> None:
    env = _environment()
    from openadapt_flow.rl_env.reward import Judgement

    class Confirming:
        def judge(self, claim, feasible, attempted=True):
            return Judgement("CONFIRMED", claim, feasible, 1.0, attempted)

    env._task = env.env.task("mockmed-01")
    env._judge = Confirming()
    monkeypatch.setattr(env.env.manifest.agent, "settle_s", 0.0)
    monkeypatch.setattr(env, "_observe", lambda **kw: kw)
    assert env._finish("success")["verdict"] == "CONFIRMED"
    env._unproven = "the stuck browser could not be proven gone"
    out = env._finish("success")
    assert (out["verdict"], out["reward"]) == ("INDETERMINATE", 0.0)
    assert "unproven" in out["extra"]["judgement"]["error"]
