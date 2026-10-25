"""Expose the taught sign-in as one MCP tool (stdio), for the agent test.

SPIKE_TOOL_STYLE picks the tool description the agent sees:
  approved  - says what it is: a sign-in the person taught and approves
  bare      - names the workflow only
Both say the tool never takes a password and the agent must never ask for one.
The agent only ever sees the fixed messages in tool_messages.py, never a raw
outcome or exception.
Each call is logged to runs/mcp_calls.jsonl.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import signin_replay as replay
import tool_messages
from mcp.server.mcpserver import MCPServer

HERE = Path(__file__).resolve().parent
STYLE = os.environ.get("SPIKE_TOOL_STYLE", "approved")

DESCRIPTIONS = {
    "approved": tool_messages.DESCRIPTION,
    "bare": (
        "Run taught workflow 'spike_signin'. It never accepts or needs a "
        "password; never ask the person for one."
    ),
}
# Test hook: report this failure kind without touching Chrome, so an agent's
# reaction to a failed sign-in can be checked on any machine.
FORCE = os.environ.get("SPIKE_FORCE_FAILURE")

mcp = MCPServer("openadapt-signin-spike")


@mcp.tool(name="sign_in_spike_app", description=DESCRIPTIONS[STYLE])
def sign_in_spike_app() -> str:
    if FORCE:
        _log({"style": STYLE, "ok": False, "forced": FORCE})
        return tool_messages.failure_message(FORCE)
    try:
        recipe = json.loads(replay.RECIPE.read_text())
        result = replay.Replay(recipe, recipe["window_title"]).run(
            "keyboard", auth_timeout_s=45.0
        )
    except Exception as exc:  # noqa: BLE001 - the agent gets a fixed message
        _log({"style": STYLE, "ok": False, "exception": type(exc).__name__})
        return tool_messages.failure_message("not_ready")
    _log({"style": STYLE, **asdict(result)})
    if result.ok:
        return tool_messages.SIGNED_IN
    return tool_messages.failure_message(tool_messages.classify(result.outcome))


def _log(entry: dict) -> None:
    """Local run record only; the agent never sees it."""
    with open(HERE / "runs" / "mcp_calls.jsonl", "a") as fh:
        fh.write(json.dumps({"t": time.time(), **entry}) + "\n")


if __name__ == "__main__":
    mcp.run()
