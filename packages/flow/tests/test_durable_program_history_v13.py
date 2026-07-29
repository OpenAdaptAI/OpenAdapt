"""Regression tests for the durable program transition-history chain.

These tests keep the ordered interpreter history append-only across ordinary
checkpoints, a durable pause, direct continuation, and an attended checkpoint.
They also prove that a restarted continuation uses the latest durable boundary
without repeating an action and that changed history refuses before input.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow.ir import ActionKind, State, StateKind, Step, Transition
from openadapt_flow.runtime.durable import CheckpointStore, StateDiverged, resume
from openadapt_flow.runtime.durable.attended import (
    BoundAttendedExecutor,
    execute_attended_action,
)
from openadapt_flow.runtime.durable.program_checkpoint import history_hash
from openadapt_flow.runtime.replayer import Replayer
from tests.test_attended_actions import (
    _attended_program,
    _request,
    _run_attended_program_to_pause,
)
from tests.test_durable_program import _approval, _run_branch_loop_to_pause
from tests.test_replayer import FakeBackend, FakeVision, Match


def _assert_boundary(
    *,
    parent: list[str],
    history: list[str],
    delta: list[str],
    parent_digest: str,
    digest: str,
) -> None:
    assert history == [*parent, *delta]
    assert parent_digest == history_hash(parent)
    assert digest == history_hash(history)


def _rewrite_checkpoint(run_dir, checkpoint) -> None:
    path = run_dir / "checkpoints" / f"pstate_{checkpoint.seq:04d}.json"
    path.write_text(checkpoint.model_dump_json(indent=2))


def _verified_checkpoint_path(checkpoints) -> list[str]:
    return [
        state_id
        for checkpoint in checkpoints
        for state_id in checkpoint.visited_states_delta
    ]


def test_normal_checkpoint_and_pause_form_one_append_only_history(tmp_path):
    report, run_dir, _bundle, _verifier = _run_branch_loop_to_pause(tmp_path)
    store = CheckpointStore(run_dir)
    checkpoints = store.program_checkpoints()
    pending = store.read_pending()

    assert report.success is False
    assert len(checkpoints) == 1
    assert pending is not None
    checkpoint = checkpoints[0]
    _assert_boundary(
        parent=[],
        history=checkpoint.transition_history,
        delta=checkpoint.transition_delta,
        parent_digest=checkpoint.transition_parent_hash,
        digest=checkpoint.transition_history_hash,
    )
    _assert_boundary(
        parent=checkpoint.transition_history,
        history=pending.program_history,
        delta=pending.program_history_delta,
        parent_digest=pending.program_parent_history_hash,
        digest=pending.program_history_hash,
    )
    assert pending.program_checkpoint_seq == checkpoint.seq
    assert pending.program_history[-1] == pending.state_id == "b_type"


def test_direct_resume_extends_the_pause_history_without_repeating_it(tmp_path):
    _report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    store = CheckpointStore(run_dir)
    pending = store.read_pending()
    assert pending is not None

    verifier.refute.clear()
    backend = FakeBackend()
    resumed = resume(
        run_dir,
        Replayer(
            backend,
            vision=FakeVision(),
            effect_verifier=verifier,
            poll_interval_s=0.0,
        ),
        approval=_approval(bundle),
    )

    assert resumed.success is True
    assert backend.actions == [("type", "Bob"), ("type", "Cara")]
    checkpoints = store.program_checkpoints()
    assert len(checkpoints) == 3
    assert (
        checkpoints[0].transition_history
        == pending.program_history[: -len(pending.program_history_delta)]
    )
    parent = pending.program_history
    for checkpoint in checkpoints[pending.program_checkpoint_seq :]:
        _assert_boundary(
            parent=parent,
            history=checkpoint.transition_history,
            delta=checkpoint.transition_delta,
            parent_digest=checkpoint.transition_parent_hash,
            digest=checkpoint.transition_history_hash,
        )
        parent = checkpoint.transition_history
    verified_path = _verified_checkpoint_path(checkpoints)
    assert resumed.visited_states[: len(verified_path)] == verified_path
    assert resumed.visited_states[-2:] == ["b_end", "done"]
    assert store.read_pending() is None


def test_restart_after_post_pause_checkpoint_uses_latest_history_boundary(
    tmp_path,
):
    _report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    verifier.refute.clear()
    approval = _approval(bundle)

    class CrashBeforeSecondResumedAction(Replayer):
        resumed_actions = 0

        def _exec_action_state(self, *args, **kwargs):
            self.resumed_actions += 1
            if self.resumed_actions == 2:
                raise KeyboardInterrupt("simulated process crash before delivery")
            return super()._exec_action_state(*args, **kwargs)

    first_backend = FakeBackend()
    with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
        resume(
            run_dir,
            CrashBeforeSecondResumedAction(
                first_backend,
                vision=FakeVision(),
                effect_verifier=verifier,
                poll_interval_s=0.0,
            ),
            approval=approval,
        )

    store = CheckpointStore(run_dir)
    pending = store.read_pending()
    checkpoints_after_crash = store.program_checkpoints()
    assert pending is not None and pending.status == "continuing"
    assert len(checkpoints_after_crash) == 2
    assert first_backend.actions == [("type", "Bob")]
    _assert_boundary(
        parent=pending.program_history,
        history=checkpoints_after_crash[-1].transition_history,
        delta=checkpoints_after_crash[-1].transition_delta,
        parent_digest=checkpoints_after_crash[-1].transition_parent_hash,
        digest=checkpoints_after_crash[-1].transition_history_hash,
    )

    second_backend = FakeBackend()
    resumed = resume(
        run_dir,
        Replayer(
            second_backend,
            vision=FakeVision(),
            effect_verifier=verifier,
            poll_interval_s=0.0,
        ),
        approval=approval,
    )
    assert resumed.success is True
    assert second_backend.actions == [("type", "Cara")]
    final_checkpoints = store.program_checkpoints()
    verified_path = _verified_checkpoint_path(final_checkpoints)
    assert resumed.visited_states[: len(verified_path)] == verified_path
    assert resumed.visited_states[-2:] == ["b_end", "done"]


def test_attended_checkpoint_is_an_empty_delta_at_the_signed_pause_boundary(
    tmp_path,
):
    workflow = _attended_program()
    _bundle, run_dir, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    pending = store.read_pending()
    assert pending is not None
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )

    decision = execute_attended_action(
        run_dir,
        _request(capability, key="program-history-attended"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(), vision=vision, poll_interval_s=0.0
            )
        ),
    )

    assert decision.status == "completed"
    checkpoints = store.program_checkpoints()
    attended = checkpoints[0]
    assert attended.attended_transition is not None
    _assert_boundary(
        parent=pending.program_history,
        history=attended.transition_history,
        delta=attended.transition_delta,
        parent_digest=attended.transition_parent_hash,
        digest=attended.transition_history_hash,
    )
    assert attended.transition_delta == []
    assert len(checkpoints) == 2
    _assert_boundary(
        parent=attended.transition_history,
        history=checkpoints[1].transition_history,
        delta=checkpoints[1].transition_delta,
        parent_digest=checkpoints[1].transition_parent_hash,
        digest=checkpoints[1].transition_history_hash,
    )


@pytest.mark.parametrize(
    "mutation",
    ["qualification_delta", "durable_history"],
)
def test_attended_resume_refuses_cross_contract_history_change_before_input(
    tmp_path,
    mutation,
):
    """Qualification deltas and durable history must prove one exact path."""

    workflow = _attended_program()
    assert workflow.program is not None
    workflow.program.states["next"].transitions = [Transition(target="later")]
    workflow.program.states["later"] = State(
        id="later",
        kind=StateKind.ACTION,
        step=Step(id="later-step", intent="later", action=ActionKind.KEY, key="C"),
        transitions=[Transition(target="done")],
    )
    _bundle, run_dir, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )

    class CrashBeforeSecondResumedAction(Replayer):
        resumed_actions = 0

        def _exec_action_state(self, *args, **kwargs):
            self.resumed_actions += 1
            if self.resumed_actions == 2:
                raise KeyboardInterrupt("simulated process crash before delivery")
            return super()._exec_action_state(*args, **kwargs)

    continued_backend = FakeBackend()
    with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
        execute_attended_action(
            run_dir,
            _request(capability, key=f"cross-history-{mutation}"),
            operator="staff",
            executor=BoundAttendedExecutor(
                lambda _manifest: CrashBeforeSecondResumedAction(
                    continued_backend,
                    vision=vision,
                    poll_interval_s=0.0,
                )
            ),
        )
    assert continued_backend.actions == [("press", "B")]

    checkpoints = store.program_checkpoints()
    assert len(checkpoints) == 2
    qualification_history = [
        state_id
        for checkpoint in checkpoints
        for state_id in checkpoint.visited_states_delta
    ]
    assert qualification_history == checkpoints[-1].transition_history
    assert qualification_history == ["human", "next"]

    checkpoint = checkpoints[0]
    if mutation == "qualification_delta":
        changed = checkpoint.model_copy(
            update={
                "visited_states_delta": [
                    "invented-state",
                    *checkpoint.visited_states_delta,
                ]
            }
        )
    else:
        changed = checkpoint.model_copy(
            update={
                "transition_history": [
                    "invented-state",
                    *checkpoint.transition_history,
                ]
            }
        )
    _rewrite_checkpoint(run_dir, changed)

    resumed_backend = FakeBackend()
    with pytest.raises(StateDiverged):
        resume(
            run_dir,
            Replayer(resumed_backend, vision=FakeVision(), poll_interval_s=0.0),
            approval=store.read_approval(),
        )
    assert resumed_backend.actions == []


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("transition_delta", ["invented-state"]),
        ("transition_history", []),
        ("transition_history_hash", "sha256:" + "0" * 64),
        ("transition_parent_hash", "sha256:" + "f" * 64),
    ],
)
def test_changed_checkpoint_history_refuses_before_input(tmp_path, mutation, value):
    _report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    store = CheckpointStore(run_dir)
    checkpoint = store.program_checkpoints()[0]
    _rewrite_checkpoint(run_dir, checkpoint.model_copy(update={mutation: value}))

    backend = FakeBackend()
    calls_before = verifier.capture_calls
    with pytest.raises(StateDiverged):
        resume(
            run_dir,
            Replayer(backend, vision=FakeVision(), effect_verifier=verifier),
            approval=_approval(bundle),
        )
    assert backend.actions == []
    assert verifier.capture_calls == calls_before


def test_reordered_checkpoint_history_refuses_before_input(tmp_path):
    _report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    store = CheckpointStore(run_dir)
    checkpoint = store.program_checkpoints()[0]
    assert len(checkpoint.transition_history) > 1
    reordered = list(reversed(checkpoint.transition_history))
    _rewrite_checkpoint(
        run_dir,
        checkpoint.model_copy(
            update={
                "transition_history": reordered,
                "transition_history_hash": history_hash(reordered),
            }
        ),
    )

    backend = FakeBackend()
    with pytest.raises(StateDiverged):
        resume(
            run_dir,
            Replayer(backend, vision=FakeVision(), effect_verifier=verifier),
            approval=_approval(bundle),
        )
    assert backend.actions == []


def test_deleted_program_checkpoint_refuses_before_input(tmp_path):
    _report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    path = run_dir / "checkpoints" / "pstate_0001.json"
    path.unlink()

    backend = FakeBackend()
    with pytest.raises(StateDiverged):
        resume(
            run_dir,
            Replayer(backend, vision=FakeVision(), effect_verifier=verifier),
            approval=_approval(bundle),
        )
    assert backend.actions == []


def test_changed_pending_history_refuses_before_input(tmp_path):
    _report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    pending_path = run_dir / "pending_escalation.json"
    raw = json.loads(pending_path.read_text())
    raw["program_history"] = list(reversed(raw["program_history"]))
    raw["program_history_hash"] = history_hash(raw["program_history"])
    pending_path.write_text(json.dumps(raw, indent=2))

    backend = FakeBackend()
    with pytest.raises(StateDiverged):
        resume(
            run_dir,
            Replayer(backend, vision=FakeVision(), effect_verifier=verifier),
            approval=_approval(bundle),
        )
    assert backend.actions == []
