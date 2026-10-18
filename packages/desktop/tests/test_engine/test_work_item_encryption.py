"""Item values are encrypted at rest with a key in the OS keychain (ledger B277).

A work item's input values, its label (the first input value) and the
expected and observed record text from a right-record check can identify a
patient. The local index database keeps them only as ciphertext. The key is in
the OS credential store, never in the database. Once an item is closed its
input values are dropped, because nothing reads them again.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from openadapt_desktop.auth.store import SERVICE_NAME
from openadapt_desktop.config import EngineConfig
from openadapt_desktop.db import IndexDB
from openadapt_desktop.dispatch import EngineDispatcher, EngineServices
from tests.test_engine.test_work_items import (
    TARGET,
    _Audit,
    _Bridge,
    _run,
    _Storage,
    _verified_report,
)

DECLARED = [
    {"name": "member_id", "required": True, "secret": False},
    {"name": "note", "required": True, "secret": False},
]


@pytest.fixture
def task(tmp_path: Path, monkeypatch):
    """A task with two inputs; Flow itself is not needed to store items."""

    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    bundle = config.data_dir / "bundles" / "wf-note"
    bundle.mkdir(parents=True)
    db.insert_bundle("wf-note", str(bundle), capture_id=None)
    services = EngineServices(config, db=db, storage=_Storage(), audit=_Audit())
    disp = EngineDispatcher(config, services=services, emit=lambda *_: None)
    monkeypatch.setattr(disp, "_task_inputs", lambda _workflow_id: DECLARED)
    yield disp, db, []
    if disp.services._db is not None:
        disp.services._db.close()

MEMBER = "M-48213377"
OBSERVED = "M-99120044"


def _raw_bytes(db_path: Path) -> bytes:
    data = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            data += candidate.read_bytes()
    return data


def _raw_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM work_items")]
    finally:
        conn.close()


def _legacy_digest(workflow_id: str, values: dict) -> str:
    canonical = json.dumps(
        {"workflow_id": workflow_id, "inputs": values},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _wrong_record_report() -> dict:
    return _verified_report(
        transaction="HALTED_BEFORE_EFFECT",
        execution="HALTED",
        halt={"state_id": "step_1", "intent": "Click Save", "reason": "Mismatch"},
        results=[
            {
                "step_id": "step_1",
                "intent": "Click Save",
                "ok": False,
                "identity": {"status": "mismatch", "expected": MEMBER, "observed": OBSERVED},
            }
        ],
    )


def test_item_values_never_reach_the_database_file_in_plain_text(task, tmp_path) -> None:
    disp, db, _events = task
    disp.services._flow_bridge = _Bridge([_wrong_record_report()])

    result = _run(disp, MEMBER)

    # The local cockpit still sees the values it needs.
    item = result["item"]
    assert item["label"] == MEMBER
    assert item["expected"] == MEMBER and item["observed"] == OBSERVED
    assert db.get_work_item(item["item_id"])["label"] == MEMBER
    # The file on disk does not.
    raw = _raw_bytes(tmp_path / "index.db")
    assert MEMBER.encode() not in raw
    assert OBSERVED.encode() not in raw
    (row,) = _raw_rows(tmp_path / "index.db")
    for column in ("label", "inputs_json", "expected", "observed"):
        assert row[column].startswith("enc:v1:"), column
    # The duplicate check can't be used to confirm a guessed member ID.
    assert row["inputs_digest"] != _legacy_digest(
        "wf-note", {"member_id": MEMBER, "note": "Reviewed"}
    )


def test_the_key_is_in_the_keychain_not_in_the_database(task, tmp_path, fake_keyring) -> None:
    disp, _db, _events = task
    disp.services._flow_bridge = _Bridge([_wrong_record_report()])
    _run(disp, MEMBER)

    keys = {
        account: value
        for (service, account), value in fake_keyring._store.items()
        if service == SERVICE_NAME and account.startswith("work-item-key|")
    }
    assert len(keys) == 1
    (key,) = keys.values()
    assert key.encode() not in _raw_bytes(tmp_path / "index.db")


def test_a_stopped_item_keeps_its_values_for_a_retry(task) -> None:
    disp, db, _events = task
    bridge = _Bridge(
        [_wrong_record_report(), _verified_report(transaction="VERIFIED", execution="VERIFIED")]
    )
    disp.services._flow_bridge = bridge
    first = _run(disp, MEMBER)

    retried = disp.dispatch(
        "resolve_attention_item",
        {"item_id": first["item"]["item_id"], "action": "retry", "target": TARGET},
    )

    assert retried["ok"] is True
    assert bridge.calls[1]["params_file"]["values"] == {"member_id": MEMBER, "note": "Reviewed"}


def test_a_closed_item_drops_its_values(task, tmp_path) -> None:
    disp, db, _events = task
    disp.services._flow_bridge = _Bridge(
        [_wrong_record_report(), _verified_report(transaction="VERIFIED", execution="VERIFIED")]
    )
    stopped = _run(disp, MEMBER)
    done = _run(disp, "M-2")
    disp.dispatch(
        "resolve_attention_item",
        {"item_id": stopped["item"]["item_id"], "action": "skip"},
    )

    for item_id in (stopped["item"]["item_id"], done["item"]["item_id"]):
        assert json.loads(db.get_work_item(item_id)["inputs_json"] or "{}") == {}
    # A done item is still never entered twice: the check uses the digest.
    again = _run(disp, "M-2")
    assert again["pre_action_refusal"] is True
    assert "already done" in again["error"]


def test_legacy_plain_text_rows_are_encrypted_on_start(tmp_path) -> None:
    db_path = tmp_path / "index.db"
    old = IndexDB(db_path)
    old.initialize()
    done_values = {"member_id": "M-70000001", "note": "Reviewed"}
    open_values = {"member_id": MEMBER, "note": "Reviewed"}
    for item_id, values, status in (
        ("item-done", done_values, "done"),
        ("item-open", open_values, "open"),
    ):
        old.conn.execute(
            "INSERT INTO work_items (item_id, batch_id, workflow_id, position, label,"
            " inputs_json, inputs_digest, status, expected, observed, created_at)"
            " VALUES (?, 'b', 'wf-note', 0, ?, ?, ?, ?, ?, ?, '2026-10-13T00:00:00+00:00')",
            (
                item_id,
                values["member_id"],
                json.dumps(values, sort_keys=True),
                _legacy_digest("wf-note", values),
                status,
                values["member_id"],
                OBSERVED,
            ),
        )
    old.conn.commit()
    old.close()

    db = IndexDB(db_path)
    db.initialize()
    try:
        raw = _raw_bytes(db_path)
        assert MEMBER.encode() not in raw
        assert b"M-70000001" not in raw
        assert OBSERVED.encode() not in raw
        opened = db.get_work_item("item-open")
        assert opened["label"] == MEMBER
        assert json.loads(opened["inputs_json"]) == open_values
        assert opened["observed"] == OBSERVED
        # The done item's values are gone; its label stays for Results.
        finished = db.get_work_item("item-done")
        assert json.loads(finished["inputs_json"] or "{}") == {}
        assert finished["label"] == "M-70000001"
        # Its digest was recomputed with the key, so it still counts as done.
        assert finished["inputs_digest"] == db.work_item_digest("wf-note", done_values)
    finally:
        db.close()


def test_no_keychain_refuses_before_anything_runs(task, monkeypatch) -> None:
    disp, db, _events = task
    bridge = _Bridge([_verified_report(transaction="VERIFIED", execution="VERIFIED")])
    disp.services._flow_bridge = bridge

    class NoKeyring:
        def get_password(self, *_a):
            raise RuntimeError("no backend")

        def set_password(self, *_a):
            raise RuntimeError("no backend")

        def delete_password(self, *_a):
            raise RuntimeError("no backend")

    monkeypatch.setattr("openadapt_desktop.auth.store._keyring", lambda: NoKeyring())

    result = _run(disp, MEMBER)

    assert result["pre_action_refusal"] is True
    assert "credential store" in result["error"]
    assert bridge.calls == []
    assert db.list_work_items() == []


def test_a_lost_key_is_never_replaced_silently(task, fake_keyring) -> None:
    disp, db, _events = task
    bridge = _Bridge([_wrong_record_report()])
    disp.services._flow_bridge = bridge
    first = _run(disp, MEMBER)
    for entry in [k for k in fake_keyring._store if k[1].startswith("work-item-key|")]:
        del fake_keyring._store[entry]

    # A new database handle (Desktop restarted) can't read the old values.
    db.close()
    reopened = IndexDB(Path(db._db_path))
    reopened.initialize()
    disp.services._db = reopened
    row = reopened.get_work_item(first["item"]["item_id"])
    assert row["label"] == ""
    assert row["sealed_unreadable"] is True
    # The stopped item still waits, and nothing runs without its key.
    inbox = disp.dispatch("list_attention_items", {})["items"]
    assert [entry["item_id"] for entry in inbox] == [first["item"]["item_id"]]
    retry = disp.dispatch(
        "resolve_attention_item",
        {"item_id": first["item"]["item_id"], "action": "retry", "target": TARGET},
    )
    assert retry["ok"] is False
    blocked = _run(disp, "M-2")
    assert blocked["pre_action_refusal"] is True
    assert len(bridge.calls) == 1
    # No new key was written over the lost one.
    assert not [k for k in fake_keyring._store if k[1].startswith("work-item-key|")]


def test_an_item_an_earlier_version_finished_is_still_never_repeated(task) -> None:
    """A done row that kept its old unkeyed digest still counts as done."""

    disp, db, _events = task
    bridge = _Bridge([_verified_report(transaction="VERIFIED", execution="VERIFIED")])
    disp.services._flow_bridge = bridge
    values = {"member_id": MEMBER, "note": "Reviewed"}
    db.conn.execute(
        "INSERT INTO work_items (item_id, batch_id, workflow_id, position, label,"
        " inputs_json, inputs_digest, status, created_at)"
        " VALUES ('item-old', 'b', 'wf-note', 0, '', '{}', ?, 'done',"
        " '2026-10-13T00:00:00+00:00')",
        (_legacy_digest("wf-note", values),),
    )
    db.conn.commit()

    again = _run(disp, MEMBER)

    assert again["pre_action_refusal"] is True
    assert "already done" in again["error"]
    assert bridge.calls == []
