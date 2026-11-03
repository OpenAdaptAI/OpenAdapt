"""``MacWindowClient.frontmost_pid`` reads the app that is frontmost now.

NSWorkspace updates ``frontmostApplication`` from notifications the run loop
delivers. A Python run that never spins one kept reporting the first app it
saw, even after that app exited, so a freshly started app's window failed the
focus check (#1712). The live test needs macOS and opts in like the AX tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from openadapt_flow.backends.remote_display import MacWindowClient


def test_frontmost_pid_applies_pending_workspace_updates(monkeypatch) -> None:
    pytest.importorskip("AppKit")  # the read itself needs AppKit
    calls: list[str] = []
    monkeypatch.setattr(
        MacWindowClient,
        "_drain_workspace_updates",
        staticmethod(lambda: calls.append("drain")),
    )
    MacWindowClient.frontmost_pid(MacWindowClient.__new__(MacWindowClient))
    assert calls == ["drain"]


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("OPENADAPT_TEST_MACOS_AX") != "1",
    reason="set OPENADAPT_TEST_MACOS_AX=1 on macOS",
)
def test_frontmost_pid_follows_a_relaunched_app(tmp_path) -> None:
    """Start an app, kill it, start another: the second reads as frontmost."""

    source = os.path.join(os.path.dirname(__file__), "native_apps", "Chart.swift")
    binary = tmp_path / "chart"
    built = subprocess.run(
        ["swiftc", "-O", "-o", str(binary), source, "-lsqlite3"],
        capture_output=True,
        timeout=300,
    )
    if built.returncode != 0:
        pytest.skip("swiftc could not build the test app")
    app = [str(binary), str(tmp_path / "db.sqlite"), "Frontmost", "p", "x", "plain"]
    client = MacWindowClient()
    first = subprocess.Popen(app)
    try:
        time.sleep(2.0)
        client.activate(first.pid)
        time.sleep(0.5)
        assert client.frontmost_pid() == first.pid
    finally:
        first.kill()
        first.wait()
    time.sleep(1.5)
    second = subprocess.Popen(app)
    try:
        time.sleep(2.0)
        client.activate(second.pid)
        time.sleep(0.5)
        assert client.frontmost_pid() == second.pid
    finally:
        second.kill()
        second.wait()
