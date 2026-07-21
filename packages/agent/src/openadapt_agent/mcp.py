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
import logging
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from openadapt_agent.attended import ATTENDED_TOOLS
from openadapt_agent.bridge import AgentBridge, BridgeError

__all__ = ["build_server", "serve"]

SERVER_NAME = "openadapt-agent"
_LOG = logging.getLogger(__name__)

_CONFIRMATION_COPY = {
    "continue_attention": (
        "A person must already have completed the paused task in the visible "
        "application. Confirm that OpenAdapt may verify that outcome and resume "
        "the governed workflow after it. The completed task will not be performed again."
    ),
    "skip_attention": (
        "Confirm that this paused task is not applicable. OpenAdapt will skip "
        "only if the compiled workflow and exact signed capability declare that "
        "skip safe; otherwise it will refuse."
    ),
    "teach_attention": (
        "Confirm that OpenAdapt should record an audited request for a corrective "
        "demonstration. This does not directly rewrite or promote the workflow."
    ),
    "escalate_attention": (
        "Confirm that OpenAdapt should record an audited escalation and preserve "
        "the exact durable pause for qualified assistance."
    ),
}


async def _confirm_attended_action(server: Server, name: str) -> None:
    """Require a second, protocol-native human confirmation before mutation."""
    context = server.request_context
    session = context.session
    params = session.client_params
    elicitation = params.capabilities.elicitation if params is not None else None
    if elicitation is None or elicitation.form is None:
        raise BridgeError(
            "attended actions require an MCP client with form elicitation so "
            "the local operator can confirm this exact decision; use Flow's "
            "attended console/CLI when the client does not support elicitation"
        )
    result = await session.elicit_form(
        _CONFIRMATION_COPY[name],
        {
            "type": "object",
            "properties": {
                "confirmed": {
                    "type": "boolean",
                    "title": "I confirm this attended action",
                }
            },
            "required": ["confirmed"],
        },
        related_request_id=context.request_id,
    )
    content = result.content or {}
    if result.action != "accept" or content.get("confirmed") is not True:
        raise BridgeError(
            "the local operator declined or cancelled; no attended action was submitted"
        )


def build_server(bridge: AgentBridge) -> Server:
    """Wrap a bridge in an ``mcp`` low-level Server (no I/O started)."""
    server: Server = Server(
        SERVER_NAME,
        instructions=(
            "Local bridge exposing compiled openadapt-flow workflow bundles "
            "and PHI-safe Needs Attention items as tools. run_* tools execute via the governed "
            "`openadapt-flow run` CLI and return a structured outcome: only "
            "status 'success' means the workflow completed and verified. "
            "'halt' means the run stopped and protected evidence remains in "
            "the local operator experience; get_run_report returns only a "
            "PHI-safe status/count summary by default. 'refused' means an "
            "admission gate refused the bundle and nothing executed. "
            "Continue/Skip require a human action "
            "plus protocol-native operator elicitation, an exact signed "
            "capability, live revalidation, and a stable idempotency key; they "
            "never re-actuate the human-completed step. "
            "Never report a halted, refused, or timed-out run as a success."
        ),
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
                annotations=(
                    types.ToolAnnotations(**spec.annotations)
                    if spec.annotations is not None
                    else None
                ),
            )
            for spec in bridge.list_tool_specs()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None):
        try:
            if name in ATTENDED_TOOLS:
                await _confirm_attended_action(server, name)

            def call() -> dict[str, Any]:
                return bridge.dispatch(name, dict(arguments or {}))

            # CLI runs and filesystem projections are blocking. Live attended
            # actions synchronously submit to their own non-async backend-owner
            # thread, so every MCP call can leave the event loop responsive.
            result = await anyio.to_thread.run_sync(call)
        except BridgeError as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps({"error": str(exc)}),
                    )
                ],
                isError=True,
            )
        except Exception:
            _LOG.exception("MCP tool dispatch failed inside the protected boundary")
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": (
                                    "Local tool execution failed safely. Inspect "
                                    "the protected local logs before retrying."
                                )
                            }
                        ),
                    )
                ],
                isError=True,
            )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


async def _run_stdio(bridge: AgentBridge) -> None:
    server = build_server(bridge)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve(bridge: AgentBridge) -> None:
    """Serve the bridge over stdio until the client disconnects."""
    anyio.run(_run_stdio, bridge)


if __name__ == "__main__":  # pragma: no cover - exercised by smoke test
    import sys

    from openadapt_agent.cli import main

    raise SystemExit(main(["serve", *sys.argv[1:]]))
