"""Teach a browser export once, then call it as a tool with new dates.

Everything here is real except the agent: the MCP SDK client talks to the
real server over its in-memory transport, the server drives a real headless
Chromium against the example reports portal on 127.0.0.1, and the CSV comes
from the portal's real HTTP response. The agent is scripted: it reads each
page listing the server returns and picks refs from it by label. No model is
called anywhere.

It asserts:

* run 1 (open the report, type two dates, pick a region, export) ends in a
  real CSV download, and teach_finish replays the compiled steps with no
  model and adds the taught_export_sales tool (the server says its tool list
  changed);
* calling taught_export_sales with new dates and region returns a CSV whose
  rows are exactly the portal's orders for that range;
* a server started later lists the taught tool at once;
* a button that sends a POST is refused, and no POST reaches the portal;
* new dates the portal rejects make the tool stop with no file, instead of
  returning something.
"""

from __future__ import annotations

import csv
import json
import re
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import anyio
import pytest

pytest.importorskip("mcp")
pytest.importorskip("playwright")

import mcp.types as types  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402

from openadapt_flow.agent.browser_teach import BrowserTeachBridge  # noqa: E402
from openadapt_flow.agent.example_app import COLUMNS, filter_orders, serve  # noqa: E402
from openadapt_flow.agent.mcp import build_server  # noqa: E402

TASK = "Export the sales report as CSV for a date range and region"


@pytest.fixture(scope="module")
def portal() -> Iterator[str]:
    base, stop = serve(0)
    yield base
    stop()


def _stats(base: str) -> dict[str, int]:
    with urllib.request.urlopen(f"{base}/_stats", timeout=5) as response:
        return json.loads(response.read())


class Client:
    def __init__(self, session: ClientSession, notes: list[str]) -> None:
        self.session = session
        self.notes = notes

    async def call(self, name: str, arguments: Optional[dict] = None) -> dict:
        result = await self.session.call_tool(name, arguments or {})
        payload = json.loads(result.content[0].text)
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            return {"_error": payload.get("error")}
        return payload

    async def tools(self) -> dict[str, Any]:
        return {t.name: t for t in (await self.session.list_tools()).tools}


@asynccontextmanager
async def connect(server: Any):
    notes: list[str] = []

    async def on_message(message: Any) -> None:
        inner = getattr(message, "root", message)
        if isinstance(inner, types.ToolListChangedNotification):
            notes.append("tools/list_changed")

    async with create_client_server_memory_streams() as (client, transport):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server.run, *transport, server.create_initialization_options()
            )
            async with ClientSession(*client, message_handler=on_message) as session:
                await session.initialize()
                yield Client(session, notes)
            tasks.cancel_scope.cancel()


def _ref(listing: str, label: str) -> str:
    for line in listing.splitlines():
        found = re.match(r"\[(e\d+)\] \S+ \"" + re.escape(label) + '"', line)
        if found:
            return found.group(1)
    raise AssertionError(f"no {label!r} in listing:\n{listing}")


def _rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_teach_export_then_call_with_new_dates(portal: str, tmp_path: Path) -> None:
    tools_dir, files_dir = tmp_path / "tools", tmp_path / "files"
    bridge = BrowserTeachBridge(tools_dir, files_dir)
    server = build_server(teach=bridge)

    async def scenario() -> dict[str, Any]:
        out: dict[str, Any] = {}
        async with connect(server) as client:
            names = set(await client.tools())
            assert {"teach_start", "teach_act", "teach_finish"} <= names
            assert not any(n.startswith("taught_") for n in names)

            started = await client.call(
                "teach_start",
                {
                    "name": "export_sales",
                    "url": f"{portal}/",
                    "task": TASK,
                    "inputs": {
                        "start_date": "2026-01-01",
                        "end_date": "2026-01-31",
                        "region": "East",
                    },
                },
            )
            assert started.get("status") == "recording", started
            page = started["page"]

            async def act(**args: Any) -> dict[str, Any]:
                nonlocal page
                result = await client.call("teach_act", args)
                assert "_error" not in result, (args, result)
                page = result["page"]
                return result

            await act(action="click", ref=_ref(page, "Sales report"))
            # A POST button is refused before anything is sent.
            refused = await client.call(
                "teach_act", {"action": "click", "ref": _ref(page, "Send email")}
            )
            out["refused"] = refused
            await act(action="type", ref=_ref(page, "From date"), input="start_date")
            await act(action="type", ref=_ref(page, "To date"), input="end_date")
            await act(action="select", ref=_ref(page, "Region"), input="region")
            await act(action="click", ref=_ref(page, "Show report"))
            exported = await act(action="click", ref=_ref(page, "Export CSV"))
            out["run_1"] = exported

            finished = await client.call(
                "teach_finish",
                {
                    "checks": {
                        "between": [
                            {"column": "date", "from": "start_date", "to": "end_date"}
                        ],
                        "equals": [{"column": "region", "input": "region"}],
                        "min_rows": 1,
                    }
                },
            )
            out["finished"] = finished
            await anyio.sleep(0.1)
            out["notes"] = list(client.notes)
            out["tools"] = await client.tools()

            out["march"] = await client.call(
                "taught_export_sales",
                {
                    "inputs": {
                        "start_date": "2026-03-01",
                        "end_date": "2026-03-15",
                        "region": "West",
                    }
                },
            )
            out["backwards"] = await client.call(
                "taught_export_sales",
                {
                    "inputs": {
                        "start_date": "2026-05-01",
                        "end_date": "2026-04-01",
                        "region": "West",
                    }
                },
            )
        return out

    out = anyio.run(scenario)

    assert "POST" in out["refused"]["_error"], out["refused"]

    run_1 = out["run_1"]
    assert run_1["downloaded"].endswith("sales_2026-01-01_to_2026-01-31.csv")
    assert run_1["columns"] == list(COLUMNS)

    finished = out["finished"]
    assert finished["status"] == "ready", finished
    assert finished["new_tool"] == "taught_export_sales"
    assert finished["replayed"]["model_calls"] == 0
    assert finished["replayed"]["same_bytes_as_run_1"] is True
    assert "tools/list_changed" in out["notes"]
    taught = out["tools"]["taught_export_sales"]
    schema = getattr(taught, "input_schema", None) or taught.inputSchema
    assert set(schema["properties"]["inputs"]["required"]) == {
        "start_date",
        "end_date",
        "region",
    }

    march = out["march"]
    assert march["outcome"] == "done", march
    assert march["model_calls"] == 0
    assert Path(march["file"]).parent == files_dir / "export_sales"
    assert march["file"].endswith("sales_2026-03-01_to_2026-03-15.csv")
    expected = filter_orders("2026-03-01", "2026-03-15", "West")
    assert expected, "the portal has West orders in early March"
    assert _rows(march["file"]) == expected
    assert march["rows"] == len(expected)
    assert any(
        "every date is from 2026-03-01 to 2026-03-15" in c
        for c in march["checks_passed"]
    )
    assert "every region is West (11 rows)" in march["checks_passed"]

    backwards = out["backwards"]
    assert backwards["outcome"] == "stopped", backwards
    assert "file" not in backwards
    assert "Export CSV" in backwards["what_happened"]

    # Read-only: the portal never received a write.
    assert _stats(portal)["writes"] == 0

    # A new server finds the taught tool on disk.
    again = BrowserTeachBridge(tools_dir, files_dir)
    assert "taught_export_sales" in {s.name for s in again.list_tool_specs()}
