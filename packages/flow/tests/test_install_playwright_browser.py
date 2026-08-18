"""Tests for the bounded Playwright installer used by GitHub Actions."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import install_playwright_browser


def test_default_install_handles_a_cache_miss_without_system_package_refresh() -> None:
    command = install_playwright_browser.playwright_install_command(
        with_system_deps=False
    )

    assert command == ("playwright", "install", "chromium")
    assert "--with-deps" not in command


def test_system_dependencies_require_an_explicit_qualification_flag() -> None:
    command = install_playwright_browser.playwright_install_command(
        with_system_deps=True
    )

    assert command == ("playwright", "install", "--with-deps", "chromium")


def test_missing_browser_library_fails_the_launch_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_library() -> None:
        raise OSError("libfixture.so: cannot open shared object file")

    result = install_playwright_browser.verify_chromium_launch(missing_library)

    assert result == install_playwright_browser.BROWSER_LAUNCH_EXIT_CODE
    output = capsys.readouterr().out
    assert "Chromium was installed but could not launch" in output
    assert "libfixture.so" in output


def test_browser_launch_probe_succeeds() -> None:
    launches = 0

    def launch() -> None:
        nonlocal launches
        launches += 1

    assert install_playwright_browser.verify_chromium_launch(launch) == 0
    assert launches == 1


def test_browser_launch_uses_the_bounded_timeout() -> None:
    launch_kwargs: dict[str, Any] = {}
    closes = 0

    class FakeBrowser:
        def close(self) -> None:
            nonlocal closes
            closes += 1

    class FakeBrowserType:
        def launch(self, **kwargs: Any) -> FakeBrowser:
            launch_kwargs.update(kwargs)
            return FakeBrowser()

    install_playwright_browser._launch_and_close(FakeBrowserType())

    assert launch_kwargs == {
        "headless": True,
        "timeout": install_playwright_browser.BROWSER_LAUNCH_TIMEOUT_MILLISECONDS,
    }
    assert install_playwright_browser.BROWSER_LAUNCH_TIMEOUT_MILLISECONDS == 30_000
    assert closes == 1


def test_browser_launch_probe_runs_in_a_bounded_process_tree() -> None:
    calls: list[tuple[tuple[str, ...], int]] = []

    def runner(command: Sequence[str], timeout_seconds: int) -> int:
        calls.append((tuple(command), timeout_seconds))
        return 0

    result = install_playwright_browser.run_browser_launch_probe(runner)

    assert result == 0
    assert calls == [
        (
            (
                sys.executable,
                os.path.abspath(install_playwright_browser.__file__),
                install_playwright_browser.BROWSER_LAUNCH_PROBE_FLAG,
            ),
            install_playwright_browser.BROWSER_LAUNCH_PROBE_TIMEOUT_SECONDS,
        )
    ]


def test_external_install_retries_once_then_succeeds() -> None:
    calls: list[tuple[tuple[str, ...], int]] = []
    outcomes = iter((124, 0))
    sleeps: list[float] = []

    def runner(command: Sequence[str], timeout_seconds: int) -> int:
        calls.append((tuple(command), timeout_seconds))
        return next(outcomes)

    result = install_playwright_browser.install_with_retry(
        attempts=2,
        timeout_seconds=600,
        retry_delay_seconds=5,
        runner=runner,
        sleeper=sleeps.append,
    )

    assert result == 0
    assert calls == [
        (install_playwright_browser.PLAYWRIGHT_INSTALL, 600),
        (install_playwright_browser.PLAYWRIGHT_INSTALL, 600),
    ]
    assert sleeps == [5]


def test_external_install_returns_the_final_failure() -> None:
    attempts = 0

    def runner(_command: Sequence[str], _timeout_seconds: int) -> int:
        nonlocal attempts
        attempts += 1
        return 7

    result = install_playwright_browser.install_with_retry(
        attempts=2,
        timeout_seconds=600,
        retry_delay_seconds=1,
        runner=runner,
        sleeper=lambda _seconds: None,
    )

    assert result == 7
    assert attempts == 2


def test_cleanup_failure_is_fatal_and_is_not_retried() -> None:
    attempts = 0
    sleeps: list[float] = []

    def runner(_command: Sequence[str], _timeout_seconds: int) -> int:
        nonlocal attempts
        attempts += 1
        return install_playwright_browser.FATAL_CLEANUP_EXIT_CODE

    result = install_playwright_browser.install_with_retry(
        attempts=2,
        timeout_seconds=600,
        retry_delay_seconds=1,
        runner=runner,
        sleeper=sleeps.append,
    )

    assert result == install_playwright_browser.FATAL_CLEANUP_EXIT_CODE
    assert attempts == 1
    assert sleeps == []


def test_attempt_timeout_terminates_the_process() -> None:
    result = install_playwright_browser.run_attempt(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_seconds=1,
    )

    assert result == 124


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
@pytest.mark.parametrize("leader_return_code", [0, 7])
def test_normal_leader_exit_cleans_surviving_child(
    tmp_path: Path, leader_return_code: int
) -> None:
    lock_path = tmp_path / "normal-exit-child.lock"
    pid_path = tmp_path / "normal-exit-child.pid"
    child_program = """
