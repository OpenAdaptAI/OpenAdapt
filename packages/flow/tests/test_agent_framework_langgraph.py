"""LangGraph adapter: a real ReAct graph with a scripted chat model.

The last test uses a real model when ``OPENADAPT_REAL_MODEL_TESTS=1`` and
``ANTHROPIC_API_KEY`` are set (needs ``langchain-anthropic``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langgraph")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode, tools_condition  # noqa: E402

from openadapt_flow.integrations.agents.langgraph import verified  # noqa: E402
from tests._orders_portal import Portal, portal  # noqa: E402,F401 - fixture

ORDER = {"customer": "Acme", "product": "Desk lamp", "units": "3"}


class ScriptedChat(BaseChatModel):
    """Calls ``save_order`` with ``args`` once, then answers."""

    args: dict[str, Any]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChat":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if any(isinstance(m, ToolMessage) for m in messages):
            message = AIMessage(content="done")
        else:
            message = AIMessage(
                content="",
                tool_calls=[{"name": "save_order", "args": self.args, "id": "call_1"}],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _graph(model: Any, tools: list) -> Any:
    bound = model.bind_tools(tools)
    graph = StateGraph(MessagesState)
    graph.add_node(
        "agent", lambda state: {"messages": [bound.invoke(state["messages"])]}
    )
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def _tool(portal: Portal, tmp_path: Path):
    @tool
    def save_order(customer: str, product: str, units: str) -> str:
        """Save a new order."""
        return portal.save_order(customer, product, units)

    return verified(save_order, contract=portal.contract(), workdir=tmp_path)


def _reply(state: dict) -> dict:
    found = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    return json.loads(found[0].content)


def test_the_agent_gets_a_receipt(portal: Portal, tmp_path: Path) -> None:
    graph = _graph(ScriptedChat(args=ORDER), [_tool(portal, tmp_path)])
    reply = _reply(graph.invoke({"messages": [("user", "Enter the order.")]}))
    assert reply["verified"] is True and len(reply["receipt_sha256"]) == 64
    assert len(portal.orders()) == 1


def test_an_unkept_order_is_refused_and_not_retried(
    portal: Portal, tmp_path: Path
) -> None:
    portal.fault("no_persist")
    save = _tool(portal, tmp_path)
    graph = _graph(ScriptedChat(args=ORDER), [save])
    reply = _reply(graph.invoke({"messages": [("user", "Enter the order.")]}))
    assert reply["verdict"] == "RECONCILIATION_REQUIRED"
    again = json.loads(save.invoke(ORDER))
    assert again["verdict"] == "NOT_WRITTEN" and portal.posts == 1


@pytest.mark.skipif(
    not (
        os.environ.get("OPENADAPT_REAL_MODEL_TESTS") == "1"
        and os.environ.get("ANTHROPIC_API_KEY")
    ),
    reason="set OPENADAPT_REAL_MODEL_TESTS=1 and ANTHROPIC_API_KEY to call a real model",
)
def test_real_model_gets_a_receipt(portal: Portal, tmp_path: Path) -> None:
    anthropic = pytest.importorskip("langchain_anthropic")
    model = anthropic.ChatAnthropic(model="claude-haiku-5-5")
    graph = _graph(model, [_tool(portal, tmp_path)])
    state = graph.invoke(
        {
            "messages": [
                ("user", "Enter an order: customer Acme, product Desk lamp, 3 units.")
            ]
        }
    )
    assert _reply(state)["verified"] is True
    assert len(portal.orders()) == 1
