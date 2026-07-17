"""Manual smoke test: drive the served MCP server over real stdio.

Not run in CI (it spawns the real server process). Usage:

    python scripts/smoke_client.py --bundles /path/to/bundles [--allow-run]

Performs tools/list plus the read-only ``list_workflows`` and
``get_workflow`` calls over the stdio transport and prints the results.
It never invokes a run tool — executing a workflow is an operator action.
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

            workflows = [w for w in payload["workflows"] if not w["load_error"]]
            if workflows:
                slug = workflows[0]["slug"]
                detail = await session.call_tool("get_workflow", {"workflow": slug})
                print(f"\nget_workflow({slug!r}):")
                print(detail.content[0].text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
