"""Missing pyobjc is a missing dependency, not a denied macOS permission.

Without the ``[macos]`` extra, Quartz, ApplicationServices and AppKit can't be
imported. The native macOS backend used to report that as "Screen Recording
is not granted" (or "Accessibility is not granted"), which sends the user to
System Settings for a permission that would not help. These tests stand in
fake framework modules, so they run on any platform and never touch a real
screen or a real permission.
"""

from __future__ import annotations

import sys
import types

import pytest

from openadapt_flow.backends.macos_backend import MacOSBackend, MacOSBackendError
from openadapt_flow.backends.remote_display import MacWindowClient, RemoteDisplayError

_PYOBJC = ("Quartz", "ApplicationServices", "AppKit")


@pytest.fixture()
def no_pyobjc(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PYOBJC:
        monkeypatch.setitem(sys.modules, name, None)


def _assert_names_the_dependency(message: str) -> None:
    assert "pyobjc" in message
    assert "openadapt-flow[macos]" in message
    assert "openadapt[macos]" in message
    assert "Screen Recording" not in message
    assert "Accessibility" not in message


def test_missing_pyobjc_is_not_reported_as_a_denied_permission(
    no_pyobjc: None,
) -> None:
    """The reported symptom: a screenshot without pyobjc said "Screen
    Recording is not granted". The error must name the missing install."""
    with pytest.raises(RemoteDisplayError) as excinfo:
        MacOSBackend(app="Finder").screenshot()
    _assert_names_the_dependency(str(excinfo.value))


def test_backend_construction_names_missing_pyobjc(no_pyobjc: None) -> None:
    from openadapt_flow.backends.remote_display import MacOSDependencyMissing

    with pytest.raises(MacOSDependencyMissing) as excinfo:
        MacOSBackend(app="TextEdit")
    assert isinstance(excinfo.value, RemoteDisplayError)
    _assert_names_the_dependency(str(excinfo.value))


@pytest.mark.parametrize(
    "method",
    [
        "capture_trusted",
        "input_trusted",
        "request_capture_access",
        "request_input_access",
    ],
)
def test_trust_checks_raise_instead_of_reporting_untrusted(
    no_pyobjc: None, method: str
) -> None:
    from openadapt_flow.backends.remote_display import MacOSDependencyMissing

    with pytest.raises(MacOSDependencyMissing) as excinfo:
        getattr(MacWindowClient(), method)()
    _assert_names_the_dependency(str(excinfo.value))


def _fake_frameworks(
    monkeypatch: pytest.MonkeyPatch, *, capture: bool, accessibility: bool
) -> None:
    quartz = types.ModuleType("Quartz")
    quartz.CGPreflightScreenCaptureAccess = lambda: capture
    services = types.ModuleType("ApplicationServices")
    services.AXIsProcessTrusted = lambda: accessibility
    appkit = types.ModuleType("AppKit")
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "ApplicationServices", services)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)


def test_denied_screen_recording_keeps_its_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_frameworks(monkeypatch, capture=False, accessibility=False)

    client = MacWindowClient()
    assert client.capture_trusted() is False
    assert client.input_trusted() is False

    backend = MacOSBackend(app="TextEdit")
    with pytest.raises(MacOSBackendError, match="Screen Recording is not granted"):
        backend.screenshot()


def test_granted_permissions_still_read_as_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_frameworks(monkeypatch, capture=True, accessibility=True)

    client = MacWindowClient()
    assert client.capture_trusted() is True
    assert client.input_trusted() is True
