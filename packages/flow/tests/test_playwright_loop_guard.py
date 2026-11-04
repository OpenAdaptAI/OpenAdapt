"""A Playwright session an interrupted test never stopped must not poison the
next browser test on the same worker (issue #1875)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

FLOW_ROOT = Path(__file__).resolve().parent.parent

# Three tests that run in order in one process, as a file does on one xdist
# worker under --dist loadfile. The first leaves a started Playwright session
# behind and keeps it referenced, which is what a pytest-timeout interruption
# inside a Playwright call does (the kept traceback holds the dispatcher).
_LEAKING_FILE = """
import asyncio

from playwright.sync_api import sync_playwright

LEAKED = []


def test_1_interrupted_inside_playwright():
    manager = sync_playwright()
    manager.start()
    LEAKED.append(manager)


def test_2_next_browser_test_starts_cleanly():
    with sync_playwright() as playwright:
        assert playwright.chromium.name == "chromium"


def test_3_stop_the_leaked_session():
    manager = LEAKED.pop()
    asyncio._set_running_loop(manager._loop)
    manager.__exit__()
    asyncio._set_running_loop(None)
"""


def _run_leaking_file(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    test_file = tmp_path / "test_leaks_a_playwright_loop.py"
    test_file.write_text(_LEAKING_FILE, encoding="utf-8")
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


@pytest.mark.timeout(150)
def test_a_leaked_playwright_loop_does_not_fail_the_next_browser_test(
    tmp_path: Path,
) -> None:
    pytest.importorskip("playwright.sync_api")
    result = _run_leaking_file(tmp_path)
    output = result.stdout + result.stderr
    assert "Sync API inside the asyncio loop" not in output, output
    assert result.returncode == 0, output
    assert "3 passed" in output, output


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
