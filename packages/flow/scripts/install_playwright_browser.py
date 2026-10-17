#!/usr/bin/env python3
"""Install Playwright Chromium with a bounded external retry.

The browser download is an external dependency. A stalled download must not
hold a hosted runner for GitHub's six-hour default job timeout. This helper
bounds each attempt, terminates its complete process group, and retries once.
Standard CI does not refresh operating-system packages. Clean-machine and
release qualification can request that separate dependency boundary with
``--with-system-deps``. The helper does not retry tests or product operations.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from typing import Any, Protocol

PLAYWRIGHT_INSTALL = ("playwright", "install", "chromium")
PLAYWRIGHT_INSTALL_WITH_SYSTEM_DEPS = (
    "playwright",
    "install",
    "--with-deps",
    "chromium",
)
WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
PROCESS_EXIT_TIMEOUT_SECONDS = 5
PROCESS_GROUP_POLL_INTERVAL_SECONDS = 0.05
SUDO_SIGNAL_TIMEOUT_SECONDS = 5
FATAL_CLEANUP_EXIT_CODE = 125
BROWSER_LAUNCH_EXIT_CODE = 126
BROWSER_LAUNCH_TIMEOUT_MILLISECONDS = 30_000
BROWSER_LAUNCH_PROBE_TIMEOUT_SECONDS = 35
WINDOWS_JOB_WRAPPER_FLAG = "--windows-job-wrapper"
BROWSER_LAUNCH_PROBE_FLAG = "--browser-launch-probe"
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _WindowsJobContract(Protocol):
    def assign(self, process: subprocess.Popen[bytes]) -> bool: ...

    def terminate_and_verify_empty(self) -> bool: ...

    def close(self) -> bool: ...


class _WindowsJob:
    """A retained Windows Job Object that owns the complete installer tree."""

    def __init__(self) -> None:
        win_dll: Any = getattr(ctypes, "WinDLL")
        self._kernel32: Any = win_dll("kernel32", use_last_error=True)
        self._configure_prototypes()
        self._handle: Any = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            return
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = self._kernel32.SetInformationJobObject(
            self._handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not configured:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def _configure_prototypes(self) -> None:
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def assign(self, process: subprocess.Popen[bytes]) -> bool:
        """Assign the gated wrapper before it can create installer children."""
        if not self._handle:
            return False
        process_handle = getattr(process, "_handle", None)
        if not process_handle:
            return False
        return bool(
            self._kernel32.AssignProcessToJobObject(self._handle, process_handle)
        )

    def _active_processes(self) -> int | None:
        if not self._handle:
            return None
        accounting = _JobObjectBasicAccountingInformation()
        queried = self._kernel32.QueryInformationJobObject(
            self._handle,
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        )
        if not queried:
            return None
        return int(accounting.ActiveProcesses)

    def terminate_and_verify_empty(self) -> bool:
        """Terminate the exact retained job and prove it has no live members."""
        if not self._handle:
            return False
        if not self._kernel32.TerminateJobObject(self._handle, FATAL_CLEANUP_EXIT_CODE):
            return False
        deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
        while True:
            active_processes = self._active_processes()
            if active_processes is None:
                return False
            if active_processes == 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(PROCESS_GROUP_POLL_INTERVAL_SECONDS, remaining))

    def close(self) -> bool:
        """Close the retained handle after result capture and tree verification."""
        if not self._handle:
            return False
        handle = self._handle
        closed = bool(self._kernel32.CloseHandle(handle))
        if closed:
            self._handle = None
        return closed


def _wait_for_exit(process: subprocess.Popen[bytes]) -> bool:
    """Wait briefly for a terminated process and report whether it exited."""
    try:
        process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def _process_group_exists(process_group_id: int) -> bool:
    """Report whether a POSIX process group still has any members."""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A privileged descendant can remain after its unprivileged group
        # leader exits. The group exists even though this user cannot signal it.
        return True
    return True


def _wait_for_process_group_exit(
    process: subprocess.Popen[bytes],
) -> bool:
    """Wait a bounded interval for every member of a POSIX group to exit."""
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    while True:
        process.poll()  # Reap the group leader so its zombie does not retain the PGID.
        if not _process_group_exists(process.pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(PROCESS_GROUP_POLL_INTERVAL_SECONDS, remaining))


def _sudo_signal_process_group(
    process_group_id: int,
    process_signal: signal.Signals,
) -> bool:
    """Signal one exact privileged POSIX process group without prompting."""
    try:
        result = subprocess.run(
            [
                "sudo",
                "-n",
                "/bin/kill",
                f"-{process_signal.name}",
                "--",
                f"-{process_group_id}",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SUDO_SIGNAL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _terminate_process_group(
    process: subprocess.Popen[bytes],
) -> bool:
    """Terminate one POSIX installer group and prove whether it became empty."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return not _process_group_exists(process.pid)
    except PermissionError:
        _sudo_signal_process_group(process.pid, signal.SIGTERM)
    if _wait_for_process_group_exit(process):
        return True

    # The group leader can exit after SIGTERM while an unprivileged child
    # ignores it or a privileged apt descendant survives it. Signal the exact
    # group again rather than trusting only the leader's exit state.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return not _process_group_exists(process.pid)
    except PermissionError:
        _sudo_signal_process_group(process.pid, signal.SIGKILL)
    if _wait_for_process_group_exit(process):
        return True

    # killpg can report success after signaling only the same-UID members of a
    # mixed-ownership group. A final bounded, non-interactive exact-PGID signal
    # handles the root-owned apt process that Playwright starts on Linux CI.
    _sudo_signal_process_group(process.pid, signal.SIGKILL)
    return _wait_for_process_group_exit(process)


