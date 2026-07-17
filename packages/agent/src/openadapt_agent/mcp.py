"""MCP (stdio) transport for :class:`openadapt_agent.bridge.AgentBridge`.

Run directly::

    python -m openadapt_agent.mcp --bundles ./bundles [--allow-run] ...

or via the CLI entry point ``openadapt-agent serve``. The server speaks
MCP over stdio (what Claude Code / Claude Desktop consume). All tool logic
lives in :mod:`openadapt_agent.bridge`; this module only adapts it to the
official ``mcp`` SDK's low-level server.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from openadapt_agent.bridge import AgentBridge, BridgeError

__all__ = ["build_server", "serve"]

SERVER_NAME = "openadapt-agent"


def build_server(bridge: AgentBridge) -> Server:
    """Wrap a bridge in an ``mcp`` low-level Server (no I/O started)."""
    server: Server = Server(
        SERVER_NAME,
        instructions=(
            "EXPERIMENTAL bridge exposing compiled openadapt-flow workflow "
            "bundles as tools. run_* tools execute via the governed "
            "`openadapt-flow run` CLI and return a structured outcome: only "
            "status 'success' means the workflow completed and verified. "
            "'halt' means the run stopped with evidence (fetch it with "
            "get_run_report); 'refused' means an admission gate refused the "
            "bundle and nothing executed. Never report a halted, refused, or "
            "timed-out run as a success."
        ),
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
            )
            for spec in bridge.list_tool_specs()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None):
        try:
            # dispatch() shells out to the flow CLI (blocking); keep the
            # event loop responsive by running it in a worker thread.
            result = await anyio.to_thread.run_sync(
                lambda: bridge.dispatch(name, dict(arguments or {}))
            )
        except BridgeError as exc:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps({"error": str(exc)}),
                )
            ]
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


async def _run_stdio(bridge: AgentBridge) -> None:
    server = build_server(bridge)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def serve(bridge: AgentBridge) -> None:
    """Serve the bridge over stdio until the client disconnects."""
    anyio.run(_run_stdio, bridge)


if __name__ == "__main__":  # pragma: no cover - exercised by smoke test
    import sys

    from openadapt_agent.cli import main

    raise SystemExit(main(["serve", *sys.argv[1:]]))
