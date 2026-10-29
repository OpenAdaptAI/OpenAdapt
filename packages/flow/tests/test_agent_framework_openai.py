"""OpenAI Agents SDK adapter: a real ``Runner`` loop with a scripted model.

The scripted model asks for the write tool the way a real model does; the
SDK runs the tool, and the model reads the receipt back. The last test uses
a real model when ``OPENADAPT_REAL_MODEL_TESTS=1`` and ``OPENAI_API_KEY`` are
set.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("agents")

from agents import (  # noqa: E402
    Agent,
    Model,
    ModelResponse,
    Runner,
    Usage,
    function_tool,
    set_tracing_disabled,
)
from openai.types.responses import (  # noqa: E402
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from openadapt_flow.integrations.agents.openai_agents import verified  # noqa: E402
from tests._orders_portal import Portal, portal  # noqa: E402,F401 - fixture

set_tracing_disabled(True)

ORDER = {"customer": "Acme", "product": "Desk lamp", "units": "3"}


class ScriptedModel(Model):
    """Calls ``save_order`` with ``args`` once, then answers with what it saw."""

    def __init__(self, args: dict[str, Any]) -> None:
        self.args = args
        self.seen: list[str] = []

    async def get_response(self, system_instructions, input, *_a, **_k):  # noqa: A002
        outputs = [
            item["output"]
            for item in (input if isinstance(input, list) else [])
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        if not outputs:
            call = ResponseFunctionToolCall(
                type="function_call",
                id="fc_1",
                call_id="call_1",
                name="save_order",
                arguments=json.dumps(self.args),
            )
            return ModelResponse(output=[call], usage=Usage(), response_id=None)
        self.seen.extend(outputs)
        text = ResponseOutputText(type="output_text", text="done", annotations=[])
        message = ResponseOutputMessage(
            type="message",
            id="m_1",
            role="assistant",
            status="completed",
            content=[text],
        )
        return ModelResponse(output=[message], usage=Usage(), response_id=None)

    def stream_response(self, *_a, **_k):  # pragma: no cover - not used
        raise NotImplementedError


def _tool(portal: Portal, tmp_path: Path):
    @function_tool
    def save_order(customer: str, product: str, units: str) -> str:
        """Save a new order."""
        return portal.save_order(customer, product, units)

    return verified(save_order, contract=portal.contract(), workdir=tmp_path)


def _run(portal: Portal, tmp_path: Path, args: dict) -> dict:
    model = ScriptedModel(args)
    agent = Agent(name="orders", model=model, tools=[_tool(portal, tmp_path)])
    asyncio.run(Runner.run(agent, "Enter the order."))
    return json.loads(model.seen[0])


def test_the_agent_gets_a_receipt(portal: Portal, tmp_path: Path) -> None:
    reply = _run(portal, tmp_path, ORDER)
    assert reply["verified"] is True and len(reply["receipt_sha256"]) == 64
    assert len(portal.orders()) == 1


def test_the_agent_gets_a_refusal_for_a_double_save(
    portal: Portal, tmp_path: Path
) -> None:
    portal.fault("twice")
    reply = _run(portal, tmp_path, ORDER)
    assert reply["verified"] is False and reply["verdict"] == "REFUTED"
    assert "Don't retry" in reply["next"]


def test_a_tool_timeout_does_not_lead_to_a_second_write(
    portal: Portal, tmp_path: Path
) -> None:
    """The SDK's own tool timeout cancels the call after the POST landed."""

    from agents.tool import invoke_function_tool
    from agents.tool_context import ToolContext

    @function_tool(timeout=0.5)
    async def save_order(customer: str, product: str, units: str) -> str:
        """Save a new order."""
        out = await asyncio.to_thread(portal.save_order, customer, product, units)
        if portal.posts == 1:
            await asyncio.sleep(1.0)  # only the first reply is slow
        return out

    tool = verified(save_order, contract=portal.contract(), workdir=tmp_path)

    async def call() -> str:
        args = json.dumps(ORDER)
        ctx = ToolContext(
            context=None, tool_name=tool.name, tool_call_id="c1", tool_arguments=args
        )
        return await invoke_function_tool(
            function_tool=tool, context=ctx, arguments=args
        )

    first = asyncio.run(call())
    assert "timed out" in first
    second = json.loads(asyncio.run(call()))
    assert second["verified"] is True and second["not_sent_again"] is True
    assert portal.posts == 1 and len(portal.orders()) == 1


@pytest.mark.skipif(
    not (
        os.environ.get("OPENADAPT_REAL_MODEL_TESTS") == "1"
        and os.environ.get("OPENAI_API_KEY")
    ),
    reason="set OPENADAPT_REAL_MODEL_TESTS=1 and OPENAI_API_KEY to call a real model",
)
def test_real_model_gets_a_receipt(portal: Portal, tmp_path: Path) -> None:
    agent = Agent(
        name="orders",
        instructions="You enter orders with the save_order tool.",
        tools=[_tool(portal, tmp_path)],
    )
    result = asyncio.run(
        Runner.run(agent, "Enter an order: customer Acme, product Desk lamp, 3 units.")
    )
    outputs = [i.output for i in result.new_items if i.type == "tool_call_output_item"]
    assert json.loads(outputs[0])["verified"] is True
    assert len(portal.orders()) == 1
