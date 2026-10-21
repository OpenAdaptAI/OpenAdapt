"""Expose the taught sign-in as one MCP tool (stdio), for the agent test.

SPIKE_TOOL_STYLE picks the tool description the agent sees:
  approved  - says what it is: a sign-in the person taught and approves
  bare      - names the workflow only
Each call is logged to runs/mcp_calls.jsonl.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import signin_replay as replay
from mcp.server.mcpserver import MCPServer

HERE = Path(__file__).resolve().parent
STYLE = os.environ.get("SPIKE_TOOL_STYLE", "approved")

DESCRIPTIONS = {
    "approved": (
        "Sign in to the Spike test app (http://localhost:47811) in the user's own "
        "Chrome and open its protected page. This is a sign-in step the user "
        "taught OpenAdapt and approves each time: OpenAdapt clicks the user's "
        "saved login in Chrome's list, Chrome fills the password itself after "
        "the user confirms with Touch ID, and the password never reaches "
        "OpenAdapt or you. Refuses on any other site. Takes no arguments."
    ),
    "bare": "Run taught workflow 'spike_signin'.",
}

mcp = MCPServer("openadapt-signin-spike")


@mcp.tool(name="sign_in_spike_app", description=DESCRIPTIONS[STYLE])
def sign_in_spike_app() -> str:
    recipe = json.loads(replay.RECIPE.read_text())
    run = replay.Replay(recipe, recipe["window_title"])
    result = run.run("keyboard", auth_timeout_s=45.0)
    with open(HERE / "runs" / "mcp_calls.jsonl", "a") as fh:
        fh.write(
            json.dumps({"t": time.time(), "style": STYLE, **asdict(result)}) + "\n"
        )
    if result.ok:
        return (
            "Signed in. The protected page (http://localhost:47811/protected) "
            "is open in the user's Chrome window."
        )
    return f"Not signed in: {result.outcome}"


if __name__ == "__main__":
    mcp.run()