import fcntl
import os
from pathlib import Path
import sys
import time

with Path(sys.argv[1]).open("w") as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
"""
    parent_program = """
from pathlib import Path
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]])
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("child did not acquire its lock")
    time.sleep(0.01)
raise SystemExit(int(sys.argv[4]))
"""

    result = install_playwright_browser.run_attempt(
        [
            sys.executable,
            "-c",
            parent_program,
            child_program,
            str(lock_path),
            str(pid_path),
            str(leader_return_code),
        ],
        timeout_seconds=10,
    )

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    lock_released = False
    try:
        import fcntl

        with lock_path.open("w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                lock_released = True
    finally:
        if not lock_released:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert result == leader_return_code
    assert lock_released, "child survived a successful installer leader exit"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_posix_timeout_kills_sigterm_ignoring_child(tmp_path: Path) -> None:
    lock_path = tmp_path / "child.lock"
    pid_path = tmp_path / "child.pid"
    child_program = """
import fcntl
import os
from pathlib import Path
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
with Path(sys.argv[1]).open("w") as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
"""
    parent_program = """
from pathlib import Path
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]])
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("child did not acquire its lock")
    time.sleep(0.01)
time.sleep(30)
"""

    result = install_playwright_browser.run_attempt(
        [
            sys.executable,
            "-c",
            parent_program,
            child_program,
            str(lock_path),
            str(pid_path),
        ],
        timeout_seconds=2,
    )

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    lock_released = False
    try:
        import fcntl

        with lock_path.open("w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                lock_released = True
    finally:
        if not lock_released:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert result == 124
    assert lock_released, "timed-out descendant still holds the installer lock"


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true",
    reason="required GitHub Linux privileged process-group proof",
)
def test_github_linux_cleans_root_owned_sigterm_ignoring_child(
    tmp_path: Path,
) -> None:
    probe = subprocess.run(
        ["sudo", "-n", "/usr/bin/id", "-u"],
        check=False,
        capture_output=True,
        text=True,
        timeout=install_playwright_browser.SUDO_SIGNAL_TIMEOUT_SECONDS,
    )
    assert probe.returncode == 0 and probe.stdout.strip() == "0", (
        "required GitHub Linux root cleanup proof needs passwordless sudo"
    )

    lock_path = tmp_path / "root-child.lock"
    metadata_path = tmp_path / "root-child.meta"
    leader_path = tmp_path / "root-child.leader"
    child_program = """
import fcntl
import os
from pathlib import Path
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
lock_path = Path(sys.argv[1])
with lock_path.open("w") as lock_file:
    os.chmod(lock_path, 0o666)
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    Path(sys.argv[2]).write_text(
        f"{os.getuid()} {os.getpid()} {os.getpgrp()}", encoding="utf-8"
    )
    time.sleep(30)
"""
    parent_program = """
import os
from pathlib import Path
import subprocess
import sys
import time

Path(sys.argv[4]).write_text(str(os.getpid()), encoding="utf-8")
subprocess.Popen(
    ["sudo", "-n", sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]]
)
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("root child did not acquire its lock")
    time.sleep(0.01)
