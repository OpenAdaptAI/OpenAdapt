"""Roundtrip tests for the in-guest win_agent server (no live VM/desktop).

The stdlib server is started on a loopback ephemeral port and exercised with
real ``requests`` calls, so the full HTTP contract WindowsBackend depends on is
proven end to end. The desktop grabber is injected (a fake PNG) so no mss / no
real desktop is needed — the suite runs on macOS/Linux CI.
"""

from __future__ import annotations

import hashlib
import struct
import sys
import threading
import time
import types
from collections.abc import Iterator

import pytest
import requests

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.backends.win_agent import AgentConfig, create_server
from openadapt_flow.backends.win_agent.server import (
    AgentRequestError,
    _perform_input,
    _perform_legacy_calls,
    _perform_uia,
    parse_legacy_command,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _fake_png() -> bytes:
    """Minimal valid-enough PNG: signature + IHDR with a 4x2 size."""
    ihdr = struct.pack(">II", 4, 2)
    return _PNG_SIGNATURE + b"\x00\x00\x00\x0dIHDR" + ihdr + b"\x00" * 8


class RunningAgent:
    """A started agent server plus its base URL (context-managed)."""

    def __init__(
        self,
        config: AgentConfig,
        grab_fn=_fake_png,
        *,
        input_fn=None,
        uia_fn=None,
        context_fn=None,
        legacy_fn=None,
    ) -> None:
        kwargs = {"grab_fn": grab_fn}
        if legacy_fn is not None:
            kwargs["legacy_fn"] = legacy_fn
        if input_fn is not None:
            kwargs["input_fn"] = input_fn
        if uia_fn is not None:
            kwargs["uia_fn"] = uia_fn
        if context_fn is not None:
            kwargs["context_fn"] = context_fn
        self.server = create_server(config, **kwargs)
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _recording_legacy_fn(sink: list):
    def legacy_fn(calls):
        sink.extend(calls)
        return {"status": "ok", "output": "", "calls": len(calls)}

    return legacy_fn


@pytest.fixture()
def agent() -> Iterator[RunningAgent]:
    calls: list = []
    a = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, allow_legacy_exec=True),
        legacy_fn=_recording_legacy_fn(calls),
    )
    a.legacy_calls = calls  # type: ignore[attr-defined]
    yield a
    a.close()


@pytest.fixture()
def authed_agent() -> Iterator[RunningAgent]:
    a = RunningAgent(
        AgentConfig(
            host="127.0.0.1",
            port=0,
            token="s3cret",
            allow_legacy_exec=True,
        ),
        legacy_fn=_recording_legacy_fn([]),
    )
    yield a
    a.close()


@pytest.fixture()
def typed_agent() -> Iterator[RunningAgent]:
    def input_fn(payload):
        return {
            "status": "delivered",
            "receipt_id": "input-1",
            "operation": f"physical_{payload['action']}",
            "native": False,
            "target_fingerprint": None,
            "delivered_at": "2026-07-17T00:00:00+00:00",
            "outcome_verified": False,
        }

    def uia_fn(operation, payload):
        if operation == "focused-at-point":
            return {"status": "ok", "focused": True}
        if operation == "find":
            return {
                "status": "ok",
                "match": "ambiguous",
                "candidate_count": 2,
                "truncated": False,
                "candidates": [{"fingerprint": "a" * 64}, {"fingerprint": "b" * 64}],
            }
        if operation == "act":
            return {
                "status": "ok",
                "candidate_count": 1,
                "receipt": {
                    "status": "delivered",
                    "receipt_id": "uia-1",
                    "operation": "uia_invoke",
                    "native": True,
                    "target_fingerprint": "a" * 64,
                    "delivered_at": "2026-07-17T00:00:00+00:00",
                    "outcome_verified": False,
                },
            }
        return {"status": "ok", "locator": None, "text": None}

    a = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        input_fn=input_fn,
        uia_fn=uia_fn,
    )
    yield a
    a.close()


# -- health -------------------------------------------------------------------


def test_health_ok_and_unauthenticated(agent: RunningAgent) -> None:
    r = requests.get(f"{agent.url}/health", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["auth_required"] is False


def test_default_agent_disables_arbitrary_exec_and_advertises_typed_contract(
    typed_agent: RunningAgent,
) -> None:
    health = requests.get(f"{typed_agent.url}/health", timeout=5).json()
    assert "context_identity_v1" in health["capabilities"]
    assert "typed_input_v1" in health["capabilities"]
    assert "uia_v1" in health["capabilities"]
    assert "legacy_exec" not in health["capabilities"]
    assert "legacy_waa_input_v1" not in health["capabilities"]
    response = requests.post(
        f"{typed_agent.url}/execute_windows",
        json={"command": "print('must not execute')"},
        timeout=5,
    )
    assert response.status_code == 404


def test_typed_input_and_uia_receipts_never_claim_outcome(
    typed_agent: RunningAgent,
) -> None:
    context = requests.post(
        f"{typed_agent.url}/context/identity",
        json={},
        timeout=5,
    )
    assert context.status_code == 200
    assert set(context.json()) == {
        "status",
        "application",
        "application_version",
        "session",
        "environment",
        "workflow_state",
    }
    assert "title" not in context.text.casefold()
    expected_echo = requests.post(
        f"{typed_agent.url}/context/identity",
        json={"expected": "accuro"},
        timeout=5,
    )
    assert expected_echo.status_code == 400

    delivered = requests.post(
        f"{typed_agent.url}/input",
        json={"action": "click", "x": 1, "y": 2, "double": False},
        timeout=5,
    )
    assert delivered.status_code == 200
    assert delivered.json()["outcome_verified"] is False

    found = requests.post(
        f"{typed_agent.url}/uia/find",
        json={"locator": {"automation_id": "duplicate"}},
        timeout=5,
    ).json()
    assert found["match"] == "ambiguous"
    assert found["candidate_count"] == 2

    acted = requests.post(
        f"{typed_agent.url}/uia/act",
        json={
            "locator": {"automation_id": "save"},
            "expected_fingerprint": "a" * 64,
            "operation": "click",
        },
        timeout=5,
    ).json()
    assert acted["receipt"]["native"] is True
    assert acted["receipt"]["outcome_verified"] is False


def test_windows_backend_guarded_key_roundtrip(typed_agent: RunningAgent) -> None:
    from openadapt_flow.backends import WindowsBackend

    backend = WindowsBackend(typed_agent.url, viewport=(4, 2))
    frame = backend.guarded_keyboard_frame()
    backend.arm_guarded_keyboard(1, 1)
    receipt = backend.press_guarded(
        "Enter",
        expected_frame_sha256=hashlib.sha256(frame).hexdigest(),
    )

    assert receipt.operation == "physical_press"
    assert receipt.outcome_verified is False


@pytest.mark.parametrize("mutation", ["frame", "context", "focus"])
def test_guarded_input_refuses_post_identity_change(mutation: str) -> None:
    state = {
        "frame": _fake_png(),
        "application": "accuro",
        "focused": True,
    }
    delivered: list[dict] = []

    def grab_fn():
        return state["frame"]

    def input_fn(payload):
        delivered.append(payload)
        return {
            "status": "delivered",
            "receipt_id": "guarded-1",
            "operation": "physical_press",
            "native": False,
            "target_fingerprint": None,
            "delivered_at": "2026-07-25T00:00:00+00:00",
            "outcome_verified": False,
        }

    def context_fn():
        return {
            "status": "ok",
            "application": state["application"],
            "session": "a" * 64,
            "workflow_state": None,
        }

    def uia_fn(operation, payload):
        assert operation == "focused-at-point"
        return {"status": "ok", "focused": state["focused"]}

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=grab_fn,
        input_fn=input_fn,
        uia_fn=uia_fn,
        context_fn=context_fn,
    )
    try:
        expected_frame = hashlib.sha256(state["frame"]).hexdigest()
        if mutation == "frame":
            state["frame"] += b"changed"
        elif mutation == "context":
            state["application"] = "other-app"
        else:
            state["focused"] = False
        response = requests.post(
            f"{agent.url}/input/guarded",
            json={
                "expected_frame_sha256": expected_frame,
                "expected_context": {
                    "application": "accuro",
                    "session": "a" * 64,
                    "workflow_state": None,
                },
                "focus_point": {"x": 1, "y": 1},
                "input": {"action": "press", "keys": ["enter"]},
            },
            timeout=5,
        )
    finally:
        agent.close()

    assert response.status_code == 409
    assert delivered == []


