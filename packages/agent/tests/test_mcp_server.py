"""MCP discovery, invocation, and confirmation through the real SDK transport."""

from __future__ import annotations

import json
import sys
import threading

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import mcp.types as types
import pytest

from openadapt_agent.attended import ATTENDED_TOOLS
from openadapt_agent.bridge import AgentBridge
from openadapt_agent.mcp import build_server


def wire(model):
    """Both SDK generations serialize the same protocol aliases."""
    return model.model_dump(by_alias=True, mode="json")


def action_arguments(name="continue_attention"):
    return {
        "attention_id": "0" * 24,
        "capability_digest": "sha256:" + "0" * 64,
        "idempotency_key": "stable-mcp-test-0001",
        ATTENDED_TOOLS[name].confirmation: True,
    }


def test_server_builds_and_lists_bridge_tools(bundles_root, runner_config, mcp_client):
    server = build_server(AgentBridge(bundles_root, runner_config, allow_run=True))

    async def list_tools():
        async with mcp_client(server) as client:
            return wire(await client.list_tools())["tools"]

    tools = anyio.run(list_tools)
    names = [t["name"] for t in tools]
    assert {"list_workflows", "get_run_report", "list_needs_attention"} <= set(names)
    run_tools = [t for t in tools if t["name"].startswith("run_workflow_")]
    assert len(run_tools) == 1
    run_tool = run_tools[0]
    assert run_tool["inputSchema"]["properties"]["note"]["type"] == "string"
    assert run_tool["inputSchema"]["required"] == ["note"]
    assert "default" not in run_tool["inputSchema"]["properties"]["note"]
    assert "governed" in run_tool["description"]
    assert run_tool["annotations"]["readOnlyHint"] is False
    assert run_tool["annotations"]["destructiveHint"] is True
    assert run_tool["_meta"] == {"requires_seal": True}
    assert "requires_seal: true" in run_tool["description"]
    assert "unsigned success" in run_tool["description"]
    list_tool = next(t for t in tools if t["name"] == "list_needs_attention")
    assert list_tool["annotations"]["readOnlyHint"] is True
    assert list_tool.get("_meta") is None


def test_server_read_only_when_run_not_allowed(bundles_root, runner_config, mcp_client):
    bridge = AgentBridge(bundles_root, runner_config, allow_run=False)
    server = build_server(bridge)

    async def probe():
        async with mcp_client(server) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            result = await client.call_tool("list_workflows", {})
            return names, json.loads(result.content[0].text), wire(result)

    names, payload, result = anyio.run(probe)
    assert names == [
        "list_workflows",
        "get_workflow",
        "get_run_report",
        "list_needs_attention",
        "get_attention_item",
    ]
    assert result["isError"] is False
    assert payload["run_tools_enabled"] is False
    assert len(payload["workflows"]) == 1


def test_server_exports_reject_as_a_destructive_local_mutation(
    bundles_root,
    runner_config,
    mcp_client,
):
    server = build_server(
        AgentBridge(
            bundles_root, runner_config, allow_attended_actions=True, attended_service=object()
        )
    )

    async def reject_tool():
        async with mcp_client(server) as client:
            return next(
                t
                for t in wire(await client.list_tools())["tools"]
                if t["name"] == "reject_attention"
            )

    annotations = anyio.run(reject_tool)["annotations"]
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is True
    assert annotations["idempotentHint"] is True
    assert annotations["openWorldHint"] is False


def test_bridge_refusals_are_mcp_error_results(bundles_root, runner_config, mcp_client):
    runner_config.runs_dir.mkdir()
    server = build_server(AgentBridge(bundles_root, runner_config))

    async def call():
        async with mcp_client(server) as client:
            return await client.call_tool("get_attention_item", {"attention_id": "0" * 24})

    result = wire(anyio.run(call))
    assert result["isError"] is True
    assert "no current attention item" in result["content"][0]["text"]


def test_unexpected_local_exception_text_never_crosses_mcp(
    monkeypatch,
    bundles_root,
    runner_config,
    mcp_client,
):
    secret = "Jane Roe MRN-9911 sk_live_secret /private/protected/path"
    bridge = AgentBridge(bundles_root, runner_config)

    def fail(_name, _arguments):
        raise RuntimeError(secret)

    monkeypatch.setattr(bridge, "dispatch", fail)

    async def call():
        async with mcp_client(build_server(bridge)) as client:
            return await client.call_tool("list_workflows", {})

    result = wire(anyio.run(call))
    assert result["isError"] is True
    assert secret not in json.dumps(result)
    assert "failed safely" in result["content"][0]["text"]


