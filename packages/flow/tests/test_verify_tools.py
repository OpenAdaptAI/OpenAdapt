"""``openadapt verify`` over MCP: an agent wraps its own writes.

A real MCP SDK client talks to the real server over its in-memory transport.
The agent's writes go over HTTP to MockMed's fault server, whose record store
the contract reads. No model is called.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402

from openadapt_flow import verifier  # noqa: E402
from openadapt_flow.agent.bridge import BridgeError  # noqa: E402
from openadapt_flow.agent.cli import main as agent_main  # noqa: E402
from openadapt_flow.agent.mcp import build_server  # noqa: E402
from openadapt_flow.agent.verify_tools import VerifyBridge  # noqa: E402
from openadapt_flow.guard_demo import FakeAgent, verify_yaml  # noqa: E402
from openadapt_flow.mockmed.fault_server import serve as fault_serve  # noqa: E402

NOTE = "Follow-up in two weeks"


@pytest.fixture(autouse=True)
def _seal_home(tmp_path, monkeypatch):
    # Receipts are sealed; keep the test's Seal key out of the home folder.
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


@pytest.fixture
def sor():
    url, _db, stop = fault_serve()
    try:
        yield url.rstrip("/")
    finally:
        stop()


def _bridge(base: str, tmp_path: Path) -> VerifyBridge:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "referral.yaml").write_text(yaml.safe_dump(verify_yaml(base)))
    return VerifyBridge(contracts, tmp_path / "state")


async def _session(server: Any, steps: Any) -> Any:
    async with create_client_server_memory_streams() as (client, transport):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server.run, *transport, server.create_initialization_options()
            )
            async with ClientSession(*client) as session:
                await session.initialize()

                async def call(name: str, args: dict) -> dict:
                    result = await session.call_tool(name, args)
                    payload = json.loads(result.content[0].text)
                    if getattr(result, "isError", None) or getattr(
                        result, "is_error", None
                    ):
                        return {"_error": payload.get("error")}
                    return payload

                out = await steps(call)
            tasks.cancel_scope.cancel()
    return out


def test_an_agent_wraps_its_writes(sor: str, tmp_path: Path) -> None:
    server = build_server(teach=_bridge(sor, tmp_path))
    agent = FakeAgent(sor)

    async def steps(call):
        out = {"contracts": await call("verify_contracts", {})}
        values = {"patient_id": "p1", "note": NOTE}
        begun = await call(
            "verify_begin",
            {"contract": "referral", "values": values, "agent_id": "bot-1"},
        )
        out["begun"] = begun
        agent.click_save("p1", NOTE)
        out["good"] = await call("verify_check", {"write_id": begun["write_id"]})
        out["again"] = await call("verify_check", {"write_id": begun["write_id"]})

        # The agent's retry of the same write (its reply was lost): handed
        # back, never sent again.
        out["retry"] = await call(
            "verify_begin", {"contract": "referral", "values": values}
        )
        other = {"patient_id": "p1", "note": NOTE + " (second)"}
        begun = await call(
            "verify_begin",
            {"contract": "referral", "values": other, "write_key": "ticket-2"},
        )
        agent.click_save("p1", other["note"], fault="wrong_record")
        out["bad"] = await call("verify_check", {"write_id": begun["write_id"]})
        out["missing"] = await call(
            "verify_begin", {"contract": "referral", "values": {"patient_id": "p1"}}
        )
        return out

    out = anyio.run(_session, server, steps)
    assert out["contracts"] == {
        "contracts": [{"contract": "referral", "values": ["note", "patient_id"]}]
    }
    assert out["begun"]["ok_to_write"] is True
    assert out["good"]["verified"] is True and out["good"]["verdict"] == "VERIFIED"
    assert len(out["good"]["receipt_sha256"]) == 64
    assert "checked once" in out["again"]["_error"]
    assert out["retry"]["ok_to_write"] is False
    assert out["retry"]["earlier_result"]["outcome"] == "CONFIRMED"
    assert out["bad"]["verified"] is False
    # The note landed on another patient: the store shows a different write.
    assert out["bad"]["verdict"] == "REFUTED"
    assert out["bad"]["outcome"] == "REFUTED"
    assert out["bad"]["retry_safe"] is False
    assert "values missing for: note" in out["missing"]["_error"]


def test_an_unreadable_store_says_not_to_write(tmp_path: Path) -> None:
    server = build_server(teach=_bridge("http://127.0.0.1:9", tmp_path))

    async def steps(call):
        return await call(
            "verify_begin",
            {"contract": "referral", "values": {"patient_id": "p1", "note": NOTE}},
        )

    out = anyio.run(_session, server, steps)
    assert out["ok_to_write"] is False
    assert "don't make the write" in out["reason"]


def test_serve_verify_refuses_other_modes(tmp_path: Path, capsys) -> None:
    assert agent_main(["serve", "--verify", str(tmp_path), "--teach-browser"]) == 2
    assert "--verify serves only the verify tools" in capsys.readouterr().err
    assert agent_main(["serve", "--verify", str(tmp_path)]) == 2
    assert "no write contracts" in capsys.readouterr().err


# -- a provisional result and verify_recheck (SQLite, made-up records) ----------

RECHECK_SCHEMA = """
CREATE TABLE patient_data (id INTEGER PRIMARY KEY, fname TEXT, lname TEXT,
  email TEXT);
