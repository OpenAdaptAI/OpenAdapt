"""Real MCP calls cannot turn a refuted write into a confirmed one."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_cli_verdicts import _files
from test_verification import VALUES


@pytest.mark.parametrize("first_refuter", ["canonical", "mcp"])
def test_mcp_restored_rows_never_clear_refutation(store, tmp_path, first_refuter):
    pytest.importorskip("mcp")
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams
    from openadapt_flow.runtime.effects.late_write import seconds_until
    from openadapt_flow.verifier import Actor, Contract, RecheckState, recheck
    from openadapt_flow.write_ledger import default_ledger

    from openadapt_verify.mcp import build_server

    contracts = tmp_path / "contracts"
    contracts.mkdir()
    contract_path, _ = _files(store, contracts, 0.01)
    server = build_server(contracts, tmp_path / "state")

    async def exercise():
        async with create_client_server_memory_streams() as (client, transport):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    server.run, *transport, server.create_initialization_options()
                )
                async with ClientSession(*client) as session:
                    await session.initialize()
                    entry = await session.call_tool(
                        "verify_begin", {"contract": "verify", "values": VALUES}
                    )
                    write_id = json.loads(entry.content[0].text)["write_id"]
                    await anyio.to_thread.run_sync(store.save, VALUES)
                    checked = await session.call_tool(
                        "verify_check", {"write_id": write_id}
                    )
                    first = json.loads(checked.content[0].text)
                    assert first["verified"] is True and first["provisional"] is True
                    with sqlite3.connect(store.path) as db:
                        db.execute(
                            "INSERT INTO item(code,quantity) VALUES ('unexplained','9')"
                        )
                    if first_refuter == "mcp":
                        await anyio.sleep(
                            max(0.0, seconds_until(first["recheck_due_at"])) + 0.02
                        )
                        refuted = await session.call_tool(
                            "verify_recheck", {"write_id": write_id}
                        )
                        assert (
                            json.loads(refuted.content[0].text)["outcome"] == "REFUTED"
                        )
                    else:
                        saved = RecheckState.load(
                            Path(first["receipt_path"]).parent / "recheck.json",
                            params=VALUES,
                        )
                        late = recheck(
                            Contract.load(contract_path),
                            saved,
                            params=VALUES,
                            actor=Actor("agent", "mcp-agent"),
                            wait=True,
                        )
                        assert late.outcome == "REFUTED"
                    with sqlite3.connect(store.path) as db:
                        db.execute("DELETE FROM item WHERE code='unexplained'")
                    retried = await session.call_tool(
                        "verify_recheck", {"write_id": write_id}
                    )
                    if first_refuter == "mcp":
                        assert (
                            getattr(
                                retried, "is_error", getattr(retried, "isError", None)
                            )
                            is True
                        )
                    else:
                        reply = json.loads(retried.content[0].text)
                        assert (
                            reply["outcome"] == "UNCERTAIN"
                            and reply["verified"] is False
                        )
                    found = default_ledger().find_attempt(write_id)
                    assert found is not None and found[1].state == "held"
                    assert found[1].resolved["outcome"] == "REFUTED"
                tasks.cancel_scope.cancel()

    anyio.run(exercise)
    assert store.posts == 1 and len(store.records()) == 1
