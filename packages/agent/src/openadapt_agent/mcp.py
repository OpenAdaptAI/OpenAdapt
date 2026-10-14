"""MCP (stdio) transport for the local OpenAdapt bridges.

Run directly::

    python -m openadapt_agent.mcp --bundles ./bundles [--allow-run] ...
    python -m openadapt_agent.mcp --authoring

or via the CLI entry point ``openadapt-agent serve``. The server speaks
MCP over stdio (what Claude Code / Claude Desktop consume). Tool logic
lives in :mod:`openadapt_agent.bridge` and :mod:`openadapt_agent.authoring`;
this module only adapts them to the official ``mcp`` SDK's low-level
server. This package does not open an HTTP listener.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator

import anyio
import mcp.server.stdio as _mcp_stdio
import mcp.types as types
from jsonschema import ValidationError, validate
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from openadapt_agent.attended import ATTENDED_TOOLS
from openadapt_agent.authoring import AuthoringBridge
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
    "reject_attention": (
        "Confirm that OpenAdapt must end this run without resuming it. This rejection "
        "dispatches no new action, but earlier run actions may have effects. Review the "
        "protected local report and transaction outcome. The durable pause remains only "
        "as the audit record. Use escalation instead when a qualified colleague must "
        "inspect and possibly continue the run."
    ),
}


async def _confirm_attended_action(context: Any, name: str) -> None:
    """Require a second, protocol-native human confirmation before mutation."""
    session = context.session
    params = session.client_params
    elicitation = params.capabilities.elicitation if params is not None else None
    if (
        elicitation is None
        or elicitation.form is None
        or not getattr(session, "can_send_request", True)
    ):
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


def _server_instructions(
    authoring: AuthoringBridge | None, bridge: AgentBridge | None = None
) -> str:
    text = ""
    if bridge is not None:
        text = (
            "OpenAdapt enters information into apps on this computer and checks "
            "that it saved. Call list_workflows to see what it can do. Then call "
            "run_workflow with a workflow name, its inputs, and your own "
            "request_id for this piece of work. Act on the outcome: done means "
            "saved and checked; needs_review means it stopped before saving and "
            "a person decides, so don't start the same work again; "
            "not_sure_if_saved means a person must check the record, so never "
            "retry it; did_not_run means nothing was written, so fix the problem "
            "and retry only when safe_to_retry is true. Reuse the same request_id "
            "when you retry: the same id never writes twice. If the outcome is "
            "running, call get_run with the run_id. Tell the user what_happened "
            "in your own words and never call anything but done a success."
        )
        if bridge.mode == "sandbox":
            text += (
                " This server is a sandbox: it runs against a synthetic app, so "
                "no real record changes. Use the sandbox_case input to see each "
                "outcome."
            )
        if bridge.legacy_tools:
            text += (
                " Older tools stay available for existing clients: get_workflow, "
                "get_run_report, list_needs_attention, get_attention_item, and "
                "per-workflow run_<id> tools (deprecated; they take no "
                "request_id). Attended Continue/Skip require a human action plus "
                "protocol-native operator elicitation, an exact signed "
                "capability, live revalidation, and a stable idempotency key; "
                "they never re-actuate the human-completed step. Reject ends the "
                "run and dispatches no new action, but earlier run effects still "
                "require review of the protected local outcome."
            )
    if authoring is not None:
        text += (
            " --authoring adds first-demo tools observe, start_record, click, "
            "and halt over this same local stdio process. Local stdio may also "
            "type through the recorder; hosted MCP has no type tool. Human type "
            "during pause_for_input is record_observed, never type_text. "
            "compile returns needs_human_admit; admit is the one-token human "
            "ok. An agent click never paints VERIFIED. --authoring does not "
            "enable run tools. This process must not be port-forwarded or "
            "served over HTTP."
        )
    return text.strip()


def build_server(
    bridge: AgentBridge | None = None,
    authoring: AuthoringBridge | None = None,
) -> Server:
    """Wrap workflow and/or authoring bridges in an MCP Server (no I/O started)."""
    if bridge is None and authoring is None:
        raise ValueError("MCP server requires a workflow bridge or an authoring bridge")

    def _tool_specs():
        return (
            *(bridge.list_tool_specs() if bridge is not None else ()),
            *(authoring.list_tool_specs() if authoring is not None else ()),
        )

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
                **({"outputSchema": spec.output_schema} if spec.output_schema else {}),
                **({"_meta": spec.meta} if spec.meta is not None else {}),
            )
            for spec in _tool_specs()
        ]

    async def _call_tool(
        context: Any, name: str, arguments: dict[str, Any] | None
    ) -> types.CallToolResult:
        try:
            spec = next((spec for spec in _tool_specs() if spec.name == name), None)
            if spec is None:
                raise BridgeError("unknown or unavailable tool name")
            # MCP 2 removed the low-level decorator's schema validation.
            # Keep it explicit on both SDKs, before confirmation or dispatch.
            # ValidationError text contains input values, so never return it.
            try:
                validate(arguments or {}, spec.input_schema)
            except ValidationError:
                raise BridgeError(
                    getattr(spec, "usage", None)
                    or "tool arguments do not match the input schema"
                ) from None
            if name in ATTENDED_TOOLS:
                await _confirm_attended_action(context, name)

            def call() -> dict[str, Any]:
                payload = dict(arguments or {})
                if authoring is not None and authoring.handles(name):
                    return authoring.dispatch(name, payload)
                if bridge is None:
                    raise BridgeError("unknown tool name")
                return bridge.dispatch(name, payload)

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
        # Both SDKs accept wire aliases in constructors. Always return the
        # explicit result type: MCP 2 no longer wraps a bare content list.
        text = [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        if getattr(spec, "output_schema", None):
            return types.CallToolResult(content=text, structuredContent=result)
        return types.CallToolResult(content=text)

    if hasattr(Server, "list_tools"):
        server = Server(SERVER_NAME, instructions=_server_instructions(authoring, bridge))
        server.list_tools()(_list_tools)

        async def _call_v1(name: str, arguments: dict[str, Any] | None):
            return await _call_tool(server.request_context, name, arguments)

        # The common handler enforces the same schema without reflecting inputs.
        server.call_tool(validate_input=False)(_call_v1)
    else:

        async def _list_v2(context: Any, params: Any) -> types.ListToolsResult:
            return types.ListToolsResult(tools=await _list_tools())

        async def _call_v2(context: Any, params: types.CallToolRequestParams):
            return await _call_tool(context, params.name, params.arguments)

        server = Server(
            SERVER_NAME,
            instructions=_server_instructions(authoring, bridge),
            on_list_tools=_list_v2,
            on_call_tool=_call_v2,
        )
    return server


@contextmanager
def _protected_stdout() -> Iterator[Any]:
    """Keep stray writes off the MCP wire on SDKs that don't divert fd 1.

    Background runs call Flow and browser tooling, which may print. MCP 2's
    stdio transport already points fd 1 at stderr while it serves; MCP 1's
    does not, so this does it here and hands the transport a private copy.
    """
    if hasattr(_mcp_stdio, "_claim_fd"):
        yield None
        return
    wire_fd = os.dup(1)
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    wire = os.fdopen(wire_fd, "wb")
    try:
        yield anyio.wrap_file(io.TextIOWrapper(wire, encoding="utf-8"))
    finally:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        os.dup2(saved_fd, 1)
        os.close(saved_fd)


async def _run_stdio(
    bridge: AgentBridge | None,
    authoring: AuthoringBridge | None = None,
) -> None:
    server = build_server(bridge, authoring=authoring)
    with _protected_stdout() as wire:
        kwargs = {"stdout": wire} if wire is not None else {}
        async with stdio_server(**kwargs) as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )


def serve(
    bridge: AgentBridge | None = None,
    authoring: AuthoringBridge | None = None,
) -> None:
    """Serve the bridge over stdio until the client disconnects."""
    anyio.run(_run_stdio, bridge, authoring)


if __name__ == "__main__":  # pragma: no cover - exercised by smoke test
    import sys

    from openadapt_agent.cli import main

    raise SystemExit(main(["serve", *sys.argv[1:]]))
