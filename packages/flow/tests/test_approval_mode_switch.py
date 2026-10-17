"""The switch record, the signed Cloud status and the runner's switch log."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from openadapt_flow.approval_mode import (
    ApprovalMode,
    OffReason,
    SwitchThresholds,
    resolve_approval_mode,
)
from openadapt_flow.approval_mode.switch import (
    StatusRejected,
    StatusRequest,
    SwitchLog,
    SwitchLogError,
    sign_status,
    turn_off,
    verify_status,
)
from tests._approval_mode_support import (
    KEY_ID,
    RUNNER_ID,
    make_record,
    open_log,
    status_keypair,
)

# -- mode resolution ---------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [None, "", "Problems_Only", "problems-only", "off", 1, object(), "each item"],
)
def test_missing_or_unknown_mode_means_each_item(value):
    assert resolve_approval_mode(value) is ApprovalMode.EACH_ITEM


def test_named_modes_resolve_exactly():
    assert resolve_approval_mode("problems_only") is ApprovalMode.PROBLEMS_ONLY
    assert resolve_approval_mode("not_gated") is ApprovalMode.NOT_GATED
    assert resolve_approval_mode("each_item") is ApprovalMode.EACH_ITEM


# -- thresholds and the record -----------------------------------------------


@pytest.mark.parametrize(
    "looser",
    [
        {"status_max_age_s": 901},
        {"problems_in_a_row": 4},
        {"problems_in_window": 6},
        {"problems_window": 19},
    ],
)
def test_thresholds_can_only_be_stricter_than_the_public_defaults(looser):
    with pytest.raises(ValidationError):
        SwitchThresholds(**looser)


def test_stricter_thresholds_are_accepted():
    thresholds = SwitchThresholds(
        status_max_age_s=300, problems_in_a_row=2, problems_in_window=3
    )
    assert thresholds.problems_window == 20


def test_record_digest_binds_every_field():
    base = make_record()
    for change in (
        {"bundle_content_digest": "b" * 64},
        {"environment_digest": "sha256:" + "d" * 64},
        {"runner_id": "runner-test-0002"},
        {"verifier_identities": ("sha256:" + "c" * 64,)},
        {"rung_floor": {"save": "ocr"}},
        {"thresholds": SwitchThresholds(problems_in_a_row=2)},
    ):
        assert make_record(**change).digest() != base.digest()


def test_record_refuses_grounder_floor_and_free_text_identities():
    with pytest.raises(ValidationError):
        make_record(rung_floor={"save": "grounder"})
    with pytest.raises(ValidationError):
        make_record(verifier_identities=("rest verifier",))
    with pytest.raises(ValidationError):
        make_record(verifier_identities=())


def test_record_is_regulated_and_desktop_list_only():
    with pytest.raises(ValidationError):
        make_record(profile="standard")
    with pytest.raises(ValidationError):
        make_record(channel="cloud_schedule")


# -- signed status -----------------------------------------------------------


def _request(record, log, *, nonce="n" * 32) -> StatusRequest:
    state = log.state()
    return StatusRequest(
        switch_id=record.switch_id,
        switch_record_sha256=record.digest(),
        runner_id=record.runner_id,
        nonce=nonce,
        runner_event_seq=state.last_seq,
        runner_event_sha256=state.last_sha256,
    )


def _signed(private, request, **overrides):
    fields = {
        "switch_id": request.switch_id,
        "switch_record_sha256": request.switch_record_sha256,
        "runner_id": request.runner_id,
        "nonce": request.nonce,
        "runner_event_seq": request.runner_event_seq,
        "runner_event_sha256": request.runner_event_sha256,
        "state": "on",
        "issued_at": "2026-10-12T10:00:00+00:00",
        "key_id": KEY_ID,
    }
    fields.update(overrides)
    return sign_status(fields, private)


def test_signed_status_round_trips(tmp_path):
    private, keys = status_keypair()
    record = make_record()
    log = open_log(tmp_path, record)
    request = _request(record, log)
    status = _signed(private, request)
    assert verify_status(status, request=request, pinned_keys=keys).state == "on"
    # The same status also verifies from its JSON form.
    raw = status.model_dump_json()
    assert verify_status(raw, request=request, pinned_keys=keys).state == "on"


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "off"),
        ("runner_event_seq", 2),
        ("nonce", "m" * 32),
        ("issued_at", "2026-10-12T10:00:01+00:00"),
    ],
)
def test_any_changed_byte_fails_the_signature(tmp_path, field, value):
    private, keys = status_keypair()
    record = make_record()
    log = open_log(tmp_path, record)
    request = _request(record, log)
    payload = _signed(private, request).model_dump(mode="json")
    payload[field] = value
    with pytest.raises(StatusRejected):
        verify_status(payload, request=request, pinned_keys=keys)


def test_status_from_an_unpinned_key_is_refused(tmp_path):
    private, _keys = status_keypair()
    _other, other_keys = status_keypair()
    record = make_record()
    log = open_log(tmp_path, record)
    request = _request(record, log)
    with pytest.raises(StatusRejected):
        verify_status(
            _signed(private, request), request=request, pinned_keys=other_keys
        )
    with pytest.raises(StatusRejected):
        verify_status(_signed(private, request), request=request, pinned_keys={})


def test_replayed_nonce_and_missing_local_event_are_refused(tmp_path):
    private, keys = status_keypair()
    record = make_record()
    log = open_log(tmp_path, record)
    old_request = _request(record, log, nonce="o" * 32)
    old_status = _signed(private, old_request)
    # A later request uses a fresh nonce: the old answer doesn't answer it.
    new_request = _request(record, log, nonce="p" * 32)
    with pytest.raises(StatusRejected, match="nonce"):
        verify_status(old_status, request=new_request, pinned_keys=keys)
    # The runner logged an event Cloud doesn't hold yet: a status bound to the
    # older sequence number doesn't include the runner's own events.
    log.append("status_unconfirmed")
    newer = _request(record, log, nonce="o" * 32)
    with pytest.raises(StatusRejected, match="latest event"):
        verify_status(old_status, request=newer, pinned_keys=keys)


def test_status_for_another_runner_or_record_is_refused(tmp_path):
    private, keys = status_keypair()
    record = make_record()
    log = open_log(tmp_path, record)
    request = _request(record, log)
    with pytest.raises(StatusRejected, match="runner"):
        verify_status(
            _signed(private, request, runner_id="runner-test-0002"),
            request=request,
            pinned_keys=keys,
        )
    with pytest.raises(StatusRejected, match="switch record"):
        verify_status(
            _signed(private, request, switch_record_sha256="sha256:" + "1" * 64),
            request=request,
            pinned_keys=keys,
        )


def test_unknown_status_fields_are_refused(tmp_path):
    private, keys = status_keypair()
    record = make_record()
    log = open_log(tmp_path, record)
    request = _request(record, log)
    payload = _signed(private, request).model_dump(mode="json")
    payload["note"] = "free text"
    with pytest.raises(StatusRejected, match="malformed"):
        verify_status(payload, request=request, pinned_keys=keys)


# -- local switch log --------------------------------------------------------


def test_log_opens_once_and_chains_events(tmp_path):
    record = make_record()
    log = open_log(tmp_path, record)
    log.append("item_started", run_ref="1" * 64)
    from openadapt_flow.approval_mode.model import ItemDecisionKind

    log.append(
        "item_finished",
        run_ref="1" * 64,
        decision=ItemDecisionKind.NO_PERSON,
        counts_toward_problems=False,
    )
    events = log.read()
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[1].prev_sha256 == events[0].sha256
    state = log.state()
    assert state.switch_record_sha256 == record.digest()
    assert state.open_items == []
    assert not state.is_off
    with pytest.raises(SwitchLogError):
        SwitchLog.open_new(log.path, record)


def test_local_switch_off_is_final(tmp_path):
    log = open_log(tmp_path, make_record())
    turn_off(log, OffReason.TURNED_OFF)
    log.append("status_restored")
    state = log.state()
    assert state.is_off
    assert state.off_reason is OffReason.TURNED_OFF


def test_missing_log_is_an_error(tmp_path):
    with pytest.raises(SwitchLogError, match="missing"):
        SwitchLog(tmp_path / "nope.jsonl").read()


def _lines(log):
    return log.path.read_bytes().split(b"\n")[:-1]


def test_edited_event_breaks_the_chain(tmp_path):
    log = open_log(tmp_path, make_record())
    turn_off(log, OffReason.SCREEN_CHANGED)
    lines = _lines(log)
    payload = json.loads(lines[1])
    payload["reason"] = "turned_off"
    lines[1] = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    log.path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(SwitchLogError):
        log.read()


def test_deleted_switch_off_event_is_detected(tmp_path):
    log = open_log(tmp_path, make_record())
    turn_off(log, OffReason.UNCERTAIN_RESULT)
    log.append("status_unconfirmed")
    lines = _lines(log)
    # Removing the switch-off (to turn the switch back on) leaves a gap.
    log.path.write_bytes(b"\n".join([lines[0], lines[2]]) + b"\n")
    with pytest.raises(SwitchLogError):
        log.read()


def test_truncated_or_noncanonical_log_is_unreadable(tmp_path):
    log = open_log(tmp_path, make_record())
    raw = log.path.read_bytes()
    log.path.write_bytes(raw[:-5])
    with pytest.raises(SwitchLogError):
        log.read()
    log.path.write_bytes(raw.replace(b",", b", ", 1))
    with pytest.raises(SwitchLogError):
        log.read()


def test_switch_off_event_requires_a_closed_reason(tmp_path):
    log = open_log(tmp_path, make_record())
    with pytest.raises(ValidationError):
        log.append("switched_off", reason="because")
    with pytest.raises(ValidationError):
        log.append("switched_off")


def test_log_files_are_private(tmp_path):
    import os
    import stat

    log = open_log(tmp_path, make_record())
    if os.name != "nt":
        assert stat.S_IMODE(log.path.stat().st_mode) == 0o600


def test_record_and_status_runner_ids_are_bound():
    assert make_record().runner_id == RUNNER_ID
