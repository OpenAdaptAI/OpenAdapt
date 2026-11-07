"""Local stdio transport for one fixed agent-cache task."""

from __future__ import annotations

import functools
import json
from typing import Any

from openadapt_flow.wrap import TaskArgumentsError, WrappedTask


def build_server(task: WrappedTask) -> Any:
    """Adapt the existing task/cache/ledger to MCP 1 and MCP 2."""
    import anyio
    import mcp.types as types
    from jsonschema import ValidationError, validate
    from mcp.server.lowlevel import Server

    schema = {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "parameters": task.input_schema(),
        },
        "required": ["request_id", "parameters"],
        "additionalProperties": False,
    }

    async def list_tools() -> list[Any]:
        return [
            types.Tool(
                name="run_task",
                description=(
                    "Run the configured task with these inputs. Reuse request_id to "
                    "read its original result. Only verified=true confirms a read-back. "
                    "A halt or reconciliation result requires review, not another ID."
                ),
                inputSchema=schema,
            )
        ]

    async def call_tool(name: str, arguments: dict[str, Any] | None) -> Any:
        try:
            if name != "run_task":
                raise TaskArgumentsError("unknown tool")
            validate(arguments, schema)
            assert arguments is not None
            result = await anyio.to_thread.run_sync(
                functools.partial(
                    task.call, arguments["request_id"], arguments["parameters"]
                ),
                abandon_on_cancel=False,
            )
        except (ValidationError, TaskArgumentsError):
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text='{"error":"invalid task call"}')
                ],
                isError=True,
            )
        except Exception:  # noqa: BLE001 - no raw exception values on the protocol
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text='{"error":"local execution unavailable; retain request_id and check the run"}',
                    )
                ],
                isError=True,
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))],
            structuredContent=result,
        )

    instructions = (
        "This server exposes one operator-configured task. Task, provider and checks "
        "cannot be changed by tool arguments. A miss uses the configured agent; an "
        "admitted hit uses the checked compiled tool. Keep each request_id stable. "
        "Never treat a draft or a halt as a completed write."
    )
    if hasattr(Server, "list_tools"):
        server = Server("openadapt-wrap", version="1", instructions=instructions)
        server.list_tools()(list_tools)
        server.call_tool(validate_input=False)(call_tool)
        return server

    async def list_v2(context: Any, params: Any) -> Any:
        return types.ListToolsResult(tools=await list_tools())

    async def call_v2(context: Any, params: Any) -> Any:
        return await call_tool(params.name, params.arguments)

    return Server(
        "openadapt-wrap",
        version="1",
        instructions=instructions,
        on_list_tools=list_v2,
        on_call_tool=call_v2,
    )


def serve(task: WrappedTask) -> None:
    """Serve over the normal SDK stdio channel, keeping logs off the wire."""
    import anyio
    from mcp.server.stdio import stdio_server

    from openadapt_flow.agent.mcp import _protected_stdout

    async def run() -> None:
        server = build_server(task)
        with _protected_stdout() as wire:
            kwargs = {"stdout": wire} if wire is not None else {}
            async with stdio_server(**kwargs) as (reader, writer):
                await server.run(reader, writer, server.create_initialization_options())

    anyio.run(run)
