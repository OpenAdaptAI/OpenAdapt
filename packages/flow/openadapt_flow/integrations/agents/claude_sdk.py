"""Claude Agent SDK: ``verified(tool, contract=...)`` for an ``@tool`` write.

::

    from claude_agent_sdk import create_sdk_mcp_server
    from openadapt_flow.integrations.agents.claude_sdk import only, verified

    orders = create_sdk_mcp_server(
        "orders", tools=[verified(save_order, contract="contracts/order.yaml")]
    )
    options = only({"orders": orders})   # no Bash, Write or WebFetch

Claude Code's built-in tools (Bash, Write, WebFetch...) can write too, around
any wrapper. :func:`only` turns them all off and allows only the servers you
pass.

Needs ``openadapt-flow[claude-agent-sdk]``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, SdkMcpTool

from openadapt_flow.integrations.agents import ContractLike, WriteGuard, reply_text

__all__ = ["only", "verified"]


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


def only(servers: dict[str, Any], **options: Any) -> ClaudeAgentOptions:
    """Options with no built-in tools, allowing only ``servers``' tools.

    Every tool the agent can still call is one of yours, so wrap each one
    that can write. ``options`` are any other ``ClaudeAgentOptions`` fields;
    ``tools`` and ``allowed_tools`` can't be among them.
    """

    clash = sorted({"tools", "allowed_tools", "mcp_servers"} & set(options))
    if clash:
        raise ValueError(f"only() sets {clash} itself")
    return ClaudeAgentOptions(
        mcp_servers=servers,
        tools=[],
        allowed_tools=[f"mcp__{name}" for name in servers],
        **options,
    )