def test_invalid_input_schema_refuses_before_loading_pyautogui(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pyautogui", None)
    with pytest.raises(AgentRequestError) as caught:
        _perform_input({"action": "click", "x": 1, "y": 2, "unknown": True})
    assert caught.value.status == 400
    assert caught.value.code == "invalid_schema"


class _FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _FakeControl:
    def __init__(
        self,
        control_type: str,
        *,
        automation_id: str = "",
        name: str = "",
        runtime_id: tuple[int, ...] = (),
        bounds: tuple[int, int, int, int] = (0, 0, 100, 40),
        parent=None,
    ) -> None:
        self.ControlTypeName = control_type
        self.AutomationId = automation_id
        self.Name = name
        self.ClassName = "WindowsForms10.TEST"
        self.ProcessId = 1234
        self.NativeWindowHandle = 0
        self.BoundingRectangle = _FakeRect(*bounds)
        self.runtime_id = runtime_id
        self.parent = parent
        self.children = []
        self.invocations = 0

    def GetRuntimeId(self):
        return list(self.runtime_id)

    def GetParentControl(self):
        return self.parent

    def GetChildren(self):
        return self.children

    def GetInvokePattern(self):
        return self

    def Invoke(self):
        self.invocations += 1


class _FakeUiaContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _fake_uia_module(*controls: _FakeControl):
    root = _FakeControl("PaneControl")
    window = _FakeControl(
        "WindowControl",
        name="Patient Notes",
        runtime_id=(42, 1),
        bounds=(0, 0, 800, 600),
        parent=root,
    )
    root.children = [window]
    window.children = list(controls)
    for control in controls:
        control.parent = window
    return types.SimpleNamespace(
        GetRootControl=lambda: root,
        ControlFromPoint=lambda _x, _y: controls[0] if controls else None,
        UIAutomationInitializerInThread=_FakeUiaContext,
    )


def test_real_uia_contract_refuses_stale_target_before_native_action(
    monkeypatch,
) -> None:
    button = _FakeControl(
        "ButtonControl",
        automation_id="saveButton",
        name="Save Note",
        runtime_id=(42, 99),
        bounds=(500, 450, 600, 484),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))
    locator = {
        "automation_id": "saveButton",
        "role": "button",
        "name": "Save Note",
        "window_name": "Patient Notes",
    }
    found = _perform_uia("find", {"locator": locator})
    assert found["match"] == "unique"
    fingerprint = found["candidates"][0]["fingerprint"]

    button.runtime_id = (42, 100)  # same locator, replaced live element
    with pytest.raises(AgentRequestError) as caught:
        _perform_uia(
            "act",
            {
                "locator": locator,
                "operation": "click",
                "expected_fingerprint": fingerprint,
            },
        )
    assert caught.value.status == 409
    assert caught.value.code == "stale_target"
    assert button.invocations == 0


def test_real_uia_contract_refuses_duplicate_candidates_without_action(
    monkeypatch,
) -> None:
    controls = [
        _FakeControl(
            "ButtonControl",
            automation_id="saveButton",
            name="Save Note",
            runtime_id=(42, index),
            bounds=(500, 450 + index * 50, 600, 484 + index * 50),
        )
        for index in (1, 2)
    ]
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(*controls))
    locator = {"automation_id": "saveButton", "window_name": "Patient Notes"}
    found = _perform_uia("find", {"locator": locator})
    assert found["match"] == "ambiguous"
    assert found["candidate_count"] == 2
    with pytest.raises(AgentRequestError) as caught:
        _perform_uia(
            "act",
            {
                "locator": locator,
                "operation": "click",
                "expected_fingerprint": found["candidates"][0]["fingerprint"],
            },
        )
    assert caught.value.code == "ambiguous_target"
    assert [control.invocations for control in controls] == [0, 0]


def test_health_open_even_when_token_set(authed_agent: RunningAgent) -> None:
    # Liveness must not require the token (no desktop bytes, no exec).
    r = requests.get(f"{authed_agent.url}/health", timeout=5)
    assert r.status_code == 200
    assert r.json()["auth_required"] is True


def test_windows_context_identity_is_live_bounded_and_authenticated() -> None:
    state = {"application": "accuro", "session": "a" * 64}

    def context_fn():
        return {
            "status": "ok",
            **state,
            "workflow_state": None,
        }

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, token="secret"),
        context_fn=context_fn,
    )
    try:
        from openadapt_flow.backends import WindowsBackend

        backend = WindowsBackend(agent.url, auth_token="secret")
        assert backend.application_identity() == "accuro"
        assert backend.session_identity() == "a" * 64
        assert backend.workflow_state_identity() is None

        state.update(application="unrelated-app", session="b" * 64)
        assert backend.application_identity() == "unrelated-app"
        assert backend.session_identity() == "b" * 64

        state.update(application="Patient Alice Example", session="NOT-A-DIGEST")
        assert backend.application_identity() is None
        assert backend.session_identity() is None

        unauthenticated = WindowsBackend(agent.url)
        assert unauthenticated.application_identity() is None
        assert unauthenticated.session_identity() is None
    finally:
        agent.close()


def test_windows_qualification_environment_is_one_agent_snapshot() -> None:
    """A qualification case on Windows observes all four signals in one call."""

    from openadapt_flow.backends import WindowsBackend
    from openadapt_flow.qualification_environment import (
        BackendQualificationEnvironmentObserver,
    )

    state = {
        "application": "patient-notes",
        "application_version": "sha256:" + "c" * 64,
        "session": "a" * 64,
        "environment": "e" * 64,
    }
    calls = []

    def context_fn():
        calls.append(1)
        return {"status": "ok", **state, "workflow_state": None}

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, token="secret"),
        context_fn=context_fn,
    )
    try:
        backend = WindowsBackend(agent.url, auth_token="secret")
        assert backend.qualification_environment_identity() == (
            "patient-notes",
            "sha256:" + "c" * 64,
            "a" * 64,
            "e" * 64,
        )
        assert len(calls) == 1
        observed = BackendQualificationEnvironmentObserver(backend).observe(
            backend, "windows"
        )
        assert observed.application_version == "sha256:" + "c" * 64
        assert observed.environment_digest == "e" * 64

        # Every signal must be present and well formed, or the case refuses.
        for key, bad in (
            ("environment", None),
            ("application_version", "1.2.3"),
            ("application_version", None),
            ("session", "NOT-A-DIGEST"),
            ("application", "Patient Alice Example"),
        ):
            saved = state[key]
            state[key] = bad
            assert backend.qualification_environment_identity() is None, key
            state[key] = saved

        # An older agent without the environment fields cannot qualify.
        del state["environment"], state["application_version"]
        assert backend.qualification_environment_identity() is None
        assert WindowsBackend(agent.url).qualification_environment_identity() is None
    finally:
        agent.close()


def test_agent_hashes_the_executable_file_and_notices_a_change(tmp_path) -> None:
    from openadapt_flow.backends.win_agent import server

    image = tmp_path / "app.exe"
    image.write_bytes(b"first build")
    first = server._image_file_digest(str(image))
    assert first == "sha256:" + hashlib.sha256(b"first build").hexdigest()
    image.write_bytes(b"second build, longer")
    second = server._image_file_digest(str(image))
    assert second == "sha256:" + hashlib.sha256(b"second build, longer").hexdigest()
    assert server._image_file_digest(str(tmp_path / "missing.exe")) is None


def test_agent_rehashes_a_same_size_file_replaced_with_the_old_times(tmp_path) -> None:
    import os

    from openadapt_flow.backends.win_agent import server

    image = tmp_path / "app.exe"
    image.write_bytes(b"build-A")
    first = server._image_file_digest(str(image))
    stat = image.stat()
    staged = tmp_path / "staged.exe"
    staged.write_bytes(b"build-B")
    os.replace(staged, image)
    os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = server._image_file_digest(str(image))
    assert second == "sha256:" + hashlib.sha256(b"build-B").hexdigest()
    assert second != first


def test_agent_refuses_a_file_changed_after_the_process_started(
    tmp_path, monkeypatch
) -> None:
    from openadapt_flow.backends.win_agent import server

    image = tmp_path / "app.exe"
    image.write_bytes(b"running build")
    changed = max(image.stat().st_mtime_ns, image.stat().st_ctime_ns)
    monkeypatch.setattr(server, "_process_start_ns", lambda pid: changed + 1)
    assert server._running_image_digest(42, str(image)) is not None
    monkeypatch.setattr(server, "_process_start_ns", lambda pid: changed - 1)
    assert server._running_image_digest(42, str(image)) is None
    monkeypatch.setattr(server, "_process_start_ns", lambda pid: None)
    assert server._running_image_digest(42, str(image)) is None


