"""CrewAI adapter: a real crew with a scripted LLM.

The scripted LLM answers in CrewAI's ReAct text the way a real model does;
CrewAI parses it and runs the tool. The last test uses a real model when
``OPENADAPT_REAL_MODEL_TESTS=1`` and ``OPENAI_API_KEY`` are set.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_TESTING", "true")  # no trace prompt at the end
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
pytest.importorskip("crewai")

from crewai import Agent, Crew, Task  # noqa: E402
from crewai.llms.base_llm import BaseLLM  # noqa: E402
from crewai.tools import tool  # noqa: E402

from openadapt_flow.integrations.agents.crewai import verified  # noqa: E402
from tests._orders_portal import Portal, portal  # noqa: E402,F401 - fixture

ORDER = {"customer": "Acme", "product": "Desk lamp", "units": "3"}


class ScriptedLLM(BaseLLM):
    """Calls ``save_order`` with ``args`` once, then gives the final answer."""

    def __init__(self, args: dict[str, Any]) -> None:
        super().__init__(model="scripted")
        self.args = args
        self.seen: list[str] = []

    def call(self, messages, *args: Any, **kwargs: Any) -> str:
        text = (
            messages
            if isinstance(messages, str)
            else "\n".join(str(m.get("content", "")) for m in messages)
        )
        found = re.findall(r"Observation: (\{.*?\})\s*$", text, re.MULTILINE)
        if not found:
            return (
                "Thought: I'll save the order.\n"
                "Action: save_order\n"
                f"Action Input: {json.dumps(self.args)}"
            )
        self.seen.append(found[-1])
        return "Thought: I now know the final answer\nFinal Answer: done"

    def supports_function_calling(self) -> bool:
        return False

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 8192


def _tool(portal: Portal, tmp_path: Path):
    @tool("save_order")
    def save_order(customer: str, product: str, units: str) -> str:
        """Save a new order."""
        return portal.save_order(customer, product, units)

    return verified(save_order, contract=portal.contract(), workdir=tmp_path)


def _crew(llm: Any, portal: Portal, tmp_path: Path) -> Crew:
    clerk = Agent(
        role="Order clerk",
        goal="Enter orders",
        backstory="You enter orders with the save_order tool.",
        tools=[_tool(portal, tmp_path)],
        llm=llm,
        max_iter=3,
        verbose=False,
    )
    task = Task(
        description="Enter an order: customer Acme, product Desk lamp, 3 units.",
        expected_output="The tool's reply.",
        agent=clerk,
    )
    return Crew(agents=[clerk], tasks=[task], verbose=False)


def test_the_agent_gets_a_receipt(portal: Portal, tmp_path: Path) -> None:
    llm = ScriptedLLM(ORDER)
    _crew(llm, portal, tmp_path).kickoff()
    reply = json.loads(llm.seen[0])
    assert reply["verified"] is True and len(reply["receipt_sha256"]) == 64
    assert len(portal.orders()) == 1


def test_the_agent_gets_a_refusal_for_a_double_save(
    portal: Portal, tmp_path: Path
) -> None:
    portal.fault("twice")
    llm = ScriptedLLM(ORDER)
    _crew(llm, portal, tmp_path).kickoff()
    reply = json.loads(llm.seen[0])
    assert reply["verified"] is False and reply["verdict"] == "REFUTED"


def test_the_description_is_not_wrapped_twice(portal: Portal, tmp_path: Path) -> None:
    assert _tool(portal, tmp_path).description.count("Tool Name:") == 1


@pytest.mark.skipif(
    not (
        os.environ.get("OPENADAPT_REAL_MODEL_TESTS") == "1"
        and os.environ.get("OPENAI_API_KEY")
    ),
    reason="set OPENADAPT_REAL_MODEL_TESTS=1 and OPENAI_API_KEY to call a real model",
)
def test_real_model_gets_a_receipt(portal: Portal, tmp_path: Path) -> None:
    from crewai import LLM

    _crew(LLM(model="gpt-4.1-mini"), portal, tmp_path).kickoff()
    assert len(portal.orders()) == 1
