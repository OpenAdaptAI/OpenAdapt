"""The loop that makes the hosted lane real, and what it refuses to do.

``decision_relay`` proved the wire. This file proves the two things a wire
cannot: that an open pause becomes answerable **without anyone calling
anything**, and that an answer is applied to the pause it was minted from
rather than to whichever run happened to be in hand.

The second is the one worth the most scrutiny. ``DecisionRelay.serve_once``
takes its run and item as arguments, which is safe only when exactly one pause
is open. A practice with two halted runs is the ordinary case, not the exotic
one, so ``test_a_decision_is_executed_against_the_pause_it_was_minted_from``
opens two and answers the second.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.console.attention import attention_item
from openadapt_flow.console.decision_relay import (
    DecisionRelay,
    RelayedDecision,
    RelayRefused,
    RelayUncertain,
)
from openadapt_flow.console.decision_supervisor import (
    DecisionSupervisor,
    DecisionSupervisorThread,
)
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionBusy,
    AttendedActionRefused,
    AttendedActionStore,
    AttendedRelayAcknowledgement,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_attended_actions import _ResultExecutor
from tests.test_decision_relay import (
    TOKEN,
    FakeTransport,
    _bound_relay_payload,
    _deployment,
    _sign,
)
from tests.test_replayer import FakeBackend, FakeVision

PROTECTED_VALUE = "Wilhelmina Featherstonehaugh"

LONG_PAST = "2000-01-01T00:00:00Z"


# --------------------------------------------------------------- fixtures


def _halted_run(runs_root: Path, bundles_root: Path, name: str):
    """One durably paused run under ``runs_root``, plus its attention item."""
    workflow = Workflow(
        name=f"supervisor-{name}",
        params={"patient": PROTECTED_VALUE},
        steps=[
            Step(
                id="human",
                intent=f"confirm coverage for {PROTECTED_VALUE}",
                action=ActionKind.KEY,
                key="A",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="COVERAGE ACTIVE",
                        timeout_s=0.01,
                    )
                ],
            ),
            Step(id="next", intent="record it", action=ActionKind.KEY, key="B"),
        ],
    )
    bundle = bundles_root / name
    run = runs_root / name
    workflow.save(bundle)
    report = Replayer(
        FakeBackend(), vision=FakeVision(), durable=True, poll_interval_s=0.0
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run,
        params={"patient": PROTECTED_VALUE},
    )
    assert report.success is False
    item = attention_item(runs_root, run)
    assert item is not None
    assert item.durably_paused is True
    return run, item


def _supervisor(
    runs_root: Path,
    *,
    deployment: Any = None,
    executor: Any = None,
    **responses: Any,
):
    deployment = deployment or _deployment()
    transport = FakeTransport(responses)
    relay = DecisionRelay(transport, token=TOKEN, deployment=deployment)
    supervisor = DecisionSupervisor(
        runs_root, relay=relay, deployment=deployment, executor=executor
    )
    return supervisor, transport


def _relayed_for(run: Path, item: Any, deployment: Any, **overrides: Any):
    """Mint the relay the control plane would mint for one real open pause.

    Built from the same helper the transport tests use, so a change to the
    projection cannot make this file agree with itself while disagreeing with
    the wire.
    """
    unsigned = _bound_relay_payload(run, item, deployment)
    unsigned.update(overrides)
    return _sign(unsigned)


# ------------------------------------------------------- publishing a queue


def test_an_open_pause_is_published_without_anyone_calling_anything(tmp_path):
    """The whole point: a halt becomes answerable from a phone by itself."""
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"})
    )

    report = supervisor.publish_open_pauses()

    assert report.certain_count == 1
    assert len(report.published) == 1
    assert report.unknown == ()
    assert [path for path, _ in transport.calls] == ["/api/human-decisions/tasks"]


def test_every_open_pause_is_published_not_only_the_first(tmp_path):
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    _halted_run(runs, bundles, "two")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"})
    )

    report = supervisor.publish_open_pauses()

    assert len(report.published) == 2
    assert len(transport.calls) == 2


def test_nothing_protected_reaches_the_wire_from_the_whole_queue(tmp_path):
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    _halted_run(runs, bundles, "two")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"})
    )
    supervisor.publish_open_pauses()

    body = json.dumps([payload for _, payload in transport.calls])
    assert PROTECTED_VALUE not in body
    assert "supervisor-one" not in body
    assert "COVERAGE ACTIVE" not in body


def test_an_uncertain_publish_is_reported_as_unknown_never_as_reachable(tmp_path):
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, _ = _supervisor(runs, tasks=RelayUncertain("connection reset"))

    report = supervisor.publish_open_pauses()

    assert len(report.unknown) == 1
    assert report.published == ()
    assert report.already_published == ()
    assert report.certain_count == 0


def test_a_run_that_cannot_be_projected_is_recorded_not_raised(tmp_path):
    """A pause with no remote issuance is still served by the local console."""
    runs = tmp_path / "runs"
    run, _item = _halted_run(runs, tmp_path / "bundles", "one")
    # Break the capability so projection refuses, exactly as a closed pause
    # would.
    AttendedActionStore(run).capability_path.write_text("{}")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True})
    )

    report = supervisor.publish_open_pauses()

    assert report.published == ()
    assert report.already_published == ()
    assert transport.calls == []


def test_a_run_that_is_not_durably_paused_is_never_published(tmp_path):
    """A run that already resumed has no pause for a decision to bind to."""
    runs = tmp_path / "runs"
    run, _item = _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, _ = _supervisor(runs)

    assert [pause.run_dir for pause in supervisor.open_pauses()] == [run]

    # Resuming clears the durable pause; the signed capability file survives it,
    # which is exactly why `durably_paused` and not the capability is the gate.
    (run / "pending_escalation.json").unlink()
    assert AttendedActionStore(run).capability_path.exists()
    assert supervisor.open_pauses() == []


class _ScriptedTransport(FakeTransport):
    """A transport whose /tasks response changes per call."""

    def __init__(self, task_responses: list, **responses) -> None:
        super().__init__(responses)
        self.task_responses = list(task_responses)

    def post(self, path, payload, *, timeout_s):
        self.calls.append((path, payload))
        if path.endswith("/tasks"):
            response = self.task_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return super().post(path, payload, timeout_s=timeout_s)


class _SequencedTransport(FakeTransport):
    """Script repeated polls and acknowledgements across supervisor cycles."""

    def __init__(self, *, polls: list[Any], acknowledgements: list[Any]) -> None:
        super().__init__()
        self.polls = list(polls)
        self.acknowledgements = list(acknowledgements)

    def post(self, path, payload, *, timeout_s):
        self.calls.append((path, payload))
        if path.endswith("/tasks"):
            return 200, {"accepted": True, "created": True, "task_id": "task_x"}
        if path.endswith("/poll"):
            response = self.polls.pop(0)
        elif path.endswith("/ack"):
            response = self.acknowledgements.pop(0)
        else:  # pragma: no cover - the relay has only these paths
            response = (204, {})
        if isinstance(response, Exception):
            raise response
        return response


def _journaled_lost_ack(tmp_path: Path, name: str):
    """Execute Continue once and retain its exact ACK record without confirming."""
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", name)
    deployment = _deployment()
    executor = _ResultExecutor()
    relay_body = _relayed_for(run, item, deployment)
    transport = _SequencedTransport(
        polls=[(200, {"decision": relay_body})],
        acknowledgements=[RelayUncertain("the first ack response was lost")],
    )
    relay = DecisionRelay(transport, token=TOKEN, deployment=deployment)
    report = DecisionSupervisor(
        runs, relay=relay, deployment=deployment, executor=executor
    ).serve_once(wait_s=0.0)
    assert report.outcome is not None
    return runs, run, deployment, executor, relay_body, report.outcome


def _assert_retained_recovery_refuses(
    runs: Path,
    deployment: Any,
    executor: Any,
    relay_body: dict[str, Any],
) -> None:
    transport = _SequencedTransport(
        polls=[(200, {"decision": relay_body})], acknowledgements=[]
    )
    relay = DecisionRelay(transport, token=TOKEN, deployment=deployment)
    supervisor = DecisionSupervisor(
        runs, relay=relay, deployment=deployment, executor=executor
    )
    with pytest.raises(RelayRefused):
        supervisor.serve_once(wait_s=0.0)
    assert executor.calls == 1
    assert not any(path.endswith("/ack") for path, _ in transport.calls)


def _read_decision_log(run: Path) -> dict[str, Any]:
    return json.loads(AttendedActionStore(run).decisions_path.read_text())


def _write_decision_log(run: Path, payload: dict[str, Any]) -> None:
    AttendedActionStore(run).decisions_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True)
    )


def _plain_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _resign_relay_ack_record(
    store: AttendedActionStore,
    payload: dict[str, Any],
    **updates: Any,
) -> dict[str, Any]:
    """Build a MAC-valid hostile record to test the independent bindings."""
    record = AttendedRelayAcknowledgement.model_validate(payload).model_copy(
        update={
            **updates,
            "record_mac": "hmac-sha256:" + ("0" * 64),
        }
    )
    return record.model_copy(
        update={"record_mac": store._relay_ack_mac(record)}
    ).model_dump(mode="json")


def test_one_refused_pause_does_not_silence_the_others(tmp_path):
    """The failure that would leave a whole practice's halts unreachable."""
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    _halted_run(runs, bundles, "two")
    deployment = _deployment()
    transport = _ScriptedTransport(
        [
            (400, {"error": "the projection is invalid"}),
            (200, {"accepted": True, "created": True, "task_id": "task_x"}),
        ]
    )
    relay = DecisionRelay(transport, token=TOKEN, deployment=deployment)
    supervisor = DecisionSupervisor(runs, relay=relay, deployment=deployment)

    report = supervisor.publish_open_pauses()

    assert len(report.refused) == 1
    assert len(report.published) == 1
    # Both were attempted; the refusal did not stop the loop.
    assert len([p for p, _ in transport.calls if p.endswith("/tasks")]) == 2