def test_guarded_input_binds_the_executable_digest_and_environment() -> None:
    from openadapt_flow.backends import WindowsBackend

    state = {
        "application": "patient-notes",
        "application_version": "sha256:" + "c" * 64,
        "session": "a" * 64,
        "environment": "e" * 64,
    }
    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        context_fn=lambda: {"status": "ok", **state, "workflow_state": None},
    )
    try:
        backend = WindowsBackend(agent.url)
        context = backend._guard_context()
        assert context["application_version"] == "sha256:" + "c" * 64
        assert context["environment"] == "e" * 64
        state["application_version"] = "not-a-digest"
        with pytest.raises(StructuralResolutionRefused):
            backend._guard_context()
        del state["application_version"], state["environment"]
        assert "application_version" not in backend._guard_context()
    finally:
        agent.close()


def test_guarded_input_refuses_when_the_executable_digest_changes() -> None:
    """A guarded POST carries the digest; a different executable is refused."""

    from openadapt_flow.backends import WindowsBackend

    frame = _fake_png()
    state = {
        "application": "patient-notes",
        "application_version": "sha256:" + "c" * 64,
        "session": "a" * 64,
        "environment": "e" * 64,
    }
    delivered = []

    def input_fn(payload):
        delivered.append(payload)
        return {
            "status": "delivered",
            "receipt_id": "guarded-env",
            "operation": "physical_click",
            "native": False,
            "target_fingerprint": None,
            "delivered_at": "2026-11-03T00:00:00+00:00",
            "outcome_verified": False,
        }

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=lambda: frame,
        input_fn=input_fn,
        context_fn=lambda: {"status": "ok", **state, "workflow_state": None},
    )
    try:
        backend = WindowsBackend(agent.url)
        expected = backend._guard_context()
        body = {
            "expected_frame_sha256": hashlib.sha256(frame).hexdigest(),
            "expected_context": expected,
            "input": {"action": "click", "x": 1, "y": 1},
        }
        ok = requests.post(f"{agent.url}/input/guarded", json=body, timeout=5)
        assert ok.status_code == 200, ok.text
        assert len(delivered) == 1
        state["application_version"] = "sha256:" + "d" * 64
        moved = requests.post(f"{agent.url}/input/guarded", json=body, timeout=5)
        assert moved.status_code == 409
        assert moved.json()["code"] == "stale_context"
        assert len(delivered) == 1
    finally:
        agent.close()


def test_agent_advertises_the_environment_context() -> None:
    agent = RunningAgent(AgentConfig(host="127.0.0.1", port=0))
    try:
        health = requests.get(f"{agent.url}/health", timeout=5).json()
    finally:
        agent.close()
    assert "context_environment_v1" in health["capabilities"]


# -- screenshot ---------------------------------------------------------------


def test_screenshot_returns_raw_png(agent: RunningAgent) -> None:
    r = requests.get(f"{agent.url}/screenshot", timeout=5)
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "image/png"
    assert r.content.startswith(_PNG_SIGNATURE)


def test_screenshot_500_when_grabber_not_png() -> None:
    a = RunningAgent(AgentConfig(port=0), grab_fn=lambda: b"not a png")
    try:
        r = requests.get(f"{a.url}/screenshot", timeout=5)
        assert r.status_code == 500
        assert r.json()["status"] == "error"
    finally:
        a.close()


def test_screenshot_500_when_grabber_raises() -> None:
    def boom() -> bytes:
        raise RuntimeError("no desktop")

    a = RunningAgent(AgentConfig(port=0), grab_fn=boom)
    try:
        r = requests.get(f"{a.url}/screenshot", timeout=5)
        assert r.status_code == 500
        assert "no desktop" in r.json()["error"]
    finally:
        a.close()


# -- execute_windows ----------------------------------------------------------


_CLICK = "import pyautogui; pyautogui.click(10, 20)"


def test_legacy_route_advertised_only_when_enabled(agent: RunningAgent) -> None:
    health = requests.get(f"{agent.url}/health", timeout=5).json()
    assert "legacy_waa_input_v1" in health["capabilities"]
    assert "legacy_exec" not in health["capabilities"]


def test_execute_windows_dispatches_allow_listed_pyautogui_calls(
    agent: RunningAgent,
) -> None:
    r = requests.post(
        f"{agent.url}/execute_windows",
        json={
            "command": (
                "import pyautogui; pyautogui.moveTo(1, 2); "
                "pyautogui.mouseDown(button='left'); "
                "pyautogui.moveTo(30, 40, duration=0.2); "
                "pyautogui.mouseUp(button='left')"
            )
        },
        timeout=5,
    )
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "output": "", "calls": 4}
    assert agent.legacy_calls == [  # type: ignore[attr-defined]
        ("moveTo", (1, 2), {}),
        ("mouseDown", (), {"button": "left"}),
        ("moveTo", (30, 40), {"duration": 0.2}),
        ("mouseUp", (), {"button": "left"}),
    ]


@pytest.mark.parametrize(
    "command",
    [
        "print('<<OAFLOW_STRUCTURED>>42<<END_OAFLOW_STRUCTURED>>')",
        "raise ValueError('boom')",
        "import os; os.system('whoami')",
        "import subprocess",
        "import pyautogui as p; p.click(1, 1)",
        "from pyautogui import click; click(1, 1)",
        "__import__('os').system('whoami')",
        "pyautogui.click(1, 1).__class__",
        "pyautogui.screenshot('C:/out.png')",
        "pyautogui.click(*[1, 1])",
        "pyautogui.click(**{'x': 1})",
        "pyautogui.click(x=__import__('os').getpid())",
        "pyautogui.click(1 + 1, 2)",
        "pyautogui.click(logScreenshot=True)",
        "pyautogui.write(open('C:/secret.txt').read())",
        "x = 1",
        "for _ in range(3): pyautogui.click(1, 1)",
        "pyautogui.os.system('whoami')",
        "import pyautogui",
        "",
        "pyautogui.click(1, 1",
    ],
)
def test_execute_windows_refuses_anything_but_literal_input_calls(
    agent: RunningAgent, command: str
) -> None:
    r = requests.post(
        f"{agent.url}/execute_windows", json={"command": command}, timeout=5
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "unsupported_legacy_command"
    assert "trace" not in body
    assert agent.legacy_calls == []  # type: ignore[attr-defined]


def test_execute_windows_never_executes_client_code(
    agent: RunningAgent, tmp_path, monkeypatch
) -> None:
    import builtins

    def forbidden(*_args, **_kwargs):
        raise AssertionError("client text reached exec/eval")

    monkeypatch.setattr(builtins, "exec", forbidden)
    monkeypatch.setattr(builtins, "eval", forbidden)
    marker = tmp_path / "pwned"
    payload = f"open({str(marker)!r}, 'w').write('x')"
    r = requests.post(
        f"{agent.url}/execute_windows", json={"command": payload}, timeout=5
    )
    assert r.status_code == 400
    assert not marker.exists()
    ok = requests.post(
        f"{agent.url}/execute_windows", json={"command": _CLICK}, timeout=5
    )
    assert ok.status_code == 200


def test_execute_windows_413_on_oversized_command(agent: RunningAgent) -> None:
    command = "import pyautogui; pyautogui.write(" + repr("a" * 20_000) + ")"
    r = requests.post(
        f"{agent.url}/execute_windows", json={"command": command}, timeout=5
    )
    assert r.status_code == 413
    assert r.json()["code"] == "legacy_command_too_large"


def test_execute_windows_500_without_traceback_when_input_fails() -> None:
    def boom(calls):
        raise OSError("SendInput failed")

    a = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, allow_legacy_exec=True), legacy_fn=boom
    )
    try:
        r = requests.post(
            f"{a.url}/execute_windows", json={"command": _CLICK}, timeout=5
        )
    finally:
        a.close()
    # A failing input must surface as an ERROR (non-200), never a silent no-op,
    # and must not leak a traceback to the network client.
    assert r.status_code == 500
    body = r.json()
    assert body == {
        "status": "error",
        "code": "operation_failed",
        "error": "legacy input operation failed",
    }


