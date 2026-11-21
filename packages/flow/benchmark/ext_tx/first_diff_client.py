"""A stand-in agent for the setup-time measurement: it gets only the MCP URL
and token from ``openadapt proxy``, asks for a dry run of a bulk UPDATE and
prints the diff. It has no database credentials."""

import json
import os

import httpx

url, token = os.environ["OPENADAPT_MCP_URL"], os.environ["OPENADAPT_MCP_TOKEN"]
assert "DATABASE_URL" not in os.environ and "PGPASSWORD" not in os.environ
h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
httpx.post(url, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "lab", "version": "1"}}}).raise_for_status()
got = httpx.post(url, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
    "name": "sql_preview", "arguments": {"sql": "UPDATE users SET active = false WHERE id <= 412"}}}).json()
print(json.loads(got["result"]["content"][0]["text"])["summary"])
