"""Roundtrip tests for the in-guest win_agent server (no live VM/desktop).

The stdlib server is started on a loopback ephemeral port and exercised with
real ``requests`` calls, so the full HTTP contract WindowsBackend depends on is
proven end to end. The desktop grabber is injected (a fake PNG) so no mss / no
real desktop is needed — the suite runs on macOS/Linux CI.
"""

from __future__ import annotations

import struct
import sys
import threading
import types
from collections.abc import Iterator

import pytest
import requests

from openadapt_flow.backends.win_agent import AgentConfig, create_server
from openadapt_flow.backends.win_agent.server import (
    AgentRequestError,
    _perform_input,
    _perform_uia,
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
    ) -> None:
        kwargs = {"grab_fn": grab_fn}
        if input_fn is not None:
            kwargs["input_fn"] = input_fn
        if uia_fn is not None:
            kwargs["uia_fn"] = uia_fn
        self.server = create_server(config, **kwargs)
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def agent() -> Iterator[RunningAgent]:
    a = RunningAgent(AgentConfig(host="127.0.0.1", port=0, allow_legacy_exec=True))
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
        )
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
    assert "typed_input_v1" in health["capabilities"]
    assert "uia_v1" in health["capabilities"]
    assert "legacy_exec" not in health["capabilities"]
    response = requests.post(
        f"{typed_agent.url}/execute_windows",
        json={"command": "print('must not execute')"},
        timeout=5,
    )
    assert response.status_code == 404


def test_typed_input_and_uia_receipts_never_claim_outcome(
    typed_agent: RunningAgent,
) -> None:
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


def test_execute_windows_runs_bare_python_and_echoes_stdout(
    agent: RunningAgent,
) -> None:
    r = requests.post(
        f"{agent.url}/execute_windows",
        json={"command": "print('<<OAFLOW_STRUCTURED>>42<<END_OAFLOW_STRUCTURED>>')"},
        timeout=5,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "<<OAFLOW_STRUCTURED>>42<<END_OAFLOW_STRUCTURED>>" in body["output"]


def test_execute_windows_500_on_exception_with_traceback(agent: RunningAgent) -> None:
    # A failing command must surface as an ERROR (non-200), never a silent no-op.
    r = requests.post(
        f"{agent.url}/execute_windows",
        json={"command": "raise ValueError('boom')"},
        timeout=5,
    )
    assert r.status_code == 500
    body = r.json()
    assert body["status"] == "error"
    assert "boom" in body["error"]
    assert "Traceback" in body["trace"]


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
        json={"command": "print('x')"},
        timeout=5,
    )
    assert r.status_code == 401
    # Wrong token -> 401.
    r = requests.post(
        f"{authed_agent.url}/execute_windows",
        json={"command": "print('x')"},
        headers={"Authorization": "Bearer wrong"},
        timeout=5,
    )
    assert r.status_code == 401
    # Correct token -> 200.
    r = requests.post(
        f"{authed_agent.url}/execute_windows",
        json={"command": "print('x')"},
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