def test_parse_accepts_every_command_shape_windows_backend_emits() -> None:
    # The exact strings WindowsBackend's legacy fallback builds.
    assert parse_legacy_command("import pyautogui; pyautogui.doubleClick(3, 4)") == [
        ("doubleClick", (3, 4), {})
    ]
    assert parse_legacy_command(
        "import pyautogui; pyautogui.click(3, 4, button='right')"
    ) == [("click", (3, 4), {"button": "right"})]
    assert parse_legacy_command(
        "import pyautogui; pyautogui.write('hi there', interval=0.01)"
    ) == [("write", ("hi there",), {"interval": 0.01})]
    assert parse_legacy_command("import pyautogui; pyautogui.press('enter')") == [
        ("press", ("enter",), {})
    ]
    assert parse_legacy_command("import pyautogui; pyautogui.hotkey('ctrl', 'a')") == [
        ("hotkey", ("ctrl", "a"), {})
    ]
    assert parse_legacy_command(
        "import pyautogui; pyautogui.scroll(-3); pyautogui.hscroll(2)"
    ) == [("scroll", (-3,), {}), ("hscroll", (2,), {})]


@pytest.mark.parametrize("value", ["1e400", "10**9", "2_000_000", "b'x'", "1j"])
def test_parse_rejects_out_of_range_or_non_plain_literals(value: str) -> None:
    with pytest.raises(AgentRequestError) as exc:
        parse_legacy_command(f"import pyautogui; pyautogui.scroll({value})")
    assert exc.value.code == "unsupported_legacy_command"


def test_default_dispatch_calls_only_the_named_pyautogui_function(monkeypatch) -> None:
    seen: list = []
    fake = types.ModuleType("pyautogui")
    fake.FAILSAFE = True  # type: ignore[attr-defined]
    fake.click = lambda *a, **k: seen.append(("click", a, k))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    result = _perform_legacy_calls(parse_legacy_command(_CLICK))
    assert result == {"status": "ok", "output": "", "calls": 1}
    assert seen == [("click", (10, 20), {})]
    assert fake.FAILSAFE is False  # type: ignore[attr-defined]
    with pytest.raises(AgentRequestError):
        _perform_legacy_calls([("screenshot", (), {})])


def test_execute_windows_400_on_non_string_command(agent: RunningAgent) -> None:
    r = requests.post(f"{agent.url}/execute_windows", json={"command": 123}, timeout=5)
    assert r.status_code == 400


def test_execute_windows_400_on_bad_json(agent: RunningAgent) -> None:
    r = requests.post(
        f"{agent.url}/execute_windows",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        timeout=5,
    )
    assert r.status_code == 400


# -- auth ---------------------------------------------------------------------


def test_execute_requires_token_when_configured(authed_agent: RunningAgent) -> None:
    # No header -> 401.
    r = requests.post(
        f"{authed_agent.url}/execute_windows",
        json={"command": _CLICK},
        timeout=5,
    )
    assert r.status_code == 401
    # Wrong token -> 401.
    r = requests.post(
        f"{authed_agent.url}/execute_windows",
        json={"command": _CLICK},
        headers={"Authorization": "Bearer wrong"},
        timeout=5,
    )
    assert r.status_code == 401
    # Correct token -> 200.
    r = requests.post(
        f"{authed_agent.url}/execute_windows",
        json={"command": _CLICK},
        headers={"Authorization": "Bearer s3cret"},
        timeout=5,
    )
    assert r.status_code == 200


def test_screenshot_requires_token_when_configured(authed_agent: RunningAgent) -> None:
    r = requests.get(f"{authed_agent.url}/screenshot", timeout=5)
    assert r.status_code == 401
    r = requests.get(
        f"{authed_agent.url}/screenshot",
        headers={"Authorization": "Bearer s3cret"},
        timeout=5,
    )
    assert r.status_code == 200


def test_windows_backend_talks_to_authed_agent(authed_agent: RunningAgent) -> None:
    # The real WindowsBackend, with the matching token, drives the agent.
    from openadapt_flow.backends import WindowsBackend

    backend = WindowsBackend(authed_agent.url, auth_token="s3cret")
    assert backend.probe() is True
    # Without the token, the action path fails loudly (never a silent no-op).
    unauth = WindowsBackend(authed_agent.url)
    assert unauth.probe() is False
    with pytest.raises(RuntimeError):
        unauth.click(1, 1)


def test_element_identity_names_the_element_and_its_window_path(monkeypatch) -> None:
    button = _FakeControl(
        "ButtonControl", automation_id="saveButton", name="Save  Note"
    )
    group = _FakeControl("PaneControl", name="Note editor")
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(group))
    group.children = [button]
    button.parent = group
    module = sys.modules["uiautomation"]
    module.ControlFromPoint = lambda _x, _y: button

    result = _perform_uia("element-identity-at", {"x": 550, "y": 460})

    assert result == {
        "status": "ok",
        "identity": {
            "source": "uia",
            "role": "button",
            "name": "Save Note",
            "automation_id": "saveButton",
            "ancestry": [[None, "Note editor"], [None, "Patient Notes"]],
            "window": "Patient Notes",
            "process": None,
        },
    }


def test_element_identity_climbs_to_the_actionable_control(monkeypatch) -> None:
    button = _FakeControl("ButtonControl", name="Save Note")
    label = _FakeControl("TextControl", name="Save Note", parent=button)
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))
    sys.modules["uiautomation"].ControlFromPoint = lambda _x, _y: label

    identity = _perform_uia("element-identity-at", {"x": 1, "y": 1})["identity"]

    assert identity["role"] == "button"
    assert identity["automation_id"] is None
    assert identity["ancestry"] == [[None, "Patient Notes"]]


def test_element_identity_without_a_window_is_none(monkeypatch) -> None:
    orphan = _FakeControl("ButtonControl", name="Save Note")
    orphan.parent = _FakeControl("PaneControl")
    monkeypatch.setitem(
        sys.modules,
        "uiautomation",
        types.SimpleNamespace(
            ControlFromPoint=lambda _x, _y: orphan,
            UIAutomationInitializerInThread=_FakeUiaContext,
        ),
    )
    assert _perform_uia("element-identity-at", {"x": 1, "y": 1}) == {
        "status": "ok",
        "identity": None,
    }


def test_element_identity_with_a_cyclic_parent_chain_is_none(monkeypatch) -> None:
    button = _FakeControl("ButtonControl", name="Save Note")
    window = _FakeControl("WindowControl", name="Patient Notes", parent=None)
    window.parent = window  # a provider that never reaches the root
    button.parent = window
    monkeypatch.setitem(
        sys.modules,
        "uiautomation",
        types.SimpleNamespace(
            ControlFromPoint=lambda _x, _y: button,
            UIAutomationInitializerInThread=_FakeUiaContext,
        ),
    )
    assert _perform_uia("element-identity-at", {"x": 1, "y": 1})["identity"] is None


def test_element_identity_with_nothing_under_the_point_is_none(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module())
    assert _perform_uia("element-identity-at", {"x": 1, "y": 1}) == {
        "status": "ok",
        "identity": None,
    }


def test_element_identity_refuses_extra_fields(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module())
    with pytest.raises(AgentRequestError) as caught:
        _perform_uia("element-identity-at", {"x": 1, "y": 1, "depth": 3})
    assert caught.value.status == 400


def _identity_with_leaf(monkeypatch, leaf: _FakeControl, *ancestors: _FakeControl):
    """Run element-identity-at for ``leaf`` under ancestors (parent-first)."""
    chain = [leaf, *ancestors]
    for child, parent in zip(chain, chain[1:]):
        child.parent = parent
    monkeypatch.setitem(
        sys.modules,
        "uiautomation",
        types.SimpleNamespace(
            ControlFromPoint=lambda _x, _y: leaf,
            UIAutomationInitializerInThread=_FakeUiaContext,
        ),
    )
    return _perform_uia("element-identity-at", {"x": 1, "y": 1})["identity"]


def test_element_identity_of_an_unnamed_custom_drawn_cell_is_none(monkeypatch) -> None:
    # Every cell of an owner-drawn grid would otherwise read the same.
    cell = _FakeControl("CustomControl")
    window = _FakeControl("WindowControl", name="Schedule")
    assert (
        _identity_with_leaf(monkeypatch, cell, window, _FakeControl("PaneControl"))
        is None
    )