def test_an_accepted_pause_is_not_republished_every_cycle(tmp_path):
    """An open pause can last hours; re-POSTing identical bytes is noise."""
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"})
    )

    first = supervisor.publish_open_pauses()
    second = supervisor.publish_open_pauses()

    assert len(first.published) == 1
    # Reported as previously confirmed, NOT as an observation: the supervisor
    # did not ask the control plane about it this pass.
    assert second.already_published == ()
    assert len(second.previously_confirmed) == 1
    assert second.certain_count == 1
    assert len([p for p, _ in transport.calls if p.endswith("/tasks")]) == 1


def test_an_uncertain_pause_is_republished_because_the_post_is_idempotent(tmp_path):
    """Uncertainty is never memoized as success."""
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    transport = _ScriptedTransport(
        [
            RelayUncertain("connection reset"),
            (200, {"accepted": True, "created": True, "task_id": "task_x"}),
        ]
    )
    relay = DecisionRelay(transport, token=TOKEN, deployment=deployment)
    supervisor = DecisionSupervisor(runs, relay=relay, deployment=deployment)

    assert len(supervisor.publish_open_pauses().unknown) == 1
    assert len(supervisor.publish_open_pauses().published) == 1


# ---------------------------------------------------- resolving an answer


def test_a_decision_is_executed_against_the_pause_it_was_minted_from(tmp_path):
    """Two runs are halted; the answer belongs to exactly one of them."""
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    run_two, item_two = _halted_run(runs, bundles, "two")
    deployment = _deployment()

    relay_body = _relayed_for(run_two, item_two, deployment)
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    resolved = supervisor.resolve(
        RelayedDecision(decision_id=str(relay_body["decision_id"]), relay=relay_body)
    )
    assert resolved is not None
    assert resolved.run_dir == run_two


