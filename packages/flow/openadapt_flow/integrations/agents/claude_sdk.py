"""Claude Agent SDK: ``verified(tool, contract=...)`` for an ``@tool`` write.

::

    from claude_agent_sdk import create_sdk_mcp_server
    from openadapt_flow.integrations.agents.claude_sdk import verified

    orders = create_sdk_mcp_server(
        "orders", tools=[verified(save_order, contract="contracts/order.yaml")]
    )

Needs ``openadapt-flow[claude-agent-sdk]``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from claude_agent_sdk import SdkMcpTool

from openadapt_flow.integrations.agents import ContractLike, WriteGuard, reply_text

__all__ = ["verified"]


def verified(tool: SdkMcpTool, *, contract: ContractLike, **options: Any) -> SdkMcpTool:
    """The same tool; each call is checked against ``contract``.

    ``options`` go to :class:`~openadapt_flow.integrations.agents.WriteGuard`
    (``agent_id``, ``pinned``, ``workdir``).
    """

    guard = WriteGuard(contract, **options)
    handler = tool.handler

    async def checked(args: dict[str, Any]) -> dict[str, Any]:
        reply = await guard.arun(args, lambda: handler(args))
        reply["tool_output"] = _text(reply.get("tool_output"))
        return {
            "content": [{"type": "text", "text": reply_text(reply)}],
            "is_error": not reply["verified"],
        }

    return replace(tool, handler=checked)


def _text(output: Any) -> Any:
    if isinstance(output, dict) and isinstance(output.get("content"), list):
        return "\n".join(
            str(part.get("text", ""))
            for part in output["content"]
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return output
