"""Xlib must be initialized before concurrent Linux capture starts."""

from __future__ import annotations

import ctypes
import threading

import pytest

from openadapt_capture import input as input_module
from openadapt_capture import utils, x11_threads
from openadapt_capture.input_observer import factory
from openadapt_capture.x11_threads import (
    X11ThreadInitializationError,
    ensure_xlib_thread_support,
)


class _FakeXInitThreads:
    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls = 0
        self.argtypes = None
        self.restype = None

    def __call__(self) -> int:
        self.calls += 1
        return self.result


class _FakeX11:
    def __init__(self, result: int = 1) -> None:
        self.XInitThreads = _FakeXInitThreads(result)


def _linux_x11(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: int = 1,
) -> _FakeX11:
    fake = _FakeX11(result)
    monkeypatch.setattr(x11_threads.sys, "platform", "linux")
    monkeypatch.setattr(x11_threads, "_initialized", False)
    monkeypatch.setattr(x11_threads.ctypes.util, "find_library", lambda _name: "libX11")
    monkeypatch.setattr(x11_threads.ctypes, "CDLL", lambda _name: fake)
    return fake


def test_xinit_threads_is_configured_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _linux_x11(monkeypatch)

    ensure_xlib_thread_support()
    ensure_xlib_thread_support()

    assert fake.XInitThreads.calls == 1
    assert fake.XInitThreads.argtypes == []
    assert fake.XInitThreads.restype is ctypes.c_int


def test_missing_x11_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x11_threads.sys, "platform", "linux")
    monkeypatch.setattr(x11_threads, "_initialized", False)
    monkeypatch.setattr(x11_threads.ctypes.util, "find_library", lambda _name: None)

    with pytest.raises(X11ThreadInitializationError, match="requires libX11"):
        ensure_xlib_thread_support()


def test_rejected_xinit_threads_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _linux_x11(monkeypatch, result=0)

    with pytest.raises(X11ThreadInitializationError, match="rejected"):
        ensure_xlib_thread_support()


def test_non_linux_initialization_is_a_side_effect_free_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(x11_threads.sys, "platform", "darwin")
    monkeypatch.setattr(
        x11_threads.ctypes.util,
        "find_library",
        lambda _name: pytest.fail("non-Linux code loaded X11"),
    )

    ensure_xlib_thread_support()


def test_mss_is_constructed_only_after_xlib_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    fake_sct = object()
    monkeypatch.setattr(utils, "_process_local", threading.local())
    monkeypatch.setattr(
        utils,
        "ensure_xlib_thread_support",
        lambda: order.append("XInitThreads"),
    )
    monkeypatch.setattr(
        utils.mss,
        "mss",
        lambda: order.append("mss") or fake_sct,
    )

    assert utils.get_process_local_sct() is fake_sct
    assert order == ["XInitThreads", "mss"]


def test_screen_capturer_surfaces_xlib_initialization_failure_before_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise X11ThreadInitializationError("XInitThreads failed")

    monkeypatch.setattr(input_module, "ensure_xlib_thread_support", fail)
    capturer = input_module.ScreenCapturer(lambda _image, _timestamp: None)

    with pytest.raises(X11ThreadInitializationError, match="failed"):
        capturer.start()

    assert capturer._thread is None
    assert not capturer._running


def test_linux_input_factory_initializes_xlib_before_observer_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(factory.sys, "platform", "linux")
    monkeypatch.setattr(
        x11_threads,
        "ensure_xlib_thread_support",
        lambda: calls.append("XInitThreads"),
    )

    observer = factory.create_input_observer(
        lambda _event: None,
        platform_name="linux",
    )

    assert calls == ["XInitThreads"]
    assert observer is not None