def test_one_cycle_publishes_executes_and_acknowledges_without_a_caller(tmp_path):
    """The whole lane, end to end, driven by nothing but the loop itself.

    This is the test that distinguishes a wired lane from a library that looks
    wired: no caller resolves the run, no caller supplies the item, and the
    engine -- not the relay -- performs the continuation.
    """
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    run_two, item_two = _halted_run(runs, bundles, "two")
    deployment = _deployment()
    executor = _ResultExecutor()
    relay_body = _relayed_for(run_two, item_two, deployment)
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=executor,
        tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    report = supervisor.serve_once(wait_s=0.0)

    # Both pauses were made answerable.
    assert len(report.publishes.published) == 2
    # Exactly one was answered, and the engine ran it.
    assert report.acknowledged == "accepted"
    assert report.outcome is not None
    assert report.outcome.status == "completed"
    assert executor.calls == 1
    ack_path, ack_body = transport.calls[-1]
    assert ack_path.endswith("/ack")
    assert ack_body["result"] == "accepted"


@pytest.mark.parametrize(
    ("action", "decision_action", "expected_status", "executor_calls"),
    [
        ("continue", "verify_and_resume", "completed", 1),
        ("reject", "reject", "rejected", 0),
    ],
)
def test_a_lost_ack_survives_restart_without_a_second_action(
    tmp_path,
    action,
    decision_action,
    expected_status,
    executor_calls,
):
    """A completed action does not become stale when its first ack is lost.

    Continue proves the live-resume path. Reject proves a terminal action that
    removes the pause from the open queue. Both re-deliver the exact signed
    relay and its original idempotency key.
    """
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", action)
    deployment = _deployment()
    executor = _ResultExecutor()
    relay_body = _relayed_for(
        run,
        item,
        deployment,
        action=action,
        decision_action=decision_action,
    )
    first_transport = _SequencedTransport(
        polls=[(200, {"decision": relay_body})],
        acknowledgements=[RelayUncertain("the engine result reached an uncertain ack")],
    )
    first_relay = DecisionRelay(first_transport, token=TOKEN, deployment=deployment)
    first_supervisor = DecisionSupervisor(
        runs, relay=first_relay, deployment=deployment, executor=executor
    )

    first = first_supervisor.serve_once(wait_s=0.0)

    # A new relay and supervisor prove that no process-local cache is needed.
    second_transport = _SequencedTransport(
        polls=[(200, {"decision": relay_body})],
        acknowledgements=[(200, {"accepted": True})],
    )
    second_relay = DecisionRelay(second_transport, token=TOKEN, deployment=deployment)
    second_supervisor = DecisionSupervisor(
        runs, relay=second_relay, deployment=deployment, executor=executor
    )
    second = second_supervisor.serve_once(wait_s=0.0)

    assert first.acknowledged == "accepted"
    assert first.reacknowledged is False
    assert first.outcome is not None
    assert first.outcome.status == expected_status
    assert second.acknowledged == "accepted"
    assert second.reacknowledged is True
    assert second.outcome == first.outcome
    assert executor.calls == executor_calls
    acknowledgements = [
        body
        for transport in (first_transport, second_transport)
        for path, body in transport.calls
        if path.endswith("/ack")
    ]
    assert [body["result"] for body in acknowledgements] == [
        "accepted",
        "accepted",
    ]
    retained = AttendedActionStore(run).relay_acknowledgement(
        RelayedDecision(
            decision_id=str(relay_body["decision_id"]), relay=relay_body
        ).durable_binding()
    )
    assert retained is not None
    assert retained[0].confirmed is True


