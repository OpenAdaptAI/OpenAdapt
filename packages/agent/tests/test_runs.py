"""Run records and the request_id at-most-once gate."""

from __future__ import annotations

import json

import pytest

from openadapt_agent.contract import build_result
from openadapt_agent.runs import RunStore, pid_alive


def finish(store, begin, reason):
    result = build_result(
        reason,
        workflow="w",
        run_id=begin.run_id,
        request_id="req-0001",
        mode="production",
    )
    return store.finish(begin.record, result)


@pytest.fixture()
def store(tmp_path):
    return RunStore(tmp_path / "runs")


def test_uses_flow_idempotency_ledger(store):
    assert store.ledger_kind == "openadapt-flow"


def test_same_request_replays_the_first_result_and_never_starts_twice(store):
    first = store.begin(request_id="req-0001", workflow="w", inputs={"a": "1"}, mode="production")
    assert first.kind == "new"
    finish(store, first, "saved_and_checked")
    again = store.begin(request_id="req-0001", workflow="w", inputs={"a": "1"}, mode="production")
    assert again.kind == "replay"
    assert again.run_id == first.run_id
    assert again.record["result"]["outcome"] == "done"


def test_a_running_request_replays_instead_of_starting_again(store):
    first = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    again = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    assert again.kind == "replay"
    assert again.run_id == first.run_id


@pytest.mark.parametrize(
    "reason", ["save_not_confirmed", "timed_out", "record_not_confirmed", "stopped_by_person"]
)
def test_uncertain_or_reviewed_results_are_never_retried(store, reason):
    first = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    finish(store, first, reason)
    again = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    assert again.kind == "replay"
    assert again.run_id == first.run_id


def test_proven_no_write_result_allows_a_new_attempt_until_the_limit(tmp_path):
    store = RunStore(tmp_path / "runs", max_attempts=2)
    first = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    finish(store, first, "platform_error")
    second = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    assert second.kind == "new"
    assert second.run_id != first.run_id
    finish(store, second, "not_ready_to_run")
    third = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    assert third.kind == "retry_limit"
    assert third.first_run_id == first.run_id


def test_reused_request_id_with_different_inputs_is_a_conflict(store):
    first = store.begin(request_id="req-0001", workflow="w", inputs={"a": "1"}, mode="production")
    conflict = store.begin(
        request_id="req-0001", workflow="w", inputs={"a": "2"}, mode="production"
    )
    assert conflict.kind == "conflict"
    assert conflict.first_run_id == first.run_id


def test_records_never_hold_input_values(store, tmp_path):
    secret = "Jane Roe MRN 8812345"
    first = store.begin(request_id="req-0001", workflow="w", inputs={"a": secret}, mode="production")
    finish(store, first, "saved_and_checked")
    for path in (tmp_path / "runs" / "openadapt-agent").rglob("*.json"):
        assert secret not in path.read_text()


def test_two_stores_on_one_directory_share_the_gate(tmp_path):
    one = RunStore(tmp_path / "runs")
    two = RunStore(tmp_path / "runs")
    first = one.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    second = two.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    assert second.kind == "replay"
    assert second.run_id == first.run_id


def test_a_race_with_different_inputs_is_a_conflict_not_a_replay(tmp_path):
    one = RunStore(tmp_path / "runs")
    two = RunStore(tmp_path / "runs")
    first = one.begin(request_id="req-0001", workflow="w", inputs={"a": "1"}, mode="production")
    # The first process reserved and wrote its record but not yet its request
    # entry, so the second one only meets the ledger reservation.
    for entry in one.requests.glob("*.json"):
        entry.unlink()
    second = two.begin(request_id="req-0001", workflow="w", inputs={"a": "2"}, mode="production")
    assert second.kind == "conflict"
    assert second.first_run_id == first.run_id
    same = two.begin(request_id="req-0001", workflow="w", inputs={"a": "1"}, mode="production")
    assert same.kind == "replay"
    assert same.run_id == first.run_id


def test_unsafe_run_ids_read_nothing(store):
    assert store.read("../etc/passwd") is None
    assert store.read("run-" + "z" * 200) is None


def test_record_files_are_owner_only(store, tmp_path):
    begin = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    path = tmp_path / "runs" / "openadapt-agent" / "runs" / f"{begin.run_id}.json"
    assert json.loads(path.read_text())["state"] == "running"
    assert path.stat().st_mode & 0o077 == 0


def test_pid_alive_detects_this_process_and_rejects_nonsense():
    import os

    assert pid_alive(os.getpid()) is True
    assert pid_alive(None) is False
    assert pid_alive(-1) is False


def test_lost_run_record_resolves_to_an_orphan_not_running_forever(store, tmp_path):
    first = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    (tmp_path / "runs" / "openadapt-agent" / "runs" / f"{first.run_id}.json").unlink()
    again = store.begin(request_id="req-0001", workflow="w", inputs={}, mode="production")
    assert again.kind == "replay"
    assert again.run_id == first.run_id
    assert again.record["state"] == "running"
    assert again.record["pid"] is None


def test_stale_reservation_without_a_record_becomes_an_orphan(store, monkeypatch):
    import openadapt_agent.runs as runs_mod

    run_id = "run-" + "d" * 24
    request_hash = store._request_hash("req-0002")
    store._ledger.reserve(f"{request_hash}:1", run_id=run_id)
    monkeypatch.setattr(runs_mod, "ORPHAN_GRACE_S", -1.0)
    begin = store.begin(request_id="req-0002", workflow="w", inputs={}, mode="production")
    assert begin.kind == "replay"
    assert begin.run_id == run_id
    assert begin.record["pid"] is None
