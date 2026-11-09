"""Environment server processes are confirmed dead and reaped, never assumed."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
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
    proc, launch = lifecycle.spawn_supervised(
        [sys.executable, "-c", ORPHANING + "import time; time.sleep(600)\n"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    escaped = int(proc.stdout.readline())
    try:
        assert os.getpgid(escaped) == escaped
        result = lifecycle.stop_supervised(launch, proc=proc, timeout=20)
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
    proc, launch = lifecycle.spawn_supervised(
        [sys.executable, "-c", ORPHANING],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    escaped = int(proc.stdout.readline())
    try:
        # The command has exited, so the supervisor tears down on its own.
        assert lifecycle.stop_supervised(launch, proc=proc, timeout=20).proven is True
        assert not _running(escaped)
    finally:
        proc.returncode = proc.returncode or 0
        _kill_quietly(escaped)


@pytest.mark.skipif(LINUX, reason="the unproven path is the non-Linux one")
def test_without_a_subreaper_the_supervisor_reports_unproven() -> None:
    proc, launch = lifecycle.spawn_supervised(
        [sys.executable, "-c", ORPHANING + "import time; time.sleep(600)\n"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    escaped = int(proc.stdout.readline())
    try:
        result = lifecycle.stop_supervised(launch, proc=proc, timeout=20)
        # Killed what it could reach, but never claims the escapee is gone.
        assert result == UNPROVEN_KILL
    finally:
        proc.returncode = proc.returncode or -signal.SIGTERM
        _kill_quietly(escaped)


def test_a_supervisor_that_leaves_no_report_is_unknown() -> None:
    # Not a supervisor at all: SIGTERM ends it without a verdict.
    proc = lifecycle.spawn([sys.executable, "-c", "import time; time.sleep(600)"])
    proc.kill()
    proc.wait(timeout=30)
    proc, launch = _fake_supervisor()
    with pytest.raises(lifecycle.DescendantsUnknown, match="reported nothing"):
        lifecycle.stop_supervised(launch, proc=proc, timeout=10)


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


# -- a malformed supervisor report, or any teardown failure, is never proof ---


#: Stands in for a supervisor: does (or skips, or garbles) the handoff with
#: the real ``handoff`` and then sleeps; its report is written by the test.
FAKE_SUPERVISOR = """
import runpy, sys, time
supervisor = runpy.run_path(sys.argv[1])
mode, nonce, fd = sys.argv[2], sys.argv[3], sys.argv[4]
if mode == "ok":
    supervisor["handoff"](fd, nonce)
elif mode == "wrong-nonce":
    supervisor["handoff"](fd, "0" * 32)
