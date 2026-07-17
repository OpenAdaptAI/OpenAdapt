"""MCP layer: the low-level Server exposes the bridge's tools over MCP types."""

from __future__ import annotations

import anyio
import mcp.types as types

from openadapt_agent.bridge import AgentBridge
from openadapt_agent.mcp import build_server


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
    assert "run_demo_triage" in names
    run_tool = next(t for t in tools if t.name == "run_demo_triage")
    assert run_tool.inputSchema["properties"]["note"]["type"] == "string"
    assert "governed" in (run_tool.description or "")


def test_server_read_only_when_run_not_allowed(bundles_root, runner_config):
    bridge = AgentBridge(bundles_root, runner_config, allow_run=False)
    server = build_server(bridge)

    async def list_tools():
        handler = server.request_handlers[types.ListToolsRequest]
        result = await handler(types.ListToolsRequest(method="tools/list"))
        return [t.name for t in result.root.tools]

    names = anyio.run(list_tools)
    assert names == ["list_workflows", "get_workflow", "get_run_report"]