def _windows_job_wrapper(command: Sequence[str]) -> int:
    """Wait for assignment to a Job Object before starting the real command."""
    if sys.stdin.buffer.readline() != b"START\n":
        return FATAL_CLEANUP_EXIT_CODE
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        return 127


def _run_windows_attempt(
    command: Sequence[str],
    timeout_seconds: int,
    *,
    job_factory: Callable[[], _WindowsJobContract] = _WindowsJob,
) -> int:
    """Run the installer inside a gated, retained, fail-closed Job Object."""
    job = job_factory()
    process: subprocess.Popen[bytes] | None = None
    job_closed = False
    result = FATAL_CLEANUP_EXIT_CODE
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                os.path.abspath(__file__),
                WINDOWS_JOB_WRAPPER_FLAG,
                *command,
            ],
            stdin=subprocess.PIPE,
            creationflags=WINDOWS_CREATE_NEW_PROCESS_GROUP,
        )
        if not job.assign(process):
            process.kill()
            _wait_for_exit(process)
            return FATAL_CLEANUP_EXIT_CODE
        if process.stdin is None:
            return FATAL_CLEANUP_EXIT_CODE
        process.stdin.write(b"START\n")
        process.stdin.close()
        try:
            result = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            result = 124

        cleanup_succeeded = job.terminate_and_verify_empty()
        leader_reaped = process.poll() is not None or _wait_for_exit(process)
        close_succeeded = job.close()
        job_closed = close_succeeded
        if not cleanup_succeeded or not leader_reaped or not close_succeeded:
            return FATAL_CLEANUP_EXIT_CODE
        return result
    finally:
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        # KILL_ON_JOB_CLOSE is the last-resort fail-safe for every exceptional
        # path, including assignment and verification failures.
        if not job_closed:
            job.close()


def run_attempt(command: Sequence[str], timeout_seconds: int) -> int:
    """Run one installer attempt and return only after bounded group cleanup."""
    if os.name == "nt":
        result = _run_windows_attempt(command, timeout_seconds)
        if result == FATAL_CLEANUP_EXIT_CODE:
            print(
                "::error::Installer cleanup could not prove the Windows "
                "Job Object empty; refusing retry",
                flush=True,
            )
        return result

    try:
        process = subprocess.Popen(command, start_new_session=True)
    except FileNotFoundError:
        return 127
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return_code = 124

    # A CLI leader can return success or failure while a downloader or
    # privileged apt process remains in its exact process group. Inspect
    # the PGID after every exit, not only after a timeout.
    cleanup_succeeded = not _process_group_exists(process.pid)
    if not cleanup_succeeded:
        cleanup_succeeded = _terminate_process_group(process)
    if not cleanup_succeeded:
        print(
            f"::error::Installer cleanup could not prove process group "
            f"{process.pid} empty; refusing retry",
            flush=True,
        )
        return FATAL_CLEANUP_EXIT_CODE
    return return_code


