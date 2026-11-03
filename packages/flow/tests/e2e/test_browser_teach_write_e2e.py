"""Teach a write once, then call it as a tool that returns a verified receipt.

Everything is real except the agent: the MCP SDK client talks to the real
server, which drives headless Chromium against the example app on
127.0.0.1. Run 1 signs in with values the server holds, fills a New Order
form and saves it. The write contract reads the app's order store
(``GET /api/orders``), so every result comes from the store, not the screen.
The agent is scripted: it picks refs from the listings by label. No model is
called.

It asserts:

* teach_finish verifies run 1's order in the store before it adds the tool;
* the taught tool, called with a new order, saves it and returns a verified
  receipt;
* an app that answers "saved" but keeps nothing, or keeps the order twice,
  gets a refusal, never done;
* the sign-in values never appear in a page listing, a result or the program.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

import anyio
import pytest

pytest.importorskip("mcp")
pytest.importorskip("playwright")

from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402

from openadapt_flow.agent.browser_teach import BrowserTeachBridge  # noqa: E402
from openadapt_flow.agent.example_app import (  # noqa: E402
    DEMO_PASSWORD,
    DEMO_USER,
    serve,
)
from openadapt_flow.agent.mcp import build_server  # noqa: E402
from openadapt_flow.verify import WriteContract  # noqa: E402

FIRST = {"customer": "Acme Ltd", "product": "Desk lamp", "units": "3"}
SECOND = {"customer": "Birch Co", "product": "Office chair", "units": "5"}


@pytest.fixture(autouse=True)
def _seal_home(tmp_path, monkeypatch):
    # Receipts are sealed; keep the test's Seal key out of the home folder.
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


@pytest.fixture
def app() -> Iterator[str]:
    base, stop = serve(0)
    yield base
    stop()


def _get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        body = response.read()
    try:
        return json.loads(body)
    except ValueError:
        return body.decode()


def _contract(base: str) -> WriteContract:
    return WriteContract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            # The requests a taught order may send: the sign-in and the save.
            "writes": ["POST /login", "POST /orders"],
            "verifier": {
                "kind": "rest",
                "base_url": base,
                "records_path": "/api/orders",
                "records_key": "records",
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {
                        "customer": {"param": "customer"},
                        "product": {"param": "product"},
                        "units": {"param": "units"},
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                    "timeout_s": 1.0,
                }
            ],
        },
        name="order",
    )


def _ref(listing: str, label: str) -> str:
    for line in listing.splitlines():
        found = re.match(r"\[(e\d+)\] \S+ \"" + re.escape(label) + '"', line)
        if found:
            return found.group(1)
    raise AssertionError(f"no {label!r} in listing:\n{listing}")


async def _session(server: Any, steps: Any) -> Any:
    async with create_client_server_memory_streams() as (client, transport):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server.run, *transport, server.create_initialization_options()
            )
            async with ClientSession(*client) as session:
                await session.initialize()

                async def call(name: str, args: Optional[dict] = None) -> dict:
                    result = await session.call_tool(name, args or {})
                    text = result.content[0].text
                    payload = json.loads(text)
                    if getattr(result, "isError", None) or getattr(
                        result, "is_error", None
                    ):
                        return {"_error": payload.get("error"), "_text": text}
                    payload["_text"] = text
                    return payload

                out = await steps(call)
            tasks.cancel_scope.cancel()
    return out


def test_teach_a_write_then_call_it_verified(
    app: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PORTAL_USER", DEMO_USER)
    monkeypatch.setenv("PORTAL_PASSWORD", DEMO_PASSWORD)
    bridge = BrowserTeachBridge(
        tmp_path / "tools",
        tmp_path / "files",
        contracts={"order": _contract(app)},
        secrets={"username": "PORTAL_USER", "password": "PORTAL_PASSWORD"},
        verify_dir=tmp_path / "verify",
    )
    server = build_server(teach=bridge)
    seen: list[str] = []

    async def steps(call):
        out: dict[str, Any] = {}
        started = await call(
            "teach_start",
            {
                "name": "add_order",
                "url": f"{app}/login",
                "task": "Add an order for a customer",
                "contract": "order",
                "inputs": FIRST,
            },
        )
        assert started.get("status") == "recording", started
        assert started["sign_in_inputs"] == ["password", "username"]
        page = started["page"]
        seen.append(started["_text"])

        async def act(**args: Any) -> None:
            nonlocal page
            result = await call("teach_act", args)
            assert "_error" not in result, (args, result)
            seen.append(result["_text"])
            page = result["page"]

        await act(action="type", ref=_ref(page, "Username"), input="username")
        await act(action="type", ref=_ref(page, "Password"), input="password")
        await act(action="click", ref=_ref(page, "Sign in"))
        await act(action="type", ref=_ref(page, "Customer"), input="customer")
        await act(action="select", ref=_ref(page, "Product"), input="product")
        await act(action="type", ref=_ref(page, "Units"), input="units")
        await act(action="click", ref=_ref(page, "Save order"))
        out["finished"] = await call("teach_finish", {})
        seen.append(out["finished"]["_text"])

        out["second"] = await call("taught_add_order", {"inputs": SECOND})
        _get(f"{app}/_fault?mode=no_persist")
        out["dropped"] = await call(
            "taught_add_order",
            {
                "inputs": {
                    "customer": "Cedar Inc",
                    "product": "USB-C dock",
                    "units": "2",
                }
            },
        )
        _get(f"{app}/_fault?mode=twice")
        out["doubled"] = await call(
            "taught_add_order",
            {
                "inputs": {
                    "customer": "Dune LLC",
                    "product": "Monitor arm",
                    "units": "4",
                }
            },
        )
        _get(f"{app}/_fault?mode=")
        for key in ("second", "dropped", "doubled"):
            seen.append(out[key]["_text"])
        return out

    out = anyio.run(_session, server, steps)

    finished = out["finished"]
    assert finished["status"] == "ready", finished
    assert finished["new_tool"] == "taught_add_order"
    assert finished["run_1"]["verified"] is True
    assert finished["writes_sent"] == ["POST /login", "POST /orders"]

    second = out["second"]
    assert second["outcome"] == "done", second
    assert second["verified"] is True and len(second["receipt_sha256"]) == 64
    assert second["model_calls"] == 0

    dropped = out["dropped"]
    assert dropped["outcome"] == "needs_review", dropped
    assert dropped["verified"] is False and dropped["retry_safe"] is False
    assert dropped["verdict"] == "RECONCILIATION_REQUIRED"

    doubled = out["doubled"]
    assert doubled["outcome"] == "needs_review", doubled
    assert doubled["verdict"] == "REFUTED"

    records = _get(f"{app}/api/orders")["records"]
    assert [r["customer"] for r in records] == [
        "Acme Ltd",
        "Birch Co",
        "Dune LLC",
        "Dune LLC",
    ]

    # The sign-in values reach no listing, result or program.
    program = (tmp_path / "tools" / "add_order" / "program.json").read_text()
    for text in [*seen, program]:
        assert DEMO_PASSWORD not in text
        assert DEMO_USER not in text
    assert json.loads(program)["sign_in"] == ["password", "username"]


def test_a_taught_write_without_its_sign_in_does_not_run(
    app: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PORTAL_PASSWORD", raising=False)
    monkeypatch.setenv("PORTAL_USER", DEMO_USER)
    bridge = BrowserTeachBridge(
        tmp_path / "tools",
        tmp_path / "files",
        contracts={"order": _contract(app)},
        secrets={"username": "PORTAL_USER", "password": "PORTAL_PASSWORD"},
        verify_dir=tmp_path / "verify",
    )
    with pytest.raises(Exception, match="PORTAL_PASSWORD"):
        bridge.dispatch(
            "teach_start",
            {
                "name": "add_order",
                "url": f"{app}/login",
                "task": "Add an order",
                "contract": "order",
                "inputs": FIRST,
            },
        )
