"""Drive a served OpenAdapt MCP server over real stdio, the way a client does.

Not run in CI (it spawns a real server process). Usage:

    # The zero-flag sandbox from this checkout:
    python scripts/smoke_client.py

    # Any launch command, for example the documented uvx line:
    python scripts/smoke_client.py -- uvx --python 3.12 --from \\
        git+https://github.com/OpenAdaptAI/openadapt-agent openadapt-agent serve

    # Operator bundles, read-only or with runs:
    python scripts/smoke_client.py --bundles /path/to/bundles [--allow-run]

In sandbox mode it calls list_workflows, then run_workflow once per
sandbox case, then get_run, and prints each outcome. With --bundles it lists
tools, workflows, and Needs Attention, and never starts a run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _structured(result):
    data = result.model_dump(by_alias=True, mode="json")
    return data.get("structuredContent") or json.loads(data["content"][0]["text"])


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles")
    parser.add_argument("--allow-run", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = [part for part in args.command if part != "--"]
    if not command:
        command = [sys.executable, "-m", "openadapt_agent.mcp"]
        if args.bundles:
            command += ["--bundles", args.bundles]
            if args.allow_run:
                command.append("--allow-run")
    sandbox_dir = tempfile.mkdtemp(prefix="openadapt-smoke-")
    env = dict(os.environ, OPENADAPT_AGENT_SANDBOX_DIR=sandbox_dir)
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [tool.name for tool in (await session.list_tools()).tools]
            print("tools:", ", ".join(tools))
            listing = _structured(await session.call_tool("list_workflows", {}))
            print("mode:", listing["mode"])
            for workflow in listing["workflows"]:
                print(f"workflow: {workflow['name']}: {workflow['purpose']}")
            if listing["mode"] != "sandbox":
                attention = await session.call_tool("list_needs_attention", {})
                print("needs attention:", attention.content[0].text)
                return 0
            print("sandbox engine:", listing["sandbox"]["engine"])
            name = listing["workflows"][0]["name"]
            last_run = None
            for case in listing["sandbox"]["cases"]:
                result = _structured(
                    await session.call_tool(
                        "run_workflow",
                        {
                            "workflow": name,
                            "inputs": {
                                "note": "Synthetic: recheck in 2 weeks",
                                "sandbox_case": case["case"],
                            },
                            "request_id": f"smoke-{case['case'].replace('_', '-')}",
                            "wait_seconds": 600,
                        },
                    )
                )
                last_run = result["run_id"]
                print(
                    f"{case['case']}: outcome={result['outcome']} "
                    f"safe_to_retry={result['safe_to_retry']} :: {result['what_happened']}"
                )
                if result["outcome"] != case["outcome"]:
                    print(f"  expected {case['outcome']}", file=sys.stderr)
                    return 1
            fetched = _structured(await session.call_tool("get_run", {"run_id": last_run}))
            print("get_run:", fetched["outcome"], fetched["run_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
