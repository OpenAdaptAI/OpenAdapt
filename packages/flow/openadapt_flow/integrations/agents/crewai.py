"""CrewAI: ``verified(tool, contract=...)`` for a write tool.

::

    from crewai import Agent
    from openadapt_flow.integrations.agents.crewai import verified

    clerk = Agent(
        role="Order clerk", goal="Enter orders", backstory="...",
        tools=[verified(save_order, contract="contracts/order.yaml")],
    )

Needs ``crewai`` installed beside ``openadapt-flow``; see docs/AGENT_FRAMEWORKS.md.
"""

from __future__ import annotations

import re
from typing import Any

from crewai.tools import BaseTool
from crewai.tools.base_tool import Tool

from openadapt_flow.integrations.agents import ContractLike, WriteGuard, reply_text

__all__ = ["verified"]

# CrewAI prefixes a tool's description with its name and arguments.
_GENERATED = re.compile(r"\ATool Name: .*?\nTool Description: ", re.DOTALL)


def verified(tool: BaseTool, *, contract: ContractLike, **options: Any) -> BaseTool:
    """The same tool; each call is checked against ``contract``.

    ``options`` go to :class:`~openadapt_flow.integrations.agents.WriteGuard`
    (``agent_id``, ``pinned``, ``workdir``).
    """

    guard = WriteGuard(contract, **options)

    def checked(**kwargs: Any) -> str:
        return reply_text(guard.run(kwargs, lambda: tool.run(**kwargs)))

    return Tool(
        name=tool.name,
        description=_GENERATED.sub("", tool.description),
        args_schema=tool.args_schema,
        func=checked,
    )