CREATE TABLE calendar (id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER, title TEXT);
INSERT INTO patient_data VALUES (1, 'Wendell', 'Prior', 'w@example.test');
"""

VALUES = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}
SAVE = (
    "INSERT INTO patient_data (id, fname, lname, email) "
    "VALUES (2, 'Ada', 'Quill', 'ada@example.test')"
)


def _sql(db: Path, *statements: str) -> None:
    con = sqlite3.connect(db)
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()


@pytest.fixture
def sqlite_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[VerifyBridge, Path]:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(verifier, "USED_DIR", tmp_path / "used")
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(RECHECK_SCHEMA)
    con.commit()
    con.close()
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    doc = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "sql",
            "sqlite_database": str(db),
            "sql_query": "SELECT * FROM patient_data",
        },
        "record": {
            "name": "patient",
            "match": ["email", "lname"],
            "fields": {"fname": "fname", "lname": "lname", "email": "email"},
        },
        "frame": {
            "identity_field": "id",
            "database": {
                "dialect": "sqlite",
                "read": {"sqlite_database": str(db)},
                "views": {"patient_data": ""},
                "settle_s": 0,
                "recheck_after_s": 60,
            },
        },
    }
    (contracts / "patient.yaml").write_text(yaml.safe_dump(doc))
    return VerifyBridge(contracts, tmp_path / "state"), db


def _provisional(bridge: VerifyBridge, db: Path) -> dict[str, Any]:
    begun = bridge.dispatch("verify_begin", {"contract": "patient", "values": VALUES})
    _sql(db, SAVE)
    checked = bridge.dispatch("verify_check", {"write_id": begun["write_id"]})
    assert checked["verified"] is True and checked["provisional"] is True
    assert checked["recheck_due_at"] and "verify_recheck" in checked["next"]
    return checked


def test_verify_recheck_is_listed_and_waits_for_its_due_time(sqlite_bridge) -> None:
    bridge, db = sqlite_bridge
    assert "verify_recheck" in [t.name for t in bridge.list_tool_specs()]
    assert "verify_recheck" in bridge.instructions
    checked = _provisional(bridge, db)
    early = bridge.dispatch("verify_recheck", {"write_id": checked["write_id"]})
    assert early["due"] is False
    assert early["recheck_due_at"] == checked["recheck_due_at"]
    assert early["wait_s"] > 30


def test_verify_recheck_confirms_then_is_spent(sqlite_bridge, monkeypatch) -> None:
    bridge, db = sqlite_bridge
    checked = _provisional(bridge, db)
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    late = bridge.dispatch("verify_recheck", {"write_id": checked["write_id"]})
    assert late["due"] is True and late["outcome"] == "CONFIRMED"
    assert late["verified"] is True and late["late"] is False
    assert late["receipt_sha256"] == checked["receipt_sha256"]
    with pytest.raises(BridgeError, match="no provisional result"):
        bridge.dispatch("verify_recheck", {"write_id": checked["write_id"]})


def test_verify_recheck_refutes_a_write_that_landed_late(
    sqlite_bridge, monkeypatch
) -> None:
    bridge, db = sqlite_bridge
    checked = _provisional(bridge, db)
    # After the verdict, before the recheck: a background job adds a row.
    _sql(db, "INSERT INTO calendar (pid, title) VALUES (2, 'New Patient')")
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    late = bridge.dispatch("verify_recheck", {"write_id": checked["write_id"]})
    assert late["outcome"] == "REFUTED" and late["late"] is True
    assert late["verified"] is False and "no longer stands" in late["next"]
    # The same write again is held for a person, not handed the CONFIRMED.
    again = bridge.dispatch("verify_begin", {"contract": "patient", "values": VALUES})
    assert again["ok_to_write"] is False and "earlier_result" not in again


def test_a_write_without_a_recheck_has_none_to_run(sqlite_bridge) -> None:
    bridge, _ = sqlite_bridge
    with pytest.raises(BridgeError, match="no provisional result"):
        bridge.dispatch("verify_recheck", {"write_id": "nope"})
