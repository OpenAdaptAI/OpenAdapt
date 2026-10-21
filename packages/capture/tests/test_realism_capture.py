"""Capture cases the single-window fakes do not model (REALISM_AUDIT.md).

Real desktops have several windows of one app: two browser windows, two EMR
charts. When more than one window matches the target, binding the largest
one records whichever chart happens to be bigger. The macOS resolver raises
``WindowCaptureAmbiguousError`` here; these tests pin the same rule on X11.
"""

from __future__ import annotations

import pytest

from openadapt_capture.window_capture import WindowCaptureError, WindowTarget
from openadapt_capture.window_capture_linux import (
    LinuxWindowCaptureError,
    X11WindowRecord,
    resolve_window_linux,
)
from tests.test_window_capture_linux import FakeX11Client


@pytest.fixture
def x11_environment(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


def _window(window_id: int, title: str, bounds) -> X11WindowRecord:
    return X11WindowRecord(window_id=window_id, title=title, pid=1000, bounds=bounds, viewable=True)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "REALISM_AUDIT capture-5: the X11 resolver binds the largest of "
        "several matching windows instead of refusing"
    ),
)
def test_two_matching_browser_windows_are_ambiguous(x11_environment) -> None:
    client = FakeX11Client(
        [
            _window(41, "Chart: Patient A - Chromium", (0, 0, 900, 700)),
            _window(42, "Chart: Patient B - Chromium", (40, 40, 1200, 800)),
        ]
    )
    with pytest.raises((WindowCaptureError, LinuxWindowCaptureError)):
        resolve_window_linux(
            WindowTarget(owner="chromium", title="Chart"),
            client_factory=lambda: client,
            process_identity=lambda _pid: ("chromium", 100.0),
        )
