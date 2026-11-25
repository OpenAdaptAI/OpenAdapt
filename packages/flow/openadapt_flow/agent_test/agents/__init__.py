"""The agents `openadapt test` can run, by name.

==========================  ================================================
``scripted``                 model-free: fills the form, believes the page
``scripted-readback``        model-free: fills the form, reads the list back
``claude-cu``                Claude computer use (Anthropic Messages API)
``openai-cu``                OpenAI computer use (Responses API)
``claude-code-mcp``          Claude Code (``claude -p``) as a coding agent,
                             with only the MCP browser tools
``mcp:<command>``            any coding agent command; it gets an MCP config
``browser-use:<script.py>``  a Browser-Use-style Python script; it gets the
                             browser's CDP URL
``--agent-cmd '<command>'``  any command (see ``agents.command``)
==========================  ================================================
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass
from typing import Callable

from openadapt_flow.agent_test.agents.common import AgentContext, AgentResult, price_for

BUILTIN = ("scripted", "scripted-readback", "claude-cu", "openai-cu", "claude-code-mcp")

DEFAULT_MODELS = {
    "claude-cu": "claude-sonnet-5-5",
    "openai-cu": "gpt-6.1-sol",
    "claude-code-mcp": "sonnet",
}


@dataclass
class Agent:
    id: str  # what the report names it, e.g. claude-cu/claude-sonnet-5-5
    kind: str
    model: str
    paid: bool
    run: Callable[[AgentContext], AgentResult]


def make_agent(
    spec: str, *, model: str = "", price: str = "", agent_cmd: str = ""
) -> Agent:
    from openadapt_flow.agent_test.agents import command, computer_use, scripted

    if agent_cmd:
        return Agent(
            f"cmd:{agent_cmd[:80]}",
            "cmd",
            model,
            True,
            lambda c: command.run_command(c, agent_cmd, kind="cmd", model=model),
        )
    if spec == "scripted":
        return Agent(
            "scripted",
            spec,
            "",
            False,
            lambda c: scripted.run_scripted(c, readback=False),
        )
    if spec == "scripted-readback":
        return Agent(
            "scripted-readback",
            spec,
            "",
            False,
            lambda c: scripted.run_scripted(c, readback=True),
        )
    if spec in ("claude-cu", "openai-cu"):
        m = model or DEFAULT_MODELS[spec]
        prices = price_for(m, price or None)
        fn = computer_use.run_claude if spec == "claude-cu" else computer_use.run_openai
        return Agent(f"{spec}/{m}", spec, m, True, lambda c: fn(c, m, prices))
    if spec == "claude-code-mcp":
        m = model or DEFAULT_MODELS[spec]
        return Agent(
            f"{spec}/{m}",
            spec,
            m,
            True,
            lambda c: command.run_command(
                c, command.CLAUDE_CODE_MCP, kind=spec, model=m
            ),
        )
    if spec.startswith("mcp:"):
        cmd = spec[4:]
        return Agent(
            f"mcp:{cmd[:80]}",
            "mcp",
            model,
            True,
            lambda c: command.run_command(c, cmd, kind="mcp", model=model),
        )
    if spec.startswith("browser-use:"):
        script = spec[len("browser-use:") :]
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(script)}"
        return Agent(
            f"browser-use:{script}",
            "browser-use",
            model,
            True,
            lambda c: command.run_command(c, cmd, kind="browser-use", model=model),
        )
    raise SystemExit(
        f"unknown agent {spec!r}: one of {', '.join(BUILTIN)}, mcp:<command>, browser-use:<script>, or --agent-cmd"
    )
