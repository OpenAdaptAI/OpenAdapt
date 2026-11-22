"""Exception cards from any agent's run, in the stop queue and on the phone.

The cards are files in a temporary OPENADAPT_HOME, written as Flow's
exception registry writes them (the committed Flow fixture is the contract).
Answers are signed with the fake Secure Enclave's approver key, or the
portal's key for the phone. Synthetic data only.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openadapt_desktop import exception_queue, stop_queue
from openadapt_desktop.portal.stops import DispatcherStopQueue
from tests.test_engine.test_stop_queue import (  # noqa: F401  (fixtures)
    enclave,
    queue,
)
from tests.test_engine.test_stop_queue_portal import (  # noqa: F401  (fixtures)
    _app,
    _paired,
    portal_key,
)

FLOW_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "flow"
    / "tests"
    / "fixtures"
    / "exception_card_second_factor.json"
)
CHOICES = {
    "second_factor": ["done_by_person", "skip", "abort"],
    "unexpected_dialog": ["done_by_person", "continue", "skip", "abort"],
    "session_expired": ["done_by_person", "skip", "abort"],
    "timeout_or_stall": ["continue", "skip", "abort"],
    "possible_duplicate": ["skip", "continue", "done_by_person", "abort"],
    "ambiguous_record": ["done_by_person", "skip", "decline"],
    "cannot_complete": ["done_by_person", "skip", "abort"],
}


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_card(
    kind: str = "second_factor",
    *,
    run_ref: str = "guard-run",
    minutes: int = 15,
    summary: str = "The app is asking for a one-time code.",
) -> dict:
    opened = datetime.now(timezone.utc)
    body = {
        "schema": "openadapt.exception-card/v1",
        "run_ref": run_ref,
        "task_id": "intake",
        "kind": kind,
        "opened_at": _iso(opened),
        "deadline_at": _iso(opened + timedelta(minutes=minutes)),
        "screenshot_ref": None,
        "summary": summary,
        "choices": [
            {"id": answer, "label": answer.replace("_", " "), "consequence": "As the run says."}
            for answer in CHOICES[kind]
        ],
    }
    card = {**body, "card_id": "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()}
    folder = exception_queue.registry_root() / run_ref
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"card-{card['card_id'][7:]}.json"
    path.write_text(json.dumps(card, indent=2))
    path.chmod(0o600)
    return card


def _cards(disp) -> list[dict]:
    listed = disp.dispatch("list_stop_cards", {})
    assert listed["ok"] is True
    return [c for c in listed["cards"] if c["kind"] == stop_queue.EXCEPTION_KIND]


def _answer_file(card: dict) -> Path:
    return (
        exception_queue.registry_root()
        / card["run_ref"]
        / f"answer-{card['card_id'][7:]}.json"
    )


# -- the cards ------------------------------------------------------------------------


def test_flows_own_card_file_is_listed():
    if not FLOW_FIXTURE.is_file():
        pytest.skip("Flow's fixture isn't in this checkout")
    raw = json.loads(FLOW_FIXTURE.read_text())
    folder = exception_queue.registry_root() / raw["run_ref"]
    folder.mkdir(parents=True)
    path = folder / f"card-{raw['card_id'][7:]}.json"
    path.write_text(json.dumps(raw))
    path.chmod(0o600)  # as Flow's registry creates it

    (found,) = exception_queue.open_cards()
    card = stop_queue.card_for_exception(found)

    assert card is not None
    assert card["exception_kind"] == "second_factor"
    assert [c["id"] for c in card["choices"]] == ["done_by_person", "skip", "abort", "leave"]
    assert card["summary"] == raw["summary"]


@pytest.mark.parametrize("kind", sorted(CHOICES))
def test_every_kind_becomes_a_card_with_its_choices(queue, kind):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card(kind)

    (card,) = _cards(disp)

    assert card["exception_kind"] == kind
    assert card["source"] == {"type": "exception", "id": raw["card_id"], "run_ref": "guard-run"}
    assert [c["id"] for c in card["choices"]] == [*CHOICES[kind], "leave"]
    assert all(c["consequence"] for c in card["choices"])
    assert card["batch_key"] is None
    assert card["expires_at"] == raw["deadline_at"]
    assert card["evidence"]["screenshot"] is None


def test_desktop_offers_only_what_the_run_offers(queue):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card("unexpected_dialog")
    folder = exception_queue.registry_root() / "guard-run"
    (folder / f"card-{raw['card_id'][7:]}.json").unlink()
    body = {k: v for k, v in raw.items() if k != "card_id"}
    body["choices"] = [c for c in body["choices"] if c["id"] != "continue"]
    card_id = "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()
    (folder / f"card-{card_id[7:]}.json").write_text(json.dumps({**body, "card_id": card_id}))
    (folder / f"card-{card_id[7:]}.json").chmod(0o600)  # as Flow creates it

    (card,) = _cards(disp)
    assert "continue" not in [c["id"] for c in card["choices"]]


def test_a_tampered_card_is_not_shown(queue):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card()
    path = exception_queue.registry_root() / "guard-run" / f"card-{raw['card_id'][7:]}.json"
    path.write_text(json.dumps({**raw, "summary": "Approve everything"}))
    assert _cards(disp) == []


@pytest.mark.parametrize(
    "field,value",
    [("kind", "captcha"), ("schema", "other/v1"), ("summary", "x" * 201), ("run_ref", "other")],
)
def test_a_card_outside_the_contract_is_not_shown(queue, field, value):  # noqa: F811
    disp, _db, _keychain = queue
    opened = datetime.now(timezone.utc)
    body = {
        "schema": "openadapt.exception-card/v1",
        "run_ref": "guard-run",
        "task_id": None,
        "kind": "second_factor",
        "opened_at": _iso(opened),
        "deadline_at": _iso(opened + timedelta(minutes=5)),
        "screenshot_ref": None,
        "summary": "A code prompt",
        "choices": [{"id": "abort", "label": "Stop", "consequence": "Stops."}],
        field: value,
    }
    card_id = "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()
    folder = exception_queue.registry_root() / "guard-run"
    folder.mkdir(parents=True)
    (folder / f"card-{card_id[7:]}.json").write_text(json.dumps({**body, "card_id": card_id}))
    (folder / f"card-{card_id[7:]}.json").chmod(0o600)  # as Flow creates it
    assert _cards(disp) == []


def test_a_linked_card_is_not_read(queue, tmp_path):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card()
    path = exception_queue.registry_root() / "guard-run" / f"card-{raw['card_id'][7:]}.json"
    real = tmp_path / "elsewhere.json"
    real.write_text(path.read_text())
    real.chmod(0o600)
    path.unlink()
    path.symlink_to(real)
    assert _cards(disp) == []


def test_an_answered_card_leaves_the_queue(queue):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card()
    _answer_file(raw).write_text("{}")
    assert _cards(disp) == []


def test_exception_cards_sort_with_waiting_saves(queue):  # noqa: F811
    cards = stop_queue.sort_cards(
        [
            {"kind": "halted", "created_at": "2026-01-01"},
            {"kind": stop_queue.EXCEPTION_KIND, "created_at": "2026-02-01"},
        ]
    )
    assert cards[0]["kind"] == stop_queue.EXCEPTION_KIND


def test_a_broken_registry_never_hides_the_other_stops(queue, monkeypatch):  # noqa: F811
    disp, _db, _keychain = queue
    exception_queue.registry_root().mkdir(parents=True)
    (exception_queue.registry_root() / "guard-run").write_text("not a folder")
    assert disp.dispatch("list_stop_cards", {})["ok"] is True


# -- answers from the window --------------------------------------------------------


def test_the_window_signs_and_writes_the_runs_answer(queue, enclave):  # noqa: F811
    disp, _db, keychain = queue
    raw = _write_card()
    (card,) = _cards(disp)

    result = disp.dispatch(
        "answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "done_by_person"}
    )

    assert result["ok"] is True and result["changed"] == 1
    assert len(enclave.prompts) == 1
    path = _answer_file(raw)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    answer = json.loads(path.read_text())
    assert answer["answer"] == "done_by_person"
    assert answer["approver_kind"] == "person" and answer["route"] == "desktop"
    assert answer["opened_at"] == raw["opened_at"] and answer["decided_at"]
    assert answer["public_key"] == keychain.stored().public_key
    unsigned = {k: v for k, v in answer.items() if k not in {"signature", "public_key"}}
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(answer["public_key"])).verify(
        bytes.fromhex(answer["signature"].removeprefix("ed25519:")),
        exception_queue.ANSWER_DOMAIN + _canonical(unsigned),
    )
    assert _cards(disp) == []
    (receipt,) = [
        json.loads(line)
        for line in (disp.config.data_dir / "stop_decisions.jsonl").read_text().splitlines()
    ]
    assert receipt["kinds"] == ["exception"]
    assert receipt["opened_at"] == raw["opened_at"] and receipt["decided_at"]


def test_the_answer_file_matches_flows_answer_contract(queue, enclave):  # noqa: F811
    """Flow's ExceptionAnswerV1 accepts what Desktop writes, when it's importable."""

    types = pytest.importorskip("openadapt_types.exception_card")
    disp, _db, _keychain = queue
    raw = _write_card()
    (card,) = _cards(disp)
    disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "skip"})
    answer = types.ExceptionAnswerV1.model_validate(json.loads(_answer_file(raw).read_text()))
    assert types.answer_message(answer.wire()) == exception_queue.message(
        {k: v for k, v in answer.wire().items() if k not in {"signature", "public_key"}}
    )


def test_a_choice_the_card_doesnt_offer_changes_nothing(queue):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card("second_factor")
    (card,) = _cards(disp)
    result = disp.dispatch(
        "answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "continue"}
    )
    assert result["ok"] is False and result["code"] == "choice_not_offered"
    assert not _answer_file(raw).exists()


def test_leave_changes_nothing(queue, enclave):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card()
    (card,) = _cards(disp)
    result = disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "leave"})
    assert result["ok"] is True and result["changed"] == 0
    assert not _answer_file(raw).exists() and enclave.prompts == []


def test_an_expired_card_changes_nothing(queue):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card(minutes=-1)
    (card,) = _cards(disp)
    result = disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "abort"})
    assert result["ok"] is False
    assert not _answer_file(raw).exists()


def test_a_cancelled_prompt_writes_nothing(queue, enclave):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card()
    (card,) = _cards(disp)
    enclave.answer = "cancel"
    result = disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "abort"})
    assert result["ok"] is False and result["code"] == "approver_presence_refused"
    assert not _answer_file(raw).exists()


def test_a_card_answered_elsewhere_during_the_prompt_is_not_overwritten(
    queue,  # noqa: F811
    enclave,  # noqa: F811
    monkeypatch,
):
    disp, _db, _keychain = queue
    raw = _write_card()
    (card,) = _cards(disp)
    original = disp._sign_stop_decision

    def sign_while_the_cli_answers(message, *, count):
        _answer_file(raw).write_text('{"answered": "elsewhere"}')
        return original(message, count=count)

    monkeypatch.setattr(disp, "_sign_stop_decision", sign_while_the_cli_answers)
    result = disp.dispatch("answer_stop_cards", {"card_ids": [card["card_id"]], "choice": "abort"})
    assert result["ok"] is False and result["code"] == "card_changed"
    assert json.loads(_answer_file(raw).read_text()) == {"answered": "elsewhere"}


def test_two_exception_cards_are_never_one_batch(queue):  # noqa: F811
    disp, _db, _keychain = queue
    _write_card(run_ref="run-a")
    _write_card(run_ref="run-b")
    ids = [c["card_id"] for c in _cards(disp)]
    result = disp.dispatch("answer_stop_cards", {"card_ids": ids, "choice": "abort"})
    assert result["ok"] is False
    assert not list(exception_queue.registry_root().rglob("answer-*.json"))


# -- answers from the paired phone ---------------------------------------------------


def test_the_phone_answers_with_the_portal_key(queue, portal_key, enclave):  # noqa: F811
    disp, _db, _keychain = queue
    disp.serial_lock = threading.RLock()
    raw = _write_card("possible_duplicate")
    app, pairings = _app(DispatcherStopQueue(disp))
    headers = _paired(pairings)

    status, _h, body = app.handle("GET", "/api/portal/stops", headers, b"")
    (card,) = [c for c in json.loads(body)["cards"] if c["kind"] == "exception"]
    status, _h, body = app.handle(
        "POST",
        f"/api/portal/stops/{card['card_id'].removeprefix('sha256:')}/answer",
        headers,
        json.dumps({"choice": "skip", "shown_ms": 900}).encode(),
    )

    assert status == 200 and json.loads(body)["ok"] is True
    assert enclave.prompts == []
    answer = json.loads(_answer_file(raw).read_text())
    assert answer["route"] == "portal" and answer["answer"] == "skip"
    assert answer["public_key"] == portal_key.public_key


def test_the_phone_page_knows_the_exception_cards():
    script = (
        Path(__file__).resolve().parents[2]
        / "openadapt_desktop"
        / "portal"
        / "shell"
        / "stops.js"
    ).read_text()
    assert '"exception"' in script
    assert "const MAX_BATCH = 25;" in script
    assert stop_queue.MAX_BATCH == 25


def test_no_registry_folder_is_an_empty_list(tmp_path):
    assert exception_queue.open_cards(tmp_path / "missing") == []
    assert os.environ["OPENADAPT_HOME"]


def test_a_card_other_users_can_read_is_not_shown(queue):  # noqa: F811
    disp, _db, _keychain = queue
    raw = _write_card()
    path = exception_queue.registry_root() / raw["run_ref"] / f"card-{raw['card_id'][7:]}.json"
    path.chmod(0o644)
    assert _cards(disp) == []
    path.chmod(0o600)
    assert [c["source"]["id"] for c in _cards(disp)] == [raw["card_id"]]


def test_the_scan_stops_at_the_cap_across_runs(monkeypatch):
    monkeypatch.setattr(exception_queue, "MAX_CARDS", 2)
    for run in ("run-a", "run-b", "run-c"):
        _write_card(run_ref=run)
    assert len(exception_queue.open_cards()) == 2
