"""Read-only Windows-host qualification for execution-context identity.

The normal win-agent tests inject context values so they can run on every
platform.  This file is intentionally different: the protected Windows job
executes the real logon-session probe and carries its digest through the typed
HTTP endpoint and ``WindowsBackend``.  No screenshot, UIA, or input endpoint is
called.

GitHub-hosted Windows runners do not promise which window owns foreground
focus.  The live application observation is therefore allowed to be absent,
but may only be a bounded PHI-free identifier when present.  A separate
deterministic test below exercises the exact foreground-window/process-image
Win32 call chain without depending on runner focus.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

import pytest
import requests

from openadapt_flow.backends import WindowsBackend
from openadapt_flow.backends.win_agent import AgentConfig, create_server
from openadapt_flow.backends.win_agent.server import (
    _foreground_application_identity,
    _native_session_digest,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="qualification executes the real Windows identity APIs",
)

_SESSION_RE = re.compile(r"^[a-f0-9]{64}$")
_APPLICATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def test_live_session_digest_roundtrips_through_typed_agent() -> None:
    """The live Windows session reaches clients without an expected-value echo."""

    direct = _native_session_digest()
    assert direct is not None, (
        "the Windows runner is not attached to its active interactive session"
    )
    assert _SESSION_RE.fullmatch(direct)
    assert _native_session_digest() == direct

    server = create_server(AgentConfig(host="127.0.0.1", port=0, token="context-probe"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host_value, port = server.server_address[:2]
    host = (
        host_value.decode("ascii") if isinstance(host_value, bytes) else str(host_value)
    )
    url = f"http://{host}:{port}"
    headers = {"Authorization": "Bearer context-probe"}
    try:
        response = requests.post(
            f"{url}/context/identity",
            json={},
            headers=headers,
            timeout=5,
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["session"] == direct
        assert payload["workflow_state"] is None
        assert "title" not in payload
        application = payload["application"]
        assert application is None or _APPLICATION_RE.fullmatch(application)

        backend = WindowsBackend(url, auth_token="context-probe")
        assert backend.session_identity() == direct
        observed_application = backend.application_identity()
        assert observed_application is None or _APPLICATION_RE.fullmatch(
            observed_application
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakeFunction:
    """Callable that also accepts ctypes ``argtypes``/``restype`` assignment."""

    def __init__(self, callback: Callable[..., object]) -> None:
        self._callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        return self._callback(*args)


def test_foreground_application_uses_process_image_not_window_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the exact Win32 foreground/process path without focus assumptions."""

    calls: list[tuple[str, object]] = []

    def get_foreground_window() -> int:
        calls.append(("foreground", None))
        return 101

    def get_window_thread_process_id(hwnd: Any, pid_pointer: Any) -> int:
        calls.append(("window_pid", int(hwnd)))
        ctypes.cast(pid_pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = 4242
        return 1

    def open_process(access: Any, inherit_handle: Any, pid: Any) -> int:
        calls.append(("open_process", (int(access), bool(inherit_handle), int(pid))))
        return 202

    def query_process_image(
        process: Any,
        flags: Any,
        path: Any,
        size_pointer: Any,
    ) -> int:
        calls.append(("process_image", (int(process), int(flags))))
        path.value = r"C:\Program Files\Clinic App\Accuro.EMR.exe"
        ctypes.cast(size_pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = len(
            path.value
        )
        return 1

    def close_handle(handle: Any) -> int:
        calls.append(("close", int(handle)))
        return 1

    class FakeUser32:
        GetForegroundWindow = _FakeFunction(get_foreground_window)
        GetWindowThreadProcessId = _FakeFunction(get_window_thread_process_id)

    class FakeKernel32:
        OpenProcess = _FakeFunction(open_process)
        QueryFullProcessImageNameW = _FakeFunction(query_process_image)
        CloseHandle = _FakeFunction(close_handle)

    def fake_win_dll(name: str, *, use_last_error: bool) -> object:
        assert use_last_error is True
        if name == "user32":
            return FakeUser32()
        if name == "kernel32":
            return FakeKernel32()
        raise AssertionError(f"unexpected Win32 DLL: {name}")

    monkeypatch.setattr(ctypes, "WinDLL", fake_win_dll)

    assert _foreground_application_identity() == "accuro.emr"
    assert calls == [
        ("foreground", None),
        ("window_pid", 101),
        ("open_process", (0x1000, False, 4242)),
        ("process_image", (202, 0)),
        ("close", 202),
    ]
