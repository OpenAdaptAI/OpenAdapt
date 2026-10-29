"""LangGraph (and LangChain): ``verified(tool, contract=...)`` for a write tool.

::

    from langgraph.prebuilt import create_react_agent
    from openadapt_flow.integrations.agents.langgraph import verified

    agent = create_react_agent(
        model, [verified(save_order, contract="contracts/order.yaml")]
    )

Needs ``openadapt-flow[langgraph]``.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from openadapt_flow.integrations.agents import ContractLike, WriteGuard, reply_text

__all__ = ["verified"]


def verified(tool: BaseTool, *, contract: ContractLike, **options: Any) -> BaseTool:
    """The same tool; each call is checked against ``contract``.

    ``options`` go to :class:`~openadapt_flow.integrations.agents.WriteGuard`
    (``agent_id``, ``pinned``, ``workdir``).
    """

    guard = WriteGuard(contract, **options)

    def checked(**kwargs: Any) -> str:
        return reply_text(guard.run(kwargs, lambda: tool.invoke(kwargs)))

    async def achecked(**kwargs: Any) -> str:
        return reply_text(await guard.arun(kwargs, lambda: tool.ainvoke(kwargs)))

    return StructuredTool.from_function(
        func=checked,
        coroutine=achecked,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )
