"""Durable checkpoint/resume for the Phase-2 ProgramGraph (RFC §5, Tier 3).

The linear durable tests (``test_durable_runtime``) pin the ``steps``-list path;
these pin the STATE-MACHINE path (``docs/design/WORKFLOW_PROGRAM_IR.md`` §2):

* a program with a BRANCH + LOOP that HALTs mid-loop checkpoints the whole
  INTERPRETER STATE (frame stack + loop cursor + bound params), and an approved
  resume RESTORES it -- finishing the in-progress row and running the remaining
  rows to completion, NOT restarting from the graph entry / a step index;
* an already-CONFIRMED consequential write is never re-performed on resume
  (idempotency via the completed-effect ledger);
* resume is an AUTHENTICATED approval workflow (P0-5): a caller with no approval
  record is REFUSED; a resume whose live app state diverged from the checkpoint
  is REFUSED; an EXPIRED (stale) pause is REFUSED.

Drives the REAL Replayer with faked backend/vision and a scripted in-memory
EffectVerifier (as in ``test_durable_runtime``) -- no Playwright, no OCR stack,
no network, ZERO model calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    ActionKind,
    FreshActuationEvent,
    LoopSpec,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    Relation,
    Resolution,
    RunReport,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.durable import (
    ApprovalRecord,
    ApprovalRequired,
    CheckpointStore,
    PauseExpired,
    StateDiverged,
    resume,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    GraphFrame,
    LoopCursor,
    ProgramCheckpoint,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.replayer import Replayer, _ProgramHalt

# Reuse the scripted fakes + the scripted system-of-record verifier.
from tests.test_durable_runtime import FakeSoRVerifier, _approval, _vision_ok
from tests.test_replayer import FakeBackend, FakeVision, make_png

# -- builders ----------------------------------------------------------------


def test_program_checkpoint_rejects_a_leaf_that_is_not_the_verified_state():
    with pytest.raises(ValueError, match="leaf program frame"):
        ProgramCheckpoint(
            workflow_name="w",
            seq=1,
            verified_state_id="verified",
            frames=[GraphFrame(graph_id="__program__", state_id="different")],
        )


def test_program_checkpoint_rejects_an_empty_control_cursor():
    with pytest.raises(ValueError, match="at least 1 item"):
        ProgramCheckpoint(
            workflow_name="w",
            seq=1,
            verified_state_id="verified",
            frames=[],
        )


def test_program_checkpoint_retains_exact_delivery_proof():
    receipt = ActionDeliveryReceipt(
        receipt_id="receipt-1",
        operation="guarded_coordinate_click",
        native=False,
        delivered_at="2026-07-28T00:00:01+00:00",
    )
    drag_end = Resolution(
        rung="template",
        point=(15, 15),
        confidence=0.98,
        elapsed_ms=1.1,
    )
    event = FreshActuationEvent(
        attempt=1,
        operation="click",
        changed_pixel_count=4,
        changed_bbox=(1, 1, 2, 2),
        frame_size=(20, 20),
        retried=True,
    )
    checkpoint = ProgramCheckpoint(
        run_id="run-1",
        workflow_name="w",
        seq=1,
        verified_state_id="action",
        step_id="submit",
        frames=[GraphFrame(graph_id="__program__", state_id="action")],
        delivery_attempted=True,
        delivery_receipt=receipt,
        drag_end_resolution=drag_end,
        fresh_actuation_events=[event],
        before_png="steps/submit_before.png",
    )
    restored_checkpoint = ProgramCheckpoint.model_validate_json(
        checkpoint.model_dump_json()
    )
    workflow = Workflow(
        name="w",
        program=ProgramGraph(
            entry="action",
            states={
                "action": State(
                    id="action",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="submit",
                        intent="Submit",
                        action=ActionKind.CLICK,
                    ),
                )
            },
        ),
    )

    restored = Replayer._resumed_program_results([restored_checkpoint], workflow)[0]

    assert restored.delivery_receipt == receipt
    assert restored.drag_end_resolution == drag_end
    assert restored.fresh_actuation_events == [event]
    assert restored.before_png == "steps/submit_before.png"


def test_program_resume_rejects_a_constructed_inconsistent_leaf(tmp_path):
    action = State(
        id="verified",
        kind=StateKind.ACTION,
        step=Step(id="type", intent="type", action=ActionKind.TYPE, text="A"),
    )
    workflow = Workflow(
        name="w",
        program=ProgramGraph(entry="verified", states={"verified": action}),
    )
    checkpoint = ProgramCheckpoint.model_construct(
        workflow_name="w",
        seq=1,
        verified_state_id="verified",
        frames=[GraphFrame(graph_id="__program__", state_id="different")],
        bound_params={},
    )

    with pytest.raises(_ProgramHalt, match="cursor does not match"):
        Replayer(FakeBackend(), vision=FakeVision())._resume_program_state(
            checkpoint,
            workflow=workflow,
            worklists={},
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run",
            report=RunReport(workflow_name="w", started_at="now"),
            new_crops={},
        )


def test_program_resume_rejects_a_constructed_empty_cursor(tmp_path):
    action = State(
        id="verified",
        kind=StateKind.ACTION,
        step=Step(id="type", intent="type", action=ActionKind.TYPE, text="A"),
    )
    workflow = Workflow(
        name="w",
        program=ProgramGraph(entry="verified", states={"verified": action}),
    )
    checkpoint = ProgramCheckpoint.model_construct(
        workflow_name="w",
        seq=1,
        verified_state_id="verified",
        frames=[],
        bound_params={},
    )
    backend = FakeBackend()

    with pytest.raises(_ProgramHalt, match="cursor does not match"):
        Replayer(backend, vision=FakeVision())._resume_program_state(
            checkpoint,
            workflow=workflow,
            worklists={},
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run",
            report=RunReport(workflow_name="w", started_at="now"),
            new_crops={},
        )

    assert backend.actions == []


@pytest.mark.parametrize(
    "kind",
    [
        StateKind.BRANCH,
        StateKind.LOOP,
        StateKind.SUBFLOW_CALL,
        StateKind.TERMINAL,
    ],
)
def test_program_resume_rejects_non_action_verified_leaf_before_input(tmp_path, kind):
    later = State(
        id="later",
        kind=StateKind.ACTION,
        step=Step(id="later-key", intent="later", action=ActionKind.KEY, key="A"),
    )
    control = State(
        id="control",
        kind=kind,
        transitions=[Transition(target="later")],
        loop=(
            LoopSpec(relation="queue", body="body") if kind is StateKind.LOOP else None
        ),
        subflow=("body" if kind is StateKind.SUBFLOW_CALL else None),
        outcome=("success" if kind is StateKind.TERMINAL else None),
    )
    workflow = Workflow(
        name="w",
        program=ProgramGraph(
            entry="control",
            states={"control": control, "later": later},
        ),
        subflows={
            "body": ProgramGraph(
                entry="body-action",
                states={
                    "body-action": State(
                        id="body-action",
                        kind=StateKind.ACTION,
                        step=Step(
                            id="body-key",
                            intent="body",
                            action=ActionKind.KEY,
                            key="B",
                        ),
                    )
                },
            )
        },
        data_sources={"queue": Relation(name="queue", rows=[{"record": "1"}])},
    )
    checkpoint = ProgramCheckpoint(
        workflow_name="w",
        seq=1,
        verified_state_id="control",
        step_id="control",
        frames=[GraphFrame(graph_id="__program__", state_id="control")],
        bound_params={},
    )
    backend = FakeBackend()

    with pytest.raises(_ProgramHalt, match="verified leaf"):
        Replayer(backend, vision=FakeVision())._resume_program_state(
            checkpoint,
            workflow=workflow,
            worklists={},
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run",
            report=RunReport(workflow_name="w", started_at="now"),
            new_crops={},
        )

    assert backend.actions == []


def test_public_program_resume_requires_authenticated_durable_admission(tmp_path):
    step = Step(id="key", intent="press", action=ActionKind.KEY, key="A")
    workflow = Workflow(
        name="w",
        program=ProgramGraph(
            entry="action",
            states={"action": State(id="action", kind=StateKind.ACTION, step=step)},
        ),
    )
    checkpoint = ProgramCheckpoint(
        workflow_name="w",
        seq=1,
        verified_state_id="action",
        step_id="key",
        frames=[GraphFrame(graph_id="__program__", state_id="action")],
        bound_params={},
    )
    backend = FakeBackend()

    with pytest.raises(ApprovalRequired, match="authenticated resume API"):
        Replayer(backend, vision=FakeVision()).run(
            workflow,
            params={},
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run",
            resume_program=checkpoint,
        )

    assert backend.actions == []


def test_public_linear_resume_requires_authenticated_durable_admission(tmp_path):
    workflow = Workflow(
        name="w",
        steps=[Step(id="key", intent="press", action=ActionKind.KEY, key="A")],
    )
    backend = FakeBackend()

    with pytest.raises(ApprovalRequired, match="authenticated resume API"):
        Replayer(backend, vision=FakeVision()).run(
            workflow,
            params={},
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run",
            resume_from=1,
        )

    assert backend.actions == []


def test_program_resume_rejects_malformed_loop_ancestry_before_input(tmp_path):
    body_action = State(
        id="body-action",
        kind=StateKind.ACTION,
        step=Step(id="body-key", intent="body", action=ActionKind.KEY, key="B"),
    )
    workflow = Workflow(
        name="w",
        program=ProgramGraph(
            entry="call",
            states={
                "call": State(
                    id="call",
                    kind=StateKind.SUBFLOW_CALL,
                    subflow="body",
                )
            },
        ),
        subflows={
            "body": ProgramGraph(
                entry="body-action", states={"body-action": body_action}
            )
        },
    )
    checkpoint = ProgramCheckpoint(
        workflow_name="w",
        seq=1,
        verified_state_id="body-action",
        step_id="body-key",
        frames=[
            GraphFrame(graph_id="__program__", state_id="call"),
            GraphFrame(
                graph_id="body",
                state_id="body-action",
                loop=LoopCursor(
                    loop_state_id="call",
                    relation="missing",
                    row_index=0,
                    rows=[{}],
                ),
            ),
        ],
        bound_params={},
    )
    backend = FakeBackend()

    with pytest.raises(_ProgramHalt, match="loop cursor"):
        Replayer(backend, vision=FakeVision())._resume_program_state(
            checkpoint,
            workflow=workflow,
            worklists={},
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run",
            report=RunReport(workflow_name="w", started_at="now"),
            new_crops={},
        )

    assert backend.actions == []


def test_program_resume_rejects_worklist_over_loop_bound_before_input(tmp_path):
    body_step = Step(id="body-key", intent="body", action=ActionKind.KEY, key="B")
    workflow = Workflow(
        name="w",
        program=ProgramGraph(
            entry="loop",
            states={
                "loop": State(
                    id="loop",
                    kind=StateKind.LOOP,
                    loop=LoopSpec(relation="queue", body="body", max_iterations=1),
                )
            },
        ),
        subflows={
            "body": ProgramGraph(
                entry="body",
                states={
                    "body": State(id="body", kind=StateKind.ACTION, step=body_step)
                },
            )
        },
        data_sources={
            "queue": Relation(name="queue", rows=[{"row": "1"}, {"row": "2"}])
        },
    )
    checkpoint = ProgramCheckpoint(
        workflow_name="w",
        seq=1,
        verified_state_id="body",
        step_id="body-key",
        frames=[
            GraphFrame(graph_id="__program__", state_id="loop"),
            GraphFrame(
                graph_id="body",
                state_id="body",
                params={"row": "1"},
                loop=LoopCursor(
                    loop_state_id="loop",
                    relation="queue",
                    row_index=0,
                    rows=[{"row": "1"}, {"row": "2"}],
                ),
            ),
        ],
        bound_params={"row": "1"},
    )
    backend = FakeBackend()

    with pytest.raises(_ProgramHalt, match="authorized worklist"):
        Replayer(backend, vision=FakeVision())._resume_program_state(
            checkpoint,
            workflow=workflow,
            worklists={},
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run",
            report=RunReport(workflow_name="w", started_at="now"),
            new_crops={},
        )

    assert backend.actions == []


def test_delivery_callback_cannot_remove_a_required_postcondition(tmp_path):
    workflow = Workflow(
        name="linear-mutation",
        steps=[
            Step(
                id="key",
                intent="submit",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                    )
                ],
            )
        ],
    )

    class MutatingBackend(FakeBackend):
        def press(self, key):
            super().press(key)
            workflow.steps[0].expect.clear()

    backend = MutatingBackend()
    report = Replayer(backend, vision=FakeVision()).run(
        workflow,
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
    )

    assert report.success is False
    assert backend.actions == [("press", "Enter")]
    assert report.results[0].safety_halt is True
    assert "workflow semantics changed" in (report.results[0].error or "")


def test_settling_callback_cannot_remove_a_required_postcondition(tmp_path):
    workflow = Workflow(
        name="settling-mutation",
        steps=[
            Step(
                id="submit",
                intent="submit",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                    )
                ],
                effects=[Effect(kind=EffectKind.RECORD_WRITTEN, match={"id": "1"})],
            )
        ],
    )

    class MutatingVision(FakeVision):
        def wait_settled(self, backend, **kwargs):
            frame = super().wait_settled(backend, **kwargs)
            if self.settle_count == 2:
                workflow.steps[0].expect.clear()
            return frame

    class ConfirmingVerifier:
        substrate = "independent-test-store"

        def capture_pre_state(self, context=None):
            return EffectState(substrate=self.substrate, reachable=True)

        def verify(self, effect, before, context=None):
            return EffectVerdict(
                verdict=Verdict.CONFIRMED,
                kind=effect.kind,
                substrate=self.substrate,
            )

    backend = FakeBackend()
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    report = Replayer(
        backend,
        vision=MutatingVision(),
        effect_verifier=ConfirmingVerifier(),
        durable=True,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
    )

    assert report.success is False
    assert report.results[0].postconditions_ok is False
    assert CheckpointStore(tmp_path / "run").checkpoints() == []


def test_program_settling_mutation_cannot_create_a_verified_checkpoint(tmp_path):
    state = State(
        id="submit-state",
        kind=StateKind.ACTION,
        step=Step(
            id="submit",
            intent="submit",
            action=ActionKind.KEY,
            key="A",
            expect=[Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")],
        ),
        transitions=[Transition(target="done")],
    )
    workflow = Workflow(
        name="program-settling-mutation",
        program=ProgramGraph(
            entry="submit-state",
            states={
                "submit-state": state,
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
    )

    class MutatingVision(FakeVision):
        def wait_settled(self, backend, **kwargs):
            frame = super().wait_settled(backend, **kwargs)
            if self.settle_count == 2:
                assert workflow.program is not None
                workflow.program.states["submit-state"].step.expect.clear()
            return frame

    workflow.save(tmp_path / "bundle")
    report = Replayer(FakeBackend(), vision=MutatingVision(), durable=True).run(
        workflow,
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
    )

    assert report.success is False
    assert CheckpointStore(tmp_path / "run").program_checkpoints() == []


def test_guard_callback_cannot_replace_the_selected_program_target(tmp_path):
    workflow = Workflow(
        name="transition-mutation",
        program=ProgramGraph(
            entry="first",
            states={
                "first": State(
                    id="first",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="first-step",
                        intent="first",
                        action=ActionKind.KEY,
                        key="A",
                    ),
                    transitions=[
                        Transition(
                            target="later",
                            guard=Predicate(
                                kind=PredicateKind.TEXT_PRESENT, text="ready"
                            ),
                        )
                    ],
                ),
                "later": State(
                    id="later",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="later-step",
                        intent="later",
                        action=ActionKind.KEY,
                        key="L",
                    ),
                ),
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
    )

    class MutatingVision(FakeVision):
        def wait_settled(self, backend, **kwargs):
            if self.settle_count == 2:
                assert workflow.program is not None
                workflow.program.states["first"].transitions[0].target = "done"
            return super().wait_settled(backend, **kwargs)

    backend = FakeBackend()
    vision = MutatingVision()
    vision.text_results["ready"] = (1, 1, 2, 2)
    report = Replayer(backend, vision=vision).run(
        workflow,
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
    )

    assert report.success is False
    assert backend.actions == [("press", "A")]
    assert report.results[-1].safety_halt is True
    assert "workflow semantics changed" in (report.results[-1].error or "")


def _patient_effect() -> Effect:
    """A per-row consequential write keyed on the loop's ``patient`` param, so the
    verifier can REFUTE a specific row (forcing a mid-loop pause)."""
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": ValueExpr(param="patient")},
        expected_count=1,
        timeout_s=0.5,
    )


def _branch_loop_workflow(patients: list[str]) -> Workflow:
    """BRANCH (mode == "go") -> LOOP over ``queue``; the body TYPEs each row's
    patient and writes a per-row effect."""
    body = ProgramGraph(
        entry="b_type",
        states={
            "b_type": State(
                id="b_type",
                kind=StateKind.ACTION,
                step=Step(
                    id="b_type",
                    intent="type patient",
                    action=ActionKind.TYPE,
                    param="patient",
                    risk="irreversible",
                    effects=[_patient_effect()],
                ),
                transitions=[Transition(target="b_end")],
            ),
            "b_end": State(id="b_end", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    program = ProgramGraph(
        entry="start",
        states={
            "start": State(
                id="start",
                kind=StateKind.BRANCH,
                transitions=[
                    Transition(
                        guard=Predicate(
                            kind=PredicateKind.PARAM_EQUALS, param="mode", value="go"
                        ),
                        target="loop",
                    )
                ],
            ),
            "loop": State(
                id="loop",
                kind=StateKind.LOOP,
                loop=LoopSpec(relation="queue", body="body", var="patient"),
                transitions=[Transition(target="done")],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    return Workflow(
        name="branch-loop-demo",
        program=program,
        subflows={"body": body},
        data_sources={
            "queue": Relation(name="queue", rows=[{"patient": p} for p in patients])
        },
    )


def _dirs(tmp_path):
    return tmp_path / "bundle", tmp_path / "run"


def _run_branch_loop_to_pause(tmp_path, *, refute="Bob"):
    """Run the branch+loop program durably; the ``refute`` row REFUTES -> pause.

    Returns (report, run_dir, bundle, verifier)."""
    verifier = FakeSoRVerifier()
    verifier.refute.add((("patient", refute),))
    workflow = _branch_loop_workflow(["Alice", "Bob", "Cara"])
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    replayer = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        effect_verifier=verifier,
        durable=True,
        poll_interval_s=0.01,
    )
    report = replayer.run(
        workflow, params={"mode": "go"}, bundle_dir=bundle, run_dir=run_dir
    )
    return report, run_dir, bundle, verifier


def test_program_loop_keeps_each_action_frame_across_checkpoints(tmp_path):
    class ChangingFrameBackend(FakeBackend):
        def type_text(self, text):
            super().type_text(text)
            color = (210, 220, 230) if text == "Alice" else (180, 200, 220)
            self._frame = make_png(color=color)

    workflow = _branch_loop_workflow(["Alice", "Bob"])
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    report = Replayer(
        ChangingFrameBackend(),
        vision=FakeVision(),
        effect_verifier=FakeSoRVerifier(),
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, params={"mode": "go"}, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is True
    before_paths = [result.before_png for result in report.results]
    assert len(before_paths) == 2
    assert None not in before_paths
    assert len(set(before_paths)) == 2
    before_bytes = [(run_dir / path).read_bytes() for path in before_paths if path]
    assert before_bytes[0] != before_bytes[1]
    checkpoint_paths = [
        checkpoint.before_png
        for checkpoint in CheckpointStore(run_dir).program_checkpoints()
    ]
    assert checkpoint_paths == before_paths


# -- 1. branch + loop: pause mid-loop, checkpoint interpreter state, resume ---


def test_program_pauses_midloop_checkpoints_interpreter_state(tmp_path):
    report, run_dir, bundle, _verifier = _run_branch_loop_to_pause(tmp_path)

    assert report.success is False
    assert report.terminal_outcome in ("halt", "escalate")

    store = CheckpointStore(run_dir)
    # A durable interpreter checkpoint was written for the VERIFIED first row
    # (Alice) -- capturing the frame stack, not a step index.
    last = store.last_program_checkpoint()
    assert last is not None
    assert last.verified_state_id == "b_type"
    # OUTER -> INNER: top program at the loop state, then the loop-body frame
    # carrying the loop cursor at row 0 (Alice).
    assert [f.graph_id for f in last.frames] == ["__program__", "body"]
    assert last.frames[0].state_id == "loop"
    body_frame = last.frames[1]
    assert body_frame.loop is not None
    assert body_frame.loop.row_index == 0
    assert [r["patient"] for r in body_frame.loop.rows] == ["Alice", "Bob", "Cara"]

    # A durable program pause (not a silent death), pointing at the halted row.
    pending = store.read_pending()
    assert pending is not None
    assert pending.program is True
    assert pending.category == "effect_refuted"


def test_program_resume_restores_interpreter_and_completes(tmp_path):
    report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    assert report.success is False

    # Operator fixed the system of record: the refuted row now confirms.
    verifier.refute.clear()
    # A FRESH backend for the resumed leg reveals exactly which rows re-executed.
    resume_backend = FakeBackend()
    resume_replayer = Replayer(
        resume_backend,
        vision=FakeVision(),
        effect_verifier=verifier,
        poll_interval_s=0.01,
    )
    resumed = resume(run_dir, resume_replayer, approval=_approval(bundle))

    assert resumed.success is True
    assert resumed.terminal_outcome == "success"
    assert resumed.results[0].risk == "irreversible"
    resumed_before_paths = [result.before_png for result in resumed.results]
    assert None not in resumed_before_paths
    assert len(set(resumed_before_paths)) == len(resumed_before_paths)
    assert all((run_dir / path).is_file() for path in resumed_before_paths if path)
    manifest = CheckpointStore(run_dir).read_manifest()
    assert manifest is not None
    assert report.started_at == manifest.created_at
    assert resumed.started_at == manifest.created_at
    # RESTORED from interpreter state: the already-confirmed row (Alice) was NOT
    # re-typed; the paused row onward (Bob, Cara) was -- in order.
    assert resume_backend.actions == [("type", "Bob"), ("type", "Cara")]
    # The pause was cleared when the approved resume started.
    assert CheckpointStore(run_dir).read_pending() is None


# -- 2. idempotency: a confirmed effect is not re-executed on resume ---------


def test_program_resume_does_not_reperform_confirmed_write(tmp_path):
    report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    assert report.success is False

    verifier.refute.clear()
    resume_backend = FakeBackend()
    resume_replayer = Replayer(
        resume_backend,
        vision=FakeVision(),
        effect_verifier=verifier,
        poll_interval_s=0.01,
    )
    resumed = resume(run_dir, resume_replayer, approval=_approval(bundle))

    assert resumed.success is True
    # The confirmed row's consequential write is NEVER re-performed: Alice's TYPE
    # does not run again on the resumed backend (idempotency via the completed-
    # effect ledger + restored loop cursor).
    assert ("type", "Alice") not in resume_backend.actions
    # And the whole workflow is accounted for (Alice reconstructed, Bob/Cara run).
    typed = [r.intent for r in resumed.results if r.intent == "type patient"]
    assert len(typed) >= 2  # Bob + Cara executed on resume


# -- 3. resume WITHOUT an approval record is refused (P0-5) -------------------


def test_program_resume_without_approval_is_refused(tmp_path):
    _report, run_dir, _bundle, _verifier = _run_branch_loop_to_pause(tmp_path)

    resume_replayer = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        effect_verifier=FakeSoRVerifier(),
        poll_interval_s=0.01,
    )
    # No approval argument and no approval.json on disk -> refused before ANY
    # re-execution.
    with pytest.raises(ApprovalRequired):
        resume(run_dir, resume_replayer)

    # An approval record with a blank approver is likewise not authentication.
    with pytest.raises(ApprovalRequired):
        resume(
            run_dir,
            resume_replayer,
            approval=ApprovalRecord(approver="   ", bundle_version=""),
        )
    # The pause is still there (nothing was consumed / resumed).
    assert CheckpointStore(run_dir).read_pending() is not None


def test_program_resume_requires_the_active_pause_and_exact_worklist(tmp_path):
    _report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    backend = FakeBackend()
    replayer = Replayer(backend, vision=FakeVision(), effect_verifier=verifier)

    with pytest.raises(StateDiverged, match="worklists differ"):
        resume(
            run_dir,
            replayer,
            approval=_approval(bundle),
            worklists={"queue": [{"patient": "different"}]},
        )

    approval = _approval(bundle)
    CheckpointStore(run_dir).clear_pending()
    with pytest.raises(ApprovalRequired, match="no active durable pause"):
        resume(run_dir, replayer, approval=approval)
    assert backend.actions == []


# -- 4. resume after the app state DIVERGED from the checkpoint is refused ----


def _two_state_effect_program() -> Workflow:
    """s0 (verified, TEXT_PRESENT 'OK', effect s0) -> s1 (effect s1 REFUTES)."""

    def eff(step_id: str) -> Effect:
        return Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"step": step_id},
            expected_count=1,
            timeout_s=0.5,
        )

    def act(step_id: str, key: str) -> State:
        return State(
            id=step_id,
            kind=StateKind.ACTION,
            step=Step(
                id=step_id,
                intent=f"press {key}",
                action=ActionKind.KEY,
                key=key,
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="OK", timeout_s=0.2
                    )
                ],
                effects=[eff(step_id)],
            ),
            transitions=[Transition(target="s1" if step_id == "s0" else "done")],
        )

    program = ProgramGraph(
        entry="s0",
        states={
            "s0": act("s0", "A"),
            "s1": act("s1", "B"),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    return Workflow(name="two-state-demo", program=program)


def _three_state_effect_program() -> Workflow:
    """s0 and s1 verify; s2 refutes and creates the durable pause."""

    def act(step_id: str, key: str, target: str) -> State:
        return State(
            id=step_id,
            kind=StateKind.ACTION,
            step=Step(
                id=step_id,
                intent=f"press {key}",
                action=ActionKind.KEY,
                key=key,
                risk="irreversible",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="OK",
                        timeout_s=0.2,
                    )
                ],
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"step": step_id},
                        expected_count=1,
                        timeout_s=0.5,
                    )
                ],
            ),
            transitions=[Transition(target=target)],
        )

    return Workflow(
        name="three-state-demo",
        program=ProgramGraph(
            entry="s0",
            states={
                "s0": act("s0", "A", "s1"),
                "s1": act("s1", "B", "s2"),
                "s2": act("s2", "C", "done"),
                "done": State(
                    id="done",
                    kind=StateKind.TERMINAL,
                    outcome="success",
                ),
            },
        ),
    )


def _run_two_state_to_pause(tmp_path):
    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", "s1"),))  # second state refutes -> pause at s1
    workflow = _two_state_effect_program()
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    replayer = Replayer(
        FakeBackend(),
        vision=_vision_ok(),
        effect_verifier=verifier,
        durable=True,
        poll_interval_s=0.01,
    )
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)
    return report, run_dir, bundle, verifier


def test_program_resume_refused_when_app_state_diverged(tmp_path):
    report, run_dir, bundle, verifier = _run_two_state_to_pause(tmp_path)
    assert report.success is False
    # The checkpoint for s0 recorded the expected on-screen text.
    last = CheckpointStore(run_dir).last_program_checkpoint()
    assert last is not None and last.expected_texts == ["OK"]

    verifier.refute.clear()
    # The live app no longer shows the checkpoint's expected state ("OK" absent).
    diverged_vision = FakeVision()  # empty text_results -> "OK" not present
    resume_replayer = Replayer(
        FakeBackend(),
        vision=diverged_vision,
        effect_verifier=verifier,
        poll_interval_s=0.01,
    )
    with pytest.raises(StateDiverged):
        resume(run_dir, resume_replayer, approval=_approval(bundle))


def test_program_resume_refused_when_confirmed_effect_no_longer_holds(tmp_path):
    report, run_dir, bundle, verifier = _run_two_state_to_pause(tmp_path)
    assert report.success is False

    # The app is still on the expected screen, but an already-confirmed effect
    # (s0) has since been reverted -> read-only re-verify REFUTES -> refuse.
    verifier.refute = {(("step", "s0"),)}
    verifier.records = []
    resume_replayer = Replayer(
        FakeBackend(),
        vision=_vision_ok(),
        effect_verifier=verifier,
        poll_interval_s=0.01,
    )
    with pytest.raises(StateDiverged):
        resume(run_dir, resume_replayer, approval=_approval(bundle))


def test_program_resume_revalidates_every_retained_effect_before_input(tmp_path):
    """A missing older effect must refuse even when the latest effect remains."""

    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", "s2"),))
    workflow = _three_state_effect_program()
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)

    report = Replayer(
        FakeBackend(),
        vision=_vision_ok(),
        effect_verifier=verifier,
        durable=True,
        poll_interval_s=0.01,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is False
    assert [
        checkpoint.verified_state_id
        for checkpoint in CheckpointStore(run_dir).program_checkpoints()
    ] == ["s0", "s1"]
    assert verifier.records == [{"step": "s0"}, {"step": "s1"}]

    # Clear the fault at the pending action. Then remove the older retained
    # effect while the newer retained effect still exists. Resume must check
    # both retained checkpoints, not only the newest checkpoint.
    verifier.refute = {(("step", "s0"),)}
    verifier.records = [{"step": "s1"}]
    backend = FakeBackend()

    with pytest.raises(StateDiverged):
        resume(
            run_dir,
            Replayer(
                backend,
                vision=_vision_ok(),
                effect_verifier=verifier,
                poll_interval_s=0.01,
            ),
            approval=_approval(bundle),
        )

    assert backend.actions == []


# -- 5. an EXPIRED (stale) pause is refused (P0-5) ---------------------------


def test_program_resume_refused_when_pause_expired(tmp_path):
    _report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    verifier.refute.clear()

    resume_replayer = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        effect_verifier=verifier,
        poll_interval_s=0.01,
    )
    # Resume attempted long after the pause's stale-after window, even WITH a
    # valid approval -> refused (a stale checkpoint's expected app state can no
    # longer be trusted). Expiry is checked before the approval.
    far_future = datetime.now(timezone.utc) + timedelta(days=365)
    with pytest.raises(PauseExpired):
        resume(run_dir, resume_replayer, approval=_approval(bundle), now=far_future)


# -- 6. clean durable program run: checkpoints every verified state, no pause -


def test_clean_program_run_checkpoints_each_state(tmp_path):
    workflow = _branch_loop_workflow(["Alice", "Bob"])
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    replayer = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        effect_verifier=FakeSoRVerifier(),  # confirms everything
        durable=True,
        poll_interval_s=0.01,
    )
    report = replayer.run(
        workflow, params={"mode": "go"}, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    store = CheckpointStore(run_dir)
    # One interpreter checkpoint per verified action state (one per loop row).
    cps = store.program_checkpoints()
    assert [c.verified_state_id for c in cps] == ["b_type", "b_type"]
    assert [c.seq for c in cps] == [1, 2]
    assert store.read_pending() is None
    assert report.model_calls == 0  # $0 runtime preserved


def test_fresh_program_run_refuses_an_owned_run_directory_before_actuation(tmp_path):
    workflow = _branch_loop_workflow(["Alice"])
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    verifier = FakeSoRVerifier()
    first = Replayer(
        FakeBackend(), vision=FakeVision(), effect_verifier=verifier, durable=True
    )
    assert first.run(
        workflow, params={"mode": "go"}, bundle_dir=bundle, run_dir=run_dir
    ).success

    second_backend = FakeBackend()
    calls_before = verifier.capture_calls
    with pytest.raises(StateDiverged, match="already contains a durable run"):
        Replayer(
            second_backend,
            vision=FakeVision(),
            effect_verifier=verifier,
            durable=True,
        ).run(
            workflow,
            params={"mode": "go"},
            bundle_dir=bundle,
            run_dir=run_dir,
        )
    assert second_backend.actions == []
    assert verifier.capture_calls == calls_before


def test_program_resume_refuses_a_noncontiguous_checkpoint_history(tmp_path):
    _report, run_dir, bundle, verifier = _run_branch_loop_to_pause(tmp_path)
    store = CheckpointStore(run_dir)
    checkpoint = store.program_checkpoints()[0]
    path = run_dir / "checkpoints" / "pstate_0001.json"
    path.write_text(checkpoint.model_copy(update={"seq": 2}).model_dump_json(indent=2))

    backend = FakeBackend()
    calls_before = verifier.capture_calls
    with pytest.raises(
        StateDiverged,
        match="checkpoint history|monotonic authority",
    ):
        resume(
            run_dir,
            Replayer(backend, vision=FakeVision(), effect_verifier=verifier),
            approval=_approval(bundle),
        )
    assert backend.actions == []
    assert verifier.capture_calls == calls_before
