"""Manual smoke test: drive the served MCP server over real stdio.

Not run in CI (it spawns the real server process). Usage:

    python scripts/smoke_client.py --bundles /path/to/bundles [--allow-run]

Performs tools/list plus ``list_workflows``, ``get_workflow``, and the
PHI-safe ``list_needs_attention`` call over stdio. It never invokes a
run or attended-action tool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles", required=True)
    parser.add_argument("--allow-run", action="store_true")
    args = parser.parse_args()

    server_args = [
        "-m",
        "openadapt_agent.mcp",
        "--bundles",
        args.bundles,
    ]
    if args.allow_run:
        server_args.append("--allow-run")
    params = StdioServerParameters(command=sys.executable, args=server_args)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools/list:")
            for tool in tools.tools:
                print(f"  - {tool.name}")

            listing = await session.call_tool("list_workflows", {})
            payload = json.loads(listing.content[0].text)
            print("\nlist_workflows:")
            print(json.dumps(payload, indent=2))

            attention = await session.call_tool("list_needs_attention", {})
            print("\nlist_needs_attention:")
            print(attention.content[0].text)

            workflows = [w for w in payload["workflows"] if w["available"]]
            if workflows:
                workflow_id = workflows[0]["id"]
                detail = await session.call_tool(
                    "get_workflow",
                    {"workflow": workflow_id},
                )
                print(f"\nget_workflow({workflow_id!r}):")
                print(detail.content[0].text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
