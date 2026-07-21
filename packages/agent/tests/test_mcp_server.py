"""MCP layer: the low-level Server exposes the bridge's tools over MCP types."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import anyio
import mcp.types as types
import pytest

import openadapt_agent.mcp as mcp_mod
from openadapt_agent.bridge import AgentBridge
from openadapt_agent.bridge import BridgeError
from openadapt_agent.mcp import _confirm_attended_action, build_server


def test_server_builds_and_lists_bridge_tools(bundles_root, runner_config):
    bridge = AgentBridge(bundles_root, runner_config, allow_run=True)
    server = build_server(bridge)

    async def list_tools():
        handler = server.request_handlers[types.ListToolsRequest]
        result = await handler(types.ListToolsRequest(method="tools/list"))
        return result.root.tools

    tools = anyio.run(list_tools)
    names = [t.name for t in tools]
    assert "list_workflows" in names
    assert "get_run_report" in names
    assert "list_needs_attention" in names
    run_names = [name for name in names if name.startswith("run_workflow_")]
    assert len(run_names) == 1
    run_tool = next(t for t in tools if t.name == run_names[0])
    assert run_tool.inputSchema["properties"]["note"]["type"] == "string"
    assert run_tool.inputSchema["required"] == ["note"]
    assert "default" not in run_tool.inputSchema["properties"]["note"]
    assert "governed" in (run_tool.description or "")
    assert run_tool.annotations.readOnlyHint is False
    assert run_tool.annotations.destructiveHint is True
    list_tool = next(t for t in tools if t.name == "list_needs_attention")
    assert list_tool.annotations.readOnlyHint is True


def test_server_read_only_when_run_not_allowed(bundles_root, runner_config):
    bridge = AgentBridge(bundles_root, runner_config, allow_run=False)
    server = build_server(bridge)

    async def list_tools():
        handler = server.request_handlers[types.ListToolsRequest]
        result = await handler(types.ListToolsRequest(method="tools/list"))
        return [t.name for t in result.root.tools]

    names = anyio.run(list_tools)
    assert names == [
        "list_workflows",
        "get_workflow",
        "get_run_report",
        "list_needs_attention",
        "get_attention_item",
    ]


def test_bridge_refusals_are_mcp_error_results(bundles_root, runner_config):
    runner_config.runs_dir.mkdir()
    bridge = AgentBridge(bundles_root, runner_config)
    server = build_server(bridge)

    async def call_missing_item():
        handler = server.request_handlers[types.CallToolRequest]
        return await handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="get_attention_item",
                    arguments={"attention_id": "0" * 24},
                )
            )
        )

    result = anyio.run(call_missing_item).root
    assert result.isError is True
    assert "no current attention item" in result.content[0].text


def test_unexpected_local_exception_text_never_crosses_mcp(
    monkeypatch,
    bundles_root,
    runner_config,
):
    secret = "Jane Roe MRN-9911 sk_live_secret /private/protected/path"
    bridge = AgentBridge(bundles_root, runner_config)
    server = build_server(bridge)

    def fail(_name, _arguments):
        raise RuntimeError(secret)

    monkeypatch.setattr(bridge, "dispatch", fail)

    async def call():
        handler = server.request_handlers[types.CallToolRequest]
        return await handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="list_workflows",
                    arguments={},
                )
            )
        )

    result = anyio.run(call).root
    assert result.isError is True
    assert secret not in result.content[0].text
    assert "failed safely" in result.content[0].text


def test_blocking_calls_leave_the_mcp_event_loop(monkeypatch, bundles_root, runner_config):
    bridge = AgentBridge(
        bundles_root,
        runner_config,
        allow_attended_actions=True,
        attended_service=object(),
    )
    server = build_server(bridge)
    seen: dict[str, int] = {}

    def dispatch(name, _arguments):
        seen[name] = threading.get_ident()
        return {"ok": True}

    monkeypatch.setattr(bridge, "dispatch", dispatch)

    async def confirm(_server, _name):
        return None

    monkeypatch.setattr(mcp_mod, "_confirm_attended_action", confirm)

    async def call_both():
        event_thread = threading.get_ident()
        handler = server.request_handlers[types.CallToolRequest]
        calls = {
            "continue_attention": {
                "attention_id": "0" * 24,
                "capability_digest": "sha256:" + "0" * 64,
                "idempotency_key": "stable-thread-test-0001",
                "human_completed": True,
            },
            "list_needs_attention": {},
        }
        for name, arguments in calls.items():
            await handler(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(
                        name=name,
                        arguments=arguments,
                    )
                )
            )
        return event_thread

    event_thread = anyio.run(call_both)
    assert seen["continue_attention"] != event_thread
    assert seen["list_needs_attention"] != event_thread


class ElicitationSession:
    def __init__(self, *, action="accept", confirmed=True, supported=True):
        self.client_params = SimpleNamespace(
            capabilities=SimpleNamespace(
                elicitation=(SimpleNamespace(form=object()) if supported else None)
            )
        )
        self.result = SimpleNamespace(
            action=action,
            content={"confirmed": confirmed},
        )
        self.calls = []

    async def elicit_form(self, message, schema, related_request_id=None):
        self.calls.append((message, schema, related_request_id))
        return self.result


class ElicitationServer:
    def __init__(self, session):
        self.request_context = SimpleNamespace(
            session=session,
            request_id="request-123",
        )


def test_attended_action_requires_protocol_native_human_confirmation():
    session = ElicitationSession()
    anyio.run(
        _confirm_attended_action,
        ElicitationServer(session),
        "continue_attention",
    )
    message, schema, request_id = session.calls[0]
    assert "person must already have completed" in message
    assert schema["properties"]["confirmed"]["type"] == "boolean"
    assert request_id == "request-123"


@pytest.mark.parametrize(
    ("session", "error"),
    [
        (ElicitationSession(supported=False), "form elicitation"),
        (ElicitationSession(action="decline"), "declined or cancelled"),
        (ElicitationSession(confirmed=False), "declined or cancelled"),
    ],
)
def test_attended_action_refuses_missing_or_declined_human_confirmation(session, error):
    with pytest.raises(BridgeError, match=error):
        anyio.run(
            _confirm_attended_action,
            ElicitationServer(session),
            "continue_attention",
        )