"""

    child_process_group: int | None = None
    try:
        result = install_playwright_browser.run_attempt(
            [
                sys.executable,
                "-c",
                parent_program,
                child_program,
                str(lock_path),
                str(metadata_path),
                str(leader_path),
            ],
            timeout_seconds=10,
        )

        child_uid, _child_pid, child_process_group = map(
            int, metadata_path.read_text(encoding="utf-8").split()
        )
        leader_pid = int(leader_path.read_text(encoding="utf-8"))
        assert child_uid == 0
        assert child_process_group == leader_pid

        import fcntl

        with lock_path.open("a+") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_released = False
            else:
                lock_released = True

        assert result == 0
        assert lock_released, "root-owned descendant retained its installer lock"
        assert not install_playwright_browser._process_group_exists(leader_pid)
    finally:
        if child_process_group is not None:
            subprocess.run(
                [
                    "sudo",
                    "-n",
                    "/bin/kill",
                    "-SIGKILL",
                    "--",
                    f"-{child_process_group}",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=install_playwright_browser.SUDO_SIGNAL_TIMEOUT_SECONDS,
            )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_posix_partial_group_kill_uses_exact_privileged_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal_signals: list[tuple[int, signal.Signals]] = []
    privileged_signals: list[tuple[int, signal.Signals]] = []
    group_exit = iter((False, False, True))

    class FakeProcess:
        pid = 2267

    def fake_killpg(process_group_id: int, process_signal: signal.Signals) -> None:
        normal_signals.append((process_group_id, process_signal))

    def fake_sudo_signal(process_group_id: int, process_signal: signal.Signals) -> bool:
        privileged_signals.append((process_group_id, process_signal))
        return True

    monkeypatch.setattr(install_playwright_browser.os, "killpg", fake_killpg)
    monkeypatch.setattr(
        install_playwright_browser,
        "_wait_for_process_group_exit",
        lambda _process: next(group_exit),
    )
    monkeypatch.setattr(
        install_playwright_browser,
        "_sudo_signal_process_group",
        fake_sudo_signal,
    )
    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())

    cleanup_succeeded = install_playwright_browser._terminate_process_group(process)

    assert cleanup_succeeded is True
    assert normal_signals == [
        (2267, signal.SIGTERM),
        (2267, signal.SIGKILL),
    ]
    assert privileged_signals == [(2267, signal.SIGKILL)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_posix_final_cleanup_verification_is_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 2267

    monkeypatch.setattr(install_playwright_browser.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(
        install_playwright_browser,
        "_wait_for_process_group_exit",
        lambda _process: False,
    )
    monkeypatch.setattr(
        install_playwright_browser,
        "_sudo_signal_process_group",
        lambda *_args: False,
    )
    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())

    assert install_playwright_browser._terminate_process_group(process) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_run_attempt_returns_fatal_when_group_cannot_be_proven_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 2267

        def wait(self, timeout: int) -> int:
            assert timeout == 600
            return 0

    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())
    monkeypatch.setattr(
        install_playwright_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        install_playwright_browser, "_process_group_exists", lambda _pgid: True
    )
    monkeypatch.setattr(
        install_playwright_browser, "_terminate_process_group", lambda _process: False
    )

    assert (
        install_playwright_browser.run_attempt(["installer"], timeout_seconds=600)
        == install_playwright_browser.FATAL_CLEANUP_EXIT_CODE
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_privileged_fallback_is_bounded_and_targets_only_the_exact_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(
        command: list[str], **kwargs: Any
    ) -> install_playwright_browser.subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return install_playwright_browser.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(install_playwright_browser.subprocess, "run", fake_run)

    assert install_playwright_browser._sudo_signal_process_group(2267, signal.SIGKILL)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["sudo", "-n", "/bin/kill", "-SIGKILL", "--", "-2267"]
    assert kwargs["timeout"] == install_playwright_browser.SUDO_SIGNAL_TIMEOUT_SECONDS


@pytest.mark.parametrize("leader_return_code", [0, 7])
def test_windows_job_cleanup_runs_after_every_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
    leader_return_code: int,
) -> None:
    events: list[str] = []

    class FakeStdin:
        def write(self, payload: bytes) -> int:
            assert events == ["assigned"]
            assert payload == b"START\n"
            events.append("started")
            return len(payload)

        def close(self) -> None:
            events.append("gate-closed")

    class FakeProcess:
        stdin = FakeStdin()

        def poll(self) -> int:
            return leader_return_code

        def wait(self, timeout: int) -> int:
            assert timeout == 600
            events.append("result-captured")
            return leader_return_code

        def kill(self) -> None:
            raise AssertionError("assigned process must be terminated through its job")

    class FakeJob:
        def assign(self, _process: object) -> bool:
            assert events == []
            events.append("assigned")
            return True

        def terminate_and_verify_empty(self) -> bool:
            assert "result-captured" in events
            events.append("job-empty")
            return True

        def close(self) -> bool:
            assert "job-empty" in events
            events.append("job-closed")
            return True

    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())
    job = cast("install_playwright_browser._WindowsJobContract", FakeJob())
    monkeypatch.setattr(
        install_playwright_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    result = install_playwright_browser._run_windows_attempt(
        ["installer"], timeout_seconds=600, job_factory=lambda: job
    )

    assert result == leader_return_code
    assert events == [
        "assigned",
        "started",
        "gate-closed",
        "result-captured",
        "job-empty",
        "job-closed",
        "gate-closed",
    ]


def test_windows_timeout_leader_exit_race_still_closes_retained_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader_exited = False
    cleanup_calls = 0

    class FakeStdin:
        def write(self, payload: bytes) -> int:
            assert payload == b"START\n"
            return len(payload)

        def close(self) -> None:
            return None

    class FakeProcess:
        stdin = FakeStdin()

        def poll(self) -> int | None:
            return 0 if leader_exited else None

        def wait(self, timeout: int) -> int:
            nonlocal leader_exited
            assert timeout == 1
            leader_exited = True
            raise install_playwright_browser.subprocess.TimeoutExpired(
                "installer", timeout
            )

        def kill(self) -> None:
            raise AssertionError("assigned process must be terminated through its job")

    class FakeJob:
        def assign(self, _process: object) -> bool:
            return True

        def terminate_and_verify_empty(self) -> bool:
            nonlocal cleanup_calls
            assert leader_exited
            cleanup_calls += 1
            return True

        def close(self) -> bool:
            return True

    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())
    job = cast("install_playwright_browser._WindowsJobContract", FakeJob())
    monkeypatch.setattr(
        install_playwright_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    result = install_playwright_browser._run_windows_attempt(
        ["installer"], timeout_seconds=1, job_factory=lambda: job
    )

    assert result == 124
    assert cleanup_calls == 1


def test_windows_unproved_job_cleanup_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStdin:
        def write(self, payload: bytes) -> int:
            return len(payload)

        def close(self) -> None:
            return None

    class FakeProcess:
        stdin = FakeStdin()

        def poll(self) -> int:
            return 0

        def wait(self, timeout: int) -> int:
            return 0

        def kill(self) -> None:
            return None

    class FakeJob:
        def assign(self, _process: object) -> bool:
            return True

        def terminate_and_verify_empty(self) -> bool:
            return False

        def close(self) -> bool:
            return True

    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())
    job = cast("install_playwright_browser._WindowsJobContract", FakeJob())
    monkeypatch.setattr(
        install_playwright_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    assert (
        install_playwright_browser._run_windows_attempt(
            ["installer"], timeout_seconds=1, job_factory=lambda: job
        )
        == install_playwright_browser.FATAL_CLEANUP_EXIT_CODE
    )


def _windows_lock_is_available(lock_path: Path) -> bool:
    import msvcrt

    with lock_path.open("r+b", buffering=0) as lock_file:
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    return True


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows Job Object proof")
@pytest.mark.parametrize("leader_return_code", [0, 7])
def test_windows_job_kills_child_after_normal_leader_exit(
    tmp_path: Path, leader_return_code: int
) -> None:
    lock_path = tmp_path / "windows-normal-child.lock"
    pid_path = tmp_path / "windows-normal-child.pid"
    child_program = """
