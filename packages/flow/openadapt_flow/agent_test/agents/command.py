"""Agents that run as their own process: ``--agent-cmd``, Browser-Use-style scripts, MCP coding agents.

The command runs once per scenario, from a fresh empty working directory,
with these environment variables:

==============================  ================================================
``OPENADAPT_TEST_TASK``          the instruction (preamble and task)
``OPENADAPT_TEST_TASK_ONLY``     the task alone
``OPENADAPT_TEST_VALUES``        the task's values, as JSON
``OPENADAPT_TEST_START_URL``     the app's front door (already signed in)
``OPENADAPT_TEST_CDP_URL``       the signed-in headless browser, over the
                                 Chrome DevTools Protocol (Playwright
                                 ``connect_over_cdp``, Browser-Use ``cdp_url``)
``OPENADAPT_TEST_MCP_CONFIG``    an MCP client config file with one stdio
                                 server, ``openadapt_browser``, that drives the
                                 same page (``python -m
                                 openadapt_flow.agent_test.mcp_browser``)
``OPENADAPT_TEST_REPORT``        where to write the report (below)
``OPENADAPT_TEST_SCENARIO``      the scenario id
``OPENADAPT_TEST_APP``           the app pack's name
==============================  ================================================

The report is JSON: ``{"claimed_success": true, "final_text": "...",
"cost_usd": 0.12, "model_calls": 9}``. Without a report, the last
``RESULT: DONE`` or ``RESULT: NOT DONE`` line the command printed decides;
with neither, the run counts as a claim of not done. Only ``claimed_success``
(or that line) is read; what the store holds is graded separately.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from openadapt_flow.agent_test.agents.common import AgentContext, AgentResult
from openadapt_flow.agent_test.browser import browser_errors

#: Built-in presets: the coding agent over MCP.
CLAUDE_CODE_MCP = (
    "claude -p --bare --tools '' --strict-mcp-config --mcp-config \"$OPENADAPT_TEST_MCP_CONFIG\" "
    "--allowedTools mcp__openadapt_browser --output-format json --max-budget-usd 1 "
    '--model "${OPENADAPT_TEST_MODEL:-sonnet}" "$OPENADAPT_TEST_TASK"'
)


def mcp_config(ctx: AgentContext) -> dict[str, Any]:
    import openadapt_flow

    # The server imports openadapt_flow from wherever this harness does.
    root = str(Path(openadapt_flow.__file__).resolve().parents[1])
    return {
        "mcpServers": {
            "openadapt_browser": {
                "type": "stdio",
                "command": sys.executable,
                "env": {"PYTHONPATH": root},
                "args": [
                    "-m",
                    "openadapt_flow.agent_test.mcp_browser",
                    "--cdp",
                    ctx.cdp_url,
                    "--base",
                    ctx.base_url,
                ],
            }
        }
    }


def run_command(
    ctx: AgentContext,
    command: str,
    *,
    kind: str,
    model: str = "",
    timeout_s: float = 900,
) -> AgentResult:
    workdir = Path(tempfile.mkdtemp(prefix="oatest-agent-"))
    report = workdir / "report.json"
    cfg = workdir / "mcp.json"
    cfg.write_text(json.dumps(mcp_config(ctx), indent=1))
    env = dict(os.environ)
    env.update(
        {
            "OPENADAPT_TEST_TASK": ctx.instruction,
            "OPENADAPT_TEST_TASK_ONLY": ctx.task_text,
            "OPENADAPT_TEST_VALUES": json.dumps(ctx.values),
            "OPENADAPT_TEST_START_URL": ctx.screen.url(),
            "OPENADAPT_TEST_BASE_URL": ctx.base_url,
            "OPENADAPT_TEST_CDP_URL": ctx.cdp_url,
            "OPENADAPT_TEST_MCP_CONFIG": str(cfg),
            "OPENADAPT_TEST_REPORT": str(report),
            "OPENADAPT_TEST_SCENARIO": ctx.scenario_id,
            "OPENADAPT_TEST_APP": ctx.app.name,
        }
    )
    if model:
        env["OPENADAPT_TEST_MODEL"] = model
    argv = ["/bin/sh", "-c", command] if os.name != "nt" else shlex.split(command)
    out_f = (ctx.evidence / "agent.stdout").open("w")
    err_f = (ctx.evidence / "agent.stderr").open("w")
    proc = subprocess.Popen(
        argv, cwd=workdir, env=env, stdout=out_f, stderr=err_f, stdin=subprocess.DEVNULL
    )
    t0 = time.monotonic()
    stopped = None
    page = ctx.screen.page
    # Keep the harness's own browser connection serving events (its route
    # rules) while the other process drives the page.
    while proc.poll() is None:
        if time.monotonic() - t0 > timeout_s:
            proc.kill()
            stopped = f"time cap: {timeout_s:.0f} s"
            break
        try:
            page.wait_for_timeout(500)
        except browser_errors():
            time.sleep(0.5)
    proc.wait()
    out_f.close()
    err_f.close()
    stdout = (ctx.evidence / "agent.stdout").read_text(errors="replace")
    final_text, claimed, cost, calls = "", None, None, None
    if report.is_file():
        try:
            rep = json.loads(report.read_text())
            claimed = bool(rep.get("claimed_success"))
            final_text = str(rep.get("final_text", ""))
            cost = rep.get("cost_usd")
            calls = rep.get("model_calls")
        except (ValueError, OSError) as exc:
            final_text = f"(unreadable report: {exc})"
    else:
        final_text = stdout[-4000:]
        if kind == "claude-code-mcp":
            # `claude -p --output-format json` prints one JSON object.
            try:
                doc = json.loads(stdout.strip().splitlines()[-1])
                final_text = str(doc.get("result", ""))
                cost = doc.get("total_cost_usd")
                calls = doc.get("num_turns")
            except (ValueError, IndexError):
                pass
    if proc.returncode not in (0, None) and stopped is None:
        stopped = f"exit {proc.returncode}"
    return AgentResult(
        final_text, claimed=claimed, stopped=stopped, cost_usd=cost, model_calls=calls
    )
