from __future__ import annotations

import json
import subprocess
import sys

import pytest
from test_verification import VALUES, check_local


def test_cli_real_write_and_portable_receipt(store, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from openadapt_flow.receipt_log import LocalLog
    from openadapt_flow.write_receipt import default_signer

    contract = tmp_path / "verify.yaml"
    contract.write_text(json.dumps(store.contract()))
    params = tmp_path / "values.json"
    params.write_text(json.dumps(VALUES))
    writer = (
        "import json,sys,urllib.request; "
        "r=urllib.request.Request(sys.argv[1],data=sys.argv[2].encode(),headers={'Content-Type':'application/json'}); "
        "urllib.request.urlopen(r).read()"
    )
    command = [
        sys.executable,
        "-m",
        "openadapt_verify",
        "run",
        "--contract",
        str(contract),
        "--params",
        str(params),
        "--state",
        str(tmp_path / "state"),
        "--",
        sys.executable,
        "-c",
        writer,
        store.base + "/items",
        json.dumps(VALUES),
    ]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr + run.stdout
    result = json.loads(run.stdout)
    assert result["outcome"] == "CONFIRMED" and result["write_receipt"] == "issued"
    assert result["command_returncode"] == 0 and store.posts == 1

    def pem(raw):
        return Ed25519PublicKey.from_public_bytes(raw).public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    issuer = tmp_path / "issuer.pub.pem"
    log = tmp_path / "log.pub.pem"
    issuer.write_bytes(pem(default_signer().public_raw()))
    log.write_bytes(pem(LocalLog().public_raw()))
    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_verify",
            "verify",
            result["write_receipt_path"],
            "--issuer-key",
            str(issuer),
            "--log-key",
            str(log),
        ],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr + checked.stdout
    receipt = json.loads(checked.stdout)
    assert receipt["valid"] is True and receipt["payload"]["verdict"] == "CONFIRMED"

    # The same persisted write cannot be resent through a new CLI process.
    again = subprocess.run(command, capture_output=True, text=True)
    assert again.returncode != 0 and store.posts == 1
    assert json.loads(again.stdout)["not_sent_again"] is True


def test_cli_unreadable_baseline_never_launches_write(tmp_path):
    contract = tmp_path / "verify.yaml"
    contract.write_text(
        json.dumps(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "sql",
                    "sqlite_database": str(tmp_path / "missing" / "store.sqlite"),
                    "sql_query": "SELECT * FROM item",
                },
                "effects": [
                    {
                        "kind": "record_written",
                        "match": {"code": {"param": "code"}},
                        "expected_count": 1,
                        "count_new_only": True,
                    }
                ],
            }
        )
    )
    params = tmp_path / "values.json"
    params.write_text(json.dumps(VALUES))
    marker = tmp_path / "should-not-exist"
    command = [
        sys.executable,
        "-m",
        "openadapt_verify",
        "run",
        "--contract",
        str(contract),
        "--params",
        str(params),
        "--state",
        str(tmp_path / "state"),
        "--",
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
        str(marker),
    ]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode != 0 and not marker.exists()
    assert json.loads(run.stdout)["written"] is False


def test_mcp_actual_transport_with_independent_store(store, tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from openadapt_verify.mcp import build_server

    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "item.yaml").write_text(json.dumps(store.contract("rest")))
    server = build_server(contracts, tmp_path / "state")

    async def run():
        async with create_client_server_memory_streams() as (client, transport):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    server.run, *transport, server.create_initialization_options()
                )
                async with ClientSession(*client) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert {t.name for t in listed.tools} == {
                        "verify_contracts",
                        "verify_begin",
                        "verify_check",
                        "verify_recheck",
                    }
                    begun = await session.call_tool(
                        "verify_begin", {"contract": "item", "values": VALUES}
                    )
                    entry = json.loads(begun.content[0].text)
                    assert entry["ok_to_write"] is True
                    await anyio.to_thread.run_sync(store.save, VALUES)
                    checked = await session.call_tool(
                        "verify_check", {"write_id": entry["write_id"]}
                    )
                    reply = json.loads(checked.content[0].text)
                    assert reply["verified"] is True
                    check_local(reply)
                    duplicate = await session.call_tool(
                        "verify_check", {"write_id": entry["write_id"]}
                    )
                    assert duplicate.isError is True
                tasks.cancel_scope.cancel()

    anyio.run(run)
    assert store.posts == 1