def test_element_identity_names_the_outermost_window_of_nested_windows(
    monkeypatch,
) -> None:
    button = _FakeControl("ButtonControl", name="OK")
    dialog = _FakeControl("WindowControl", name="Confirm delete")
    owner = _FakeControl("WindowControl", name="Patient chart")
    identity = _identity_with_leaf(
        monkeypatch,
        button,
        dialog,
        owner,
        _FakeControl("PaneControl", name="Desktop 1"),
    )
    assert identity["window"] == "Patient chart"
    assert identity["ancestry"] == [[None, "Confirm delete"], [None, "Patient chart"]]


def test_element_identity_when_a_parent_read_fails_is_none(monkeypatch) -> None:
    class Faulty(_FakeControl):
        def GetParentControl(self):
            raise OSError("COM provider went away")

    button = _FakeControl("ButtonControl", name="Save Note")
    inner = Faulty("WindowControl", name="Note editor")
    # Without the fault guard the inner window would pass as top level.
    assert _identity_with_leaf(monkeypatch, button, inner) is None


def test_element_identity_whose_chain_stops_at_a_window_is_none(monkeypatch) -> None:
    button = _FakeControl("ButtonControl", name="Save Note")
    window = _FakeControl("WindowControl", name="Patient Notes")
    assert _identity_with_leaf(monkeypatch, button, window) is None


def test_element_identity_with_an_overlong_name_is_none(monkeypatch) -> None:
    row = _FakeControl("ListItemControl", name="a" * 513)
    window = _FakeControl("WindowControl", name="Notes")
    root = _FakeControl("PaneControl")
    assert _identity_with_leaf(monkeypatch, row, window, root) is None
    row.Name = "a" * 512
    assert _identity_with_leaf(monkeypatch, row, window, root)["name"] == "a" * 512


def test_element_identity_round_trips_through_the_agent_and_backend(
    monkeypatch,
) -> None:
    from openadapt_flow.backends.windows_backend import WindowsBackend

    button = _FakeControl("ButtonControl", automation_id="saveButton", name="Save")
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))
    agent = RunningAgent(AgentConfig(host="127.0.0.1", port=0, token="secret"))
    try:
        backend = WindowsBackend(agent.url, auth_token="secret")
        assert backend.element_identity_at(550, 460) == {
            "source": "uia",
            "role": "button",
            "name": "Save",
            "automation_id": "saveButton",
            "ancestry": [[None, "Patient Notes"]],
            "window": "Patient Notes",
            "process": None,
        }
        assert WindowsBackend(agent.url).element_identity_at(550, 460) is None
    finally:
        agent.close()


def _save_button(**kwargs) -> _FakeControl:
    return _FakeControl(
        "ButtonControl",
        automation_id="saveButton",
        name="Save Note",
        runtime_id=(42, 99),
        bounds=(500, 450, 600, 484),
        **kwargs,
    )


_SAVE_LOCATOR = {
    "automation_id": "saveButton",
    "role": "button",
    "name": "Save Note",
    "window_name": "Patient Notes",
}


def _act_click(fingerprint: str) -> dict:
    return _perform_uia(
        "act",
        {
            "locator": _SAVE_LOCATOR,
            "operation": "click",
            "expected_fingerprint": fingerprint,
        },
    )


def test_native_invoke_on_the_visible_target_is_delivered(monkeypatch) -> None:
    button = _save_button()
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))
    found = _perform_uia("find", {"locator": _SAVE_LOCATOR})
    result = _act_click(found["candidates"][0]["fingerprint"])
    assert result["receipt"]["operation"] == "uia_invoke"
    assert button.invocations == 1


def test_native_action_on_a_covered_target_is_refused(monkeypatch) -> None:
    # Real lab finding: UIA still finds a button that another app's window
    # (Notepad, the Start menu) or a modal dialog covers.
    button = _save_button()
    module = _fake_uia_module(button)
    cover = _FakeControl("EditControl", name="Text editor", runtime_id=(77, 1))
    module.ControlFromPoint = lambda _x, _y: cover
    monkeypatch.setitem(sys.modules, "uiautomation", module)
    found = _perform_uia("find", {"locator": _SAVE_LOCATOR})
    with pytest.raises(AgentRequestError) as caught:
        _act_click(found["candidates"][0]["fingerprint"])
    assert caught.value.code == "target_obscured"
    assert button.invocations == 0


def test_a_hit_on_the_targets_own_text_counts_as_the_target(monkeypatch) -> None:
    button = _save_button()
    module = _fake_uia_module(button)
    label = _FakeControl("TextControl", name="Save Note", runtime_id=(42, 100))
    label.parent = button
    module.ControlFromPoint = lambda _x, _y: label
    monkeypatch.setitem(sys.modules, "uiautomation", module)
    found = _perform_uia("find", {"locator": _SAVE_LOCATOR})
    _act_click(found["candidates"][0]["fingerprint"])
    assert button.invocations == 1


def test_a_target_replaced_before_the_worker_resolves_it_is_refused(
    monkeypatch,
) -> None:
    button = _save_button()
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))
    fingerprint = _perform_uia("find", {"locator": _SAVE_LOCATOR})["candidates"][0][
        "fingerprint"
    ]
    import openadapt_flow.backends.win_agent.server as agent_server

    original = agent_server._find_candidates
    calls = {"n": 0}

    def find_then_replace(locator, auto):
        calls["n"] += 1
        if calls["n"] == 2:  # the worker's own resolution
            button.runtime_id = (42, 100)
        return original(locator, auto)

    monkeypatch.setattr(agent_server, "_find_candidates", find_then_replace)
    with pytest.raises(AgentRequestError) as caught:
        _act_click(fingerprint)
    assert caught.value.code == "stale_target"
    assert button.invocations == 0


class _FakeUser32:
    """The user32 surface the native-action guards read, scripted."""

    def __init__(self, hwnd: int = 0x1234, root: int = 0x1000) -> None:
        self.hwnd, self.root = hwnd, root
        self.windows = {hwnd, root}
        self.enabled = {hwnd: True, root: True}
        self.visible = {hwnd: True}
        self.classes = {hwnd: "WindowsForms10.BUTTON.app.0.2bf8098_r6_ad1"}
        self.under = hwnd
        self.foreground_window = root
        self.post_ok = True
        self.posted: list[tuple[int, int]] = []

    def is_window(self, hwnd):
        return hwnd in self.windows

    def is_enabled(self, hwnd):
        return self.enabled.get(hwnd, False)

    def is_visible(self, hwnd):
        return self.visible.get(hwnd, False)

    def ancestor(self, hwnd, flag):
        return self.root if hwnd in (self.hwnd, self.root) else hwnd

    def foreground(self):
        return self.foreground_window

    def window_from_point(self, x, y):
        return self.under

    def class_name(self, hwnd):
        return self.classes.get(hwnd, "")

    def post(self, hwnd, message):
        if self.post_ok:
            self.posted.append((hwnd, message))
        return self.post_ok


def _on_windows(monkeypatch, user32: _FakeUser32, button: _FakeControl) -> None:
    import openadapt_flow.backends.win_agent.server as agent_server

    button.NativeWindowHandle = user32.hwnd
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))
    client = types.SimpleNamespace(gen_dir=None)
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    monkeypatch.setitem(sys.modules, "comtypes", types.SimpleNamespace(client=client))
    monkeypatch.setattr(agent_server.os, "name", "nt")
    monkeypatch.setattr(agent_server, "_user32_factory", lambda: user32)


def _fingerprint() -> str:
    return _perform_uia("find", {"locator": _SAVE_LOCATOR})["candidates"][0][
        "fingerprint"
    ]


def test_a_push_button_is_clicked_by_a_posted_bm_click(monkeypatch) -> None:
    # Real lab finding: an Invoke that opens a modal dialog stays open inside
    # the app, which then answers no other UI Automation call.
    user32, button = _FakeUser32(), _save_button()
    _on_windows(monkeypatch, user32, button)
    result = _act_click(_fingerprint())
    assert result["receipt"]["operation"] == "win32_bm_click"
    assert user32.posted == [(0x1234, 0x00F5)]
    assert button.invocations == 0


