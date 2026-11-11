"""Named protected-ledger holds across canonical, MCP and Verify CLI paths."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from openadapt_flow import verifier
from openadapt_flow.agent.verify_tools import VerifyBridge
from openadapt_flow.write_ledger import Ledger, LedgerIntegrityHeld, MemoryLedger

from openadapt_verify import cli

_CODE = "ledger_authority_interrupted"
_ACTOR = verifier.Actor("agent", "synthetic")


class _HoldOnAppend:
    def __init__(self, journal, event):
        self._journal = journal
        self._event = event

    def events(self):
        return self._journal.events()

    def append(self, event):
        if event["event"] == self._event:
            raise LedgerIntegrityHeld(_CODE)
        self._journal.append(event)


class _ProtectedFake(MemoryLedger):
    integrity_protected = True
    read_held = False
    append_held = ""

    def events(self, key):
        if self.read_held:
            raise LedgerIntegrityHeld(_CODE)
        return super().events(key)

    @contextmanager
    def transaction(self, key):
        with super().transaction(key) as journal:
            yield _HoldOnAppend(journal, self.append_held)


@pytest.fixture
def held_setup(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "sql",
                    "sqlite_database": str(tmp_path / "unused.sqlite"),
                    "sql_query": "SELECT id FROM items",
                },
                "effects": [
                    {
                        "kind": "record_written",
                        "match": {"id": {"literal": "1", "number": True}},
                        "expected_count": 1,
                    }
                ],
            }
        )
    )
    values = tmp_path / "values.json"
    values.write_text("{}")
    bridge = VerifyBridge(tmp_path, tmp_path / "attempts")
    backend = _ProtectedFake(tmp_path / "ledger")
    ledger = Ledger(backend)
    monkeypatch.setattr(verifier, "default_ledger", lambda: ledger)
    return contract_path, values, bridge, backend, ledger


def _unexpected(*_args, **_kwargs):
    pytest.fail("an integrity hold reached an operation that must stay blocked")


def test_begin_hold_blocks_canonical_cli_and_mcp(held_setup, monkeypatch, capsys):
    path, values, bridge, backend, _ledger = held_setup
    backend.read_held = True
    monkeypatch.setattr(verifier, "capture_baseline", _unexpected)
    monkeypatch.setattr(cli.subprocess, "run", _unexpected)
    with pytest.raises(LedgerIntegrityHeld, match=f"^{_CODE}$"):
        verifier.begin_write(bridge.contracts["contract"], {}, actor=_ACTOR)
    assert (
        cli.main(
            [
                "run",
                "--contract",
                str(path),
                "--params",
                str(values),
                "--state",
                str(bridge.state_dir),
                "--",
                "unused",
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out) == {
        "verified": False,
        "written": False,
        "outcome": "UNCERTAIN",
        "hold_reason": _CODE,
    }
    mcp = bridge._begin({"contract": "contract", "values": {}})
    assert mcp["ok_to_write"] is False and mcp["hold_reason"] == _CODE
    assert not bridge._pending


def test_settlement_hold_never_publishes_or_signs_staged_positive(
    held_setup, monkeypatch
):
    _path, _values, bridge, backend, ledger = held_setup
    contract = bridge.contracts["contract"]
    key = ledger.key_for(contract.identity, {}, None)
    write_id = "write-synthetic"
    folder = backend.attempt_dir(write_id)
    ledger.begun(key, write_id, folder=str(folder))
    lease = backend.lease(key)
    assert lease is not None
    pending = verifier.PendingWrite(
        contract,
        {},
        _ACTOR,
        write_id,
        folder,
        "synthetic",
        write_key=key,
        ledger=ledger,
        _lease=lease,
    )
    pending.baseline_path.write_text("{}")
    monkeypatch.setattr(
        verifier,
        "verify_run",
        lambda *_args, **_kwargs: {
            "receipt": {"outcome": "verified", "checked": {}},
            "evidence": {},
        },
    )
    monkeypatch.setattr(
        verifier, "recheck_state", lambda _base, doc, _post: {"receipt": doc["receipt"]}
    )
    for name in ("write_result", "_seal", "_issue_recheck", "issue_write_receipt"):
        monkeypatch.setattr(verifier, name, _unexpected)
    backend.append_held = "resolved"
    with pytest.raises(LedgerIntegrityHeld, match=f"^{_CODE}$"):
        verifier.verify_write(pending, write_receipt=True)
    attempt = ledger.latest(key)
    assert attempt is not None and attempt.resolved is None
    assert pending.baseline_path.is_file()
    assert not (folder / "effect-receipt.json").exists()


@pytest.mark.parametrize("boundary", ["read", "append"])
def test_recheck_hold_does_not_spend_owed_mcp_state(
    held_setup, monkeypatch, capsys, boundary
):
    path, values, bridge, backend, ledger = held_setup
    contract = bridge.contracts["contract"]
    key = ledger.key_for(contract.identity, {}, None)
    write_id = "write-synthetic"
    receipt = {"outcome": "verified", "correlation_id": write_id}
    state = verifier.RecheckState(
        payload={
            "receipt": receipt,
            "evidence": {"frame": {"recheck": {"due_at": "2026-01-01T00:00:00Z"}}},
        }
    )
    pending = verifier.PendingWrite(
        contract, {}, _ACTOR, write_id, bridge.state_dir, ""
    )
    first = verifier.WriteResult(
        outcome=verifier.Outcome.CONFIRMED,
        ledger=ledger,
        recheck=state,
        write_key=key,
        write_id=write_id,
        scope=verifier._scope(contract, {}),
    )
    bridge._rechecks[write_id] = (pending, first)
    monkeypatch.setattr(verifier, "_recheck_issued", lambda _payload: True)
    monkeypatch.setattr(verifier, "_recheck_withdrawn", lambda _payload: False)
    monkeypatch.setattr(verifier, "recheck_problem", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verifier, "seconds_until", lambda _due: 0)
    if boundary == "read":
        monkeypatch.setattr(verifier, "recheck_run", _unexpected)
    else:
        monkeypatch.setattr(
            verifier,
            "recheck_run",
            lambda *_args, **_kwargs: (
                SimpleNamespace(status="held", excused_by=()),
                "2026-01-02T00:00:00Z",
            ),
        )
    monkeypatch.setattr(
        verifier.RecheckState,
        "load",
        classmethod(lambda _cls, *_args, **_kwargs: state),
    )
    ledger.begun(key, write_id)
    ledger.resolved(
        key,
        write_id,
        outcome="CONFIRMED",
        receipt_sha256=verifier.receipt_sha256(receipt),
        result={"provisional": True, "recheck_due_at": state.due_at},
    )
    if boundary == "read":
        backend.read_held = True
    else:
        backend.append_held = "rechecked"
    mcp = bridge._recheck({"write_id": write_id})
    assert mcp["hold_reason"] == _CODE and mcp["recheck_owed"] is True
    assert mcp["outcome"] == "UNCERTAIN" and mcp["verified"] is False
    assert bridge._rechecks[write_id] == (pending, first) and not bridge._spent
    monkeypatch.setattr("openadapt_flow.write_ledger.default_ledger", lambda: ledger)
    assert (
        cli.main(
            [
                "recheck",
                "--contract",
                str(path),
                "--params",
                str(values),
                "--recheck-state",
                "unused",
                "--agent-id",
                _ACTOR.id,
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out) == {
        "verified": False,
        "outcome": "UNCERTAIN",
        "hold_reason": _CODE,
    }


def test_incomplete_proof_cannot_be_handed_back(held_setup, monkeypatch):
    _path, _values, bridge, backend, ledger = held_setup
    contract = bridge.contracts["contract"]
    key = ledger.key_for(contract.identity, {}, None)
    write_id = "write-synthetic"
    folder = backend.attempt_dir(write_id)
    ledger.begun(key, write_id, folder=str(folder))
    ledger.resolved(
        key,
        write_id,
        outcome="CONFIRMED",
        receipt_sha256="a" * 64,
        result={"verified": True, "receipt_path": str(folder / "effect-receipt.json")},
    )
    before = backend.events(key)
    monkeypatch.setattr(verifier, "capture_baseline", _unexpected)
    with pytest.raises(LedgerIntegrityHeld, match=f"^{_CODE}$"):
        verifier.begin_write(contract, {}, actor=_ACTOR)
    assert backend.events(key) == before


def test_trusted_owed_lookup_propagates_integrity_hold(held_setup, monkeypatch):
    _path, _values, bridge, backend, ledger = held_setup
    contract = bridge.contracts["contract"]
    write_id = "write-synthetic"
    key = ledger.key_for(contract.identity, {}, None)
    first = verifier.WriteResult(
        outcome=verifier.Outcome.CONFIRMED,
        ledger=ledger,
        write_id=write_id,
        write_key=key,
        scope=verifier._scope(contract, {}),
    )
    owed = verifier.RecheckResult(
        outcome="UNCERTAIN", frame="not_checked", recheck_owed=True
    )
    monkeypatch.setattr(
        verifier, "_judge_recheck", lambda *_args, **_kwargs: (owed, True, 1)
    )
    backend.read_held = True
    with pytest.raises(LedgerIntegrityHeld, match=f"^{_CODE}$"):
        verifier.recheck(contract, first, params={}, actor=_ACTOR)


def test_post_commit_proof_failure_keeps_baseline_and_owed_recheck(
    held_setup, monkeypatch
):
    _path, _values, bridge, backend, ledger = held_setup
    contract = bridge.contracts["contract"]
    key = ledger.key_for(contract.identity, {}, None)
    write_id = "write-proof-failure"
    folder = backend.attempt_dir(write_id)
    ledger.begun(key, write_id, folder=str(folder))
    lease = backend.lease(key)
    assert lease is not None
    pending = verifier.PendingWrite(
        contract,
        {},
        _ACTOR,
        write_id,
        folder,
        "synthetic",
        write_key=key,
        ledger=ledger,
        _lease=lease,
    )
    pending.baseline_path.write_text("{}")
    due_at = "2026-01-01T00:00:00Z"
    receipt = {"outcome": "verified", "checked": {}, "recheck_due_at": due_at}
    monkeypatch.setattr(
        verifier,
        "verify_run",
        lambda *_args, **_kwargs: {"receipt": receipt, "evidence": {}},
    )
    monkeypatch.setattr(
        verifier, "recheck_state", lambda _base, doc, _post: {"receipt": doc["receipt"]}
    )

    def fail_artifact(*_args, **_kwargs):
        raise OSError("synthetic artifact write failure")

    monkeypatch.setattr(verifier, "write_result", fail_artifact)
    monkeypatch.setattr(verifier, "_issue_recheck", _unexpected)
    monkeypatch.setattr(verifier, "_seal", _unexpected)
    monkeypatch.setattr(verifier, "issue_write_receipt", _unexpected)
    with pytest.raises(LedgerIntegrityHeld, match=f"^{_CODE}$"):
        verifier.verify_write(pending, write_receipt=True)
    attempt = ledger.latest(key)
    assert attempt is not None and attempt.resolved is not None
    assert attempt.resolved["receipt_sha256"] == verifier.receipt_sha256(receipt)
    assert attempt.events[-1]["applied"] is True
    assert attempt.recheck_owed == due_at
    assert pending.baseline_path.read_text() == "{}"
    assert not (folder / "effect-receipt.json").exists()
    assert not (folder / "recheck.json").exists()