def test_lost_ack_recovery_refuses_a_changed_signed_or_idempotency_binding(tmp_path):
    """A repeated decision id cannot select an earlier local outcome."""
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    executor = _ResultExecutor()
    relay_body = _relayed_for(run, item, deployment)
    changed = {
        key: value
        for key, value in relay_body.items()
        if key not in {"relay_digest", "relay_signature"}
    }
    changed["idempotency_key"] = "relay-idempotency-key-CHANGED-0002"
    changed_relay = _sign(changed)
    first_transport = _SequencedTransport(
        polls=[(200, {"decision": relay_body})],
        acknowledgements=[
            RelayUncertain("the first ack response was lost"),
        ],
    )
    first_relay = DecisionRelay(first_transport, token=TOKEN, deployment=deployment)
    first_supervisor = DecisionSupervisor(
        runs, relay=first_relay, deployment=deployment, executor=executor
    )

    first_supervisor.serve_once(wait_s=0.0)

    second_transport = _SequencedTransport(
        polls=[(200, {"decision": changed_relay})],
        acknowledgements=[],
    )
    second_relay = DecisionRelay(second_transport, token=TOKEN, deployment=deployment)
    second_supervisor = DecisionSupervisor(
        runs, relay=second_relay, deployment=deployment, executor=executor
    )

    with pytest.raises(RelayRefused, match="exact signed or idempotency binding"):
        second_supervisor.serve_once(wait_s=0.0)

    assert executor.calls == 1
    assert (
        len(
            [
                path
                for transport in (first_transport, second_transport)
                for path, _ in transport.calls
                if path.endswith("/ack")
            ]
        )
        == 1
    )


def test_recovery_refuses_an_outcome_and_plain_digest_changed_together(tmp_path):
    """A plain SHA beside mutable JSON cannot replace the per-run HMAC."""
    runs, run, deployment, executor, relay_body, _outcome = _journaled_lost_ack(
        tmp_path, "plain-digest-tamper"
    )
    log = _read_decision_log(run)
    record = log["relay_acknowledgements"][0]
    outcome = next(
        item
        for item in log["decisions"]
        if item["decision_id"] == record["retained_decision_id"]
    )
    outcome["status"] = "refused"
    record["retained_status"] = "refused"
    record["engine_ack_result"] = "refused"
    record["retained_decision_digest"] = _plain_digest(outcome)
    _write_decision_log(run, log)

    _assert_retained_recovery_refuses(runs, deployment, executor, relay_body)


