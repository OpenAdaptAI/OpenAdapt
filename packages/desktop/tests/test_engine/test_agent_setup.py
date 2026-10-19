"""The person's agent sets up Check it over the local socket (and MCP).

The agent may read the setup, answer the record-check and values questions
and run the checks. It can't approve, and every answer it sends is recorded
as the agent's, whatever it claims.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from openadapt_desktop import agent_mcp
from openadapt_desktop.config import EngineConfig
from openadapt_desktop.socket_server import _SOCKET_COMMANDS, DesktopSocketServer


class _Dispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.emit = lambda event, data: None

    def dispatch(self, cmd: str, params: dict | None = None) -> Any:
        self.calls.append((cmd, dict(params or {})))
        if cmd == "get_workflows":
            return [{"id": "wf1", "name": "Add a triage note", "status": "not_checked"}]
        return {
            "ok": True,
            "workflow_id": "wf1",
            "record_check": {"found": True, "read": "GET /api/db", "binding": {"x": 1}, "effects": {}},
            "quiet_steps": {"evidence": "sent nothing"},
            "values": [{"name": "note", "changes": True}],
            "answers": {},
            "approval": "A person approves in OpenAdapt Desktop. An agent can't.",
        }


@pytest.fixture
def desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dispatcher = _Dispatcher()
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    discovery = tmp_path / "desktop_ipc.json"
    server = DesktopSocketServer(
        config, discovery_path=discovery, dispatcher=dispatcher, dispatch_lock=threading.RLock()
    )
    server.start()
    monkeypatch.setenv(agent_mcp.DISCOVERY_ENV, str(discovery))
    yield dispatcher
    server.stop()


def test_agent_reads_and_answers_through_the_running_desktop(desktop: _Dispatcher) -> None:
    assert agent_mcp.handle("list_workflows", {}) == [
        {"workflow_id": "wf1", "name": "Add a triage note", "status": "not_checked"}
    ]
    setup = agent_mcp.handle("get_check_setup", {"workflow_id": "wf1"})
    # The binding internals stay in the engine.
    assert "binding" not in setup["record_check"] and setup["record_check"]["read"] == "GET /api/db"
    assert "An agent can't" in setup["approval"]

    agent_mcp.handle("answer_record_check", {"workflow_id": "wf1"})
    agent_mcp.handle("answer_values", {"workflow_id": "wf1", "changes": ["note"]})
    answers = [params for cmd, params in desktop.calls if cmd == "answer_check_setup"]
    assert [params["question"] for params in answers] == ["record_check", "values"]
    assert all(params["answered_by"] == "agent" for params in answers)


def test_an_agent_claiming_to_be_the_person_is_recorded_as_the_agent(desktop: _Dispatcher) -> None:
    agent_mcp.call_desktop(
        "answer_check_setup",
        {"workflow_id": "wf1", "question": "record_check", "answer": "yes", "answered_by": "person"},
    )
    (params,) = [params for cmd, params in desktop.calls if cmd == "answer_check_setup"]
    assert params["answered_by"] == "agent"


def test_the_socket_never_approves_or_answers_a_save() -> None:
    for command in (
        "certify_qualification",
        "seal_qualification_workflow",
        "answer_item_approval",
        "run_qualification_case",
        "bind_qualification_effect",
        "set_qualification_risk",
        "run_item",
    ):
        assert command not in _SOCKET_COMMANDS
    for tool in agent_mcp.TOOLS:
        assert "approve" not in tool


def test_no_desktop_running_is_a_plain_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(agent_mcp.DISCOVERY_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(agent_mcp.DesktopUnavailable, match="isn't running"):
        agent_mcp.handle("list_workflows", {})
