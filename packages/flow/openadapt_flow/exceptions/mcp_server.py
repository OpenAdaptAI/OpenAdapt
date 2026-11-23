"""``openadapt-flow exceptions serve``: one MCP tool any agent can call.

``raise_exception(kind, summary, screenshot_path?)`` opens a card in the
local registry (:mod:`openadapt_flow.exceptions.registry`), blocks until a
person answers it or it expires, and returns the closed answer:

    {"card_id": "sha256:…", "kind": "second_factor", "answer": "done_by_person",
     "proceed": true, "reason": null, "opened_at": "…", "decided_at": "…",
     "instruction": "A person answered done_by_person. Go on from the current screen."}

``proceed`` is true only for a signed ``continue`` or ``done_by_person``.
Anything else tells the agent to stop before any write. The tool can tell an
agent to stop; it can't make a foreign agent stop. Wrap the agent with a
guard for that.

The server speaks MCP over stdio. Heartbeats go to stderr, never stdout.
"""

from __future__ import annotations

import functools
import json
from importlib import import_module
from typing import Any, Optional

from openadapt_types.exception_card import EXCEPTION_KINDS

from openadapt_flow.exceptions.registry import (
    DEFAULT_DEADLINE_S,
    AnswerVerifier,
    ExceptionRegistry,
    ExceptionRegistryError,
)

TOOL_NAME = "raise_exception"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(EXCEPTION_KINDS)},
        "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "description": (
                "What you see, in a sentence. It is scrubbed and cut to 200 "
                "characters before a person sees it; never put a code, a "
                "password or a record's values here."
            ),
        },
        "screenshot_path": {
            "type": "string",
            "maxLength": 4096,
            "description": "A local PNG of the screen. Kept only as a scrubbed copy.",
        },
    },
    "required": ["kind", "summary"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Ask a person, then wait for the answer. Call it when you hit a one-time "
    "code prompt (second_factor), a dialog you didn't expect (unexpected_dialog), "
    "a sign-in page partway through (session_expired), no progress "
    "(timeout_or_stall), a record that may already exist (possible_duplicate), "
    "two records you can't tell apart (ambiguous_record), or a task you can't "
    "finish (cannot_complete). Never type a code or password yourself. Go on "
    "only when the result says proceed=true; otherwise stop and write nothing."
)


class RaiseException:
    """The tool's work, without the protocol (used by tests and the server)."""

    def __init__(
        self,
        registry: ExceptionRegistry,
        *,
        run_ref: str,
        verify: AnswerVerifier,
        task_id: Optional[str] = None,
        deadline_s: float = DEFAULT_DEADLINE_S,
        poll_s: float = 0.5,
    ) -> None:
        self.registry = registry
        self.run_ref = run_ref
        self.verify = verify
        self.task_id = task_id
        self.deadline_s = deadline_s
        self.poll_s = poll_s

    def __call__(
        self, kind: str, summary: str, screenshot_path: Optional[str] = None
    ) -> dict[str, Any]:
        card = self.registry.open(
            self.run_ref,
            kind,
            summary,
            task_id=self.task_id,
            screenshot_path=screenshot_path,
            deadline_s=self.deadline_s,
        )
        return self.registry.wait(card, verify=self.verify, poll_s=self.poll_s).wire()


def _error(types: Any, error_field: str, code: str) -> Any:
    body = {
        "error": code,
        "proceed": False,
        "instruction": "Stop now. Make no further writes for this task.",
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body))],
        **{error_field: True},
    )


def build_server(tool: RaiseException) -> Any:
    """The MCP server (MCP 1 decorator API or MCP 2 callbacks)."""

    import anyio
    from jsonschema import ValidationError, validate
    from mcp.server.lowlevel import Server

    types: Any
    try:
        types = import_module("mcp.types")
    except ModuleNotFoundError as error:
        if error.name != "mcp.types":
            raise
        types = import_module("mcp_types")
    modern = types.__name__ == "mcp_types"
    input_schema_field = "input_schema" if modern else "inputSchema"
    error_field = "is_error" if modern else "isError"
    structured_field = "structured_content" if modern else "structuredContent"

    async def list_tools() -> list[Any]:
        return [
            types.Tool(
                name=TOOL_NAME,
                description=DESCRIPTION,
                **{input_schema_field: INPUT_SCHEMA},
            )
        ]

    async def call_tool(name: str, arguments: Optional[dict[str, Any]]) -> Any:
        if name != TOOL_NAME:
            return _error(types, error_field, "unknown_tool")
        try:
            validate(arguments, INPUT_SCHEMA)
        except ValidationError:
            return _error(types, error_field, "invalid_arguments")
        assert arguments is not None
        try:
            result = await anyio.to_thread.run_sync(
                functools.partial(
                    tool,
                    arguments["kind"],
                    arguments["summary"],
                    arguments.get("screenshot_path"),
                ),
                abandon_on_cancel=False,
            )
        except ExceptionRegistryError as exc:
            return _error(types, error_field, exc.code)
        except Exception:  # noqa: BLE001 - no raw exception text on the wire
            return _error(types, error_field, "registry_unavailable")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))],
            **{structured_field: result},
        )

    instructions = (
        "One tool, raise_exception, asks a person and waits. Go on only when its "
        "result has proceed=true. Never relay a code or a password through it."
    )
    if hasattr(Server, "list_tools"):
        server = Server("openadapt-exceptions", version="1", instructions=instructions)
        server.list_tools()(list_tools)
        server.call_tool(validate_input=False)(call_tool)
        return server

    async def list_v2(context: Any, params: Any) -> Any:
        return types.ListToolsResult(tools=await list_tools())

    async def call_v2(context: Any, params: Any) -> Any:
        return await call_tool(params.name, params.arguments)

    callback_server: Any = Server
    return callback_server(
        "openadapt-exceptions",
        version="1",
        instructions=instructions,
        on_list_tools=list_v2,
        on_call_tool=call_v2,
    )


def serve(tool: RaiseException) -> None:
    """Serve over stdio, keeping every log line off the protocol."""

    import anyio
    from mcp.server.stdio import stdio_server

    from openadapt_flow.agent.mcp import _protected_stdout

    async def run() -> None:
        server = build_server(tool)
        with _protected_stdout() as wire:
            kwargs = {"stdout": wire} if wire is not None else {}
            async with stdio_server(**kwargs) as (reader, writer):
                await server.run(reader, writer, server.create_initialization_options())

    anyio.run(run)


__all__ = [
    "DESCRIPTION",
    "INPUT_SCHEMA",
    "TOOL_NAME",
    "RaiseException",
    "build_server",
    "serve",
]