def test_recovery_refuses_a_fabricated_accepted_outcome(tmp_path):
    runs, run, deployment, executor, relay_body, _outcome = _journaled_lost_ack(
        tmp_path, "fabricated-outcome"
    )
    log = _read_decision_log(run)
    record = log["relay_acknowledgements"][0]
    original = next(
        item
        for item in log["decisions"]
        if item["decision_id"] == record["retained_decision_id"]
    )
    fabricated = {
        **original,
        "decision_id": "f" * 32,
        "status": "completed",
        "message": "fabricated local success",
    }
    log["decisions"].append(fabricated)
    record["retained_decision_id"] = fabricated["decision_id"]
    record["retained_decision_digest"] = _plain_digest(fabricated)
    record["retained_status"] = "completed"
    record["engine_ack_result"] = "accepted"
    _write_decision_log(run, log)

    _assert_retained_recovery_refuses(runs, deployment, executor, relay_body)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("run_id", "changed-run-id"),
        ("bundle_version", "sha256:" + ("0" * 64)),
        ("pause_id", "0" * 32),
        ("workflow_digest", "hmac-sha256:" + ("0" * 64)),
        ("capability_digest", "sha256:" + ("0" * 64)),
        ("event_sequence", None),
    ],
)
def test_recovery_refuses_a_mac_valid_changed_run_or_pause_binding(
    tmp_path,
    field,
    changed,
):
    """The record MAC is necessary but not sufficient execution evidence."""
    runs, run, deployment, executor, relay_body, _outcome = _journaled_lost_ack(
        tmp_path, f"changed-{field}"
    )
    log = _read_decision_log(run)
    record = log["relay_acknowledgements"][0]
    if field == "event_sequence":
        changed = int(record[field]) + 1
    log["relay_acknowledgements"][0] = _resign_relay_ack_record(
        AttendedActionStore(run), record, **{field: changed}
    )
    _write_decision_log(run, log)

    _assert_retained_recovery_refuses(runs, deployment, executor, relay_body)


def test_recovery_refuses_a_replaced_per_run_key(tmp_path):
    runs, run, deployment, executor, relay_body, _outcome = _journaled_lost_ack(
        tmp_path, "replaced-key"
    )
    store = AttendedActionStore(run)
    store.key_path.write_bytes(b"x" * 32)
    if os.name != "nt":
        os.chmod(store.key_path, 0o600)

    _assert_retained_recovery_refuses(runs, deployment, executor, relay_body)


@pytest.mark.parametrize("damage", ["truncate", "delete", "duplicate"])
def test_recovery_refuses_a_damaged_or_missing_ack_record(tmp_path, damage):
    runs, run, deployment, executor, relay_body, _outcome = _journaled_lost_ack(
        tmp_path, f"record-{damage}"
    )
    store = AttendedActionStore(run)
    if damage == "truncate":
        store.decisions_path.write_text("{")
    else:
        log = _read_decision_log(run)
        if damage == "delete":
            log["relay_acknowledgements"] = []
        else:
            log["relay_acknowledgements"].append(dict(log["relay_acknowledgements"][0]))
        _write_decision_log(run, log)

    _assert_retained_recovery_refuses(runs, deployment, executor, relay_body)


def test_recovery_refuses_when_the_signed_capability_is_missing(tmp_path):
    runs, run, deployment, executor, relay_body, _outcome = _journaled_lost_ack(
        tmp_path, "missing-capability"
    )
    store = AttendedActionStore(run)
    store.capability_path.unlink()
    assert not store.capability_history_path.exists()

    _assert_retained_recovery_refuses(runs, deployment, executor, relay_body)


def test_recovery_refuses_a_journal_moved_to_another_run(tmp_path):
    runs, run, deployment, executor, relay_body, _outcome = _journaled_lost_ack(
        tmp_path, "moved-source"
    )
    other, _item = _halted_run(runs, tmp_path / "bundles", "moved-destination")
    source = AttendedActionStore(run).decisions_path
    destination = AttendedActionStore(other).decisions_path
    source.replace(destination)

    _assert_retained_recovery_refuses(runs, deployment, executor, relay_body)


def test_relay_ack_record_contains_only_closed_or_opaque_context(tmp_path):
    _runs, run, _deployment_cfg, _executor, _relay, _outcome = _journaled_lost_ack(
        tmp_path, "secret-free-record"
    )
    record = _read_decision_log(run)["relay_acknowledgements"][0]
    encoded = json.dumps(record, sort_keys=True)

    assert PROTECTED_VALUE not in encoded
    assert "supervisor-secret-free-record" not in encoded
    assert str(run) not in encoded
    assert set(record).isdisjoint(
        {
            "actor_id",
            "operator",
            "workflow_name",
            "task",
            "task_content",
            "screenshot",
            "ocr_text",
            "path",
            "message",
        }
    )


