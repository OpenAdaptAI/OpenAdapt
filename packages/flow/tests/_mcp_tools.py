"""Exercise generated tools through the genuine SDK's public request API."""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from types import ModuleType
from typing import Any


def sdk_modules() -> tuple[Any, Any, Any]:
    try:
        server = importlib.import_module("mcp.server.mcpserver")
    except ModuleNotFoundError as error:
        if error.name != "mcp.server.mcpserver":
            raise
        server = importlib.import_module("mcp.server.fastmcp")
        return (
            server.FastMCP,
            importlib.import_module("mcp.server.fastmcp.exceptions"),
            importlib.import_module("mcp.types"),
        )
    return (
        server.MCPServer,
        importlib.import_module("mcp.server.mcpserver.exceptions"),
        importlib.import_module("mcp_types"),
    )


def registered_server(namespace: Mapping[str, Any] | ModuleType) -> Any:
    server_type, _, _ = sdk_modules()
    values = vars(namespace).values() if isinstance(namespace, ModuleType) else namespace.values()
    servers = [value for value in values if isinstance(value, server_type)]
    assert len(servers) == 1
    return servers[0]


async def call_generated_tool(
    namespace: Mapping[str, Any] | ModuleType,
    arguments: dict[str, Any],
    *,
    observed_shapes: list[str] | None = None,
) -> dict[str, Any]:
    server = registered_server(namespace)
    tools = await server.list_tools()
    assert len(tools) == 1
    # Both SDKs create their own genuine request context at this public seam.
    # Validation errors propagate; they are never normalized into tool replies.
    result = await server.call_tool(tools[0].name, arguments)
    _, _, types = sdk_modules()
    structured = None
    if isinstance(result, types.CallToolResult):
        if hasattr(result, "is_error"):
            assert not result.is_error
            structured = result.structured_content
        else:
            assert not result.isError
            structured = result.structuredContent
        content = result.content
        shape = "CallToolResult"
    elif isinstance(result, tuple):
        assert len(result) == 2
        content, structured = result
        assert isinstance(structured, dict)
        shape = "content_and_structured_tuple"
    else:
        assert isinstance(result, list)
        content = result
        shape = "content_list"
    assert isinstance(content, list) and len(content) == 1
    block = content[0]
    assert isinstance(block, types.TextContent) and block.type == "text"
    decoded = json.loads(block.text)
    assert isinstance(decoded, dict)
    # Generic dict return annotations may wrap structured content in `result`;
    # the JSON text still carries the original handler reply in both SDKs.
    if structured is not None:
        assert structured == decoded or structured == {"result": decoded}
    if observed_shapes is not None:
        observed_shapes.append(shape)
    return decoded
