"""Claude Agent SDK adapter: the SDK's own ``query`` loop and in-process MCP
server, with a scripted CLI in place of Claude Code.

The SDK talks to Claude Code over a JSON-lines transport. The scripted
transport speaks that protocol: it lists the tools, calls ``save_order``
through the SDK's MCP bridge as the model would, and ends the turn. The last
test runs the real Claude Code CLI when ``OPENADAPT_REAL_MODEL_TESTS=1`` and
``ANTHROPIC_API_KEY`` are set.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from claude_agent_sdk._internal.transport import Transport  # noqa: E402

from openadapt_flow.integrations.agents.claude_sdk import verified  # noqa: E402
from tests._orders_portal import Portal, portal  # noqa: E402,F401 - fixture

ORDER = {"customer": "Acme", "product": "Desk lamp", "units": "3"}


class ScriptedCLI(Transport):
    """Stands in for Claude Code: MCP initialize, tools/list, one tools/call, done."""

    def __init__(self, args: dict[str, Any]) -> None:
        self.args = args
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.tools: list[dict] = []
        self.result: dict[str, Any] = {}
        self._ready = False

    async def connect(self) -> None:
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    async def close(self) -> None:
        self._ready = False

    async def end_input(self) -> None:
        return None

    def _mcp(self, request_id: str, method: str, params: dict) -> None:
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self.outbox.put_nowait(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "mcp_message",
                    "server_name": "orders",
                    "message": message,
                },
            }
        )

    async def write(self, data: str) -> None:
        for line in filter(None, data.splitlines()):
            msg = json.loads(line)
            kind = msg.get("type")
            if kind == "control_request" and msg["request"]["subtype"] == "initialize":
                self.outbox.put_nowait(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": msg["request_id"],
                            "response": {},
                        },
                    }
                )
            elif kind == "user":
                self._mcp(
                    "init",
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "scripted", "version": "0"},
                    },
                )
            elif kind == "control_response":
                response = msg["response"]
                rpc = response["response"]["mcp_response"]
                if response["request_id"] == "init":
                    self._mcp("list", "tools/list", {})
                elif response["request_id"] == "list":
                    self.tools = rpc["result"]["tools"]
                    self._mcp(
                        "call",
                        "tools/call",
                        {"name": "save_order", "arguments": self.args},
                    )
                elif response["request_id"] == "call":
                    self.result = rpc["result"]
                    self.outbox.put_nowait(_result())

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            msg = await self.outbox.get()
            yield msg
            if msg["type"] == "result":
                return


def _result() -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 0,
        "is_error": False,
        "num_turns": 1,
        "session_id": "scripted",
        "result": "done",
    }


def _server(portal: Portal, tmp_path: Path):
    @tool(
        "save_order",
        "Save a new order.",
        {"customer": str, "product": str, "units": str},
    )
    async def save_order(args: dict[str, Any]) -> dict[str, Any]:
        text = await asyncio.to_thread(portal.save_order, **args)
        return {"content": [{"type": "text", "text": text}]}

    checked = verified(save_order, contract=portal.contract(), workdir=tmp_path)
    return create_sdk_mcp_server("orders", tools=[checked])


async def _drive(portal: Portal, tmp_path: Path, cli: ScriptedCLI) -> None:
    options = ClaudeAgentOptions(mcp_servers={"orders": _server(portal, tmp_path)})
    async for _ in query(prompt="Enter the order.", options=options, transport=cli):
        pass


def _reply(cli: ScriptedCLI) -> dict:
    return json.loads(cli.result["content"][0]["text"])


def test_the_agent_gets_a_receipt(portal: Portal, tmp_path: Path) -> None:
    cli = ScriptedCLI(ORDER)
    asyncio.run(_drive(portal, tmp_path, cli))
    assert [t["name"] for t in cli.tools] == ["save_order"]
    reply = _reply(cli)
    assert reply["verified"] is True and len(reply["receipt_sha256"]) == 64
    assert reply["tool_output"].startswith("saved: /orders/WO-")
    assert not cli.result.get("isError")
    assert len(portal.orders()) == 1


def test_the_agent_gets_a_refusal_for_an_unkept_order(
    portal: Portal, tmp_path: Path
) -> None:
    portal.fault("no_persist")
    cli = ScriptedCLI(ORDER)
    asyncio.run(_drive(portal, tmp_path, cli))
    reply = _reply(cli)
    assert reply["verdict"] == "RECONCILIATION_REQUIRED"
    assert cli.result.get("isError") is True


@pytest.mark.skipif(
    not (
        os.environ.get("OPENADAPT_REAL_MODEL_TESTS") == "1"
        and os.environ.get("ANTHROPIC_API_KEY")
        and shutil.which("claude")
    ),
    reason="set OPENADAPT_REAL_MODEL_TESTS=1 and ANTHROPIC_API_KEY, with Claude Code installed",
)
def test_real_model_gets_a_receipt(portal: Portal, tmp_path: Path) -> None:
    async def run() -> ResultMessage:
        options = ClaudeAgentOptions(
            mcp_servers={"orders": _server(portal, tmp_path)},
            allowed_tools=["mcp__orders__save_order"],
            model="claude-haiku-5-5",
            max_turns=4,
        )
        last = None
        async for message in query(
            prompt="Enter an order: customer Acme, product Desk lamp, 3 units.",
            options=options,
        ):
            last = message
        return last

    assert isinstance(asyncio.run(run()), ResultMessage)
    assert len(portal.orders()) == 1
