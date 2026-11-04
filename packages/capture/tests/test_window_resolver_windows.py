"""The Win32 window resolver skips windows that show no pixels.

Runs on every platform: ``ctypes.windll``, ``WINFUNCTYPE`` and ``psutil``
are replaced with fakes.
"""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

import openadapt_capture.window_capture as window_capture_module
from openadapt_capture.window_capture import WindowTarget

try:
    import ctypes.wintypes  # noqa: F401
except (ImportError, ValueError):  # pragma: no cover - very old interpreters
    pytest.skip("ctypes.wintypes is unavailable on this interpreter", allow_module_level=True)

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14


@pytest.fixture
def win32(monkeypatch):
    """Fake Win32: three visible 'Target' windows, one minimized, one cloaked."""
    windows = {
        101: {"title": "Target", "iconic": False, "cloaked": 0, "rect": (0, 0, 400, 300)},
        102: {"title": "Target", "iconic": True, "cloaked": 0, "rect": (0, 0, 1600, 1200)},
        103: {"title": "Target", "iconic": False, "cloaked": 2, "rect": (0, 0, 1800, 1000)},
    }

    def get_text_length(hwnd):
        return len(windows[int(hwnd)]["title"])

    def get_text(hwnd, buffer, _size):
        buffer.value = windows[int(hwnd)]["title"]
        return len(buffer.value)

    def get_pid(hwnd, pid_ref):
        pid_ref._obj.value = 4000 + int(hwnd)
        return 1

    def enum_windows(callback, _lparam):
        for hwnd in windows:
            if not callback(hwnd, 0):
                break
        return True

    def dwm_attribute(hwnd, attribute, value_ref, _size):
        window = windows[int(hwnd.value)]
        if attribute.value == DWMWA_CLOAKED:
            value_ref._obj.value = window["cloaked"]
        elif attribute.value == DWMWA_EXTENDED_FRAME_BOUNDS:
            rect = value_ref._obj
            rect.left, rect.top, rect.right, rect.bottom = window["rect"]
        else:
            return 1
        return 0

    user32 = SimpleNamespace(
        IsWindowVisible=lambda _hwnd: True,
        IsIconic=lambda hwnd: windows[int(hwnd)]["iconic"],
        GetWindowTextLengthW=get_text_length,
        GetWindowTextW=get_text,
        GetWindowThreadProcessId=get_pid,
        EnumWindows=enum_windows,
    )
    dwmapi = SimpleNamespace(DwmGetWindowAttribute=dwm_attribute)
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(user32=user32, dwmapi=dwmapi), raising=False
    )
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *_types: lambda fn: fn, raising=False)

    import psutil

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def name(self):
            return "target.exe"

        def create_time(self):
            return 100.0

    monkeypatch.setattr(psutil, "Process", Process)
    return windows


def test_minimized_and_cloaked_windows_are_skipped(win32):
    window = window_capture_module._resolve_window_windows(WindowTarget(title="Target"))
    assert window is not None
    assert window.window_id == 101  # the larger minimized/cloaked matches lose


def test_unreadable_cloak_state_is_treated_as_cloaked(win32, monkeypatch):
    win32[101]["cloaked"] = 0
    dwmapi = ctypes.windll.dwmapi
    original = dwmapi.DwmGetWindowAttribute

    def failing(hwnd, attribute, value_ref, size):
        if attribute.value == DWMWA_CLOAKED:
            return 0x80070057  # E_INVALIDARG
        return original(hwnd, attribute, value_ref, size)

    monkeypatch.setattr(dwmapi, "DwmGetWindowAttribute", failing)
    assert window_capture_module._resolve_window_windows(WindowTarget(title="Target")) is None
