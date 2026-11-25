"""A Playwright session an interrupted test never stopped must not poison the
next browser test on the same worker (issue #1875)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

FLOW_ROOT = Path(__file__).resolve().parent.parent

# Four tests that run in order in one process, as a file does on one xdist
# worker under --dist loadfile. The first opens a Playwright session and the
# second is stopped by pytest-timeout while it is open, keeping it referenced
# as the traceback pytest keeps does on CI.
_LEAKING_FILE = """
import asyncio
import socket
import threading

import pytest
from playwright.sync_api import sync_playwright

LEAKED = []


def test_0_open_a_playwright_session():
    manager = sync_playwright()
    pw = manager.start()
    LEAKED.append((manager, pw))


@pytest.mark.timeout(1)
def test_1_times_out_inside_a_playwright_session():
    assert LEAKED
    _, pw = LEAKED[0]
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _hold():
        try:
            conn, _ = srv.accept()
            conn.settimeout(5)
            try:
                conn.recv(1024)
                # Hold the socket open without replying so pytest-timeout fires
                # inside Playwright's sync API call instead of socket hang up.
                threading.Event().wait(5)
            except OSError:
                pass
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=_hold, daemon=True).start()
    ctx = pw.request.new_context()
    ctx.get(f"http://127.0.0.1:{port}/hang", timeout=30_000)


def test_2_next_browser_test_starts_cleanly():
    with sync_playwright() as playwright:
        assert playwright.chromium.name == "chromium"


def test_3_stop_the_leaked_session():
    manager, _ = LEAKED.pop()
    if manager._loop.is_running():
        asyncio._set_running_loop(manager._loop)
    try:
        manager.__exit__()
    finally:
        asyncio._set_running_loop(None)
"""

# A healthy module-scoped session, as the e2e browser fixtures use: the
# guard must leave its loop alone between tests.
_SCOPED_FILE = """
import pytest
from playwright.sync_api import sync_playwright


@pytest.fixture(scope="module")
def playwright():
    with sync_playwright() as session:
        yield session


def test_1_uses_the_shared_session(playwright):
    playwright.request.new_context().dispose()


def test_2_still_uses_the_shared_session(playwright):
    playwright.request.new_context().dispose()
"""


def _run_file(
    tmp_path: Path, name: str, source: str
) -> subprocess.CompletedProcess[str]:
    test_file = tmp_path / name
    test_file.write_text(source, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            # The real suite conftest, so this checks the fixture that ships.
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-q",
            f"--rootdir={tmp_path}",
            f"--basetemp={tmp_path / 'basetemp'}",
            str(test_file),
        ],
        cwd=FLOW_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="pytest-timeout interrupts by signal only on POSIX"
)
@pytest.mark.timeout(150)
def test_a_timed_out_playwright_test_does_not_fail_the_next_browser_test(
    tmp_path: Path,
) -> None:
    pytest.importorskip("playwright.sync_api")
    result = _run_file(tmp_path, "test_leaks_a_playwright_loop.py", _LEAKING_FILE)
    output = result.stdout + result.stderr
    assert "Sync API inside the asyncio loop" not in output, output
    assert "from pytest-timeout" in output, output
    assert "1 failed, 3 passed" in output, output
    assert "::test_1_times_out_inside_a_playwright_session" in output, output


@pytest.mark.timeout(150)
def test_a_scoped_playwright_session_survives_between_tests(tmp_path: Path) -> None:
    pytest.importorskip("playwright.sync_api")
    result = _run_file(tmp_path, "test_scoped_playwright_session.py", _SCOPED_FILE)
    output = result.stdout + result.stderr
    assert "no running event loop" not in output, output
    assert result.returncode == 0, output
    assert "2 passed" in output, output


def test_only_a_pytest_timeout_failure_counts() -> None:
    from tests._playwright_loop_guard import is_pytest_timeout

    assert is_pytest_timeout(Exception("Timeout (>1.0s) from pytest-timeout."))
    assert not is_pytest_timeout(AssertionError("values differ"))
    assert not is_pytest_timeout(None)


def test_the_guard_clears_only_a_leaked_loop() -> None:
    import asyncio

    from tests._playwright_loop_guard import clear_leaked_running_loop

    assert clear_leaked_running_loop() is False
    loop = asyncio.new_event_loop()
    try:
        asyncio._set_running_loop(loop)
        assert clear_leaked_running_loop() is True
        assert asyncio._get_running_loop() is None
    finally:
        asyncio._set_running_loop(None)
        loop.close()


def test_has_playwright_frame_distinguishes_playwright_from_non_playwright_tracebacks() -> (
    None
):
    from tests._playwright_loop_guard import has_playwright_frame

    assert has_playwright_frame(None) is False

    try:
        raise RuntimeError("Timeout (>1.0s) from pytest-timeout.")
    except RuntimeError as exc:
        assert has_playwright_frame(exc.__traceback__) is False

    ns = {"__name__": "playwright.sync_api._generated"}
    exec(
        "def _pw_call():\n    raise RuntimeError('Timeout (>1.0s) from pytest-timeout.')\n",
        ns,
    )
    try:
        ns["_pw_call"]()
    except RuntimeError as exc:
        assert has_playwright_frame(exc.__traceback__) is True


def test_non_playwright_timeout_preserves_running_loop() -> None:
    import asyncio
    from types import SimpleNamespace

    from tests.conftest import pytest_runtest_makereport

    loop = asyncio.new_event_loop()
    try:
        asyncio._set_running_loop(loop)
        try:
            raise RuntimeError("Timeout (>1.0s) from pytest-timeout.")
        except RuntimeError as exc:
            excinfo = SimpleNamespace(value=exc, tb=exc.__traceback__)
        call = SimpleNamespace(when="call", excinfo=excinfo)
        hook = pytest_runtest_makereport(item=None, call=call)
        next(hook)
        try:
            next(hook)
        except StopIteration:
            pass
        assert asyncio._get_running_loop() is loop
    finally:
        asyncio._set_running_loop(None)
        loop.close()
