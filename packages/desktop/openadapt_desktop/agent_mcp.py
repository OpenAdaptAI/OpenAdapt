"""Let the person's own agent set up Check it over MCP (stdio).

Run it from the agent's MCP settings while OpenAdapt Desktop is open::

    python -m openadapt_desktop.agent_mcp

It forwards to the running Desktop engine over its loopback socket
(``~/.openadapt/desktop_ipc.json``, owner-only, with a per-session token), so
the window and the agent see the same workflows and answers. The agent can:

- read what OpenAdapt filled in from a recording (``get_check_setup``);
- answer "Is this the right record check?" with yes, and "Which values change
  per item?";
- run the practice item and the safety drills (``run_checks``). The practice
  item saves for real, so Flow stops before its save until a person approves
  it in the Desktop window.

It can't approve a workflow, change a check, or approve a save. Answers it
gives are recorded as the agent's, and the window shows them as such until a
person approves. This process opens no network listener.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

DISCOVERY_ENV = "OPENADAPT_DESKTOP_IPC"
DEFAULT_DISCOVERY = Path.home() / ".openadapt" / "desktop_ipc.json"
SERVER_NAME = "openadapt-desktop-setup"

INSTRUCTIONS = (
    "OpenAdapt Desktop turns a recorded workflow into one that runs on its own "
    "and checks every save. Before it runs, Check it asks three things. You can "
    "answer the first two for the person: call list_workflows, then "
    "get_check_setup to read what OpenAdapt learned from the recording. If the "
    "record check it learned matches what the workflow saves, call "
    "answer_record_check. Then answer_values with the names of the values that "
    "change from one item to the next. run_checks runs one practice item (a "
    "person approves its save in the Desktop window) and the safety drills. The "
    "third question, approving the workflow, is the person's: tell them to "
    "approve it in OpenAdapt Desktop. Never say the workflow is ready until they "
    "have."
)


class DesktopUnavailable(RuntimeError):
    """OpenAdapt Desktop isn't running, or refused the call."""


def _discovery() -> dict[str, Any]:
    path = Path(os.environ.get(DISCOVERY_ENV) or DEFAULT_DISCOVERY)
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise DesktopUnavailable(
            "OpenAdapt Desktop isn't running on this computer. Open it and try again."
        ) from exc
    if not isinstance(data, dict) or not data.get("port") or not data.get("token"):
        raise DesktopUnavailable("OpenAdapt Desktop's local address file is incomplete.")
    if str(data.get("host") or "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
        raise DesktopUnavailable("OpenAdapt Desktop's local address isn't on this computer.")
    return data


def call_desktop(cmd: str, params: dict[str, Any], *, timeout: float = 60.0) -> Any:
    """Send one setup command to the running Desktop and wait for its answer."""

    found = _discovery()
    frame = json.dumps({"type": cmd, "data": params, "token": found["token"]}) + "\n"
    with socket.create_connection(
        (str(found.get("host") or "127.0.0.1"), int(found["port"])), timeout=timeout
    ) as conn:
        conn.sendall(frame.encode("utf-8"))
        buffer = ""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                raise DesktopUnavailable("OpenAdapt Desktop closed the connection.")
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                message = json.loads(line)
                kind = message.get("type")
                data = message.get("data") or {}
                if kind == "check_setup" and data.get("cmd") == cmd:
                    return data.get("result")
                if kind == "recording_error":
                    raise DesktopUnavailable(str(data.get("error") or "Desktop refused the call."))


def _plain_setup(setup: dict[str, Any]) -> dict[str, Any]:
    """The setup without the binding internals an agent doesn't need."""

    if not setup or not setup.get("ok"):
        return setup
    learned = dict(setup.get("record_check") or {})
    learned.pop("binding", None)
    learned.pop("effects", None)
    return {
        "workflow_id": setup.get("workflow_id"),
        "record_check": learned,
        "steps_that_only_read": (setup.get("quiet_steps") or {}).get("evidence"),
        "values": setup.get("values"),
        "answers": setup.get("answers"),
        "approval": setup.get("approval"),
    }


TOOLS: dict[str, dict[str, Any]] = {
    "list_workflows": {
        "description": "The person's recorded workflows in OpenAdapt Desktop, with their ids.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "get_check_setup": {
        "description": (
            "What OpenAdapt filled in for one workflow from its recording: the record "
            "check it learned (which read, what it checks, what its fault test caught "
            "and missed), which steps only read, the values and whether each seems to "
            "change per item, and what was already answered and by whom."
        ),
        "schema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
            "additionalProperties": False,
        },
    },
    "answer_record_check": {
        "description": (
            "Answer yes to 'Is this the right record check?' for a workflow. Only when "
            "the learned read is where the app keeps what this workflow saves. Recorded "
            "as your answer; the person sees it before approving."
        ),
        "schema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
            "additionalProperties": False,
        },
    },
    "answer_values": {
        "description": (
            "Answer 'Which values change per item?' with the names of the values that "
            "change. Any other value is filled in with its recorded value on every item."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "changes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["workflow_id", "changes"],
            "additionalProperties": False,
        },
    },
    "run_checks": {
        "description": (
            "Run one practice item with the recorded values, then the safety drills. "
            "The practice item saves for real, so it waits for the person to approve "
            "its save in the Desktop window. Can take several minutes."
        ),
        "schema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
            "additionalProperties": False,
        },
    },
}


def handle(name: str, arguments: dict[str, Any]) -> Any:
    """Run one tool against the running Desktop."""

    if name == "list_workflows":
        workflows = call_desktop("get_workflows", {}) or []
        return [
            {"workflow_id": item.get("id"), "name": item.get("name"), "status": item.get("status")}
            for item in workflows
            if isinstance(item, dict)
        ]
    workflow_id = str(arguments.get("workflow_id") or "")
    if name == "get_check_setup":
        return _plain_setup(call_desktop("get_check_setup", {"workflow_id": workflow_id}))
    if name == "answer_record_check":
        return _plain_setup(
            call_desktop(
                "answer_check_setup",
                {"workflow_id": workflow_id, "question": "record_check", "answer": "yes"},
            )
        )
    if name == "answer_values":
        changes = [str(item) for item in arguments.get("changes") or []]
        return _plain_setup(
            call_desktop(
                "answer_check_setup",
                {"workflow_id": workflow_id, "question": "values", "changes": changes},
            )
        )
    if name == "run_checks":
        return call_desktop("run_check_cases", {"workflow_id": workflow_id}, timeout=3600.0)
    raise ValueError(f"unknown tool {name}")


def build_server() -> Any:
    """The MCP server (no I/O started). Needs the ``mcp`` package."""

    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server: Any = Server(SERVER_NAME, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=name, description=spec["description"], inputSchema=spec["schema"])
            for name, spec in TOOLS.items()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        try:
            result = await anyio.to_thread.run_sync(handle, name, dict(arguments or {}))
        except (DesktopUnavailable, OSError, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def main() -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server()

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()
