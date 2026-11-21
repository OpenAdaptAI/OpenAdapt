"""A small outside agent for ``openadapt wrap -- COMMAND`` (no model).

It connects to the MCP endpoint OpenAdapt gives it (``OPENADAPT_MCP_URL``,
with ``OPENADAPT_MCP_TOKEN``), reads ``OPENADAPT_TASK``, and does the sample
clinic task by reading each listing and choosing the next control, the way a
model-driven agent would. Its choices use the listing only.

Run it under wrap::

    openadapt wrap --task task.json --param note="..." -- python mcp_agent.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys


def _ref(listing: str, name: str, near: str | None = None) -> str:
    for line in listing.splitlines():
        if near and near not in line:
            continue
        found = re.search(r"\[(e\d+)\] \S+ \"" + re.escape(name) + '"', line)
        if found:
            return found.group(1)
    raise SystemExit(f"no control named {name!r} on the page")


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ["OPENADAPT_MCP_URL"]
    headers = {"Authorization": "Bearer " + os.environ["OPENADAPT_MCP_TOKEN"]}
    task = os.environ["OPENADAPT_TASK"]
    note = task.rsplit(": ", 1)[1].strip()
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name: str, **args: str) -> str:
                result = await session.call_tool(name, args)
                text = "".join(getattr(c, "text", "") for c in result.content)
                if text.startswith("Stopped:"):
                    raise SystemExit(text)
                return text

            listing = await call("observe")
            triage_done = typed = False
            for _ in range(20):
                if "Encounter saved" in listing:
                    await call("finish", summary="saved the note")
                    return 0
                if "# Referral Tasks" in listing:
                    listing = await call("click", ref=_ref(listing, "Open", "Jane"))
                elif "# New Encounter" in listing:
                    if not triage_done:
                        triage_done = True
                        listing = await call("click", ref=_ref(listing, "Triage"))
                    elif not typed:
                        typed = True
                        listing = await call(
                            "type_text", text=note, ref=_ref(listing, "Note")
                        )
                    else:
                        listing = await call(
                            "click", ref=_ref(listing, "Save Encounter")
                        )
                elif "New Encounter" in listing:
                    listing = await call("click", ref=_ref(listing, "New Encounter"))
                else:
                    raise SystemExit("unexpected page")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
