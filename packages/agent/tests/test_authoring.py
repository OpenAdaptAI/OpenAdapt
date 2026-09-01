"""Stdio --authoring tools: probe names, PHI observe, record_observed."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import mcp.types as types
import pytest

from openadapt_agent.authoring import (
    AUTHORING_PROBE_TOOLS,
    AuthoringBridge,
    AuthoringError,
    CoachOnlySession,
    MAX_AUTHORING_WIRE_BYTES,
    discover_desktop_authoring_ipc,
    open_authoring_session,
    pin_local_backend,
    project_observe,
)
from openadapt_agent.mcp import build_server


class FakeAuthoringSession:
    """In-process stand-in until openadapt_flow.authoring lands."""

    def __init__(
        self,
        *,
        backend: str = "web",
        coach_only: bool = False,
        secret_pause_without_type: bool = False,
    ):
        self.backend = backend
        self.coach_only = coach_only or backend in {"windows", "rdp", "citrix"}
        self.secret_pause_without_type = secret_pause_without_type
        self.calls: list = []
        self.typed_via_backend: list[str] = []
        self.observed_events: list[dict] = []
        self.recording = False
        self._secret_type_recorded = False
        self.nodes = {
            "n_9f2c001a": {
                "node_id": "n_9f2c001a",
                "role": "button",
                "control_type": "button",
                "automation_id": "btnContinue",
                "enabled": True,
                "focused": False,
                "bounds": {"x": 0.72, "y": 0.88, "w": 0.14, "h": 0.05},
                "backend_pixels": {"x": 920, "y": 640, "w": 180, "h": 36},
                "value": "4111111111111111",
                "title": "Chart — Jane Roe",
                "name": "Save",
            }
        }

    def observe(self):
        self.calls.append("observe")
        if self.coach_only:
            return {
                "backend": self.backend,
                "agent_drive": True,
                "coach_only": True,
                "tree": list(self.nodes.values()),
                "title": "Remote desktop",
            }
        return {
            "schema_version": "openadapt.authoring.observe/v1",
            "backend": self.backend,
            "provider": "playwright_ax",
            "agent_drive": True,
            "coach_only": False,
            "recording": self.recording,
            "window": {
                "process_name": "Chromium",
                "role": "window",
                "title": "Patient Jane Roe MRN-9911",
                "bounds": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            },
            "tree": [
                {
                    **node,
                    "screenshot": "iVBORw0KGgo=",
                    "url": "https://example.invalid/chart",
                    "name": "Call 555-0100" if node_id == "leak" else node.get("name"),
                }
                for node_id, node in self.nodes.items()
            ]
            + [
                {
                    "node_id": "n_ssn1",
                    "role": "textbox",
                    "name": "SSN 123-45-6789",
                    "automation_id": "patient@clinic.example",
                    "value": "123-45-6789",
                }
            ],
            "value": "raw AX dump",
            "screenshot": "pixels",
        }

    def start_record(self):
        self.calls.append("start_record")
        if self.coach_only:
            return {"error": "COACH_ONLY", "coach_only": True, "backend": self.backend}
        self.recording = True
        return {"status": "recording"}

    def click(self, node_id=None, x=None, y=None):
        self.calls.append(("click", node_id, x, y))
        if self.coach_only:
            return {"error": "COACH_ONLY"}
        if node_id and node_id not in self.nodes:
            return {"status": "error", "error": "stale_node"}
        return {"status": "ok", "node_id": node_id, "backend_pixels": {"x": 1, "y": 2}}

    def type_agent(self, text, param=None, node_id=None):
        self.calls.append(("type_agent", text, param, node_id))
        self.typed_via_backend.append(text)
        return {"status": "ok", "param": param, "text": text}

    def type_text(self, text, param=None):
        raise AssertionError("human pause must not call backend type_text")

    def pause_for_input(self, node_id=None, param=None, secret=False):
        self.calls.append(("pause_for_input", node_id, param, secret))
        event = {"kind": "type", "param": param, "secret": secret}
        if not secret:
            event["text"] = "synthetic follow-up"
        self.observed_events.append(event)
        if secret and not self.secret_pause_without_type:
            self._secret_type_recorded = True
        return {"recorded": True, "param": param, "text": "must-not-cross-mcp"}

    def halt(self):
        self.calls.append("halt")
        self.recording = False
        return {"status": "halted"}

    def stop_record(self):
        self.calls.append("stop_record")
        self.recording = False
        return {"status": "stopped"}

    def compile(self):
        self.calls.append("compile")
        if self.secret_pause_without_type and not self._secret_type_recorded:
            return {"error": "missing_secret_type"}
        return {
            "status": "needs_human_admit",
            "workflow_id": "wf_demo0001",
            "execution_outcome": "VERIFIED",
            "success": True,
        }


def test_probe_tool_names_match_hosted_surface():
    names = [spec.name for spec in AuthoringBridge(FakeAuthoringSession()).list_tool_specs()]
    for probe in AUTHORING_PROBE_TOOLS:
        assert probe in names
    assert "type" in names
    assert names.index("observe") < names.index("type")


def test_observe_drops_values_titles_screenshots_and_unsafe_names():
    bridge = AuthoringBridge(FakeAuthoringSession())
    result = bridge.dispatch("observe", {})
    blob = json.dumps(result)
    assert result["schema_version"] == "openadapt.authoring.observe/v1"
    assert result["mode"] == "authoring"
    assert result["agent_drive"] is True
    assert "value" not in blob
    assert "screenshot" not in blob
    assert "title" not in blob
    assert "backend_pixels" not in blob
    assert "Jane Roe" not in blob
    assert "4111111111111111" not in blob
    assert "123-45-6789" not in blob
    assert "patient@clinic.example" not in blob
    assert "https://" not in blob
    node = next(item for item in result["tree"] if item["node_id"] == "n_9f2c001a")
    assert node["automation_id"] == "btnContinue"
    assert node["name"] == "Save"
    assert all(item["node_id"] != "n_ssn1" or "name" not in item for item in result["tree"])


def test_project_observe_empty_tree_is_not_a_raw_fallback():
    projected = project_observe({"backend": "web", "tree": [{"role": "button", "value": "x"}]})
    assert projected["tree"] == []
    assert projected["reason"] == "empty_projection"
    assert "value" not in json.dumps(projected)


def test_start_record_and_click_and_halt_round_trip():
    session = FakeAuthoringSession()
    bridge = AuthoringBridge(session)
    assert bridge.dispatch("start_record", {})["status"] == "recording"
    clicked = bridge.dispatch("click", {"node_id": "n_9f2c001a"})
    assert clicked["status"] == "ok"
    assert "backend_pixels" not in clicked
    assert bridge.dispatch("click", {"node_id": "n_missing"}) == {
        "status": "error",
        "error": "stale_node",
    }
    local = bridge.dispatch("click", {"x": 10, "y": 20})
    assert local["status"] == "ok"
    halted = bridge.dispatch("halt", {})
    assert halted["status"] == "halted"
    assert halted["compiled"] is False
    assert ("click", "n_9f2c001a", None, None) in session.calls
    assert ("click", None, 10, 20) in session.calls


def test_local_type_is_agent_driven_and_strips_text():
    session = FakeAuthoringSession()
    bridge = AuthoringBridge(session)
    result = bridge.dispatch("type", {"text": "synthetic follow-up", "param": "note"})
    assert result["recorded"] is True
    assert result["param"] == "note"
    assert "text" not in result
    assert session.typed_via_backend == ["synthetic follow-up"]
    assert session.observed_events == []


def test_pause_continue_uses_record_observed_never_type_text():
    session = FakeAuthoringSession()
    bridge = AuthoringBridge(session)
    result = bridge.dispatch(
        "pause_for_input",
        {"node_id": "n_9f2c001a", "param": "note", "secret": False},
    )
    assert result == {"recorded": True, "param": "note"}
    assert session.typed_via_backend == []
    assert session.observed_events == [
        {"kind": "type", "param": "note", "secret": False, "text": "synthetic follow-up"}
    ]


def test_secret_pause_result_has_no_value_and_compile_can_refuse():
    session = FakeAuthoringSession(secret_pause_without_type=True)
    bridge = AuthoringBridge(session)
    paused = bridge.dispatch(
        "pause_for_input",
        {"node_id": "n_9f2c001a", "param": "identifier", "secret": True},
    )
    assert paused["recorded"] is True
    assert paused["secret"] is True
    assert "text" not in paused
    assert bridge.dispatch("compile", {}) == {
        "status": "error",
        "error": "missing_secret_type",
    }


def test_compile_returns_needs_human_admit_never_verified():
    bridge = AuthoringBridge(FakeAuthoringSession())
    result = bridge.dispatch("compile", {})
    assert result["status"] == "needs_human_admit"
    assert result["workflow_id"] == "wf_demo0001"
    assert result["recording_retained"] is True
    assert "VERIFIED" not in json.dumps(result)
    assert "success" not in result


def test_windows_native_is_coach_only():
    session = FakeAuthoringSession(backend="windows")
    bridge = AuthoringBridge(session)
    observed = bridge.dispatch("observe", {})
    assert observed["coach_only"] is True
    assert observed["agent_drive"] is False
    assert observed["tree"] == []
    with pytest.raises(AuthoringError, match="COACH_ONLY"):
        bridge.dispatch("start_record", {})
    with pytest.raises(AuthoringError, match="COACH_ONLY"):
        bridge.dispatch("click", {"node_id": "n_9f2c001a"})


def test_get_command_result_returns_last_in_process_result():
    bridge = AuthoringBridge(FakeAuthoringSession())
    idle = bridge.dispatch("get_command_result", {})
    assert idle["status"] == "idle"
    assert idle["result"] is None
    bridge.dispatch("start_record", {})
    last = bridge.dispatch("get_command_result", {})
    assert last["command_id"] == "start_record"
    assert last["status"] == "done"
    assert last["result"]["status"] == "recording"


def test_unknown_fields_and_missing_click_target_are_refused():
    bridge = AuthoringBridge(FakeAuthoringSession())
    with pytest.raises(AuthoringError, match="schema"):
        bridge.dispatch("observe", {"title": "nope"})
    with pytest.raises(AuthoringError, match="node_id or local"):
        bridge.dispatch("click", {})


def test_mcp_lists_authoring_probe_tools_without_run_tools(bundles_root, runner_config):
    from openadapt_agent.bridge import AgentBridge

    authoring = AuthoringBridge(FakeAuthoringSession())
    server = build_server(authoring=authoring)

    async def list_names():
        handler = server.request_handlers[types.ListToolsRequest]
        result = await handler(types.ListToolsRequest(method="tools/list"))
        return [tool.name for tool in result.root.tools]

    names = anyio.run(list_names)
    assert names[:4] == list(AUTHORING_PROBE_TOOLS)
    assert "type" in names
    assert not any(name.startswith("run_") for name in names)

    combined = build_server(
        AgentBridge(bundles_root, runner_config, allow_run=False),
        authoring=authoring,
    )

    async def combined_names():
        handler = combined.request_handlers[types.ListToolsRequest]
        result = await handler(types.ListToolsRequest(method="tools/list"))
        return [tool.name for tool in result.root.tools]

    both = anyio.run(combined_names)
    assert "list_workflows" in both
    assert "observe" in both
    assert not any(name.startswith("run_") for name in both)


def test_mcp_observe_call_is_projected():
    server = build_server(authoring=AuthoringBridge(FakeAuthoringSession()))

    async def call_observe():
        handler = server.request_handlers[types.CallToolRequest]
        return await handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name="observe", arguments={})
            )
        )

    result = anyio.run(call_observe).root
    payload = json.loads(result.content[0].text)
    assert payload["schema_version"] == "openadapt.authoring.observe/v1"
    assert "screenshot" not in result.content[0].text
    assert "Jane Roe" not in result.content[0].text


def test_open_authoring_session_notes_flow_dependency_when_missing(monkeypatch):
    import sys
    import types

    fake_flow = types.ModuleType("openadapt_flow")
    monkeypatch.setitem(sys.modules, "openadapt_flow", fake_flow)
    monkeypatch.delitem(sys.modules, "openadapt_flow.authoring", raising=False)
    with pytest.raises(AuthoringError, match="openadapt_flow.authoring"):
        open_authoring_session()


def test_authoring_sources_stay_stdio_without_http_listener():
    root = Path(__file__).resolve().parents[1] / "src" / "openadapt_agent"
    for name in ("authoring.py", "mcp.py", "cli.py", "mailbox.py", "runner_bind.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "HTTPServer" not in text
        assert "uvicorn" not in text
        assert "streamable_http" not in text.lower()
        assert "FastAPI" not in text
        assert "win_agent" not in text
        assert "parallels_vm" not in text


def test_observe_wire_omits_extra_keys_and_invalid_node_ids():
    projected = project_observe(
        {
            "backend": "web",
            "provider": "playwright_ax",
            "value": "nope",
            "title": "Chart",
            "screenshot": "px",
            "window": {
                "process_name": "Chromium",
                "role": "window",
                "title": "secret",
                "bounds": {"x": 0, "y": 0, "w": 1, "h": 1, "pixels": 9},
            },
            "tree": [
                {
                    "node_id": "n_9f2c001a",
                    "role": "button",
                    "enabled": True,
                    "focused": False,
                    "bounds": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1},
                    "value": "4111",
                    "backend_pixels": {"x": 1, "y": 2, "w": 3, "h": 4},
                },
                {"node_id": "n_9f2c", "role": "button", "enabled": True, "focused": False},
            ],
        }
    )
    blob = json.dumps(projected)
    assert "value" not in blob
    assert "title" not in blob
    assert "screenshot" not in blob
    assert "backend_pixels" not in blob
    assert projected["tree"][0]["node_id"] == "n_9f2c001a"
    assert all(item["node_id"] != "n_9f2c" for item in projected["tree"])
    assert set(projected) <= {
        "schema_version",
        "backend",
        "provider",
        "mode",
        "agent_drive",
        "coach_only",
        "recording",
        "window",
        "tree",
        "truncated",
        "node_count",
        "reason",
    }


def test_invalid_backend_is_coach_only_not_unknown():
    projected = project_observe({"backend": "turbo", "tree": [{"node_id": "n_9f2c001a"}]})
    assert projected["backend"] == "windows"
    assert projected["coach_only"] is True
    assert projected["agent_drive"] is False
    assert projected["provider"] == "none"
    assert projected["tree"] == []
    assert "unknown" not in json.dumps(projected)


def test_observe_wire_truncates_to_32kib(monkeypatch):
    monkeypatch.setattr(
        "openadapt_agent.authoring.MAX_AUTHORING_WIRE_BYTES", 800
    )
    tree = []
    for index in range(40):
        tree.append(
            {
                "node_id": f"n_{index:08x}",
                "role": "button",
                "name": "SaveButtonLabelOk",
                "enabled": True,
                "focused": False,
                "bounds": {"x": 0.01, "y": 0.01, "w": 0.1, "h": 0.1},
            }
        )
    projected = project_observe(
        {
            "backend": "web",
            "provider": "playwright_ax",
            "window": {
                "process_name": "Chromium",
                "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
            },
            "tree": tree,
        }
    )
    assert projected["truncated"] is True
    assert len(json.dumps(projected, separators=(",", ":")).encode("utf-8")) <= 800
    assert MAX_AUTHORING_WIRE_BYTES == 32 * 1024


def test_remembered_pixels_stay_off_the_wire():
    session = FakeAuthoringSession()
    bridge = AuthoringBridge(session)
    observed = bridge.dispatch("observe", {})
    assert "backend_pixels" not in json.dumps(observed)
    assert "n_9f2c001a" in bridge._nodes


class _FlowError(RuntimeError):
    def __init__(self, message, *, code):
        super().__init__(message)
        self.code = code


class FlowShapedSession:
    """Matches F1 AuthoringSession: no observe, type_text, continue_input, compile(dir, name=)."""

    def __init__(self):
        self.backend_kind = "web"
        self.calls = []
        self.nodes = {}
        self.typed_via_backend = []
        self.paused = None
        self.observed_events = []

    def remember_node(self, node_id, backend_pixels):
        self.nodes[node_id] = dict(backend_pixels)

    def start_record(self):
        self.calls.append("start_record")
        return None

    def click(self, x=None, y=None, *, node_id=None):
        self.calls.append(("click", node_id, x, y))
        if node_id is not None and node_id not in self.nodes:
            raise _FlowError("unknown node", code="stale_node")
        return None

    def type_text(self, text, param=None):
        self.calls.append(("type_text", text, param))
        self.typed_via_backend.append(text)
        return None

    def pause_for_input(self, *, param, secret=False, node_id=None, backend_pixels=None):
        self.calls.append(("pause_for_input", node_id, param, secret))
        self.paused = {"param": param, "secret": secret, "node_id": node_id}
        return None

    def continue_input(self, *, operator_confirmed=True):
        self.calls.append(("continue_input", operator_confirmed))
        if self.paused is None:
            raise _FlowError("no pause", code="not_paused")
        event = {
            "kind": "type",
            "param": self.paused["param"],
            "secret": self.paused["secret"],
        }
        if not self.paused["secret"]:
            event["text"] = "synthetic follow-up"
        self.observed_events.append(event)
        param = self.paused["param"]
        self.paused = None
        return {"recorded": True, "param": param}

    def halt(self):
        self.calls.append("halt")
        return None

    def compile(self, bundle_dir, *, name):
        self.calls.append(("compile", str(bundle_dir), name))
        return {
            "status": "needs_human_admit",
            "workflow_id": "wf_demo0001",
            "recording_retained": True,
            "execution_outcome": "VERIFIED",
        }


def test_flow_shaped_session_maps_f1_signatures_and_stale_node():
    session = FlowShapedSession()
    bridge = AuthoringBridge(session, out_dir="/tmp/authoring-out")
    assert bridge.dispatch("start_record", {}) == {"status": "recording"}
    session.remember_node("n_9f2c001a", {"x": 10, "y": 20, "w": 4, "h": 4})
    assert bridge.dispatch("click", {"node_id": "n_9f2c001a"})["status"] == "ok"
    assert bridge.dispatch("click", {"node_id": "n_deadbeef"}) == {
        "status": "error",
        "error": "stale_node",
    }
    typed = bridge.dispatch("type", {"text": "hello", "param": "note", "node_id": "n_9f2c001a"})
    assert typed["recorded"] is True
    assert "text" not in typed
    assert session.typed_via_backend == ["hello"]
    paused = bridge.dispatch(
        "pause_for_input",
        {"node_id": "n_9f2c001a", "param": "note", "secret": False},
    )
    assert paused == {"status": "paused", "param": "note"}
    assert session.observed_events == []
    continued = bridge.dispatch("continue_input", {})
    assert continued == {"recorded": True, "param": "note"}
    assert session.observed_events == [
        {"kind": "type", "param": "note", "secret": False, "text": "synthetic follow-up"}
    ]
    compiled = bridge.dispatch("compile", {})
    assert compiled["status"] == "needs_human_admit"
    assert compiled["workflow_id"] == "wf_demo0001"
    assert "VERIFIED" not in json.dumps(compiled)
    assert session.calls[-1][0] == "compile"
    assert session.calls[-1][2] == "authoring"


def test_coach_hint_and_bind_status_are_local():
    bridge = AuthoringBridge(FakeAuthoringSession())
    with pytest.raises(AuthoringError, match="PHI"):
        bridge.dispatch("set_coach", {"hint": "open https://example.invalid"})
    with pytest.raises(AuthoringError, match="PHI"):
        bridge.dispatch("set_coach", {"hint": "call 1234567 now"})
    set_ok = bridge.dispatch("set_coach", {"hint": "Click Save"})
    assert set_ok == {"status": "ok", "hint": "Click Save"}
    assert bridge.dispatch("get_coach", {}) == {"hint": "Click Save"}
    status = bridge.dispatch("bind_status", {})
    assert status == {"status": "stdio", "bound": True, "transport": "stdio"}
    assert "tree" not in status
    names = [spec.name for spec in bridge.list_tool_specs()]
    assert names[:4] == list(AUTHORING_PROBE_TOOLS)
    assert "continue_input" in names
    assert "set_coach" in names
    assert "bind_status" in names


def test_desktop_ipc_discovery_does_not_open_a_listener(tmp_path, monkeypatch):
    monkeypatch.setattr("openadapt_agent.authoring.Path.home", lambda: tmp_path)
    assert discover_desktop_authoring_ipc() is None
    ipc_dir = tmp_path / ".openadapt"
    ipc_dir.mkdir()
    (ipc_dir / "desktop_ipc.json").write_text(
        json.dumps({"authoring": {"host": "127.0.0.1", "port": 9}}),
        encoding="utf-8",
    )
    found = discover_desktop_authoring_ipc()
    assert found is not None
    assert found["endpoint"]["host"] == "127.0.0.1"
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "openadapt_agent"
        / "authoring.py"
    ).read_text(encoding="utf-8")
    assert "http.client" not in source
    assert "urlopen" not in source


def test_pin_windows_is_coach_only_without_win_agent():
    backend, kind, close = pin_local_backend(platform="win32")
    assert backend is None
    assert kind == "windows"
    assert close is None
    session = CoachOnlySession("windows")
    bridge = AuthoringBridge(session)
    observed = bridge.dispatch("observe", {})
    assert observed["coach_only"] is True
    assert observed["agent_drive"] is False
    with pytest.raises(AuthoringError, match="COACH_ONLY"):
        bridge.dispatch("start_record", {})


def test_open_authoring_session_uses_f1_constructor_when_present(monkeypatch, tmp_path):
    captured = {}

    class Session:
        def __init__(self, backend, out_dir, *, backend_kind, app_url=None, **kwargs):
            captured["backend"] = backend
            captured["out_dir"] = Path(out_dir)
            captured["backend_kind"] = backend_kind
            captured["app_url"] = app_url

    import types
    import sys

    fake_flow = types.ModuleType("openadapt_flow")
    fake_authoring = types.ModuleType("openadapt_flow.authoring")
    fake_authoring.AuthoringSession = Session
    monkeypatch.setitem(sys.modules, "openadapt_flow", fake_flow)
    monkeypatch.setitem(sys.modules, "openadapt_flow.authoring", fake_authoring)
    monkeypatch.setattr(
        "openadapt_agent.authoring.pin_local_backend",
        lambda **kwargs: ("backend-obj", "web", None),
    )
    session = open_authoring_session(out_dir=tmp_path, url="https://example.invalid/")
    assert isinstance(session, Session)
    assert captured["backend"] == "backend-obj"
    assert captured["backend_kind"] == "web"
    assert captured["app_url"] == "https://example.invalid/"
    assert captured["out_dir"] == tmp_path


def test_open_authoring_session_windows_never_constructs_flow_session(monkeypatch, tmp_path):
    class Session:
        def __init__(self, *args, **kwargs):
            raise AssertionError("must not construct AuthoringSession for windows")

    import types
    import sys

    fake_flow = types.ModuleType("openadapt_flow")
    fake_authoring = types.ModuleType("openadapt_flow.authoring")
    fake_authoring.AuthoringSession = Session
    monkeypatch.setitem(sys.modules, "openadapt_flow", fake_flow)
    monkeypatch.setitem(sys.modules, "openadapt_flow.authoring", fake_authoring)
    session = open_authoring_session(
        out_dir=tmp_path, backend=object(), backend_kind="windows"
    )
    assert isinstance(session, CoachOnlySession)
    assert session.backend_kind == "windows"


def test_open_authoring_session_maps_f1_coach_only_error(monkeypatch, tmp_path):
    class CoachOnlyError(RuntimeError):
        def __init__(self):
            super().__init__("COACH_ONLY")
            self.code = "COACH_ONLY"

    class Session:
        def __init__(self, *args, **kwargs):
            raise CoachOnlyError()

    import types
    import sys

    fake_flow = types.ModuleType("openadapt_flow")
    fake_authoring = types.ModuleType("openadapt_flow.authoring")
    fake_authoring.AuthoringSession = Session
    monkeypatch.setitem(sys.modules, "openadapt_flow", fake_flow)
    monkeypatch.setitem(sys.modules, "openadapt_flow.authoring", fake_authoring)
    session = open_authoring_session(
        out_dir=tmp_path, backend=object(), backend_kind="web"
    )
    assert isinstance(session, CoachOnlySession)