@pytest.mark.parametrize("name", list(ATTENDED_TOOLS))
@pytest.mark.parametrize("answer", ["accept", "decline", "cancel", "unconfirmed", "unsupported"])
def test_attended_confirmation_over_real_session(
    name,
    answer,
    monkeypatch,
    bundles_root,
    runner_config,
    mcp_client,
):
    bridge = AgentBridge(
        bundles_root, runner_config, allow_attended_actions=True, attended_service=object()
    )
    calls = []
    prompts = []

    def dispatch(tool, arguments):
        calls.append((tool, arguments, threading.get_ident()))
        return {"ok": True}

    monkeypatch.setattr(bridge, "dispatch", dispatch)

    async def elicit(context, params):
        prompts.append((context.request_id, wire(params)))
        return types.ElicitResult(
            action="accept" if answer == "unconfirmed" else answer,
            content={"confirmed": answer == "accept"},
        )

    async def call():
        event_thread = threading.get_ident()
        kwargs = {} if answer == "unsupported" else {"elicitation_callback": elicit}
        async with mcp_client(build_server(bridge), **kwargs) as client:
            result = await client.call_tool(name, action_arguments(name))
            if answer == "accept":
                await client.call_tool("list_needs_attention", {})
            return event_thread, wire(result)

    event_thread, result = anyio.run(call)
    assert result["isError"] is (answer != "accept")
    if answer == "accept":
        assert [(tool, args) for tool, args, _ in calls] == [
            (name, action_arguments(name)),
            ("list_needs_attention", {}),
        ]
        assert all(thread != event_thread for _, _, thread in calls)
    else:
        assert calls == []
    if answer == "unsupported":
        assert prompts == []
        assert "form elicitation" in result["content"][0]["text"]
    else:
        assert len(prompts) == 1
        request_id, prompt = prompts[0]
        assert request_id is not None
        assert prompt["requestedSchema"]["properties"]["confirmed"]["type"] == "boolean"
        if name == "reject_attention":
            assert "earlier run actions may have effects" in prompt["message"]
        if name == "continue_attention":
            assert "person must already have completed" in prompt["message"]


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("continue_attention", {}),
        ("continue_attention", {**action_arguments(), "human_completed": False}),
        ("continue_attention", {**action_arguments(), "attention_id": "private-input-value"}),
        ("continue_attention", {**action_arguments(), "extra": "private-input-value"}),
        ("get_workflow", {"workflow": 7}),
        ("unknown_tool", {}),
    ],
)
def test_invalid_arguments_never_confirm_or_dispatch(
    name,
    arguments,
    monkeypatch,
    bundles_root,
    runner_config,
    mcp_client,
):
    bridge = AgentBridge(
        bundles_root, runner_config, allow_attended_actions=True, attended_service=object()
    )
    calls = []

    def dispatch(*args):
        calls.append(args)
        return {"ok": True}

    async def elicit(*args):
        calls.append(args)
        return types.ElicitResult(action="accept", content={"confirmed": True})

    monkeypatch.setattr(bridge, "dispatch", dispatch)

    async def call():
        async with mcp_client(build_server(bridge), elicitation_callback=elicit) as client:
            return wire(await client.call_tool(name, arguments))

    result = anyio.run(call)
    assert result["isError"] is True
    assert calls == []
    assert "private-input-value" not in json.dumps(result)


@pytest.mark.skipif(not hasattr(ClientSession, "discover"), reason="MCP 2 protocol only")
def test_modern_discovery_preserves_tools_and_refuses_unavailable_confirmation(
    monkeypatch,
    bundles_root,
    runner_config,
    mcp_client,
):
    bridge = AgentBridge(
        bundles_root, runner_config, allow_attended_actions=True, attended_service=object()
    )
    calls = []
    monkeypatch.setattr(bridge, "dispatch", lambda *args: calls.append(args))

    async def probe():
        async with mcp_client(build_server(bridge), modern=True) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            result = await client.call_tool("continue_attention", action_arguments())
            return names, wire(result)

    names, result = anyio.run(probe)
    assert "continue_attention" in names
    assert result["isError"] is True
    assert "form elicitation" in result["content"][0]["text"]
    assert calls == []


def test_cli_stdio_discovers_and_calls_without_protocol_contamination(bundles_root, tmp_path):
    async def probe():
        with anyio.fail_after(15):
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "openadapt_agent.mcp",
                    "--bundles",
                    str(bundles_root),
                    "--runs-dir",
                    str(tmp_path / "runs"),
                ],
            )
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as client:
                    initialized = wire(await client.initialize())
                    names = [t.name for t in (await client.list_tools()).tools]
                    result = wire(await client.call_tool("list_workflows", {}))
                    refused = wire(await client.call_tool("continue_attention", action_arguments()))
                    return initialized, names, result, refused

    initialized, names, result, refused = anyio.run(probe)
    assert initialized["serverInfo"]["name"] == "openadapt-agent"
    assert "list_workflows" in names
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["run_tools_enabled"] is False
    assert refused["isError"] is True


def test_run_tool_preserves_real_flow_admission_refusal(bundles_root, tmp_path, mcp_client):
    from openadapt_agent.runner import RunnerConfig

    config = RunnerConfig(runs_dir=tmp_path / "runs", timeout_s=15)
    server = build_server(AgentBridge(bundles_root, config, allow_run=True))

    async def call():
        async with mcp_client(server) as client:
            tool = next(
                t for t in (await client.list_tools()).tools if t.name.startswith("run_workflow_")
            )
            return await client.call_tool(tool.name, {"note": "Synthetic test input"})

    result = anyio.run(call)
    payload = json.loads(result.content[0].text)
    # This unsigned synthetic bundle cannot actuate. Execute the installed
    # Flow CLI and check its real governed refusal, without a subprocess stub.
    assert payload["status"] == "refused"
    assert payload["success"] is False
    assert payload["sealed"] is False
    assert "protected" not in payload