def test_a_window_that_is_not_a_push_button_is_invoked_instead(monkeypatch) -> None:
    # A WPF or hosted control can report its container's window handle;
    # BM_CLICK there would be dropped while PostMessage still succeeds.
    user32, button = _FakeUser32(), _save_button()
    user32.classes[user32.hwnd] = "HwndWrapper[App;;1234]"
    _on_windows(monkeypatch, user32, button)
    result = _act_click(_fingerprint())
    assert result["receipt"]["operation"] == "uia_invoke"
    assert user32.posted == []
    assert button.invocations == 1


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("root_disabled", "target_blocked"),
        ("button_disabled", "target_blocked"),
        ("other_window_under_point", "target_obscured"),
        ("not_in_front", "target_blocked"),
        ("window_gone", "stale_target"),
        ("invisible", "target_obscured"),
        ("post_fails", "native_action_failed"),
    ],
)
def test_a_posted_click_is_refused_when_the_window_cannot_take_it(
    monkeypatch, fault, code
) -> None:
    user32, button = _FakeUser32(), _save_button()
    if fault == "root_disabled":
        user32.enabled[user32.root] = False  # a modal dialog is up
    elif fault == "button_disabled":
        user32.enabled[user32.hwnd] = False
    elif fault == "other_window_under_point":
        user32.under = 0x9999  # a loading window UIA's hit test sees through
    elif fault == "not_in_front":
        user32.foreground_window = 0x7777
    elif fault == "window_gone":
        user32.windows.discard(user32.hwnd)
    elif fault == "invisible":
        user32.visible[user32.hwnd] = False
    elif fault == "post_fails":
        user32.post_ok = False
    _on_windows(monkeypatch, user32, button)
    with pytest.raises(AgentRequestError) as caught:
        _act_click(_fingerprint())
    assert caught.value.code == code
    assert user32.posted == []
    assert button.invocations == 0


def test_an_invoke_that_has_not_returned_is_an_uncertain_delivery(monkeypatch) -> None:
    # A modal loop and a hung app look the same from the agent: neither may
    # be reported as a delivered click.
    import openadapt_flow.backends.win_agent.server as agent_server

    monkeypatch.setattr(agent_server, "_INVOKE_RETURN_S", 0.2)
    released = threading.Event()

    class StuckButton(_FakeControl):
        def Invoke(self):
            self.invocations += 1
            released.wait(5)

    button = StuckButton(
        "ButtonControl",
        automation_id="saveButton",
        name="Save Note",
        runtime_id=(42, 99),
        bounds=(500, 450, 600, 484),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))
    started = time.monotonic()
    try:
        with pytest.raises(AgentRequestError) as caught:
            _act_click(_fingerprint())
    finally:
        released.set()
    assert caught.value.status == 503
    assert caught.value.code == "delivery_uncertain"
    assert time.monotonic() - started < 2.0


def test_an_invoke_that_raises_is_a_failed_action(monkeypatch) -> None:
    class FailingButton(_FakeControl):
        def Invoke(self):
            raise OSError("provider refused")

    button = FailingButton(
        "ButtonControl",
        automation_id="saveButton",
        name="Save Note",
        runtime_id=(42, 99),
        bounds=(500, 450, 600, 484),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))
    with pytest.raises(AgentRequestError) as caught:
        _act_click(_fingerprint())
    assert caught.value.code == "native_action_failed"


class _FakeDesktop:
    """A z-ordered stack of top-level windows (topmost first) for the
    owned-window checks: pid, owner, rect, visibility and cloaking."""

    PINNED = 0x100

    def __init__(self) -> None:
        # hwnd -> (pid, owner, rect, visible, cloaked, title)
        self.stack: list[int] = [self.PINNED]
        self.windows = {
            self.PINNED: (10, 0, (0, 0, 800, 600), True, False, "Patient Notes")
        }
        self.dpi_ok = True
        self.foreground_window = self.PINNED
        self.roots: dict[int, int] = {}  # child hwnd -> top-level
        self.paint_border: dict[int, int] = {}  # self-drawn frame, in px

    def add(
        self,
        hwnd,
        *,
        pid=10,
        owner=0,
        rect=(200, 200, 500, 400),
        visible=True,
        cloaked=False,
        title="Window",
    ):
        self.stack.insert(0, hwnd)  # new windows go on top
        self.windows[hwnd] = (pid, owner, rect, visible, cloaked, title)

    def top_level_windows(self):
        return list(self.stack)

    def is_window(self, hwnd):
        return hwnd in self.windows

    def pid_of(self, hwnd):
        return self.windows[hwnd][0]

    def owner(self, hwnd):
        return self.windows.get(hwnd, (0, 0))[1]

    def rect(self, hwnd):
        return self.windows[hwnd][2]

    def outer_rect(self, hwnd):
        x0, y0, x1, y1 = self.windows[hwnd][2]
        grow = self.paint_border.get(hwnd, 0)
        return (x0 - grow, y0 - grow, x1 + grow, y1 + grow)

    def is_visible(self, hwnd):
        return self.windows[hwnd][3]

    def is_cloaked(self, hwnd):
        return self.windows[hwnd][4]

    def title(self, hwnd):
        return self.windows[hwnd][5]

    def window_from_point(self, x, y):
        return self.stack[0]

    def ancestor(self, hwnd, flag):
        if flag == 2:  # GA_ROOT
            return self.roots.get(hwnd, hwnd if hwnd in self.windows else 0)
        root = hwnd  # GA_ROOTOWNER: up the owner chain
        while self.owner(root):
            root = self.owner(root)
        return root

    def foreground(self):
        return self.foreground_window

    def per_monitor_dpi(self):
        return self.dpi_ok


def _owned_over(monkeypatch, desktop, region=(250, 250, 50, 20), **pinned):
    import openadapt_flow.backends.win_agent.server as agent_server

    monkeypatch.setattr(agent_server.os, "name", "nt")
    monkeypatch.setattr(agent_server, "_user32_factory", lambda: desktop)
    payload = {"hwnd": _FakeDesktop.PINNED, "pid": 10, "region": list(region)}
    payload.update(pinned)
    return _perform_uia("owned-window-over", payload)["window"]


def test_an_owned_dialog_over_the_region_is_reported(monkeypatch) -> None:
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, title="Session notice")
    assert _owned_over(monkeypatch, desktop) == {
        "title": "Session notice",
        "hwnd": 0x200,
        "owner_hwnd": _FakeDesktop.PINNED,
        "pid": 10,
        "region": [200, 200, 300, 200],
    }


def test_the_reported_region_is_the_owned_windows_visible_frame(monkeypatch) -> None:
    # The whole frame, not its overlap with the query, so the caller can
    # check that it spans the band from the cut point to the right edge.
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(295, 265, 400, 300))
    assert _owned_over(monkeypatch, desktop)["region"] == [295, 265, 105, 35]


def test_a_one_pixel_region_reports_the_topmost_owned_window_there(
    monkeypatch,
) -> None:
    # agnostic-repair asks at the dismiss-click point and requires the same
    # hwnd as the cover.
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(200, 200, 500, 400))
    desktop.add(0x300, owner=_FakeDesktop.PINNED, rect=(450, 350, 600, 450))
    assert _owned_over(monkeypatch, desktop, region=(300, 300, 1, 1))["hwnd"] == 0x200
    assert _owned_over(monkeypatch, desktop, region=(460, 360, 1, 1))["hwnd"] == 0x300
    # Its right and bottom edges are exclusive: (500, 300) is off 0x200.
    assert _owned_over(monkeypatch, desktop, region=(500, 300, 1, 1)) is None


def test_a_one_pixel_region_under_a_foreign_window_is_none(monkeypatch) -> None:
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(200, 200, 500, 400))
    desktop.add(0x900, pid=99, rect=(280, 280, 320, 320))
    assert _owned_over(monkeypatch, desktop, region=(300, 300, 1, 1)) is None
    # A foreign window elsewhere does not decide a point it does not reach.
    assert _owned_over(monkeypatch, desktop, region=(400, 350, 1, 1))["hwnd"] == 0x200


def test_a_topmost_owned_popup_does_not_hide_a_foreign_window_over_the_target(
    monkeypatch,
) -> None:
    # Review blocker: a topmost owned tooltip sorts above every normal window;
    # Notepad below it still covers the target.
    desktop = _FakeDesktop()
    desktop.add(0x900, pid=99, title="Untitled - Notepad")
    desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(240, 240, 300, 270))
    assert _owned_over(monkeypatch, desktop) is None


def test_an_owned_dialog_over_part_of_the_region_is_reported(monkeypatch) -> None:
    # Live lab finding: the "Session notice" dialog covers the right part of
    # the identity band, not its center; the caller still sees the rest.
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(295, 265, 400, 300))
    assert _owned_over(monkeypatch, desktop)["hwnd"] == 0x200