def test_confirm_and_append_are_serialized_without_a_lost_write(
    tmp_path,
    monkeypatch,
):
    _runs, run, _deployment_cfg, _executor, relay_body, outcome = _journaled_lost_ack(
        tmp_path, "serialized-journal"
    )
    store = AttendedActionStore(run)
    binding = RelayedDecision(
        decision_id=str(relay_body["decision_id"]), relay=relay_body
    ).durable_binding()
    extra = outcome.model_copy(update={"decision_id": "e" * 32})
    append_entered = threading.Event()
    release_append = threading.Event()
    failures: list[BaseException] = []
    original_atomic_write = AttendedActionStore._atomic_write

    def slow_append(path, payload, *, mode=0o600):
        if threading.current_thread().name == "append-worker":
            append_entered.set()
            assert release_append.wait(timeout=2.0)
        return original_atomic_write(path, payload, mode=mode)

    monkeypatch.setattr(AttendedActionStore, "_atomic_write", staticmethod(slow_append))

    def append_worker():
        try:
            store.append(extra)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def confirm_worker():
        try:
            store.confirm_relay_acknowledgement(binding)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    append_thread = threading.Thread(target=append_worker, name="append-worker")
    confirm_thread = threading.Thread(target=confirm_worker, name="confirm-worker")
    append_thread.start()
    assert append_entered.wait(timeout=2.0)
    confirm_thread.start()
    time.sleep(0.05)
    assert confirm_thread.is_alive()
    release_append.set()
    append_thread.join(timeout=2.0)
    confirm_thread.join(timeout=2.0)

    assert failures == []
    assert not append_thread.is_alive()
    assert not confirm_thread.is_alive()
    log = _read_decision_log(run)
    assert any(item["decision_id"] == extra.decision_id for item in log["decisions"])
    assert log["relay_acknowledgements"][0]["confirmed"] is True


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows prevents replacement while the owner handle stays open",
)
def test_a_lock_owner_never_deletes_a_replacement_lock(tmp_path):
    store = AttendedActionStore(tmp_path / "run")
    replacement_nonce = b"replacement-lock-owner"

    with pytest.raises(AttendedActionBusy, match="lock changed while owned"):
        with store._decision_log_lock():
            store.decisions_lock_path.unlink()
            replacement_fd = os.open(
                store.decisions_lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(replacement_fd, replacement_nonce)
                os.fsync(replacement_fd)
            finally:
                os.close(replacement_fd)

    assert store.decisions_lock_path.read_bytes() == replacement_nonce
    with pytest.raises(AttendedActionBusy):
        with store._decision_log_lock(timeout_s=0.0):
            raise AssertionError("a third writer acquired the replacement lock")


def test_a_reject_from_a_phone_ends_the_right_run(tmp_path):
    """The action that terminates a run must reach the pause it names.

    `reject` is the one answer whose whole value is the disagreement signal it
    records, so delivering it to the wrong open pause -- or not at all -- is the
    failure that matters most here.
    """
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    run_two, item_two = _halted_run(runs, bundles, "two")
    deployment = _deployment()
    executor = _ResultExecutor()
    relay_body = _relayed_for(
        run_two, item_two, deployment, action="reject", decision_action="reject"
    )
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=executor,
        tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    report = supervisor.serve_once(wait_s=0.0)

    assert report.acknowledged == "accepted"
    assert report.outcome is not None
    assert report.outcome.action == "reject"
    assert report.outcome.pause_id == relay_body["task_id"].removeprefix("task_")


def test_a_governed_refusal_is_acknowledged_and_re_raised(tmp_path):
    """A refused answer must reach the operator, not vanish into the loop."""
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    # A binding the engine will not admit: the relay resolves to the right
    # pause, and revalidation still refuses it.
    relay_body = _relayed_for(
        run, item, deployment, binding_digest="sha256:" + "9" * 64
    )
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    with pytest.raises(AttendedActionRefused):
        supervisor.serve_once(wait_s=0.0)

    assert transport.calls[-1][1]["result"] == "refused"


def test_a_decision_matching_no_open_pause_is_acknowledged_stale_not_executed(
    tmp_path,
):
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    relay_body = _relayed_for(
        run, item, deployment, capability_digest="sha256:" + "f" * 64
    )
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    report = supervisor.serve_once(wait_s=0.0)

    assert report.acknowledged == "stale"
    assert report.outcome is None
    ack = [payload for path, payload in transport.calls if path.endswith("/ack")]
    assert ack and ack[0]["result"] == "stale"


def test_an_expired_decision_is_acknowledged_expired_not_executed(tmp_path):
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    relay_body = _relayed_for(run, item, deployment, expires_at=LONG_PAST)
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    report = supervisor.serve_once(wait_s=0.0)

    assert report.acknowledged == "expired"
    assert report.outcome is None


def test_an_unreadable_deadline_is_treated_as_expired_not_as_open_ended(tmp_path):
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    relay_body = _relayed_for(run, item, deployment, expires_at="whenever")
    supervisor, _ = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    assert supervisor.serve_once(wait_s=0.0).acknowledged == "expired"


def test_no_waiting_decision_is_a_quiet_cycle_not_an_error(tmp_path):
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, _ = _supervisor(
        runs,
        tasks=(200, {"accepted": True, "created": True}),
        poll=(204, {}),
    )

    report = supervisor.serve_once(wait_s=0.0)

    assert report.decision_id is None
    assert report.acknowledged is None
    assert len(report.publishes.published) == 1


def test_an_unverifiable_decision_is_refused_before_any_resolution(tmp_path):
    """A relay this runner's credential cannot have produced is never run."""
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    forged = _relayed_for(run, item, deployment)
    forged = {**forged, "relay_signature": "hmac-sha256:" + "0" * 64}
    supervisor, _ = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": forged}),
    )

    with pytest.raises(RelayRefused):
        supervisor.serve_once(wait_s=0.0)