def install_with_retry(
    *,
    attempts: int,
    timeout_seconds: int,
    retry_delay_seconds: int,
    command: Sequence[str] = PLAYWRIGHT_INSTALL,
    runner: Callable[[Sequence[str], int], int] = run_attempt,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Run only the external installer again after a bounded failure."""
    for attempt in range(1, attempts + 1):
        print(
            f"Playwright install attempt {attempt}/{attempts} "
            f"(timeout={timeout_seconds}s)",
            flush=True,
        )
        return_code = runner(command, timeout_seconds)
        if return_code == FATAL_CLEANUP_EXIT_CODE:
            print(
                "::error::Playwright install cleanup failed; "
                "a retry could overlap a surviving process",
                flush=True,
            )
            return return_code
        if return_code == 0:
            return 0
        if attempt == attempts:
            print(
                f"::error::Playwright install failed after {attempts} "
                f"bounded attempts (last exit={return_code})",
                flush=True,
            )
            return return_code
        print(
            f"::warning::Playwright install attempt {attempt} failed "
            f"(exit={return_code}); retrying external download",
            flush=True,
        )
        sleeper(retry_delay_seconds)
    raise AssertionError("attempt loop did not return")


def playwright_install_command(*, with_system_deps: bool) -> tuple[str, ...]:
    """Select whether this qualification owns the host package boundary."""
    if with_system_deps:
        return PLAYWRIGHT_INSTALL_WITH_SYSTEM_DEPS
    return PLAYWRIGHT_INSTALL


def _launch_and_close(browser_type: Any) -> None:
    """Launch one headless browser within the enclosing installer budget."""
    browser = browser_type.launch(
        headless=True,
        timeout=BROWSER_LAUNCH_TIMEOUT_MILLISECONDS,
    )
    browser.close()


def _launch_chromium_headless() -> None:
    """Launch and close Chromium so a missing host library fails immediately."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        _launch_and_close(playwright.chromium)


def verify_chromium_launch(
    launcher: Callable[[], None] | None = None,
) -> int:
    """Verify the installed browser and its host libraries without a retry."""
    if launcher is None:
        launcher = _launch_chromium_headless
    try:
        launcher()
    except Exception as error:
        print(
            "::error::Chromium was installed but could not launch. "
            "The runner can be missing a required system library: "
            f"{error}",
            flush=True,
        )
        return BROWSER_LAUNCH_EXIT_CODE
    return 0


def run_browser_launch_probe(
    runner: Callable[[Sequence[str], int], int] = run_attempt,
) -> int:
    """Run the launch probe in a bounded process tree and return its result."""
    return runner(
        [
            sys.executable,
            os.path.abspath(__file__),
            BROWSER_LAUNCH_PROBE_FLAG,
        ],
        BROWSER_LAUNCH_PROBE_TIMEOUT_SECONDS,
    )


def positive_int(value: str) -> int:
    """Parse a strictly positive command-line integer."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == WINDOWS_JOB_WRAPPER_FLAG:
        return _windows_job_wrapper(sys.argv[2:])
    if len(sys.argv) == 2 and sys.argv[1] == BROWSER_LAUNCH_PROBE_FLAG:
        return verify_chromium_launch()
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=positive_int, default=2)
    parser.add_argument(
        "--attempt-timeout-seconds",
        type=positive_int,
        default=600,
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=positive_int,
        default=5,
    )
    parser.add_argument(
        "--with-system-deps",
        action="store_true",
        help=(
            "also install operating-system dependencies; use only when this "
            "clean-machine or release qualification owns that package boundary"
        ),
    )
    args = parser.parse_args()
    result = install_with_retry(
        attempts=args.attempts,
        timeout_seconds=args.attempt_timeout_seconds,
        retry_delay_seconds=args.retry_delay_seconds,
        command=playwright_install_command(
            with_system_deps=args.with_system_deps,
        ),
    )
    if result != 0:
        return result
    return run_browser_launch_probe()


if __name__ == "__main__":
    raise SystemExit(main())