def test_a_foreign_self_drawn_frame_reaching_the_band_is_none(monkeypatch) -> None:
    # Review should-fix: an Electron-style window paints into the ~7 px border
    # DWM leaves out of its visible frame.
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(280, 240, 400, 300))
    desktop.add(0x900, pid=99, rect=(200, 240, 245, 300))  # frame ends 5 px short
    desktop.paint_border[0x900] = 7
    assert _owned_over(monkeypatch, desktop) is None


def test_a_foreign_window_beside_an_owned_dialog_over_the_band_is_none(
    monkeypatch,
) -> None:
    # Review attack (a): the app's dialog covers one end of the band and
    # another process's window the other end.
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(280, 240, 400, 300))
    desktop.add(0x900, pid=99, rect=(200, 240, 262, 300))
    assert _owned_over(monkeypatch, desktop) is None
    # Same with the foreign window below the dialog in z-order.
    desktop = _FakeDesktop()
    desktop.add(0x900, pid=99, rect=(200, 240, 262, 300))
    desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(280, 240, 400, 300))
    assert _owned_over(monkeypatch, desktop) is None


def test_an_owner_cycle_is_none(monkeypatch) -> None:
    desktop = _FakeDesktop()
    desktop.add(0x300, owner=0x200)
    desktop.windows[0x200] = (10, 0x300, (900, 900, 950, 950), True, False, "x")
    assert _owned_over(monkeypatch, desktop) is None


def test_an_unreadable_cloak_state_on_the_apps_window_is_none(monkeypatch) -> None:
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, cloaked=None)
    assert _owned_over(monkeypatch, desktop) is None


def test_window_operations_need_per_monitor_dpi(monkeypatch) -> None:
    desktop = _FakeDesktop()
    desktop.dpi_ok = False
    with pytest.raises(AgentRequestError) as caught:
        _owned_over(monkeypatch, desktop)
    assert caught.value.status == 503


@pytest.mark.parametrize("fault", ["not_foreground", "no_root"])
def test_window_root_at_refuses_a_doubtful_capture(monkeypatch, fault) -> None:
    import openadapt_flow.backends.win_agent.server as agent_server

    desktop = _FakeDesktop()
    desktop.add(0x900, pid=99)  # what is now at the point
    if fault == "not_foreground":
        desktop.foreground_window = _FakeDesktop.PINNED
    else:
        desktop.roots[0x900] = 0  # GetAncestor fails
        desktop.foreground_window = 0x900
    monkeypatch.setattr(agent_server.os, "name", "nt")
    monkeypatch.setattr(agent_server, "_user32_factory", lambda: desktop)
    assert _perform_uia("window-root-at", {"x": 5, "y": 5})["window"] is None


def test_a_dialog_owned_through_a_chain_counts(monkeypatch) -> None:
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(700, 0, 800, 50))
    desktop.add(0x300, owner=0x200, title="Confirm")
    assert _owned_over(monkeypatch, desktop)["hwnd"] == 0x300


def test_the_topmost_owned_window_over_the_region_is_reported(monkeypatch) -> None:
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, title="Session notice")
    desktop.add(0x300, owner=_FakeDesktop.PINNED, rect=(295, 265, 400, 300))
    assert _owned_over(monkeypatch, desktop)["hwnd"] == 0x300


@pytest.mark.parametrize(
    "layout",
    [
        "foreign_window_over",
        "same_process_unowned",
        "owned_but_not_over_the_region",
        "foreign_over_an_owned_dialog",
    ],
)
def test_anything_but_an_owned_window_over_the_region_is_none(
    monkeypatch, layout
) -> None:
    desktop = _FakeDesktop()
    if layout == "foreign_window_over":
        desktop.add(0x900, pid=99, title="Untitled - Notepad")
    elif layout == "same_process_unowned":
        desktop.add(0x200, owner=0, title="Second Patient Notes window")
    elif layout == "owned_but_not_over_the_region":
        desktop.add(0x200, owner=_FakeDesktop.PINNED, rect=(600, 500, 700, 580))
    elif layout == "foreign_over_an_owned_dialog":
        desktop.add(0x200, owner=_FakeDesktop.PINNED)
        desktop.add(0x900, pid=99, rect=(240, 240, 320, 300))
    assert _owned_over(monkeypatch, desktop) is None


def test_hidden_and_cloaked_windows_do_not_decide(monkeypatch) -> None:
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED, title="Session notice")
    desktop.add(0x800, pid=99, visible=False)
    desktop.add(0x801, pid=99, cloaked=True)
    assert _owned_over(monkeypatch, desktop)["hwnd"] == 0x200


@pytest.mark.parametrize("change", ["gone", "pid_changed", "not_listed"])
def test_a_captured_window_that_changed_is_none(monkeypatch, change) -> None:
    desktop = _FakeDesktop()
    desktop.add(0x200, owner=_FakeDesktop.PINNED)
    if change == "gone":
        del desktop.windows[_FakeDesktop.PINNED]
        desktop.stack.remove(_FakeDesktop.PINNED)
    elif change == "pid_changed":
        desktop.windows[_FakeDesktop.PINNED] = (11,) + desktop.windows[
            _FakeDesktop.PINNED
        ][1:]
    else:
        desktop.stack.remove(_FakeDesktop.PINNED)
        desktop.windows[0x200] = (10, 0x555) + desktop.windows[0x200][2:]
    assert _owned_over(monkeypatch, desktop) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"hwnd": True},
        {"pid": 0},
        {"region": [1, 2, 3]},
        {"region": [1, 2, 0, 5]},
        {"extra": 1},
    ],
)
def test_owned_window_over_refuses_a_malformed_request(monkeypatch, bad) -> None:
    with pytest.raises(AgentRequestError) as caught:
        _owned_over(monkeypatch, _FakeDesktop(), **bad)
    assert caught.value.status == 400


def test_window_root_at_names_the_top_level_window(monkeypatch) -> None:
    import openadapt_flow.backends.win_agent.server as agent_server

    desktop = _FakeDesktop()
    monkeypatch.setattr(agent_server.os, "name", "nt")
    monkeypatch.setattr(agent_server, "_user32_factory", lambda: desktop)
    assert _perform_uia("window-root-at", {"x": 5, "y": 5}) == {
        "status": "ok",
        "window": {"hwnd": _FakeDesktop.PINNED, "pid": 10},
    }


def test_locator_at_refuses_a_painted_toolbar_pane(monkeypatch) -> None:
    # Open Dental paints its toolbar buttons: the only UIA element under
    # "Select Patient" is the whole toolbar, whose centre is another button.
    toolbar = _FakeControl(
        "PaneControl", automation_id="ToolBarMain", bounds=(51, 50, 1024, 75)
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(toolbar))

    assert _perform_uia("locator-at", {"x": 110, "y": 61}) == {
        "status": "ok",
        "locator": None,
    }


def test_locator_at_keeps_a_custom_control_clicked_at_its_centre(monkeypatch) -> None:
    # A WPF button can surface as a CustomControl with an AutomationId.
    ok = _FakeControl(
        "CustomControl", automation_id="butOK", bounds=(899, 688, 975, 714)
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(ok))

    locator = _perform_uia("locator-at", {"x": 937, "y": 701})["locator"]

    assert locator["automation_id"] == "butOK"


def test_locator_at_keeps_an_actionable_control_clicked_off_centre(monkeypatch) -> None:
    button = _FakeControl(
        "ButtonControl",
        automation_id="saveButton",
        name="Save",
        bounds=(500, 450, 600, 484),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))

    locator = _perform_uia("locator-at", {"x": 503, "y": 452})["locator"]

    assert locator["automation_id"] == "saveButton"


def test_locator_at_refuses_an_automation_id_others_share(monkeypatch) -> None:
    # WPF names the inner edit of every text box "textBox".
    last = _FakeControl(
        "EditControl",
        automation_id="textBox",
        runtime_id=(42, 7),
        bounds=(879, 81, 969, 101),
    )
    first = _FakeControl(
        "EditControl",
        automation_id="textBox",
        runtime_id=(42, 8),
        bounds=(879, 101, 969, 121),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(last, first))

    assert _perform_uia("locator-at", {"x": 920, "y": 91})["locator"] is None


def test_locator_at_keeps_a_unique_automation_id(monkeypatch) -> None:
    note = _FakeControl(
        "EditControl",
        automation_id="noteBox",
        runtime_id=(42, 7),
        bounds=(254, 304, 879, 514),
    )
    other = _FakeControl(
        "EditControl",
        automation_id="searchBox",
        runtime_id=(42, 8),
        bounds=(10, 10, 200, 30),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(note, other))

    assert (
        _perform_uia("locator-at", {"x": 560, "y": 400})["locator"]["automation_id"]
        == "noteBox"
    )


