"""Per-item approval requests and answers (phase 0). Made-up values only.

A request is what the person sees; an answer is what they decided. These
tests pin the file contract Desktop's Run it relies on, and every way an
answer is refused: a non-human or non-Desktop decider, a different request,
an answer from before the request or after it expired, a second answer, a
reused approval, a malformed or redirected file, a claimed Cloud sign-in.
The person signs with a registered Desktop approver key (``approver``).
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from openadapt_flow.approval_mode.human_attestation import ANSWER_DOMAIN
from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalAnswer,
    ItemApprovalRefused,
    ItemApprovalStore,
    answer_refusal,
    make_answer,
    request_summary,
    wait_for_answer,
)
from openadapt_flow.approval_mode.model import ApprovalMode, ItemApprovalRef

FRAME = b"\x89PNG\r\n\x1a\nmade-up filled form"
FORM = ("dom", "sha256:" + "1" * 64)
APPROVER = "cloud-user-0001"


def _open(store: ItemApprovalStore, **overrides):
    fields = dict(
        run_id="run-0001",
        workflow_name="referral",
        step_id="save",
        step_index=3,
        step_intent="Click 'Save'",
        mode=ApprovalMode.EACH_ITEM,
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        frame_png=FRAME,
        form_state=FORM,
    )
    fields.update(overrides)
    return store.open_request(**fields)


@pytest.fixture(autouse=True)
def _signer(approver):
    global SIGNER
    SIGNER = approver[1]


SIGNER = None


def _approve(store, request, **overrides):
    fields = dict(
        decision="approve",
        decided_by="human",
        route="desktop_run_it",
        approver_id=None,
        auth_method="none",
        policy=store.context(),
        signer=SIGNER,
    )
    fields.update(overrides)
    return make_answer(request, **fields)


def test_a_request_is_two_private_files_and_waits(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)

    request_file = store.dir / f"request-{request.request_id}.json"
    shot = store.dir / f"{request.request_id}.png"
    assert shot.read_bytes() == FRAME
    if os.name != "nt":
        assert stat.S_IMODE(request_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700
    assert store.status(request) == "waiting"
    assert store.read_request(request.request_id) == request
    summary = request_summary(store, request)
    assert summary["status"] == "waiting"
    assert summary["request_sha256"] == request.digest()
    assert summary["screenshot"].endswith(f"{request.request_id}.png")
    # The form digest stays in the run directory, never in the summary.
    assert "form_state_sha256" not in summary


def test_an_approval_is_recorded_once_and_used_once(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    answer = _approve(store, request)
    store.write_answer(answer)
    assert store.status(request) == "approved"

    with pytest.raises(ItemApprovalRefused) as second:
        store.write_answer(_approve(store, request))
    assert second.value.code == "already_answered"

    assert store.consume(request.request_id) is True
    assert store.consume(request.request_id) is False
    assert store.status(request) == "used"
    rows = store.consumed_approvals("run-0001")
    assert len(rows) == 1 and isinstance(rows[0], ItemApprovalRef)
    assert rows[0].approver_id is None
    assert answer.attestation is not None
    assert answer.attestation.signer == SIGNER.principal_id
    assert rows[0].decided_by == "human"
    assert rows[0].answer_sha256 == answer.digest()


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"decided_by": "automation"}, "not_decided_by_a_person"),
        ({"decided_by": "unknown"}, "not_decided_by_a_person"),
        ({"route": "phone_portal"}, "route_not_allowed"),
        ({"route": "local_console"}, "route_not_allowed"),
        (
            {"approver_id": None, "auth_method": "cloud_aal2"},
            "auth_method_unverified",
        ),
        ({"approver_id": APPROVER}, "answer_fields_inconsistent"),
        # A claimed Cloud sign-in can't be verified here, so it is refused.
        (
            {"approver_id": APPROVER, "auth_method": "cloud_aal2"},
            "auth_method_unverified",
        ),
        ({"signer": None}, "human_answer_unsigned"),
        ({"decision": "decline"}, "answer_fields_inconsistent"),
        ({"decline_reason": "wrong_values"}, "answer_fields_inconsistent"),
        ({"decided_by": "a robot"}, "answer_fields_invalid"),
    ],
)
def test_only_a_person_in_desktop_run_it_can_answer(tmp_path, overrides, code):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    with pytest.raises(ItemApprovalRefused) as refused:
        _approve(store, request, **overrides)
    assert refused.value.code == code


def test_an_unauthenticated_approval_names_nobody(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    answer = _approve(store, request, approver_id=None, auth_method="none")
    ref = answer.reference(request)
    assert ref.approver_kind == "unauthenticated"
    assert ref.approver_id is None


def test_a_decline_needs_one_closed_reason(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    answer = _approve(store, request, decision="decline", decline_reason="wrong_record")
    store.write_answer(answer)
    assert store.status(request) == "declined"
    with pytest.raises(ItemApprovalRefused):
        _approve(store, request, decision="decline", decline_reason="other")


def test_an_answer_for_another_request_or_out_of_time_is_refused(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    other = _open(store)
    answer = _approve(store, other)
    assert answer_refusal(request, answer) == "request_mismatch"
    forged = answer.model_copy(update={"request_id": request.request_id})
    assert answer_refusal(request, forged) == "request_digest_mismatch"

    created = datetime.fromisoformat(request.created_at)
    early = _approve(store, request).model_copy(
        update={"answered_at": (created - timedelta(seconds=5)).isoformat()}
    )
    # Changing a signed answer breaks its signature; re-signed, it is still
    # refused for when it was given.
    assert answer_refusal(request, early, policy=store.context()) == (
        "human_answer_signature_invalid"
    )
    early = early.model_copy(
        update={"attestation": SIGNER.attest(ANSWER_DOMAIN, early.signed_payload())}
    )
    assert (
        answer_refusal(request, early, policy=store.context())
        == "answer_predates_request"
    )
    late_now = datetime.fromisoformat(request.expires_at) + timedelta(seconds=1)
    assert (
        answer_refusal(
            request, _approve(store, request), now=late_now, policy=store.context()
        )
        == "request_expired"
    )
    with pytest.raises(ItemApprovalRefused) as expired:
        _approve(store, request, now=late_now)
    assert expired.value.code in {"answer_after_expiry", "request_expired"}


def test_waiting_times_out_without_an_answer(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    answer, code = wait_for_answer(
        store, request, timeout_s=5, poll_s=1, monotonic=lambda: clock[0], sleep=sleep
    )
    assert answer is None and code == "approval_wait_timed_out"
    assert store.status(request) == "waiting"


def test_waiting_returns_the_answer_the_person_gives(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds
        if clock[0] >= 2 and store.status(request) == "waiting":
            store.write_answer(_approve(store, request))

    answer, code = wait_for_answer(
        store, request, timeout_s=60, poll_s=1, monotonic=lambda: clock[0], sleep=sleep
    )
    assert code is None and answer is not None and answer.decision == "approve"


def test_a_malformed_or_non_human_answer_file_ends_the_wait(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    (store.dir / f"answer-{request.request_id}.json").write_text("{not json")
    answer, code = wait_for_answer(store, request, timeout_s=5, poll_s=1)
    assert answer is None and code == "answer_malformed"
    assert store.status(request) == "invalid_answer"

    second = _open(store)
    payload = _approve(store, second).model_dump(mode="json")
    payload["decided_by"] = "automation"
    (store.dir / f"answer-{second.request_id}.json").write_text(json.dumps(payload))
    answer, code = wait_for_answer(store, second, timeout_s=5, poll_s=1)
    assert answer is None and code == "not_decided_by_a_person"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_redirected_answer_file_is_never_followed(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    outside = tmp_path / "outside.json"
    outside.write_text(_approve(store, request).model_dump_json())
    os.symlink(outside, store.dir / f"answer-{request.request_id}.json")
    with pytest.raises(ItemApprovalRefused) as refused:
        store.read_answer(request.request_id)
    assert refused.value.code == "answer_unreadable"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_redirected_approval_folder_is_refused(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, run / "item-approval")
    store = ItemApprovalStore(run)
    with pytest.raises(ItemApprovalRefused) as refused:
        _open(store)
    assert refused.value.code == "store_unsafe"


def test_a_resumed_run_uses_only_the_newest_unused_approval(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    older = _open(store)
    store.write_answer(_approve(store, older))
    newer = _open(store)
    # The person approved the older request, but the newest request for the
    # same save is the one they last saw; an older answer can't stand in.
    assert (
        store.find_unused_approval(run_id="run-0001", step_id="save", step_index=3)
        is None
    )
    store.write_answer(_approve(store, newer))
    found = store.find_unused_approval(run_id="run-0001", step_id="save", step_index=3)
    assert found is not None and found[0].request_id == newer.request_id
    assert store.consume(newer.request_id)
    assert (
        store.find_unused_approval(run_id="run-0001", step_id="save", step_index=3)
        is None
    )
    assert (
        store.find_unused_approval(run_id="other-run", step_id="save", step_index=3)
        is None
    )


def test_unknown_or_traversing_request_ids_are_refused(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    _open(store)
    for bad in ("../../etc/passwd", "A" * 32, "", "0" * 31):
        with pytest.raises(ItemApprovalRefused):
            store.read_request(bad)


def test_the_answer_model_rejects_unknown_fields(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    request = _open(store)
    payload = _approve(store, request).model_dump(mode="json")
    payload["note"] = "free text is not allowed"
    with pytest.raises(ValueError):
        ItemApprovalAnswer.model_validate(payload)


def test_request_timestamps_are_utc_and_bounded(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    now = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    request = _open(store, now=now, ttl_s=60)
    assert request.created_at.startswith("2026-10-14T09:00:00")
    assert datetime.fromisoformat(request.expires_at) - now == timedelta(seconds=60)
    assert request.expired(now + timedelta(seconds=61))
