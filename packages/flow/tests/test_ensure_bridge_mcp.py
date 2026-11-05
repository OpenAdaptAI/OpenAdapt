"""Owed UNKNOWN routing through the actual Flow MCP bridge and canonical API.

All records and transports are synthetic. These controls qualify no app or
published facade floor; they retain the issued state and use the same fence.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier as api
from openadapt_flow.agent import verify_tools
from openadapt_flow.agent.verify_tools import VerifyBridge
from openadapt_flow.write_ledger import default_ledger
from tests.test_ensure_bridge_http import post, prepare
from tests.test_targeted_recheck import PARAMS, RECORD_ID, contract
from tests.test_targeted_recheck import server as server


def bridge_for(
    cfg: api.Contract, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> VerifyBridge:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "synthetic.yaml").write_text(
        json.dumps(cfg.config.model_dump(mode="json", by_alias=True))
    )
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    return VerifyBridge(contracts, tmp_path / "runs")


def begin(bridge: VerifyBridge) -> str:
    result = bridge.dispatch(
        "verify_begin",
        {"contract": "synthetic", "values": PARAMS, "agent_id": "synthetic-run"},
    )
    assert result["ok_to_write"], result
    return result["write_id"]


@pytest.mark.parametrize("ledger_mode", ["original", "none", "alternate"])
def test_mcp_initial_unknown_retains_owed_state_and_never_promotes_scoped_hold(
    server: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_mode: str,
) -> None:
    store, base = server
    prepare(store)
    bridge = bridge_for(contract(base), tmp_path, monkeypatch)
    write_id = begin(bridge)
    post(base, {"resourceType": "Basic", "name": PARAMS["name"]})
    store.history_mode = "empty"
    checked = bridge.dispatch("verify_check", {"write_id": write_id})
    assert checked["outcome"] == "UNCERTAIN" and not checked["verified"]
    assert not checked["provisional"] and not checked["final"]
    assert checked["recheck_purpose"] == "read_only_reconciliation"
    assert checked["recheck_due_at"] and "initial outcome" in checked["next"]
    assert write_id in bridge._rechecks
    _, write = bridge._rechecks[write_id]
    assert "recheck_due_at" not in write.receipt
    store.history_mode = "normal"
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    if ledger_mode != "original":
        selected = (
            api.Ledger(api.MemoryLedger()) if ledger_mode == "alternate" else None
        )

    def selected_recheck(*args: Any, **kwargs: Any) -> Any:
        if ledger_mode != "original":
            kwargs["ledger"] = selected
        return api.recheck(*args, **kwargs, wait=True)

    monkeypatch.setattr(verify_tools, "recheck", selected_recheck)
    late = bridge.dispatch("verify_recheck", {"write_id": write_id})
    assert late["outcome"] == "UNCERTAIN" and not late["verified"] and not late["final"]
    assert late["read_at"] and late["reconciliation"]["status"] == "owed"
    assert late["recheck_purpose"] == "read_only_reconciliation"
    assert write_id in bridge._rechecks and write_id not in bridge._spent
    found = default_ledger().find_attempt(write_id)
    assert found is not None
    attempt = found[1]
    assert attempt.resolved["outcome"] == "UNCERTAIN" and attempt.review
    assert attempt.recheck_owed == checked["recheck_due_at"]
    assert len(store.current) == 1


def test_mcp_unresolved_identity_is_retained_nonexecutable_and_reads_nothing(
    server: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, base = server
    db = tmp_path / "source.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE records (id TEXT PRIMARY KEY, name TEXT)")
    document = contract(base).config.model_dump(mode="json", by_alias=True)
    document["verifier"] = {
        "kind": "sql",
        "sqlite_database": str(db),
        "sql_query": "SELECT * FROM records",
    }
    bridge = bridge_for(api.Contract.from_dict(document), tmp_path, monkeypatch)
    write_id = begin(bridge)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO records VALUES (?, ?)", (RECORD_ID, PARAMS["name"])
        )
    checked = bridge.dispatch("verify_check", {"write_id": write_id})
    assert checked["outcome"] == "UNCERTAIN" and not checked["verified"]
    assert not checked["landed"] and checked["review"] and not checked["provisional"]
    assert checked["reconciliation"]["status"] == "owed"
    assert checked["reconciliation"]["executable"] is False
    assert "not executable" in checked["next"]
    assert write_id in bridge._rechecks

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unresolved MCP identity must not open the native source")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    late = bridge.dispatch("verify_recheck", {"write_id": write_id})
    assert late["outcome"] == "UNCERTAIN" and not late["verified"] and not late["final"]
    assert any(
        "independent_target_identity_unknown" in reason for reason in late["reasons"]
    )
    assert late["reconciliation"]["executable"] is False
    assert write_id in bridge._rechecks and write_id not in bridge._spent
    assert not list((tmp_path / "authority").rglob("*.inflight"))
    attempt = default_ledger().find_attempt(write_id)[1]
    assert attempt.resolved["landed"] is False and attempt.review
    assert attempt.recheck_owed == checked["recheck_due_at"]
    again = bridge.dispatch(
        "verify_begin",
        {"contract": "synthetic", "values": PARAMS, "agent_id": "synthetic-run"},
    )
    assert again["ok_to_write"] is False