def test_window_bounds_reports_the_visible_frame(monkeypatch) -> None:
    import openadapt_flow.backends.win_agent.server as agent_server

    desktop = _FakeDesktop()
    monkeypatch.setattr(agent_server.os, "name", "nt")
    monkeypatch.setattr(agent_server, "_user32_factory", lambda: desktop)
    pinned = _FakeDesktop.PINNED
    assert _perform_uia("window-bounds", {"hwnd": pinned, "pid": 10}) == {
        "status": "ok",
        "window": {"hwnd": pinned, "pid": 10, "bounds": [0, 0, 800, 600]},
    }
    # Another process now owns the handle, or the window is hidden: no frame.
    assert _perform_uia("window-bounds", {"hwnd": pinned, "pid": 11})["window"] is None
    desktop.add(0x200, visible=False)
    assert _perform_uia("window-bounds", {"hwnd": 0x200, "pid": 10})["window"] is None
    with pytest.raises(AgentRequestError):
        _perform_uia("window-bounds", {"hwnd": True, "pid": 10})


@pytest.mark.parametrize(
    "extra,bounded",
    [
        ({"rect": (200, 200, 500, 400)}, True),  # an owned dialog inside
        ({"rect": (600, 200, 900, 400)}, False),  # reaches past the frame
        ({"rect": (1920, 0, 2400, 400)}, False),  # on another monitor
        ({"rect": (600, 200, 900, 400), "pid": 11}, True),  # another app
        ({"rect": (600, 200, 900, 400), "visible": False}, True),
        ({"rect": (600, 200, 900, 400), "cloaked": True}, True),
        ({"rect": (600, 200, 900, 400), "cloaked": None}, False),
    ],
    ids=[
        "dialog-inside",
        "dialog-outside",
        "other-monitor",
        "other-process",
        "hidden",
        "cloaked",
        "cloak-unknown",
    ],
)
def test_window_bounds_covers_every_window_the_app_shows(
    monkeypatch, extra, bounded
) -> None:
    import openadapt_flow.backends.win_agent.server as agent_server

    desktop = _FakeDesktop()
    desktop.add(0x300, owner=_FakeDesktop.PINNED, **extra)
    monkeypatch.setattr(agent_server.os, "name", "nt")
    monkeypatch.setattr(agent_server, "_user32_factory", lambda: desktop)
    window = _perform_uia("window-bounds", {"hwnd": _FakeDesktop.PINNED, "pid": 10})[
        "window"
    ]
    assert (window is not None) is bounded


def test_uia_tree_lists_one_windows_elements_without_values(monkeypatch) -> None:
    box = _FakeControl(
        "EditControl",
        automation_id="noteBox",
        name="Chart note",
        bounds=(20, 350, 740, 460),
    )
    box.Value = "a typed note the tree must not carry"
    button = _FakeControl(
        "ButtonControl",
        automation_id="saveButton",
        name="Save Note",
        bounds=(630, 494, 740, 528),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(box, button))
    tree = _perform_uia("tree", {"window_name": "Patient Notes"})
    ids = [n["automation_id"] for n in tree["nodes"]]
    assert ids[1:] == ["noteBox", "saveButton"]
    assert tree["nodes"][2]["bounds"] == [630, 494, 740, 528]
    assert tree["truncated"] is False
    assert "typed note" not in str(tree)
    assert _perform_uia("tree", {"window_name": "Another app"})["nodes"] == []
    cut = _perform_uia("tree", {"window_name": "Patient Notes", "max_nodes": 1})
    assert len(cut["nodes"]) == 1 and cut["truncated"] is True
    for bad in (
        {},
        {"window_name": ""},
        {"window_name": "x", "max_nodes": 0},
        {"window_name": "x", "max_nodes": True},
    ):
        with pytest.raises(AgentRequestError):
            _perform_uia("tree", bad)


@pytest.fixture
def physical_keyboard(monkeypatch):
    """A physical-input seam with no desktop, clipboard or real key events."""
    from openadapt_flow.backends.win_agent import server

    state = types.SimpleNamespace(
        foreground=(101, 202), typed="", pastes=0, after_write=None, after_paste=None
    )
    fake = types.ModuleType("pyautogui")

    def write(text, **_kwargs):
        state.typed += text
        if state.after_write is not None:
            state.after_write()

    def hotkey(*keys):
        assert keys == ("ctrl", "v")
        state.pastes += 1
        if state.after_paste is not None:
            state.after_paste()

    fake.write = write
    fake.hotkey = hotkey
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    monkeypatch.setattr(server, "_foreground_window_identity", lambda: state.foreground)
    monkeypatch.setattr(server.time, "sleep", lambda _delay: None)
    return state


def test_physical_typing_preserves_the_complete_value_with_stable_focus(
    physical_keyboard,
):
    receipt = _perform_input(
        {"action": "type_text", "text": "safe-text", "interval_s": 0}
    )
    assert physical_keyboard.typed == "safe-text"
    assert receipt["status"] == "delivered"
    assert receipt["operation"] == "physical_type_text"


def test_physical_typing_refuses_an_unobservable_foreground(physical_keyboard):
    physical_keyboard.foreground = None
    with pytest.raises(AgentRequestError, match="foreground window"):
        _perform_input({"action": "type_text", "text": "abc"})
    assert physical_keyboard.typed == ""
    assert physical_keyboard.pastes == 0


@pytest.mark.parametrize("changed", [(303, 202), (101, 404), None])
def test_physical_typing_stops_after_focus_changes(physical_keyboard, changed):
    physical_keyboard.after_write = lambda: setattr(
        physical_keyboard, "foreground", changed
    )
    with pytest.raises(AgentRequestError, match="delivery may be partial") as error:
        _perform_input({"action": "type_text", "text": "abc"})
    assert error.value.code == "input_focus_changed"
    assert physical_keyboard.typed == "a"


def test_physical_typing_refuses_focus_change_before_the_first_key(
    physical_keyboard, monkeypatch
):
    from openadapt_flow.backends.win_agent import server

    readings = iter([(101, 202), (303, 202)])
    monkeypatch.setattr(server, "_foreground_window_identity", lambda: next(readings))
    with pytest.raises(AgentRequestError, match="delivery may be partial"):
        _perform_input({"action": "type_text", "text": "abc"})
    assert physical_keyboard.typed == ""


def test_unicode_paste_is_refused_if_focus_changes_during_clipboard_setup(
    physical_keyboard, monkeypatch
):
    from openadapt_flow.backends.win_agent import server

    def clipboard(*_args, **_kwargs):
        physical_keyboard.foreground = (303, 202)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(server.subprocess, "run", clipboard)
    with pytest.raises(AgentRequestError, match="delivery may be partial"):
        _perform_input({"action": "type_text", "text": "caf\u00e9"})
    assert physical_keyboard.typed == ""
    assert physical_keyboard.pastes == 0


def test_unicode_paste_reports_uncertainty_if_focus_changes_after_delivery(
    physical_keyboard, monkeypatch
):
    from openadapt_flow.backends.win_agent import server

    monkeypatch.setattr(
        server.subprocess, "run", lambda *_a, **_k: types.SimpleNamespace(returncode=0)
    )
    physical_keyboard.after_paste = lambda: setattr(
        physical_keyboard, "foreground", (303, 202)
    )
    with pytest.raises(AgentRequestError, match="delivery may be partial"):
        _perform_input({"action": "type_text", "text": "caf\u00e9"})
    assert physical_keyboard.pastes == 1


def test_focus_change_over_http_is_uncertain_and_never_uses_legacy_delivery(
    physical_keyboard,
):
    from openadapt_flow.backend import ActionDeliveryUncertain
    from openadapt_flow.backends.windows_backend import WindowsBackend

    physical_keyboard.after_write = lambda: setattr(
        physical_keyboard, "foreground", (303, 202)
    )
    legacy_calls = []
    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, allow_legacy_exec=True),
        legacy_fn=_recording_legacy_fn(legacy_calls),
    )
    try:
        backend = WindowsBackend(agent.url, allow_legacy_exec=True)
        with pytest.raises(ActionDeliveryUncertain) as error:
            backend.type_text("abc")
        assert error.value.operation == "physical_type_text"
        assert physical_keyboard.typed == "a"
        assert legacy_calls == []
    finally:
        agent.close()
