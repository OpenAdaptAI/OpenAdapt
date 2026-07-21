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

# Reuse the scripted fakes + the scripted system-of-record verifier.
from tests.test_durable_runtime import FakeSoRVerifier, _approval, _vision_ok
from tests.test_replayer import FakeBackend, FakeVision

from openadapt_flow.ir import (
    ActionKind,
    LoopSpec,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    Relation,
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
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from openadapt_flow.runtime.replayer import Replayer

# -- builders ----------------------------------------------------------------


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
    resume_replayer = Replayer(
        FakeBackend(),
        vision=_vision_ok(),
        effect_verifier=verifier,
        poll_interval_s=0.01,
    )
    with pytest.raises(StateDiverged):
        resume(run_dir, resume_replayer, approval=_approval(bundle))


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