import msvcrt
import os
from pathlib import Path
import sys
import time

with Path(sys.argv[1]).open("w+b", buffering=0) as lock_file:
    lock_file.write(b"0")
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
"""
    parent_program = """
from pathlib import Path
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]])
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("child did not acquire its lock")
    time.sleep(0.01)
raise SystemExit(int(sys.argv[4]))
"""

    result = install_playwright_browser.run_attempt(
        [
            sys.executable,
            "-c",
            parent_program,
            child_program,
            str(lock_path),
            str(pid_path),
            str(leader_return_code),
        ],
        timeout_seconds=10,
    )

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    try:
        assert result == leader_return_code
        assert _windows_lock_is_available(lock_path)
    finally:
        subprocess.run(
            ["taskkill", "/PID", str(child_pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows Job Object proof")
def test_windows_job_kills_child_on_timeout(tmp_path: Path) -> None:
    lock_path = tmp_path / "windows-timeout-child.lock"
    pid_path = tmp_path / "windows-timeout-child.pid"
    child_program = """
import msvcrt
import os
from pathlib import Path
import sys
import time

with Path(sys.argv[1]).open("w+b", buffering=0) as lock_file:
    lock_file.write(b"0")
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
"""
    parent_program = """
from pathlib import Path
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]])
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("child did not acquire its lock")
    time.sleep(0.01)
time.sleep(30)
"""

    result = install_playwright_browser.run_attempt(
        [
            sys.executable,
            "-c",
            parent_program,
            child_program,
            str(lock_path),
            str(pid_path),
        ],
        timeout_seconds=1,
    )

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    try:
        assert result == 124
        assert _windows_lock_is_available(lock_path)
    finally:
        subprocess.run(
            ["taskkill", "/PID", str(child_pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