# "silent": keep the socket open and never send
time.sleep(600)
"""


def _fake_supervisor(
    mode: str = "ok",
) -> tuple[subprocess.Popen[bytes], lifecycle.SupervisedLaunch]:
    return lifecycle.launch_supervised(
        lambda nonce, fd: [
            sys.executable,
            "-I",
            "-c",
            FAKE_SUPERVISOR,
            lifecycle.SUPERVISOR,
            mode,
            nonce,
            "-" if fd is None else str(fd),
        ]
    )


def _write(path: str, body: object) -> None:
    with open(path, "w") as handle:
        handle.write(body if isinstance(body, str) else json.dumps(body))


@pytest.mark.parametrize(
    "report",
    [
        "[]",
        "null",
        "42",
        '"proven"',
        "{}",
        '{"verdict": "maybe"}',
        '{"verdict": ["proven"]}',
        '{"verdict": null}',
        "not json",
    ],
)
def test_a_malformed_supervisor_report_is_unknown(report: str) -> None:
    proc, launch = _fake_supervisor()
    _write(launch.report, report)
    with pytest.raises(lifecycle.DescendantsUnknown, match="reported"):
        lifecycle.stop_supervised(launch, proc=proc, timeout=10)
    assert not os.path.exists(launch.report)


def _receipt(launch: lifecycle.SupervisedLaunch, **change: object) -> dict:
    body = {
        "nonce": launch.nonce,
        "pid": launch.pid,
        "start": launch.start,
        "verdict": "proven",
        "subreaper": True,
    }
    body.update(change)
    return body


def test_a_well_formed_proven_report_is_read_as_proven() -> None:
    proc, launch = _fake_supervisor()
    assert launch.start is not None
    _write(launch.report, _receipt(launch))
    # Proven only where the launch has a pidfd (Linux); elsewhere a signal
    # can race a concurrent reaper, so the teardown is never proven.
    assert lifecycle.stop_supervised(launch, proc=proc, timeout=10) == (
        lifecycle.Teardown("killed", proven=LINUX)
    )
    assert not os.path.exists(launch.report)  # consumed once
    assert launch.pidfd is None  # closed after the final reap


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "0" * 32},  # another launch's receipt
        {"pid": 1},  # written by another process
        {"start": "linux:1"},  # an earlier process that had this pid
        {"start": None},
    ],
)
def test_a_stale_or_foreign_proven_receipt_is_not_accepted(change: dict) -> None:
    proc, launch = _fake_supervisor()
    _write(launch.report, _receipt(launch, **change))
    with pytest.raises(lifecycle.DescendantsUnknown):
        lifecycle.stop_supervised(launch, proc=proc, timeout=10)


def test_a_receipt_left_under_the_pid_is_never_read() -> None:
    proc, launch = _fake_supervisor()
    # The old naming, as a natural exit could have left it: ignored.
    _write(os.path.join(lifecycle._report_dir(), str(proc.pid)), _receipt(launch))
    _write(os.path.join(lifecycle._report_dir(), f"{proc.pid}.json"), _receipt(launch))
    with pytest.raises(lifecycle.DescendantsUnknown):
        lifecycle.stop_supervised(launch, proc=proc, timeout=10)


def test_a_launch_whose_start_is_unknown_is_never_proven() -> None:
    proc, launch = _fake_supervisor()
    blind = lifecycle.SupervisedLaunch(pid=launch.pid, nonce=launch.nonce, start=None)
    _write(blind.report, _receipt(blind))
    if LINUX:
        # Unbound on Linux: never signalled, never proven.
        assert lifecycle.stop_supervised(blind, proc=proc) == lifecycle.Teardown(
            "unsignalled", proven=False
        )
        proc.kill()
        proc.wait(timeout=30)
        launch.close()
    else:
        with pytest.raises(lifecycle.DescendantsUnknown):
            lifecycle.stop_supervised(blind, proc=proc, timeout=10)


def test_a_reaped_supervisor_is_never_signalled(monkeypatch) -> None:
    proc, launch = _fake_supervisor()
    proc.kill()
    proc.wait(timeout=30)  # reaped: its pid may now belong to anyone
    sent: list[tuple[str, int]] = []
    monkeypatch.setattr(lifecycle.os, "kill", lambda p, s: sent.append(("kill", p)))
    monkeypatch.setattr(lifecycle.os, "killpg", lambda p, s: sent.append(("killpg", p)))
    with pytest.raises(lifecycle.DescendantsUnknown):
        lifecycle.stop_supervised(launch, proc=proc, timeout=5)
    assert sent == []


class _Confirming:
    def judge(self, claim, feasible, attempted=True):
        from openadapt_flow.rl_env.reward import Judgement

        return Judgement("CONFIRMED", claim, feasible, 1.0, attempted)

    def close(self) -> None:
        pass


def _judged(env, monkeypatch) -> dict:
    env._task = env.env.task("mockmed-01")
    env._judge = _Confirming()
    monkeypatch.setattr(env.env.manifest.agent, "settle_s", 0.0)
    monkeypatch.setattr(env, "_observe", lambda **kw: kw)
    return env._finish("success")


def test_any_teardown_failure_leaves_later_episodes_indeterminate(
    monkeypatch,
) -> None:
    env = _environment()
    from openadapt_flow.rl_env import environment as E

    def broken(pid: int, **_: object) -> lifecycle.Teardown:
        raise RuntimeError("kill failed in an unexpected way")

    monkeypatch.setattr(E, "kill_child_tree", broken)
    env._pw, env._driver_pid = object(), 424242
    with pytest.raises(RuntimeError, match="unexpected"):
        env._abandon_worker()
    assert env._unproven is not None and env._teardown is None
    out = _judged(env, monkeypatch)
    assert (out["verdict"], out["reward"]) == ("INDETERMINATE", 0.0)


def test_a_later_proven_teardown_never_lifts_the_taint(monkeypatch) -> None:
    env = _environment()
    from openadapt_flow.rl_env import environment as E

    monkeypatch.setattr(
        E, "kill_child_tree", lambda pid, **_: lifecycle.Teardown("killed", True)
    )
    env._pw, env._driver_pid = object(), 424242
    env._abandon_worker()
    # Proven, and still tainted: the abandoned worker may have started more
    # than the driver this code saw.
    assert env._teardown == {"driver_pid": 424242, "driver": "killed", "proven": True}
    assert env._unproven is not None
    first = env._unproven
    env._pw, env._driver_pid = object(), 424243
    env._abandon_worker()
    assert env._unproven == first
    out = _judged(env, monkeypatch)
    assert (out["verdict"], out["reward"]) == ("INDETERMINATE", 0.0)


def test_a_timeout_inside_playwright_start_taints_later_episodes(monkeypatch) -> None:
    env = _environment()
    from openadapt_flow.rl_env import environment as E

    started = threading.Event()

    def stuck_start(*_a, **_k):
        # Like a timeout while Playwright.start has spawned its driver but not
        # returned: neither _pw nor the driver pid is visible yet.
        started.set()
        time.sleep(2)

    monkeypatch.setattr(E, "RESET_TIMEOUT_S", 0.3)
    monkeypatch.setattr(env, "_reset", stuck_start)
    with pytest.raises(RuntimeError, match="reset took longer"):
        env.reset(task_id="mockmed-01")
    assert started.is_set()
    assert env._pw is None and env._driver_pid is None and env._teardown is None
    assert env._unproven is not None
    monkeypatch.undo()
    out = _judged(env, monkeypatch)
    assert (out["verdict"], out["reward"]) == ("INDETERMINATE", 0.0)


# -- signals go through a pidfd: a reaped process is ESRCH, never another pid --


@pytest.mark.skipif(not LINUX, reason="pidfd is Linux-only")
def test_a_pidfd_signal_after_another_reaper_collected_it_is_esrch(
    monkeypatch,
) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    pidfd = os.pidfd_open(proc.pid)
    try:
        # Another reaper (as asyncio's watcher thread would) collects it.
        def reap() -> None:
            os.kill(proc.pid, signal.SIGKILL)
            os.waitpid(proc.pid, 0)

        helper = threading.Thread(target=reap)
        helper.start()
        helper.join(timeout=30)
        proc.returncode = -signal.SIGKILL
        sent: list[int] = []
        monkeypatch.setattr(lifecycle.os, "kill", lambda p, s: sent.append(p))
        monkeypatch.setattr(lifecycle.os, "killpg", lambda p, s: sent.append(p))
        assert lifecycle._pidfd_signal(pidfd, signal.SIGTERM) is False
        assert lifecycle._pidfd_reaped(pidfd) is True
        assert sent == []  # nothing went out by pid
    finally:
        os.close(pidfd)


@pytest.mark.skipif(not LINUX, reason="pidfd is Linux-only")
def test_stop_supervised_after_a_concurrent_reap_signals_no_pid(monkeypatch) -> None:
    proc, launch = _fake_supervisor()
    assert launch.pidfd is not None
    _write(launch.report, _receipt(launch))
    helper = threading.Thread(
        target=lambda: (os.kill(proc.pid, signal.SIGKILL), os.waitpid(proc.pid, 0))
    )
    helper.start()
    helper.join(timeout=30)
    proc.returncode = -signal.SIGKILL
    sent: list[int] = []
    monkeypatch.setattr(lifecycle.os, "kill", lambda p, s: sent.append(p))
    monkeypatch.setattr(lifecycle.os, "killpg", lambda p, s: sent.append(p))
    real_pidfd_signal = lifecycle._pidfd_signal
    via_pidfd: list[bool] = []

    def recording(fd: int, sig: int) -> bool:
        via_pidfd.append(real_pidfd_signal(fd, sig))
        return via_pidfd[-1]

    monkeypatch.setattr(lifecycle, "_pidfd_signal", recording)
    # Reaped already, so nothing is signalled at all; the receipt still reads.
    assert lifecycle.stop_supervised(launch, timeout=5).how == "killed"
    assert sent == [] and via_pidfd == []
    assert launch.pidfd is None


@pytest.mark.skipif(LINUX, reason="the no-pidfd path is the non-Linux one")
def test_without_a_pidfd_a_proven_receipt_is_still_unproven() -> None:
    proc, launch = _fake_supervisor()
    assert launch.pidfd is None
    _write(launch.report, _receipt(launch))
    assert lifecycle.stop_supervised(launch, proc=proc, timeout=10) == UNPROVEN_KILL


# -- Linux: a launch is signalled only through the pidfd its supervisor sent --


def _no_signals(monkeypatch) -> list[tuple[str, int]]:
    sent: list[tuple[str, int]] = []
    monkeypatch.setattr(lifecycle.os, "kill", lambda p, s: sent.append(("kill", p)))
    monkeypatch.setattr(lifecycle.os, "killpg", lambda p, s: sent.append(("killpg", p)))
    monkeypatch.setattr(
        lifecycle, "_pidfd_signal", lambda fd, s: sent.append(("pidfd", fd)) or True
    )
    return sent


@pytest.mark.skipif(not LINUX, reason="pidfd handoff is Linux-only")
def test_the_handoff_binds_the_launch_and_teardown_is_proven() -> None:
    proc, launch = lifecycle.spawn_supervised(
        [sys.executable, "-c", "import time; time.sleep(600)"]
    )
    assert launch.bound and launch.pidfd is not None
    assert lifecycle._pidfd_target(launch.pidfd) == proc.pid
    assert launch.start is not None
    assert lifecycle.stop_supervised(launch, proc=proc, timeout=20) == (
        lifecycle.Teardown("killed", proven=True)
    )
    assert launch.pidfd is None


@pytest.mark.skipif(not LINUX, reason="pidfd handoff is Linux-only")
@pytest.mark.parametrize("mode", ["silent", "wrong-nonce"])
def test_a_launch_without_a_valid_handoff_is_never_signalled(
    mode: str, monkeypatch
) -> None:
    monkeypatch.setattr(lifecycle, "HANDOFF_TIMEOUT_S", 0.5)
    proc, launch = _fake_supervisor(mode)
    try:
        assert launch.bound is False and launch.pidfd is None
        sent = _no_signals(monkeypatch)
        assert lifecycle.stop_supervised(launch, proc=proc, timeout=5) == (
            lifecycle.Teardown("unsignalled", proven=False)
        )
        assert sent == [] and proc.poll() is None  # untouched, by any route
    finally:
        monkeypatch.undo()
        proc.kill()
        proc.wait(timeout=30)


@pytest.mark.skipif(not LINUX, reason="pidfd handoff is Linux-only")
def test_an_unbound_driver_taints_the_environment_and_is_not_signalled(
    monkeypatch,
) -> None:
    env = _environment()
    from openadapt_flow.rl_env import environment as E

    monkeypatch.setattr(lifecycle, "HANDOFF_TIMEOUT_S", 0.5)
    proc, launch = _fake_supervisor("silent")
    try:
        monkeypatch.setitem(E._SUPERVISED, proc.pid, launch)
        sent = _no_signals(monkeypatch)
        env._pw, env._driver_pid = object(), proc.pid
        env._abandon_worker()
        assert env._teardown == {
            "driver_pid": proc.pid,
            "driver": "unsignalled",
            "proven": False,
        }
        assert sent == [] and proc.poll() is None
        assert env._unproven is not None
        out = _judged(env, monkeypatch)
        assert (out["verdict"], out["reward"]) == ("INDETERMINATE", 0.0)
    finally:
        proc.kill()
        proc.wait(timeout=30)


@pytest.mark.skipif(not LINUX, reason="pidfd handoff is Linux-only")
def test_a_pid_now_held_by_another_live_child_is_never_signalled(monkeypatch) -> None:
    # As if the supervisor's pid were reused by another child of this
    # process: an unbound launch naming that pid must not touch it.
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        stale = lifecycle.SupervisedLaunch(
            pid=other.pid, nonce=lifecycle.new_nonce(), start=None
        )
        sent = _no_signals(monkeypatch)
        assert lifecycle.stop_supervised(stale, timeout=5).how == "unsignalled"
        assert sent == []
        monkeypatch.undo()
        assert other.poll() is None  # alive and not reaped behind its back
    finally:
        other.kill()
        other.wait(timeout=30)