# ------------------------------------------------------------- the thread


class _StubSupervisor:
    """A supervisor whose cycles are scripted, so the loop is testable."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def serve_once(self, *, wait_s: float = 0.0, publish: bool = True) -> Any:
        self.calls += 1
        if not self.script:
            raise RelayUncertain("no more script")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _cycle(acknowledged: Optional[str] = None, unknown: tuple[str, ...] = ()):
    from openadapt_flow.console.decision_supervisor import CycleReport, PublishReport

    return CycleReport(
        publishes=PublishReport(unknown=unknown), acknowledged=acknowledged
    )


def test_the_loop_counts_outcomes_and_stops_when_asked():
    stub = _StubSupervisor([_cycle("accepted"), _cycle("stale"), _cycle("expired")])
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=lambda _s: None)

    stop_after = {"n": 0}

    def on_cycle(_report: Any) -> None:
        stop_after["n"] += 1
        if stop_after["n"] >= 3:
            thread.stop(timeout_s=0.0)

    thread._on_cycle = on_cycle
    thread.run()

    assert thread.stats.cycles == 3
    assert thread.stats.decisions_executed == 1
    assert thread.stats.decisions_stale == 1
    assert thread.stats.decisions_expired == 1


def test_a_relay_refusal_backs_the_loop_off_instead_of_spinning():
    slept: list[float] = []
    stub = _StubSupervisor(
        [RelayRefused("control plane refused"), RelayRefused("again"), _cycle()]
    )
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=slept.append)

    def on_cycle(_report: Any) -> None:
        thread.stop(timeout_s=0.0)

    thread._on_cycle = on_cycle
    thread.run()

    assert slept == [2.0, 4.0]
    # A successful cycle clears the backoff, so an outage does not permanently
    # slow the lane.
    assert thread.stats.consecutive_failures == 0


def test_a_governed_refusal_is_an_answer_not_an_outage():
    """A refused decision must not back the transport off; nothing is wrong.

    It does take one short pause. An acknowledgement can itself be uncertain,
    which leaves the decision leased and re-delivered at once, and a poll with a
    decision waiting returns immediately -- so the loop would otherwise spin at
    full speed on a decision it refuses every time.
    """
    slept: list[float] = []
    stub = _StubSupervisor(
        [
            AttendedActionRefused("revalidation failed"),
            AttendedActionRefused("again"),
            _cycle(),
        ]
    )
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=slept.append)

    def on_cycle(_report: Any) -> None:
        thread.stop(timeout_s=0.0)

    thread._on_cycle = on_cycle
    thread.run()

    # A flat floor, not a doubling backoff: the transport is healthy.
    assert slept == [1.0, 1.0]
    assert thread.stats.decisions_refused == 2
    assert thread.stats.consecutive_failures == 0


def test_the_loop_never_dies_on_an_unexpected_error():
    slept: list[float] = []
    stub = _StubSupervisor([ValueError("something unforeseen"), _cycle()])
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=slept.append)

    def on_cycle(_report: Any) -> None:
        thread.stop(timeout_s=0.0)

    thread._on_cycle = on_cycle
    thread.run()

    assert thread.stats.cycles == 1
    assert slept == [2.0]


def test_starting_twice_is_refused():
    stub = _StubSupervisor([])
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=lambda _s: None)
    thread._stop.set()  # so the thread exits immediately
    thread.start()
    with pytest.raises(RuntimeError):
        thread.start()
    thread.stop()


def test_the_thread_is_a_daemon_so_it_cannot_hold_the_console_open():
    stub = _StubSupervisor([])
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=lambda _s: None)
    thread._stop.set()
    thread.start()
    assert isinstance(thread._thread, threading.Thread)
    assert thread._thread.daemon is True
    thread.stop()
    assert thread.running is False


# ------------------------------------------------------------------- the CLI
#
# `--remote-decisions` must fail loudly, never quietly. An operator who asked
# for it and silently got a loopback-only console would believe a phone can
# answer a halt when nothing is listening for one.


def _console_args(*extra: str, config: Optional[Path] = None):
    from openadapt_flow.__main__ import build_parser

    argv = ["console", *extra]
    if config is not None:
        argv += ["--config", str(config)]
    return build_parser().parse_args(argv)


def _remote_config(tmp_path: Path, *, enabled: bool = True) -> Path:
    import yaml

    path = tmp_path / "deployment.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "human_decisions": {
                    "remote": {
                        "enabled": enabled,
                        "tenant_id": "tenant_exact_01",
                        "runner_id": "runner_exact_01",
                    }
                }
            }
        )
    )
    return path


def test_remote_decisions_is_off_unless_asked_for():
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    args = _console_args("--attend")
    assert _decision_supervisor_from_args(args, None) is None


def test_remote_decisions_refuses_a_read_only_console(tmp_path):
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    args = _console_args(
        "--attend", "--remote-decisions", config=_remote_config(tmp_path)
    )
    with pytest.raises(SystemExit, match="--attend --allow-actions"):
        _decision_supervisor_from_args(args, None)


def test_remote_decisions_refuses_a_deployment_that_did_not_enable_it(tmp_path):
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    args = _console_args(
        "--attend",
        "--allow-actions",
        "--remote-decisions",
        config=_remote_config(tmp_path, enabled=False),
    )
    with pytest.raises(SystemExit, match="human_decisions.remote.enabled"):
        _decision_supervisor_from_args(args, object())


def test_remote_decisions_refuses_without_a_runner_credential(tmp_path, monkeypatch):
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    monkeypatch.delenv("OPENADAPT_RUNNER_TOKEN", raising=False)
    args = _console_args(
        "--attend",
        "--allow-actions",
        "--remote-decisions",
        config=_remote_config(tmp_path),
    )
    with pytest.raises(SystemExit, match="OPENADAPT_RUNNER_TOKEN"):
        _decision_supervisor_from_args(args, object())


def test_remote_decisions_refuses_a_plaintext_control_plane(tmp_path, monkeypatch):
    """A runner credential is never sent over plaintext, even to localhost."""
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    monkeypatch.setenv("OPENADAPT_RUNNER_TOKEN", TOKEN)
    args = _console_args(
        "--attend",
        "--allow-actions",
        "--remote-decisions",
        "--remote-decision-host",
        "http://localhost:3000",
        config=_remote_config(tmp_path),
    )
    with pytest.raises(SystemExit, match="https"):
        _decision_supervisor_from_args(args, object())


def test_remote_decisions_builds_a_supervisor_when_fully_configured(
    tmp_path, monkeypatch
):
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    monkeypatch.setenv("OPENADAPT_RUNNER_TOKEN", TOKEN)
    args = _console_args(
        "--attend",
        "--allow-actions",
        "--remote-decisions",
        "--remote-decision-host",
        "https://app.openadapt.test",
        config=_remote_config(tmp_path),
    )
    supervisor = _decision_supervisor_from_args(args, object())
    assert isinstance(supervisor, DecisionSupervisorThread)
    assert supervisor.running is False


def test_the_console_starts_and_stops_the_lane_with_the_server(monkeypatch):
    """The lane's lifetime is the server's; a stopped console leaves none."""
    import openadapt_flow.console.server as server_mod

    started: list[str] = []
    monkeypatch.setattr(
        server_mod, "create_app", lambda *a, **k: object(), raising=False
    )

    class _FakeUvicorn:
        @staticmethod
        def run(*_args: Any, **_kwargs: Any) -> None:
            started.append("served")

    class _Lane:
        def __init__(self) -> None:
            self.events: list[str] = []

        def start(self) -> None:
            self.events.append("start")

        def stop(self) -> None:
            self.events.append("stop")

    lane = _Lane()
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setattr(
        "openadapt_flow.console.app.create_app", lambda *a, **k: object()
    )
    server_mod.serve("bundles", "runs", None, decision_supervisor=lane, port=0)

    assert started == ["served"]
    assert lane.events == ["start", "stop"]
