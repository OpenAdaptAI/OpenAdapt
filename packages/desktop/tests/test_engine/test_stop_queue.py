"""The stop-review queue: cards, signed answers, batches, and what never acts.

Uses the real index database and a fake Secure Enclave, so the approver key's
prompt can be confirmed or cancelled. Item values are synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openadapt_desktop import stop_queue
from openadapt_desktop.approver_keychain import ApproverKeychain
from openadapt_desktop.config import EngineConfig
from openadapt_desktop.db import IndexDB
from openadapt_desktop.dispatch import EngineDispatcher, EngineServices
from openadapt_desktop.flow_bridge import FlowBridge
from tests.test_engine.test_approver_keychain import FakeEnclave, FakeRegistry

DUPLICATE = (
    "Postconditions failed for step 'step_039' (click 'Create New Patient'): "
    "failed: no_duplicate_candidate (no listed row has this run's lname, DOB)"
)


class _Audit:
    def __init__(self) -> None:
        self.events: list = []

    def log(self, event, **fields):
        self.events.append((event, fields))


@pytest.fixture
def enclave() -> FakeEnclave:
    return FakeEnclave()


@pytest.fixture
def queue(tmp_path: Path, enclave):
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    db.insert_bundle("wf-ref", str(tmp_path / "bundle"))
    registry = FakeRegistry()
    keychain = ApproverKeychain(
        enclave,
        register=registry.register,
        unregister=registry.unregister,
        lookup=registry.lookup,
    )
    keychain.setup()
    enclave.prompts.clear()
    services = EngineServices(
        config, db=db, audit=_Audit(), flow_bridge=FlowBridge(approver_keychain=keychain)
    )
    disp = EngineDispatcher(config, services=services)
    yield disp, db, keychain
    db.close()


def _stop(
    db,
    item_id: str,
    *,
    lname: str,
    transaction="HALTED_BEFORE_EFFECT",
    reason=DUPLICATE,
    execution="HALTED",
    diffs=None,
) -> None:
    db.insert_work_item(
        item_id,
        batch_id="batch-1",
        workflow_id="wf-ref",
        position=int(item_id[-2:]),
        label=f"{lname} referral",
        inputs_json=json.dumps({"fname": "Ana", "lname": lname, "DOB": "1941-02-27"}),
        inputs_digest=item_id,
    )
    db.update_work_item(
        item_id,
        status="open",
        run_id=f"run-{item_id}",
        execution_outcome=execution,
        transaction_outcome=transaction,
        halt_kind="screen_changed",
        halt_step="click 'Create New Patient'",
        halt_reason=reason,
        field_diffs_json=json.dumps(diffs) if diffs else None,
        finished_at="2026-10-24T12:00:00+00:00",
    )


def _cards(disp) -> list[dict]:
    listed = disp.dispatch("list_stop_cards", {})
    assert listed["ok"] is True
    return listed["cards"]


# -- cards ---------------------------------------------------------------------


def test_a_duplicate_stop_becomes_a_same_person_card(queue):
    disp, db, _keychain = queue
    _stop(db, "item-01", lname="Fairwick")

    (card,) = _cards(disp)

    assert card["kind"] == "same_record"
    assert card["question"] == "Same person?"
    assert card["batch_key"] == "same_record:wf-ref:duplicate"
    assert {row["field"]: row["value"] for row in card["evidence"]["source"]} == {
        "DOB": "1941-02-27",
        "fname": "Ana",
        "lname": "Fairwick",
    }
    assert card["evidence"]["screenshot"] == {"item_id": "item-01"}
    assert [c["id"] for c in card["choices"]] == [
        "same",
        "decline",
        "different_by_hand",
        "leave",
    ]
    # Every choice says what it does.
    assert all(c["consequence"] for c in card["choices"])


def test_record_differences_are_marked_side_by_side(queue):
    disp, db, _keychain = queue
    _stop(
        db,
        "item-02",
        lname="Fairwick",
        diffs=[
            {"field": "lname", "typed": "Fairwick", "on_file": "Fairwick"},
            {"field": "DOB", "typed": "1941-02-27", "on_file": "1941-07-22"},
        ],
    )

    (card,) = _cards(disp)

    by_field = {d["field"]: d for d in card["evidence"]["differences"]}
    assert by_field["lname"]["same"] is True
    assert by_field["DOB"] == {
        "field": "DOB",
        "label": "DOB",
        "source": "1941-02-27",
        "record": "1941-07-22",
        "same": False,
    }


def test_an_uncertain_save_asks_did_it_save(queue):
    disp, db, _keychain = queue
    _stop(
        db,
        "item-03",
        lname="Caldley",
        transaction="RECONCILIATION_REQUIRED",
        reason="record_written refuted",
    )

    (card,) = _cards(disp)

    assert card["kind"] == "confirm_save"
    assert [c["id"] for c in card["choices"]] == [
        "saved",
        "not_saved_retry",
        "fixed_by_hand",
        "leave",
    ]


def test_a_card_id_covers_everything_it_shows():
    item = {
        "item_id": "item-04",
        "source": "item",
        "workflow_id": "wf",
        "run_id": "r",
        "status": "open",
        "nothing_written": True,
        "actions": ["retry", "skip", "decline", "teach"],
        "halt_reason": "x",
        "inputs": {"lname": "A"},
    }
    first = stop_queue.card_for_item(item)
    changed = stop_queue.card_for_item({**item, "inputs": {"lname": "B"}})

    assert first is not None and changed is not None
    assert first["card_id"] != changed["card_id"]
    assert stop_queue.card_for_item(item)["card_id"] == first["card_id"]


# -- answers -------------------------------------------------------------------


def test_an_answer_is_signed_after_touch_id_then_applied(queue, enclave):
    disp, db, keychain = queue
    _stop(db, "item-05", lname="Fairwick")
    (card,) = _cards(disp)

    answer = disp.dispatch(
        "answer_stop_cards",
        {"card_ids": [card["card_id"]], "choice": "same", "shown_ms": {card["card_id"]: 4200}},
    )

    assert answer == {
        "ok": True,
        "results": [{"card_id": card["card_id"], "ok": True, "error": None}],
        "changed": 1,
    }
    assert enclave.prompts == ["record your answer to this stop"]
    assert db.get_work_item("item-05")["status"] == "skipped"
    assert _cards(disp) == []
    (receipt,) = [
        json.loads(line)
        for line in (disp.config.data_dir / "stop_decisions.jsonl").read_text().splitlines()
    ]
    assert receipt["shown_ms"] == {card["card_id"]: 4200}
    message = stop_queue.decision_message(
        [card["card_id"]], "same", decided_at=receipt["decided_at"]
    )
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(keychain.stored().public_key)).verify(
        bytes.fromhex(receipt["signature"].removeprefix("ed25519:")), message
    )


def test_a_cancelled_prompt_changes_nothing(queue, enclave):
    disp, db, _keychain = queue
    _stop(db, "item-06", lname="Fairwick")
    (card,) = _cards(disp)
    enclave.answer = "cancel"

    answer = disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "same"})

    assert answer["ok"] is False and answer["code"] == "approver_presence_refused"
    assert db.get_work_item("item-06")["status"] == "open"
    assert not (disp.config.data_dir / "stop_decisions.jsonl").exists()


def test_a_card_that_changed_since_it_was_shown_changes_nothing(queue, enclave):
    disp, db, _keychain = queue
    _stop(db, "item-07", lname="Fairwick")
    (card,) = _cards(disp)
    db.update_work_item("item-07", halt_reason="a different stop")

    answer = disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "same"})

    assert answer["code"] == "card_changed"
    assert enclave.prompts == []  # never asked to sign
    assert db.get_work_item("item-07")["status"] == "open"


@pytest.mark.parametrize("choice", ["approve", "saved", "", "teach"])
def test_a_choice_the_card_doesnt_offer_changes_nothing(queue, enclave, choice):
    disp, db, _keychain = queue
    _stop(db, "item-08", lname="Fairwick")
    (card,) = _cards(disp)

    answer = disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": choice})

    assert answer["code"] == "choice_not_offered"
    assert enclave.prompts == []
    assert db.get_work_item("item-08")["status"] == "open"


def test_not_sure_leaves_the_stop_without_a_signature(queue, enclave):
    disp, db, _keychain = queue
    _stop(db, "item-09", lname="Fairwick")
    (card,) = _cards(disp)

    answer = disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "leave"})

    assert answer == {"ok": True, "results": [], "changed": 0}
    assert enclave.prompts == []
    assert len(_cards(disp)) == 1


def test_a_batch_of_similar_stops_is_one_prompt(queue, enclave):
    disp, db, _keychain = queue
    for index, name in enumerate(["Fairwick", "Caldley", "Jesswood"], start=10):
        _stop(db, f"item-{index}", lname=name)
    cards = _cards(disp)
    assert len({c["batch_key"] for c in cards}) == 1

    answer = disp.dispatch(
        "answer_stop_cards",
        {"card_ids": [c["card_id"] for c in cards], "choice": "same"},
    )

    assert answer["ok"] is True and answer["changed"] == 3
    assert enclave.prompts == ["record your answer to 3 stops"]
    assert _cards(disp) == []


def test_a_batch_never_mixes_kinds(queue, enclave):
    disp, db, _keychain = queue
    _stop(db, "item-20", lname="Fairwick")
    _stop(db, "item-21", lname="Caldley", transaction="RECONCILIATION_REQUIRED", reason="x")
    cards = _cards(disp)

    answer = disp.dispatch(
        "answer_stop_cards",
        {"card_ids": [c["card_id"] for c in cards], "choice": "leave"},
    )

    assert answer["code"] == "card_changed"
    assert "similar" in answer["error"]
    assert enclave.prompts == []


def test_one_stale_card_stops_the_whole_batch(queue, enclave):
    disp, db, _keychain = queue
    _stop(db, "item-30", lname="Fairwick")
    _stop(db, "item-31", lname="Caldley")
    cards = _cards(disp)
    db.update_work_item("item-31", status="skipped")

    answer = disp.dispatch(
        "answer_stop_cards",
        {"card_ids": [c["card_id"] for c in cards], "choice": "same"},
    )

    assert answer["code"] == "card_changed"
    assert db.get_work_item("item-30")["status"] == "open"


def test_without_an_approver_key_nothing_changes(tmp_path):
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    config.data_dir.mkdir(parents=True)
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    db.insert_bundle("wf-ref", str(tmp_path / "bundle"))
    disp = EngineDispatcher(config, services=EngineServices(config, db=db, audit=_Audit()))
    _stop(db, "item-40", lname="Fairwick")
    listed = disp.dispatch("list_stop_cards", {})
    assert listed["approver_key"] == {"ready": False, "in_app": False, "touch_id": False}

    answer = disp.dispatch(
        "answer_stop_cards", {"card_ids": [listed["cards"][0]["card_id"]], "choice": "same"}
    )

    assert answer["code"] == "approver_key_missing"
    assert db.get_work_item("item-40")["status"] == "open"
    db.close()


def test_a_record_check_choice_states_what_the_person_found(queue):
    disp, db, _keychain = queue
    _stop(
        db,
        "item-50",
        lname="Caldley",
        transaction="RECONCILIATION_REQUIRED",
        reason="record_written refuted",
    )
    (card,) = _cards(disp)

    answer = disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "saved"})

    assert answer["ok"] is True
    assert db.get_work_item("item-50")["status"] == "confirmed_saved"


# -- save approvals --------------------------------------------------------------


def _waiting_state(answer_by: str) -> dict:
    return {
        "ok": True,
        "running": True,
        "waiting": True,
        "workflow_id": "wf-ref",
        "kind": "item",
        "request": {
            "request_id": "0" * 31 + "1",
            "request_sha256": "sha256:" + "a" * 64,
            "step_intent": "Click 'Save new patient'",
            "answer_by": answer_by,
            "screenshot": "data:image/png;base64,AAAA",
            "values": [{"name": "lname", "value": "Fairwick"}],
        },
    }


def test_a_waiting_save_is_answered_through_the_signed_item_approval(queue, monkeypatch):
    disp, _db, _keychain = queue
    disp._approval_watches["run"] = {"workflow_id": "wf-ref"}
    monkeypatch.setattr(
        disp, "get_item_approval", lambda **_: _waiting_state("2999-01-01T00:00:00+00:00")
    )
    sent: list = []
    monkeypatch.setattr(
        disp,
        "answer_item_approval",
        lambda **params: sent.append(params) or {"ok": True, "decision": "approved"},
    )
    (card,) = _cards(disp)
    assert card["kind"] == "approve_save" and card["batch_key"] is None

    answer = disp.dispatch(
        "answer_stop_approval", {"card_id": card["card_id"], "choice": "approve"}
    )

    assert answer["ok"] is True
    assert sent == [
        {
            "workflow_id": "wf-ref",
            "decision": "approve",
            "request_id": "0" * 31 + "1",
            "request_sha256": "sha256:" + "a" * 64,
            "reason": None,
        }
    ]
    # Batch answers never carry a save approval.
    refused = disp.dispatch(
        "answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "approve"}
    )
    assert refused["code"] == "use_approval"


def test_an_expired_save_card_changes_nothing(queue, monkeypatch):
    disp, _db, _keychain = queue
    disp._approval_watches["run"] = {"workflow_id": "wf-ref"}
    monkeypatch.setattr(
        disp, "get_item_approval", lambda **_: _waiting_state("2000-01-01T00:00:00+00:00")
    )
    monkeypatch.setattr(
        disp, "answer_item_approval", lambda **_: pytest.fail("sent an expired answer")
    )
    (card,) = _cards(disp)

    answer = disp.dispatch(
        "answer_stop_approval", {"card_id": card["card_id"], "choice": "approve"}
    )

    assert answer["ok"] is False and "expired" in answer["error"]


def test_the_queue_commands_answer_while_a_run_waits_and_stay_off_the_agent_socket():
    from openadapt_desktop.ipc import CONCURRENT_COMMANDS
    from openadapt_desktop.socket_server import _SOCKET_COMMANDS

    assert {"list_stop_cards", "answer_stop_approval"} <= CONCURRENT_COMMANDS
    # Batch answers can run an item again, so they wait their turn.
    assert "answer_stop_cards" not in CONCURRENT_COMMANDS
    assert not {"list_stop_cards", "answer_stop_cards", "answer_stop_approval"} & (_SOCKET_COMMANDS)
