"""The halted-page hold serves a repair demonstration, then cleans up."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from openadapt_flow.repair.hold import HOLD_FILE, hold_for_repair, request

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")


class FakeBackend:
    viewport = (1280, 800)

    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    def screenshot(self) -> bytes:
        return b"\x89PNG fake"

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))


def _start(backend, run_dir: Path, timeout: float):
    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "ended", hold_for_repair(backend, run_dir, timeout)
        )
    )
    thread.start()
    hold_path = run_dir / HOLD_FILE
    deadline = time.monotonic() + 5
    while not hold_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.05)
    return thread, result, json.loads(hold_path.read_text())


def test_serves_screen_and_click_then_releases(tmp_path: Path) -> None:
    backend = FakeBackend()
    thread, result, hold = _start(backend, tmp_path, 10)
    assert oct((tmp_path / HOLD_FILE).stat().st_mode & 0o777) == "0o600"
    assert request(hold, {"op": "viewport"})["viewport"] == [1280, 800]
    assert request(hold, {"op": "screenshot"})["ok"]
    assert request(hold, {"op": "click", "x": 3, "y": 4}) == {"ok": True}
    assert request(hold, {"op": "release"}) == {"ok": True}
    thread.join(5)
    assert result["ended"] == "released"
    assert backend.clicks == [(3, 4)]
    assert not (tmp_path / HOLD_FILE).exists()
    assert not Path(hold["socket"]).exists()


def test_wrong_secret_is_refused(tmp_path: Path) -> None:
    backend = FakeBackend()
    thread, result, hold = _start(backend, tmp_path, 10)
    bad = {**hold, "secret": "guess"}
    assert request(bad, {"op": "click", "x": 1, "y": 1})["ok"] is False
    assert backend.clicks == []
    request(hold, {"op": "release"})
    thread.join(5)


def test_times_out_and_cleans_up(tmp_path: Path) -> None:
    thread, result, hold = _start(FakeBackend(), tmp_path, 0.5)
    thread.join(5)
    assert result["ended"] == "timeout"
    assert not (tmp_path / HOLD_FILE).exists()
    assert not Path(hold["socket"]).parent.exists()


def test_sigterm_ends_the_hold_and_cleans_up(tmp_path: Path) -> None:
    import os
    import signal
    import subprocess

    script = (
        "import sys\n"
        "from openadapt_flow.repair.hold import hold_for_repair\n"
        "class B:\n"
        "    viewport = (1, 1)\n"
        "print(hold_for_repair(B(), sys.argv[1], 60), flush=True)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    hold_path = tmp_path / HOLD_FILE
    deadline = time.monotonic() + 20
    while not hold_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    hold = json.loads(hold_path.read_text())
    os.kill(proc.pid, signal.SIGTERM)
    out, _ = proc.communicate(timeout=10)
    assert out.strip() == "terminated"
    assert not hold_path.exists()
    assert not Path(hold["socket"]).parent.exists()
