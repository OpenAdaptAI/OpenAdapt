"""Guarded-save tools through the real MCP SDK session (in-memory transport)."""

from __future__ import annotations

import base64
import json

import anyio
import pytest

from openadapt_flow.agent.guard_tools import GuardBridge
from openadapt_flow.approval_mode.demo_approver import demo_approver
from openadapt_flow.agent.mcp import build_server
from openadapt_flow.guard import GUARD_TASK_SCHEMA, GuardedSave
from openadapt_flow.guard_demo import (
    NOTE,
    FakeAgent,
    person_approves,
    tiny_png,
    verify_yaml,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.verify_run import parse_verify_config


@pytest.fixture
def mockmed():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _server(base, tmp_path, approval="each_item"):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "referral-task-01.json").write_text(
        json.dumps(
            {
                "schema_version": GUARD_TASK_SCHEMA,
                "task_id": "referral-task-01",
                "expected_record": {"patient_id": "p1"},
                "intended_fields": {"note": NOTE},
            }
        )
    )
    guarded = GuardedSave(
        parse_verify_config(verify_yaml(base)),
        tmp_path / "state",
        approval=approval,
        key_dir=tmp_path / "keys",
    )
    return build_server(guard=GuardBridge(guarded, tasks))


def test_guard_server_lists_only_the_guard_tools(mockmed, tmp_path, mcp_client):
    server = _server(mockmed[0], tmp_path)

    async def probe():
        async with mcp_client(server) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert anyio.run(probe) == ["guard_begin", "guard_commit", "guard_confirm"]
    assert "Only VERIFIED" in (server.instructions or "")


def test_guarded_save_over_mcp(mockmed, tmp_path, mcp_client):
    base, db = mockmed
    server = _server(base, tmp_path)
    agent = FakeAgent(base)
    # The test plays the person: a throwaway approver key registered in the
    # guard's own key directory signs the answer.
    signer = demo_approver(tmp_path / "keys")

    async def call(client, name, args):
        result = await client.call_tool(name, args)
        return json.loads(result.content[0].text)

    async def flow():
        async with mcp_client(server) as client:
            wrong = await call(
                client,
                "guard_begin",
                {"task_id": "referral-task-01", "screen_text": agent.open_chart("p2")},
            )
            begun = await call(
                client,
                "guard_begin",
                {"task_id": "referral-task-01", "screen_text": agent.open_chart("p1")},
            )
            shot = base64.b64encode(tiny_png()).decode()
            waiting = await call(
                client,
                "guard_commit",
                {"guard_id": begun["guard_id"], "screenshot_png_base64": shot},
            )
            person_approves(
                waiting["approval_run_dir"],
                waiting["approval_request_id"],
                waiting["approval_request_sha256"],
                signer=signer,
                key_dir=tmp_path / "keys",
            )
            go = await call(client, "guard_commit", {"guard_id": begun["guard_id"]})
            agent.click_save("p1", NOTE, fault="no_persist")
            done = await call(client, "guard_confirm", {"guard_id": begun["guard_id"]})
            again = await call(
                client,
                "guard_begin",
                {"task_id": "referral-task-01", "screen_text": agent.open_chart("p1")},
            )
            return wrong, waiting, go, done, again

    wrong, waiting, go, done, again = anyio.run(flow)
    assert wrong["refused"] == "identity_mismatch"
    assert wrong["write_allowed"] is False
    assert waiting["status"] == "waiting_for_approval"
    assert go["status"] == "go"
    assert done["verdict"] == "RECONCILIATION_REQUIRED"
    assert done["retry_allowed"] is False
    assert again["refused"] == "needs_reconciliation"
    assert db.snapshot()["records"] == []


def test_guard_rejects_bad_arguments_before_dispatch(mockmed, tmp_path, mcp_client):
    server = _server(mockmed[0], tmp_path)

    async def probe():
        async with mcp_client(server) as client:
            return await client.call_tool("guard_confirm", {"guard_id": "../x"})

    # Both SDK generations serialize the same protocol alias.
    result = anyio.run(probe).model_dump(by_alias=True, mode="json")
    assert result["isError"] is True
