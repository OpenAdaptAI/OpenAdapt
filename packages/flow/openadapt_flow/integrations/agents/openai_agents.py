"""OpenAI Agents SDK: ``verified(tool, contract=...)`` for a ``@function_tool`` write.

::

    from agents import Agent
    from openadapt_flow.integrations.agents.openai_agents import verified

    agent = Agent(
        name="orders",
        tools=[verified(save_order, contract="contracts/order.yaml")],
    )

Needs ``openadapt-flow[openai-agents]``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from agents import FunctionTool

from openadapt_flow.integrations.agents import ContractLike, WriteGuard, reply_text

__all__ = ["verified"]


def verified(
    tool: FunctionTool, *, contract: ContractLike, **options: Any
) -> FunctionTool:
    """The same tool; each call is checked against ``contract``.

    ``options`` go to :class:`~openadapt_flow.integrations.agents.WriteGuard`
    (``agent_id``, ``pinned``, ``workdir``).
    """

    guard = WriteGuard(contract, **options)
    invoke = tool.on_invoke_tool

    async def checked(ctx: Any, input_json: str) -> str:
        try:
            args = json.loads(input_json or "{}")
        except json.JSONDecodeError:
            args = None
        if not isinstance(args, dict):
            return reply_text(
                {
                    "verified": False,
                    "verdict": "NOT_WRITTEN",
                    "written": False,
                    "reason": "the tool call's arguments aren't a JSON object",
                }
            )
        return reply_text(await guard.arun(args, lambda: invoke(ctx, input_json)))

    return replace(tool, on_invoke_tool=checked)
